"""Ticket 24 gates (v8): generic full template, no legacy placeholder, factory fallback.

RED first: GENERIC_TEMPLATE does not exist, new_template_prompt() returns the
legacy placeholder, and B derivation injects that placeholder into every
factory route. All expectations are hand-derived from CHANGE-generic-template-v8.md
section 4. No network: the offscreen dialog test patches urlopen to fail loudly.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit  # noqa: E402

from clipllm02 import Settings, load_defaults  # noqa: E402
from clipllm02 import prompts as prompts_mod  # noqa: E402
from clipllm02 import settings as settings_mod  # noqa: E402
from clipllm02.desktop import gateway as gateway_mod  # noqa: E402
from clipllm02.prompts import compose_prompt  # noqa: E402
from clipllm02.studio import config as config_mod  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402
from tests._json_editor_v10 import editor_json  # noqa: E402

# Hand-derived from v8 section 4 (not read from the artifact under test).
EXPECTED_PROMPT = (
    "你是通用解析专家，服务于桌面词典。用户输入一段内容（可能是词、句、段落、公式或截图文字），"
    "读者希望能跨学科、多角度理解它。严格按 JSON 输出，禁止代码围栏。用清晰的中文，"
    "分块给出：它是什么、核心意思、结构或组成、常见用法或例子、必要的背景与来源、"
    "不同视角下的理解与争议。"
)
EXPECTED_FIELDS = [
    "title", "what_is_it", "core_meaning", "structure", "examples",
    "background", "perspectives", "caveats",
]
EXPECTED_SECTIONS = {
    "what_is_it": "【它是什么】一句话说清这是什么，它属于哪个领域。",
    "core_meaning": "【核心意思】用小白能懂的话讲清它到底表达什么，避免术语堆砌。",
    "structure": "【结构或组成】如果有内部结构或组成部分，分点列出并说明相互关系。",
    "examples": "【例子】至少两个具体例子，覆盖不同使用场景。",
    "background": "【背景与来源】必要的历史、起源或发展脉络，帮助理解它为什么是这样。",
    "perspectives": "【不同视角】从不同学科、范式或立场给出不同的理解方式。",
    "caveats": "【争议与注意】常见的误解、反对观点或需要注意的边界。",
}

# Encoded so this gate file itself contains no literal legacy placeholder.
LEGACY = "\u8fd9\u662f\u4ec0\u4e48?"
NEW_CATEGORY = "票24跨学科解析"
NEW_CRITERIA = "票24：任意内容的跨学科结构化解释（端到端新增类别）"
FACTORY_HINT = "你是一位英语词汇专家"


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _template():
    return getattr(prompts_mod, "GENERIC_TEMPLATE", None)


def _composed(settings, route_id):
    intent = next(i for i in settings.intents if i.primary_route == route_id)
    return compose_prompt(intent, settings)


class TestGenericTemplateContract(unittest.TestCase):
    def test_generic_template_exists_with_exact_prompt(self):
        tpl = _template()
        self.assertIsNotNone(tpl, "prompts.py must expose GENERIC_TEMPLATE")
        self.assertEqual(str(tpl.get("prompt") or ""), EXPECTED_PROMPT)

    def test_generic_template_has_exact_fields_and_sections(self):
        tpl = _template() or {}
        self.assertEqual(list(tpl.get("fields") or []), EXPECTED_FIELDS)
        self.assertEqual(dict(tpl.get("sections") or {}), EXPECTED_SECTIONS)

    def test_generic_template_contains_no_legacy_placeholder(self):
        tpl = _template()
        self.assertIsNotNone(tpl, "prompts.py must expose GENERIC_TEMPLATE")
        self.assertNotIn(LEGACY, json.dumps(tpl, ensure_ascii=False))


class TestNoLegacyDefault(unittest.TestCase):
    def test_new_template_prompt_returns_generic_prompt(self):
        self.assertEqual(prompts_mod.new_template_prompt(), EXPECTED_PROMPT)

    def test_both_default_constants_point_at_generic_template(self):
        for mod in (prompts_mod, settings_mod):
            self.assertEqual(getattr(mod, "NEW_TEMPLATE_DEFAULT_PROMPT", None),
                             EXPECTED_PROMPT)

    def test_no_legacy_placeholder_in_production_source(self):
        root = Path(prompts_mod.__file__).resolve().parents[2]
        sources = [root / "src" / "clipllm02" / "prompts.py",
                   root / "src" / "clipllm02" / "settings.py",
                   root / "src" / "clipllm02" / "studio" / "settings_dialog.py",
                   root / "spec" / "builtin_settings.json"]
        for path in sources:
            self.assertFalse(LEGACY in path.read_text(encoding="utf-8"),
                             f"{path.name} 仍含已废弃的默认占位文案")


class TestDerivedRouteInheritsGenericTemplate(unittest.TestCase):
    def test_new_category_gets_generic_prompt_fields_sections_and_labels(self):
        base = load_defaults().as_dict()
        b = dict(base["decision_model_categories"])
        self.assertNotIn(NEW_CATEGORY, b)
        b[NEW_CATEGORY] = NEW_CRITERIA
        out, errors = settings_mod.apply_decision_model_json(
            base, json.dumps(b, ensure_ascii=False))
        self.assertEqual(errors, [])

        route = (out.get("routes") or {}).get(NEW_CATEGORY)
        self.assertIsNotNone(route, "B 新类别必须派生同名路由")
        self.assertEqual(str(route.get("label") or ""), NEW_CATEGORY)
        self.assertEqual(route.get("derived_template_source"),
                         settings_mod.DERIVED_TEMPLATE_SOURCE)
        self.assertEqual(list(route.get("fields") or []), EXPECTED_FIELDS)
        self.assertEqual(dict(route.get("sections") or {}), EXPECTED_SECTIONS)
        self.assertEqual((out.get("route_prompts") or {}).get(NEW_CATEGORY),
                         EXPECTED_PROMPT)

        derived = [i for i in out.get("intents") or []
                   if isinstance(i, dict) and i.get("id") == NEW_CATEGORY]
        self.assertEqual(len(derived), 1, "必须派生一条同名查询方式")
        self.assertEqual(str(derived[0].get("label") or ""), NEW_CATEGORY)
        self.assertEqual(derived[0].get("primary_route"), NEW_CATEGORY)

        composed = _composed(Settings(out), NEW_CATEGORY)
        self.assertTrue(composed.prompt.startswith(EXPECTED_PROMPT),
                        "compose_prompt 必须拿到完整通用指令")
        for field in EXPECTED_FIELDS:
            self.assertIn(field, composed.fields)


class TestFactoryPromptNotOverwritten(unittest.TestCase):
    def test_sync_route_templates_keeps_factory_route_prompt_empty(self):
        base = load_defaults().as_dict()
        base["route_prompts"] = {
            rid: "" for rid in base["routes"] if rid != "图片识别"}
        out = settings_mod.sync_route_templates(base)
        for rid in ("英语单词", "中文词语", "LaTeX 公式"):
            self.assertFalse(str((out["route_prompts"].get(rid) or "").strip()),
                             f"出厂路由 {rid} 的空模板不能被通用默认值填充")

    def test_factory_b_apply_does_not_override_detailed_prompt(self):
        base = load_defaults().as_dict()
        b = base["decision_model_categories"]
        out, errors = settings_mod.apply_decision_model_json(
            base, json.dumps(b, ensure_ascii=False))
        self.assertEqual(errors, [])
        for rid in ("英语单词", "中文词语", "LaTeX 公式"):
            self.assertFalse(str((out["route_prompts"].get(rid) or "").strip()),
                             f"出厂路由 {rid} 不应被派生逻辑覆盖")
        settings = Settings(out)
        composed = _composed(settings, "英语单词")
        self.assertIn(FACTORY_HINT, composed.prompt,
                      "空 route_prompts 必须回退到出厂详细指令")
        self.assertNotIn(LEGACY, composed.prompt)
        self.assertNotIn(EXPECTED_PROMPT, composed.prompt)

    def test_compose_prompt_priority_still_unchanged(self):
        base = load_defaults().as_dict()
        base.setdefault("route_prompts", {})["英语单词"] = "票24用户编辑的模板"
        settings = Settings(base)
        composed = _composed(settings, "英语单词")
        self.assertTrue(composed.prompt.startswith("票24用户编辑的模板"))
        self.assertNotIn(EXPECTED_PROMPT, composed.prompt)


class TestSettingsUiCopy(unittest.TestCase):
    def test_ui_source_has_new_copy_and_no_legacy_placeholder(self):
        path = Path(settings_mod.__file__).resolve().parent / "studio" / "settings_dialog.py"
        text = path.read_text(encoding="utf-8")
        self.assertFalse(LEGACY in text, "设置页源码不得再出现废弃占位文案")
        # v9：routes JSON 编辑区块已按用户拍板移除，对应文案只剩派生路由提示两处。
        self.assertEqual(text.count("默认通用结构化模板"), 2,
                         "两处 UI 文案都必须改为默认通用结构化模板")

    def test_dialog_widget_texts_have_no_legacy_placeholder(self):
        dlg = SettingsDialog(load_defaults())
        try:
            texts = [w.text() for w in dlg.findChildren(QLabel)]
            texts += [w.toPlainText() for w in dlg.findChildren(QPlainTextEdit)]
            blob = "\n".join(texts)
            self.assertFalse(LEGACY in blob, "设置页控件文案不得出现废弃占位文案")
            self.assertIn("默认通用结构化模板", blob)
        finally:
            dlg.close()

    def test_derived_route_hint_uses_new_copy(self):
        base = load_defaults().as_dict()
        b = dict(base["decision_model_categories"])
        b[NEW_CATEGORY] = NEW_CRITERIA
        out, _ = settings_mod.apply_decision_model_json(
            base, json.dumps(b, ensure_ascii=False))
        dlg = SettingsDialog(Settings(out))
        try:
            idx = dlg.route_combo.findData(NEW_CATEGORY)
            self.assertGreaterEqual(idx, 0)
            dlg.route_combo.setCurrentIndex(idx)
            self.assertIn("默认通用结构化模板", dlg.route_kind_hint.text())
            self.assertNotIn(LEGACY, dlg.route_kind_hint.text())
        finally:
            dlg.close()


class _TempConfigHome(unittest.TestCase):
    def setUp(self):
        self._old_xdg = os.environ.get("XDG_CONFIG_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        self.dlg = None

    def tearDown(self):
        if self.dlg is not None:
            self.dlg.close()
        self._tmp.cleanup()
        if self._old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._old_xdg


class TestEndToEndDerivedTemplate(_TempConfigHome):
    def test_b_add_category_survives_disk_restart_and_composition(self):
        with patch.object(gateway_mod.urllib.request, "urlopen",
                          side_effect=AssertionError("票据24不得发起真实网络调用")):
            self.dlg = SettingsDialog(load_defaults())
            b = json.loads(self.dlg.decision_model_json_edit.toPlainText())
            self.assertNotIn(NEW_CATEGORY, b)
            b[NEW_CATEGORY] = NEW_CRITERIA
            self.dlg.decision_model_json_edit.setPlainText(
                json.dumps(b, ensure_ascii=False, indent=2))
            self.dlg.decision_model_json_apply.click()

            self.assertIn(NEW_CATEGORY, self.dlg.data["routes"])
            self.assertEqual(self.dlg.data["route_prompts"].get(NEW_CATEGORY),
                             EXPECTED_PROMPT)
            self.assertEqual(list(self.dlg.data["routes"][NEW_CATEGORY].get("fields") or []),
                             EXPECTED_FIELDS)

            ok = self.dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
            ok.click()  # 真实信号/槽 → _apply → save_config
            self.assertEqual(self.dlg.result(), QDialog.Accepted)

            path = config_mod.config_path()
            self.assertTrue(path.exists(), f"save_config 未写盘：{path}")
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(NEW_CATEGORY, disk["decision_model_categories"])
            self.assertIn(NEW_CATEGORY, disk["routes"])
            self.assertEqual(disk["route_prompts"].get(NEW_CATEGORY), EXPECTED_PROMPT)
            self.assertEqual(list(disk["routes"][NEW_CATEGORY].get("fields") or []),
                             EXPECTED_FIELDS)
            self.assertEqual(dict(disk["routes"][NEW_CATEGORY].get("sections") or {}),
                             EXPECTED_SECTIONS)

            restarted = config_mod.load_config()  # 重启：从磁盘重新加载
            self.assertIn(NEW_CATEGORY, restarted.routes)
            self.assertEqual(restarted.route_prompts.get(NEW_CATEGORY), EXPECTED_PROMPT)
            self.assertEqual(restarted.routes[NEW_CATEGORY].get("label"), NEW_CATEGORY)
            self.assertIn(NEW_CATEGORY, restarted.data["decision_model_categories"])
            intent = restarted.intent(NEW_CATEGORY)
            self.assertIsNotNone(intent)
            self.assertEqual(intent.label, NEW_CATEGORY)
            self.assertEqual(intent.primary_route, NEW_CATEGORY)
            composed = _composed(restarted, NEW_CATEGORY)
            self.assertTrue(composed.prompt.startswith(EXPECTED_PROMPT))

            reboot = SettingsDialog(restarted)
            try:
                idx = reboot.route_combo.findData(NEW_CATEGORY)
                self.assertGreaterEqual(idx, 0)
                reboot.route_combo.setCurrentIndex(idx)
                self.assertEqual(editor_json(reboot)["prompt"], EXPECTED_PROMPT)
                self.assertIn("默认通用结构化模板", reboot.route_kind_hint.text())
            finally:
                reboot.close()


if __name__ == "__main__":
    unittest.main()
