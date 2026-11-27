"""Ticket 12 gate tests: decision-model JSON (B) + B->A one-way derivation.

FR-024..026 / SC-008 / SC-009. RED first: none of the B helpers, the derived
template marker, or the 拦截-tab JSON editor exist yet. No network: SystemOne
urlopen is replaced with a structured reply; assertions target our own request
shape and the real settings/dialog code.
"""
import copy
import json
import os
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import Settings, load_defaults, new_template_prompt  # noqa: E402
from clipllm02 import prompts as prompts_mod  # noqa: E402
from clipllm02 import settings as settings_mod  # noqa: E402
from clipllm02.desktop import gateway as gmod  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

DEFAULT_PROMPT = getattr(
    prompts_mod, "GENERIC_TEMPLATE", {}).get("prompt", "__missing_generic_template__")
CATCH_ALL = "通用解析"

# Hand-derived from ADR-010 / CHANGE (10 text categories + catch-all).
B_DEFAULT_IDS = {
    "英语单词", "英语短语", "中文词语", "中文句解", "中文答疑",
    "中文段落", "LaTeX 公式", "代码", "化学物质", "通用解析",
}
B_CUSTOM = {
    "英语单词": "英语单词，单个词",
    "化学物质": "化学物质如 NaCl",
    "通用解析": "其他无法归类的内容（兜底）",
}


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _b_json(categories):
    return json.dumps(categories, ensure_ascii=False)


class _Reply:
    def __init__(self, data):
        self._body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _reply(choice):
    return {"answers": {"category": {"choice": choice,
                                      "probabilities": {choice: 0.9}}}}


class TestBIsSingleSource(unittest.TestCase):
    def test_defaults_expose_decision_model_categories(self):
        b = load_defaults().get("decision_model_categories")
        self.assertIsInstance(b, dict)
        self.assertEqual(set(b), B_DEFAULT_IDS)
        self.assertTrue(all(isinstance(v, str) and v for v in b.values()))

    def test_categories_come_from_b_not_registry(self):
        # systemone_categories takes B ({id: description}), not a route list.
        cats = gmod.systemone_categories(B_CUSTOM)
        self.assertEqual(cats, B_CUSTOM)

    def test_b_edit_changes_candidate_list(self):
        cats = gmod.systemone_categories({"英语单词": "自定义判别描述"})
        self.assertEqual(set(cats), {"英语单词", CATCH_ALL})
        self.assertEqual(cats["英语单词"], "自定义判别描述")

    def test_missing_general_gets_catch_all(self):
        cats = gmod.systemone_categories({"英语单词": "自定义判别描述"})
        self.assertIn(CATCH_ALL, cats)
        self.assertEqual(cats["英语单词"], "自定义判别描述")

    def test_request_criteria_comes_from_settings_b(self):
        settings = load_defaults()
        settings.data["decision_model_categories"] = dict(B_CUSTOM)
        with patch.dict(os.environ, {"AGNES_API_KEY": "ENV_KEY"}), \
                patch.object(gmod.urllib.request, "urlopen",
                             return_value=_Reply(_reply("英语单词"))) as up:
            gmod.request_route(settings, state="NaCl",
                               route_registry=("英语单词", "通用解析"))
        payload = json.loads(up.call_args.args[0].data)
        self.assertEqual(payload["questions"]["category"]["criteria"], B_CUSTOM)


class TestBAOneWayDerivation(unittest.TestCase):
    def setUp(self):
        self.base = load_defaults().as_dict()

    def test_add_category_creates_derived_template(self):
        b = dict(self.base["decision_model_categories"])
        b["my_category"] = "新类别判别描述"
        out, errors = settings_mod.apply_decision_model_json(self.base, _b_json(b))
        self.assertEqual(errors, [])
        self.assertIn("my_category", out["routes"])
        self.assertEqual(out["routes"]["my_category"]["derived_template_source"],
                         "decision_model")
        self.assertEqual(out["route_prompts"]["my_category"], DEFAULT_PROMPT)

    def test_add_category_does_not_overwrite_existing_template_text(self):
        base = copy.deepcopy(self.base)
        base.setdefault("route_prompts", {})["英语单词"] = "用户改过的解释"
        out, errors = settings_mod.apply_decision_model_json(
            base, _b_json(base["decision_model_categories"]))
        self.assertEqual(errors, [])
        self.assertEqual(out["route_prompts"]["英语单词"], "用户改过的解释")

    def test_delete_category_removes_only_its_derived_template(self):
        base = copy.deepcopy(self.base)
        base["decision_model_categories"]["my_category"] = "新类别"
        added, errors = settings_mod.apply_decision_model_json(
            base, _b_json(base["decision_model_categories"]))
        self.assertEqual(errors, [])
        # A user template that shares no category must survive the deletion.
        added["routes"]["user_tpl"] = {"label": "用户模板", "enabled": True}
        added["route_prompts"]["user_tpl"] = "自由文案"
        reduced = {k: v for k, v in added["decision_model_categories"].items()
                   if k != "my_category"}
        out, errors = settings_mod.apply_decision_model_json(
            added, _b_json(reduced))
        self.assertEqual(errors, [])
        self.assertNotIn("my_category", out["routes"])
        self.assertNotIn("my_category", out["route_prompts"])
        self.assertIn("user_tpl", out["routes"])
        self.assertEqual(out["route_prompts"]["user_tpl"], "自由文案")

    def test_change_description_updates_criteria_not_template_text(self):
        base = copy.deepcopy(self.base)
        base.setdefault("route_prompts", {})["英语单词"] = "用户改过的解释"
        b = dict(base["decision_model_categories"])
        b["英语单词"] = "英语单词，单个词"
        out, errors = settings_mod.apply_decision_model_json(base, _b_json(b))
        self.assertEqual(errors, [])
        self.assertEqual(out["decision_model_categories"]["英语单词"],
                         "英语单词，单个词")
        self.assertEqual(gmod.systemone_categories(
            out["decision_model_categories"])["英语单词"], "英语单词，单个词")
        self.assertEqual(out["route_prompts"]["英语单词"], "用户改过的解释")

    def test_template_edit_cannot_write_back_b(self):
        base = copy.deepcopy(self.base)
        patch_data = copy.deepcopy(base)
        patch_data["decision_model_categories"]["英语单词"] = "被回写了"
        with self.assertRaises(ValueError):
            settings_mod.assert_no_decision_model_writeback(base, patch_data)

    def test_template_edit_helper_leaves_b_intact(self):
        edited = settings_mod.apply_template_edit(
            Settings(self.base), "英语单词", "改过的模板")
        self.assertEqual(edited.as_dict()["decision_model_categories"],
                         self.base["decision_model_categories"])
        self.assertEqual(edited.route_prompts["英语单词"], "改过的模板")


class TestBJsonValidation(unittest.TestCase):
    def setUp(self):
        self.base = load_defaults().as_dict()

    def test_bad_json_reports_error_and_keeps_original(self):
        out, errors = settings_mod.apply_decision_model_json(
            self.base, "{ not json")
        self.assertTrue(errors)
        self.assertEqual(out, self.base)

    def test_non_object_json_reports_error(self):
        out, errors = settings_mod.apply_decision_model_json(self.base, "[]")
        self.assertTrue(errors)
        self.assertEqual(out, self.base)

    def test_non_string_description_reports_error(self):
        out, errors = settings_mod.apply_decision_model_json(
            self.base, _b_json({"英语单词": 42}))
        self.assertTrue(errors)
        self.assertEqual(out, self.base)

    def test_new_template_default_is_editable_value(self):
        # 通用模板是初始默认值，不是锁定/校验后的固定值；用户可自由改文案。
        base = copy.deepcopy(self.base)
        b_before = copy.deepcopy(base["decision_model_categories"])
        edited = settings_mod.apply_template_edit(
            Settings(base), "英语单词", "解释一下")
        self.assertEqual(edited.route_prompts["英语单词"], "解释一下")
        self.assertEqual(edited.as_dict()["decision_model_categories"], b_before)
        self.assertEqual(new_template_prompt(), DEFAULT_PROMPT)


class TestDialogBEditor(unittest.TestCase):
    def test_b_editor_lives_on_policy_tab_and_applies_changes(self):
        dlg = SettingsDialog(load_defaults())
        try:
            self.assertIn(dlg.policy_tab, [dlg.tabs.widget(i)
                                           for i in range(dlg.tabs.count())])
            current = json.loads(dlg.decision_model_json_edit.toPlainText())
            self.assertIn("英语单词", current)

            b = dict(current)
            b["from_ui"] = "界面新增类别"
            dlg.decision_model_json_edit.setPlainText(_b_json(b))
            dlg.decision_model_json_apply.click()
            self.assertIn("from_ui", dlg.data["decision_model_categories"])
            self.assertIn("from_ui", dlg.data["routes"])
            self.assertEqual(dlg.data["route_prompts"]["from_ui"], DEFAULT_PROMPT)
        finally:
            dlg.close()

    def test_bad_b_json_only_shows_error(self):
        dlg = SettingsDialog(load_defaults())
        try:
            before = copy.deepcopy(dlg.data)
            dlg.decision_model_json_edit.setPlainText("{ broken")
            dlg.decision_model_json_apply.click()
            self.assertIn("JSON", dlg.status_hint.text())
            self.assertEqual(dlg.data["decision_model_categories"],
                             before["decision_model_categories"])
        finally:
            dlg.close()

    def test_derived_template_shows_badge_in_prompts_tab(self):
        dlg = SettingsDialog(load_defaults())
        try:
            dlg.data["routes"]["from_ui"] = {
                "label": "from_ui", "enabled": True,
                "derived_template_source": "decision_model"}
            dlg.data["route_prompts"]["from_ui"] = DEFAULT_PROMPT
            dlg._rebuild_route_combo()
            labels = [dlg.route_combo.itemText(i)
                      for i in range(dlg.route_combo.count())]
            self.assertTrue(any("来自决策模型" in t for t in labels))
        finally:
            dlg.close()


if __name__ == "__main__":
    unittest.main()
