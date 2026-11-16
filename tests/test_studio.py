"""统一 Studio 应用回归：两层 UI、标签页、精美排版、三资产融合、设置。"""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import (  # noqa: E402
    InputEnvelope, build_menu, load_defaults, preflight, suggest,
)
from clipllm02.studio.app import StudioApp  # noqa: E402
from clipllm02.studio.config import (  # noqa: E402
    CONFIG_VERSION, DEFAULT_VISION_PROMPT, load_config, load_legacy,
    save_config, settings_from_data,
)
from clipllm02.studio.json_render import (  # noqa: E402
    render_json_result, render_local_preview, render_notice,
)
from clipllm02.studio.main_window import MainWindow  # noqa: E402
from clipllm02.studio.query_tab import QueryTab  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402
from clipllm02.studio.vision import recognize  # noqa: E402


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def context(text="NaCl"):
    settings = load_defaults()
    env = InputEnvelope.for_text(text, source="test")
    policy = preflight(env, settings)
    s = suggest(env, policy, settings)
    return settings, env, policy, s, build_menu(s, settings)


class TestJsonRender(unittest.TestCase):
    def test_dict_with_table_and_cards(self):
        raw = json.dumps({
            "title": "construct", "query_type": "english_word",
            "definitions": [{"pos": "v.", "cn": "建造"}, {"pos": "n.", "cn": "构想"}],
            "examples": ["A", "B"], "memory_tip": "记法",
        }, ensure_ascii=False)
        out = render_json_result(raw, font_size=16)
        self.assertIn("<table>", out)
        self.assertIn("义项", out)
        self.assertIn("construct", out)
        self.assertIn("16px", out)
        self.assertIn("<div class='card'>", out)

    def test_arbitrary_and_malformed_response(self):
        self.assertIn("模型未返回内容", render_json_result(""))
        out = render_json_result("plain text <script>alert(1)</script>")
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>", out)

    def test_nested_and_dark_theme(self):
        raw = '{"title":"X","identification":{"target":"对象","domain":"物理"},"answer":"是"}'
        out = render_json_result(raw, dark=True)
        self.assertIn("辨识", out)
        self.assertIn("#181b22", out)

    def test_code_fence_is_stripped(self):
        raw = '```json\n{"title":"T","answer":"A"}\n```'
        self.assertIn("回答", render_json_result(raw))

    def test_local_preview_never_claims_network(self):
        from clipllm02.prompts import compose_prompt
        settings = load_defaults()
        composed = compose_prompt(settings.intent("chem.naming"), settings)
        out = render_local_preview(composed, "NaCl")
        self.assertIn("未发送任何网络请求", out)
        self.assertIn("可在设置里修改", out)
        self.assertIn("IUPAC", out)

    def test_notice(self):
        self.assertIn("标题", render_notice("标题", "<p>正文</p>"))


class TestConfigAndAssets(unittest.TestCase):
    def test_legacy_assets_are_self_contained(self):
        legacy = load_legacy()
        self.assertEqual(len(legacy["packs"]), 9)
        self.assertEqual(len(legacy["rules"]), 17)
        self.assertEqual(legacy["cases_count"], 135)
        self.assertGreater(sum(len(p["prompt"]) for p in legacy["packs"].values()),
                           15000)

    def test_config_has_legacy_prompts_and_vision_prompt(self):
        data = load_defaults().as_dict()
        data["config_version"] = CONFIG_VERSION
        settings = settings_from_data(data)
        self.assertEqual(settings.get("config_version"), CONFIG_VERSION)
        self.assertGreater(len(settings.routes["en_word"]["prompt"]), 1000)
        self.assertIn("LaTeX", str(settings.get("desktop_vision_prompt")))
        self.assertEqual(len(settings.data.get("legacy_rules", [])), 17)

    def test_stale_config_keeps_user_prompt_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            data = load_defaults().as_dict()
            data["config_version"] = 1
            data["desktop_font_size"] = 20
            for item in data["intents"]:
                if item["id"] == "chem.naming":
                    item["prompt_override"] = "我自定义的化学提示词"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with patch("clipllm02.studio.config.config_path", return_value=path):
                settings = load_config()
            self.assertEqual(settings.get("config_version"), CONFIG_VERSION)
            self.assertEqual(settings.get("desktop_font_size"), 20)
            self.assertEqual(settings.intent("chem.naming").prompt_override,
                             "我自定义的化学提示词")
            self.assertIsNotNone(settings.intent("chem.naming"))

    def test_save_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            data = load_defaults().as_dict()
            data["desktop_font_size"] = 18
            with patch("clipllm02.studio.config.config_path", return_value=path):
                save_config(data)
                saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["desktop_font_size"], 18)
            self.assertEqual(saved["config_version"], CONFIG_VERSION)


class TestQueryTabTwoLayers(unittest.TestCase):
    def setUp(self):
        self.settings, self.env, self.policy, self.s, self.menu = context()
        self.tab = QueryTab(self.settings)

    def test_first_layer_is_domain_specific(self):
        self.tab.set_context(self.env, self.policy, self.s, self.menu)
        self.assertEqual(self.tab.stack.currentIndex(), 0)
        texts = [b.text() for b in self.tab._intent_buttons if not b.isHidden()]
        # 首位固定为复用模板的那条，推荐意图紧随其后
        self.assertIn("综合化学条目", texts[0])
        self.assertIn("这个式子该怎么念", texts[1])
        visible = [b for b in self.tab._intent_buttons if not b.isHidden()]
        self.assertEqual(len(visible), len(self.menu.recommended[:24]))
        self.assertFalse(self.tab.category_drawer.isHidden())
        self.assertTrue(self.tab._chip_options)

    def test_click_intent_switches_to_second_layer(self):
        self.tab.set_context(self.env, self.policy, self.s, self.menu)
        captured = []
        self.tab.intent_selected.connect(captured.append)
        self.tab._intent_buttons[0].click()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["intent_id"], "chem.legacy")
        self.assertEqual(self.tab.stack.currentIndex(), 1)
        self.tab.back_button.click()
        self.assertEqual(self.tab.stack.currentIndex(), 0)

    def test_free_input(self):
        self.tab.set_context(self.env, self.policy, self.s, self.menu)
        captured = []
        self.tab.intent_selected.connect(captured.append)
        self.tab.free_edit.setText("请分析晶体结构")
        self.tab.free_send.click()
        self.assertIsNone(captured[0]["intent_id"])
        self.assertIn("晶体结构", captured[0]["custom_instruction"])

    def test_domain_chip_emits(self):
        # 有类别初判：chips 显示类别，点击发 category_changed
        self.tab.set_context(self.env, self.policy, self.s, self.menu)
        captured = []
        self.tab.category_changed.connect(captured.append)
        self.tab._select_chip(0)
        self.assertEqual(captured[0], self.menu.category_chips[0].id)

        # 无类别初判（如纯图片资产）：回退到领域 chips
        from dataclasses import replace as dc_replace
        plain = dc_replace(self.s, classification=None)
        plain_menu = build_menu(plain, self.settings)
        self.tab.set_context(self.env, self.policy, plain, plain_menu)
        domains = []
        self.tab.domain_changed.connect(domains.append)
        self.tab._select_chip(0)
        self.assertEqual(domains[0], plain_menu.domain_chips[0].id)

    def test_show_result_and_look(self):
        self.tab.show_result("<p>结果</p>", title="解释结果", preview_only=True)
        self.assertIn("结果", self.tab.view.toPlainText())
        self.assertIn("本地预览", self.tab.status.text())
        self.tab.set_look(18, True)
        self.assertEqual(self.tab._font_size, 18)


class TestMainWindowTabs(unittest.TestCase):
    def setUp(self):
        self.settings = load_defaults()
        self.window = MainWindow(self.settings)

    def test_new_tab_and_close(self):
        self.assertEqual(self.window.tabs.count(), 1)
        self.window.new_tab("E=mc²")
        self.assertEqual(self.window.tabs.count(), 2)
        self.window._close_tab(1)
        self.assertEqual(self.window.tabs.count(), 1)

    def test_new_query_overwrites_current_tab(self):
        _, env1, policy1, s1, menu1 = context("NaCl")
        tab1 = self.window.apply_context(env1, policy1, s1, menu1)
        _, env2, policy2, s2, menu2 = context("E=mc²")
        tab2 = self.window.apply_context(env2, policy2, s2, menu2)
        self.assertIs(tab1, tab2)
        self.assertIn("E=mc²", tab2.preview.text())
        self.assertEqual(self.window.tabs.count(), 1)

    def test_new_tab_option(self):
        _, env, policy, s, menu = context("NaCl")
        tab2 = self.window.apply_context(env, policy, s, menu, new_tab=True)
        self.assertEqual(self.window.tabs.count(), 2)
        self.assertIsInstance(tab2, QueryTab)

    def test_query_bar_signal(self):
        captured = []
        self.window.query_submitted.connect(captured.append)
        self.window.query_edit.setText("break the ice")
        self.window._submit_query()
        self.assertEqual(captured, ["break the ice"])


class TestStudioApp(unittest.TestCase):
    def setUp(self):
        settings = load_defaults()
        # 测试固定走「本地预览」路径，不依赖用户是否配置了真实模型
        settings.data["desktop_api_url"] = ""
        settings.data["desktop_api_key"] = ""
        settings.data["desktop_model"] = ""
        settings.data["desktop_vision_api_url"] = ""
        settings.data["desktop_vision_api_key"] = ""
        settings.data["desktop_vision_model"] = ""
        self.app = StudioApp(settings, poll_interval=5.0)

    def tearDown(self):
        self.app._alive = False
        self.app.clip_worker._active = False

    def test_first_layer_then_second_layer_preview(self):
        self.app.process_text("NaCl", source="test")
        tab = self.app.window.current_tab()
        self.assertEqual(tab.stack.currentIndex(), 0)
        texts = [b.text() for b in tab._intent_buttons if not b.isHidden()]
        self.assertIn("综合化学条目", texts[0])
        self.assertIn("这个式子该怎么念", texts[1])
        self.app.on_intent(tab, {"intent_id": "chem.naming",
                                 "domain_id": "chem", "format_id": "static"})
        self.assertEqual(tab.stack.currentIndex(), 1)
        self.assertIn("未发送任何网络请求", tab.view.toPlainText())
        self.assertIn("IUPAC", tab.view.toPlainText())
        self.assertEqual(self.app.state.errors, [])

    def test_new_query_overwrites_current_tab(self):
        self.app.process_text("NaCl", source="test")
        tab = self.app.window.current_tab()
        self.app.process_text("E=mc²", source="test")
        self.assertIs(self.app.window.current_tab(), tab)
        self.assertIn("E=mc²", tab.preview.text())
        self.assertEqual(tab.stack.currentIndex(), 0)

    def test_new_tab_request(self):
        self.app.on_new_tab("break the ice")
        self.assertEqual(self.app.window.tabs.count(), 2)
        self.assertIn("break the ice",
                      self.app.window.current_tab().preview.text())

    def test_copy_query_request(self):
        self.app.on_copy_query("H2O")
        self.assertIn("H2O", self.app.window.current_tab().preview.text())

    def test_domain_change(self):
        self.app.process_text("NaCl", source="test")
        tab = self.app.window.current_tab()
        self.app.on_domain(tab, "en")
        self.assertEqual(tab.menu.domain_chips[0].id, "en")
        self.assertTrue(tab.menu.recommended)

    def test_settings_are_editable(self):
        dialog = SettingsDialog(self.app.settings)
        self.assertGreater(dialog.intent_total, 10)
        dialog.select_first_intent()
        # 首条是复用模板的 legacy（提示词为空是预期），另选一条专属的验证可编辑
        dialog._load_intent("chem.naming")
        self.assertTrue(dialog.intent_prompt.toPlainText())
        # 图片侧只有一条模板：选中「图片识别」后能直接改它的兜底提示词
        # （提示词允许留空，这里只验控件可用，不断言具体文案）
        dialog.route_combo.setCurrentIndex(
            dialog.route_combo.findData("image"))
        self.assertEqual(dialog.route_combo.currentData(), "image")
        self.assertTrue(dialog.route_prompt.isEnabled())
        # 下拉列出全部模板（文本 + 图片分组），且可增删
        self.assertGreaterEqual(dialog.route_combo.count(), 9)
        dialog.close()


class TestVisionIntegration(unittest.TestCase):
    def test_no_model_means_no_network(self):
        settings = load_defaults()
        # 视觉模型优先读 desktop_vision_*，只清 desktop_api_* 在配了视觉模型的
        # 机器上会真的发出请求 —— 两条来源都要清空才是「未配置」。
        for key in ("desktop_api_url", "desktop_api_key", "desktop_model",
                    "desktop_vision_api_url", "desktop_vision_api_key",
                    "desktop_vision_model"):
            settings.data[key] = ""
        result = recognize(b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png",
                           DEFAULT_VISION_PROMPT, settings)
        self.assertFalse(result.ok)
        self.assertIn("未配置视觉模型", result.error)

    def test_legacy_prompt_matches_chrome_extension_style(self):
        self.assertIn("LaTeX", DEFAULT_VISION_PROMPT)
        self.assertIn("行内公式", DEFAULT_VISION_PROMPT)


if __name__ == "__main__":
    unittest.main()
