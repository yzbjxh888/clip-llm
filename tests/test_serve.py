import io
import json
import unittest
from unittest.mock import patch

from clipllm02 import InputEnvelope, build_menu, load_defaults, preflight, suggest
from clipllm02.serve import (
    ClipboardReader, ServeStats, iter_stdin, render_menu, serve,
)


def run(lines, **kw):
    out: list[str] = []
    stats = serve(load_defaults(), iter(lines), printer=out.append, **kw)
    return stats, "\n".join(out)


class TestServeLoop(unittest.TestCase):
    def test_text_then_choice(self):
        stats, out = run(["NaCl", "1", "q"])
        self.assertEqual(stats.events, 1)
        self.assertEqual(stats.menus, 1)
        self.assertEqual(stats.selections, 1)
        self.assertEqual(stats.blocked, 0)
        self.assertIn("这个式子该怎么念", out)
        self.assertIn("[已选择] chem.legacy", out)   # 首位即提示词模板条目
        self.assertIn("综合化学条目", out)
        self.assertIn("未发送任何网络请求", out)
        self.assertIn("[退出]", out)

    def test_freeform_choice(self):
        stats, out = run(["NaCl", "f 请分析它的晶体结构", "q"])
        self.assertEqual(stats.selections, 1)
        self.assertIn("[已选择] 自由输入", out)
        self.assertIn("query_type=freeform", out)

    def test_blocked_input_has_no_menu(self):
        stats, out = run(["A" * 1200, "q"])
        self.assertEqual(stats.blocked, 1)
        self.assertEqual(stats.menus, 0)
        self.assertIn("[拦截] BLOCK", out)
        self.assertNotIn("[推荐]", out)

    def test_warn_input_still_shows_menu(self):
        stats, out = run(["https://example.com", "q"])
        self.assertEqual(stats.blocked, 0)
        self.assertEqual(stats.menus, 1)
        self.assertIn("⚠", out)

    def test_duplicate_is_skipped(self):
        stats, out = run(["NaCl", "NaCl", "q"])
        self.assertEqual(stats.menus, 1)
        self.assertEqual(stats.duplicates, 1)

    def test_low_level_still_shows_automatic_menu(self):
        """初判自动完成，LOW 档也直接给菜单，不强制用户先选领域。"""
        _, out = run(["12345", "q"])
        self.assertIn("[初判] LOW", out)
        self.assertIn("[推荐]", out)
        self.assertIn("通用解释", out)
        self.assertNotIn("先选领域", out)

    def test_once_stops_after_first_input(self):
        stats, out = run(["NaCl", "1", "q"], once=True)
        self.assertEqual(stats.menus, 1)
        self.assertEqual(stats.selections, 0)

    def test_json_mode_emits_parseable_lines_without_banner(self):
        _, out = run(["NaCl", "E=mc²", "q"], as_json=True)
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["level"], "MIXED")
        self.assertIn("recommended", rows[0])

    def test_hide_preview(self):
        env = InputEnvelope.for_text("含有隐私的原文")
        settings = load_defaults()
        policy = preflight(env, settings)
        s = suggest(env, policy, settings)
        menu = build_menu(s, settings)
        self.assertNotIn("含有隐私的原文",
                         render_menu(env, policy, s, menu, show_preview=False))

    def test_returns_stats_object(self):
        stats, _ = run(["q"])
        self.assertIsInstance(stats, ServeStats)
        self.assertEqual(stats.events, 0)


class TestSources(unittest.TestCase):
    def test_stdin_source(self):
        with patch("clipllm02.serve.sys.stdin", io.StringIO("NaCl\nE=mc²\nq\n")):
            self.assertEqual(list(iter_stdin()), ["NaCl", "E=mc²", "q"])

    def test_clipboard_reader_command_is_readonly(self):
        reader = ClipboardReader()
        command = reader.command()
        self.assertIsInstance(reader.available, bool)
        if command:
            self.assertTrue(command[0] in {"wl-paste", "xclip"})
            self.assertNotIn("systemd", " ".join(command))
            self.assertNotIn("systemctl", " ".join(command))


if __name__ == "__main__":
    unittest.main()
