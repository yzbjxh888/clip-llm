"""知识库落盘：目录按模板分、文件名按查询稳定、一次会话一篇。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from clipllm02 import load_defaults
from clipllm02.studio.library import (
    library_root, safe_name, save_entry, to_markdown,
)


class TestLibraryNaming(unittest.TestCase):
    def test_safe_name_strips_path_and_illegal_chars(self):
        self.assertEqual(safe_name('a/b\\c:d*e?f"g<h>i|j'), "a b c d e f g h i j")
        self.assertEqual(safe_name("  多  空格  "), "多 空格")
        self.assertEqual(safe_name(""), "未命名")
        self.assertEqual(safe_name("..."), "未命名")   # 只有点号 → 兜底

    def test_safe_name_truncates(self):
        self.assertLessEqual(len(safe_name("x" * 100)), 40)


class TestMarkdown(unittest.TestCase):
    def test_rows_become_table(self):
        text = to_markdown({"symbols": [{"symbol": "q", "reading": "q"}]})
        self.assertIn("| 符号 | 读法 |", text)
        self.assertIn("| q | q |", text)

    def test_scalar_list_becomes_bullets(self):
        text = to_markdown({"notes": ["一", "二"]})
        self.assertIn("- 一", text)
        self.assertIn("- 二", text)

    def test_nested_dict_becomes_heading(self):
        text = to_markdown({"plot": {"kind": "function"}})
        self.assertIn("##", text)           # 顶层字段是二级小节
        self.assertIn("function", text)

    def test_query_type_and_top_title_are_skipped(self):
        text = to_markdown({"query_type": "latex", "title": "T",
                            "answer": "A"})
        self.assertNotIn("query_type", text)
        self.assertNotIn("**标题**", text)      # 已作为 H1，正文不重复
        self.assertIn("A", text)


class TestSaveEntry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = load_defaults()
        self.settings.data["desktop_library_dir"] = self.tmp.name
        self.settings.data["desktop_library_enabled"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def test_folder_matches_template_and_file_has_date(self):
        raw = json.dumps({"title": "温度梯度与热流密度",
                          "symbols": [{"symbol": "q", "reading": "q"}]},
                         ensure_ascii=False)
        path = save_entry(self.settings, query="q'' = -k dT/dx",
                          query_type="latex", route_label="LaTeX 公式",
                          intent_label="逐项读这个式子", raw=raw)
        self.assertIsNotNone(path)
        self.assertEqual(path.parent.name, "LaTeX 公式")
        self.assertTrue(path.name.startswith(datetime.now().strftime("%Y-%m-%d")))
        self.assertTrue(path.name.endswith(".md"))
        text = path.read_text(encoding="utf-8")
        self.assertIn("---", text)                  # frontmatter
        self.assertIn('template: "LaTeX 公式"', text)
        self.assertIn('intent: "逐项读这个式子"', text)
        self.assertIn("| 符号 | 读法 |", text)

    def test_same_title_overwrites(self):
        raw = json.dumps({"title": "同一个", "answer": "旧"}, ensure_ascii=False)
        first = save_entry(self.settings, query="q", route_label="通用", raw=raw)
        raw2 = json.dumps({"title": "同一个", "answer": "新"}, ensure_ascii=False)
        second = save_entry(self.settings, query="q", route_label="通用", raw=raw2)
        self.assertEqual(first, second)
        self.assertIn("新", second.read_text(encoding="utf-8"))
        self.assertNotIn("旧", second.read_text(encoding="utf-8"))

    def test_disabled_writes_nothing(self):
        self.settings.data["desktop_library_enabled"] = False
        path = save_entry(self.settings, query="q", raw='{"title":"T"}')
        self.assertIsNone(path)
        self.assertEqual(list(Path(self.tmp.name).rglob("*.md")), [])

    def test_non_json_falls_back_to_text(self):
        path = save_entry(self.settings, query="q", route_label="通用",
                          raw="模型返回的不是 JSON")
        self.assertIsNotNone(path)
        self.assertIn("模型返回的不是 JSON",
                      path.read_text(encoding="utf-8"))

    def test_one_session_stays_in_one_note(self):
        first = save_entry(self.settings, query="q", route_label="通用",
                           raw='{"title":"T","answer":"A"}', session_id="s1")
        second = save_entry(self.settings, query="q", route_label="通用",
                            followup="为什么？", raw="因为 B",
                            is_followup=True, session_id="s1")
        third = save_entry(self.settings, query="q", route_label="通用",
                           followup="再展开", raw="还有 C",
                           is_followup=True, session_id="s1")
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        text = third.read_text(encoding="utf-8")
        self.assertIn('session: "s1"', text)
        self.assertIn('rounds: "3"', text)
        self.assertIn("## 追问 1：为什么？", text)
        self.assertIn("## 追问 2：再展开", text)
        for mark in ("A", "因为 B", "还有 C"):
            self.assertIn(mark, text)
        self.assertEqual(len(list(Path(self.tmp.name).rglob("*.md"))), 1)

    def test_new_session_starts_clean_note(self):
        save_entry(self.settings, query="q", route_label="通用",
                   raw="第一轮 A", session_id="s1")
        save_entry(self.settings, query="q", route_label="通用",
                   followup="追问", raw="追问 B",
                   is_followup=True, session_id="s1")
        path = save_entry(self.settings, query="q", route_label="通用",
                          raw="另一次查询", session_id="s2")
        text = path.read_text(encoding="utf-8")
        self.assertIn("另一次查询", text)
        self.assertNotIn("追问 B", text)
        self.assertIn('rounds: "1"', text)

    def test_stale_session_does_not_merge_with_old_note(self):
        """缓存命中未落盘时，新会话的追问也不能串进旧会话的笔记。"""
        save_entry(self.settings, query="q", route_label="通用",
                   raw="旧会话 A", session_id="s1")
        path = save_entry(self.settings, query="q", route_label="通用",
                          followup="新会话的追问", raw="新回答",
                          is_followup=True, session_id="s2")
        text = path.read_text(encoding="utf-8")
        self.assertIn("新回答", text)
        self.assertNotIn("旧会话 A", text)

    def test_root_falls_back_to_xdg(self):
        self.settings.data["desktop_library_dir"] = ""
        root = library_root(self.settings)
        self.assertTrue(str(root).endswith("clip-llm02/library"))


if __name__ == "__main__":
    unittest.main()
