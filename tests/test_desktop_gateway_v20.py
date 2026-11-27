"""票据 36 拆分保留：原 tests/test_desktop.py 中的非 classic 用例（gateway / cache）。

全部 offscreen；这些用例与已删除的 MenuWindow / DesktopApp 无关，故完整保留。
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import (  # noqa: E402
    InputEnvelope, build_menu, load_defaults, preflight, suggest,
)
from clipllm02.desktop.gateway import (  # noqa: E402
    Gateway, GatewayConfig, ResultCache, clean_output, render_response,
)


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def make_context(text="NaCl"):
    settings = load_defaults()
    env = InputEnvelope.for_text(text, source="test")
    policy = preflight(env, settings)
    s = suggest(env, policy, settings)
    menu = build_menu(s, settings)
    return settings, env, policy, s, menu


class TestGateway(unittest.TestCase):
    def test_clean_output_removes_fence(self):
        self.assertEqual(clean_output("```json\n{\"a\":1}\n```"), '{"a":1}')

    def test_render_arbitrary_response(self):
        self.assertIn("模型未返回内容", render_response(""))
        self.assertIn("plain", render_response("plain text"))
        self.assertIn("<h3>", render_response('{"title":"T","answer":"A"}'))

    def test_no_key_means_preview_only(self):
        gateway = Gateway(GatewayConfig())
        self.assertFalse(gateway.config.enabled)
        settings, env, policy, s, menu = make_context()
        from clipllm02.prompts import compose_prompt
        composed = compose_prompt(settings.intent("chem.legacy"), settings)
        result = gateway.preview(composed, env.text)
        self.assertTrue(result.preview_only)
        self.assertIn("未发送任何网络请求", result.html)
        self.assertNotIn(env.text * 2, result.html)


class TestResultCache(unittest.TestCase):
    def test_put_get_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.json"
            cache = ResultCache(path)
            cache.put("key-1", "<p>结果</p>")
            self.assertEqual(cache.get("key-1"), "<p>结果</p>")
            self.assertIsNone(cache.get("missing"))
            self.assertEqual(ResultCache(path).get("key-1"), "<p>结果</p>")

    def test_max_entries_eviction(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = ResultCache(Path(tmp) / "r.json", max_entries=2)
            for idx in range(3):
                cache.put(f"k{idx}", f"v{idx}")
            self.assertEqual(len(cache._data), 2)
            self.assertIsNone(cache.get("k0"))


if __name__ == "__main__":
    unittest.main()
