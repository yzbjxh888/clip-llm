"""桌面层回归测试，全部在 offscreen 平台运行，不会弹出真实窗口。"""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import (  # noqa: E402
    InputEnvelope, build_menu, load_defaults, preflight, suggest,
)
from clipllm02.desktop.app import DesktopApp  # noqa: E402
from clipllm02.desktop.gateway import (  # noqa: E402
    Gateway, GatewayConfig, ResultCache, clean_output, render_response,
)
from clipllm02.desktop.menu_window import MenuWindow  # noqa: E402
from clipllm02.desktop.result_window import ResultWindow  # noqa: E402
from clipllm02.desktop.settings_window import SettingsWindow  # noqa: E402


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


class TestMenuWindow(unittest.TestCase):
    def test_builds_five_sections(self):
        settings, env, policy, s, menu = make_context()
        window = MenuWindow(settings, env, policy, s, menu)
        self.assertIn("clip-llm", window.windowTitle())
        visible = [b for b in window._intent_buttons if not b.isHidden()]
        self.assertEqual(len(visible), len(window.menu.recommended))
        self.assertTrue(window.format_box)
        self.assertTrue(window.search_menu.actions())
        self.assertTrue(window.free_edit)
        window.close()

    def test_selection_signal(self):
        settings, env, policy, s, menu = make_context()
        window = MenuWindow(settings, env, policy, s, menu)
        captured = []
        window.selection_made.connect(captured.append)
        window._submit_index(1)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["intent_id"], menu.recommended[0].intent.id)
        self.assertIn("format_id", captured[0])
        window.close()

    def test_free_input_signal(self):
        settings, env, policy, s, menu = make_context()
        window = MenuWindow(settings, env, policy, s, menu)
        captured = []
        window.selection_made.connect(captured.append)
        window.mode_combo.setCurrentIndex(1)
        window.free_edit.setText("请自己写提示词")
        window._submit_free()
        self.assertIsNone(captured[0]["intent_id"])
        self.assertEqual(captured[0]["free_input_mode"], "prompt")
        self.assertIn("请自己写提示词", captured[0]["custom_instruction"])
        window.close()

    def test_low_level_shows_domain_picker(self):
        settings, env, policy, s, menu = make_context("12345")
        window = MenuWindow(settings, env, policy, s, menu)
        self.assertEqual(menu.level, "LOW")
        self.assertFalse(window.domain_combo.isHidden())
        self.assertGreater(window.domain_combo.count(), 0)
        window.close()

    def test_explicit_format_radio(self):
        settings, env, policy, s, menu = make_context("E=mc²")
        window = MenuWindow(settings, env, policy, s, menu)
        self.assertIn("interactive", [b.property("format_id")
                                      for b in window._format_buttons])
        window.close()


class TestResultWindow(unittest.TestCase):
    def test_show_html_and_plain_text(self):
        window = ResultWindow(tuple(load_defaults().providers))
        window.show_html("<h3>标题</h3><p>内容</p>", title="结果")
        self.assertIn("内容", window.current_plain_text())
        self.assertIn("搜索", [a.text() for a in window.menu.actions()])
        window.close()

    def test_loading_then_result(self):
        window = ResultWindow(())
        window.show_loading("NaCl", hint="正在组装请求")
        self.assertIn("正在处理", window.current_plain_text())
        window.show_html("<p>完成</p>")
        self.assertIn("完成", window.current_plain_text())
        window.close()


class TestSettingsWindow(unittest.TestCase):
    def test_draft_cancel_discards_changes(self):
        settings = load_defaults()
        window = SettingsWindow(settings)
        original = copy.deepcopy(window._original)
        window.cb_hide_preview.setChecked(True)
        self.assertTrue(window.draft["desktop_hide_preview"])
        window._reject()
        self.assertEqual(window.draft, original)
        window.close()

    def test_apply_writes_and_emits(self):
        settings = load_defaults()
        window = SettingsWindow(settings)
        emitted = []
        window.applied.connect(emitted.append)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "settings.json"
            with patch("clipllm02.studio.config.config_path", return_value=target):
                window.cb_hide_preview.setChecked(True)
                self.assertTrue(window._apply())
                self.assertTrue(target.exists())
                data = json.loads(target.read_text(encoding="utf-8"))
                self.assertTrue(data["desktop_hide_preview"])
        self.assertEqual(len(emitted), 1)
        self.assertTrue(emitted[0]["desktop_hide_preview"])
        window.close()

    def test_intent_checkbox_updates_draft(self):
        settings = load_defaults()
        window = SettingsWindow(settings)
        item = window.intent_list.item(0)
        item.setCheckState(Qt.Unchecked)
        intent_id = item.data(Qt.UserRole)
        self.assertFalse(next(i for i in window.draft["intents"]
                              if i["id"] == intent_id)["enabled"])
        window.close()


class TestDesktopApp(unittest.TestCase):
    def _app(self):
        return DesktopApp(load_defaults(), use_tray=False)

    def test_text_shows_menu_without_request(self):
        app = self._app()
        app.process_text("NaCl", source="test")
        self.assertEqual(app.stats["menus"], 1)
        self.assertIsNotNone(app.menu_window)
        self.assertEqual(app.stats["requests"], 0)
        self.assertFalse(app.gateway.config.enabled)
        app.shutdown()

    def test_selection_produces_preview_card(self):
        app = self._app()
        app.process_text("NaCl", source="test")
        app.on_selection({
            "domain_id": "chem", "intent_id": "chem.legacy",
            "format_id": "static", "allowed_route_ids": ["chem_substance"],
        })
        self.assertEqual(app.stats["selections"], 1)
        self.assertIsNotNone(app.result_window)
        self.assertIn("本地请求预览", app.result_window.current_plain_text())
        self.assertIn("未发送任何网络请求", app.result_window.current_plain_text())
        app.shutdown()

    def test_blocked_input_has_no_window_or_request(self):
        app = self._app()
        app.process_text("A" * 1200, source="test")
        self.assertEqual(app.stats["blocked"], 1)
        self.assertEqual(app.stats["menus"], 0)
        self.assertIsNone(app.menu_window)
        self.assertNotIn("A" * 1200, str(app.intercepts))
        app.shutdown()

    def test_domain_change_rebuilds_menu(self):
        app = self._app()
        app.process_text("NaCl", source="test")
        app.on_domain_changed("en")
        self.assertEqual(app.menu_window.menu.level, "HIGH")
        self.assertEqual(app.menu_window.menu.domain_chips[0].id, "en")
        self.assertEqual(app.stats["requests"], 0)
        app.shutdown()

    def test_self_copy_does_not_trigger_new_query(self):
        app = self._app()
        app.process_text("NaCl", source="test")
        app.on_selection({"domain_id": "chem", "intent_id": "chem.legacy",
                          "format_id": "static"})
        copied = app.result_window.current_plain_text().strip()
        app.result_window._copy_all()
        before = app.stats["events"]
        app.process_text(copied, source="test")
        self.assertEqual(app.stats["events"], before)
        app.shutdown()

    def test_requery_reuses_selection(self):
        app = self._app()
        app.process_text("NaCl", source="test")
        app.on_selection({"domain_id": "chem", "intent_id": "chem.legacy",
                          "format_id": "static"})
        app.on_requery()
        self.assertEqual(app.stats["selections"], 2)
        self.assertEqual(app.stats["requests"], 2)
        app.shutdown()


if __name__ == "__main__":
    unittest.main()
