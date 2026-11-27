"""Ticket 19 gate tests: B new category -> query-method reuse intent.

RED first: B->A derivation creates only the route + route_prompts + label;
no query-method (intent) entry is generated, so the constraint chain's third
link (criteria label = template label = query-method label) is missing.
No network: assertions target our settings data and dialog state only.
"""
import copy
import json
import os
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import Settings, load_defaults, settings as settings_mod  # noqa: E402
from clipllm02.studio import config as config_mod  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

CATEGORY = "英语句子"
DESCRIPTION = "英文句子，含主谓宾结构的完整句子"
CATCH_ALL = "通用解析"
SOURCE = "decision_model"


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _b_json(categories):
    return json.dumps(categories, ensure_ascii=False)


def _with_category(base, category=CATEGORY, description=DESCRIPTION):
    b = dict(base["decision_model_categories"])
    b[category] = description
    return settings_mod.apply_decision_model_json(base, _b_json(b))


def _intent(data, intent_id):
    return next((i for i in data.get("intents") or []
                 if isinstance(i, dict) and i.get("id") == intent_id), None)


class TestDerivedQueryMethod(unittest.TestCase):
    def setUp(self):
        self.base = load_defaults().as_dict()

    def test_add_category_generates_route_template_and_reuse_intent(self):
        out, errors = _with_category(self.base)

        self.assertEqual(errors, [])
        self.assertEqual(out["decision_model_categories"][CATEGORY], DESCRIPTION)
        route = out["routes"][CATEGORY]
        self.assertEqual(route["label"], CATEGORY)
        self.assertTrue(str(out["route_prompts"][CATEGORY]).strip())

        intent = _intent(out, CATEGORY)
        self.assertIsNotNone(intent, "B new category must generate a query method")
        self.assertEqual(intent["label"], CATEGORY)
        self.assertEqual(intent["primary_route"], CATEGORY)
        self.assertEqual(intent.get("prompt_override"), "")
        self.assertEqual(intent.get("derived_template_source"), SOURCE)
        self.assertTrue(intent.get("custom"))

        self.assertEqual(set(settings_mod.validate(Settings(out))), set())

    def test_delete_category_removes_derived_intent_but_keeps_factory_intents(self):
        added, errors = _with_category(self.base)
        self.assertEqual(errors, [])
        self.assertIsNotNone(_intent(added, CATEGORY))
        reduced = {k: v for k, v in added["decision_model_categories"].items()
                   if k != CATEGORY}
        out, errors = settings_mod.apply_decision_model_json(added, _b_json(reduced))

        self.assertEqual(errors, [])
        self.assertNotIn(CATEGORY, out["routes"])
        self.assertNotIn(CATEGORY, out["route_prompts"])
        self.assertIsNone(_intent(out, CATEGORY))
        self.assertIsNotNone(_intent(out, "chem.legacy"))
        self.assertIsNotNone(_intent(out, "image.ocr"))

    def test_derived_intent_survives_config_merge(self):
        added, errors = _with_category(self.base)
        self.assertEqual(errors, [])

        merged = config_mod._merge_intents(self.base, added)

        self.assertIsNotNone(_intent({"intents": merged}, CATEGORY))

    def test_query_method_edits_do_not_write_back_b(self):
        added, errors = _with_category(self.base)
        self.assertEqual(errors, [])
        b_before = copy.deepcopy(added["decision_model_categories"])
        edited = copy.deepcopy(added)
        intent = _intent(edited, CATEGORY)
        intent["label"] = "用户改过的查询方式名"
        intent["prompt_override"] = "用户专属提示词"

        self.assertEqual(edited["decision_model_categories"], b_before)
        settings_mod.assert_no_decision_model_writeback(added, edited)


class TestDerivedQueryMethodDialog(unittest.TestCase):
    def test_b_apply_rebuilds_query_method_tree_entry(self):
        dlg = SettingsDialog(load_defaults())
        try:
            b = json.loads(dlg.decision_model_json_edit.toPlainText())
            b[CATEGORY] = DESCRIPTION
            dlg.decision_model_json_edit.setPlainText(_b_json(b))
            dlg.decision_model_json_apply.click()

            self.assertIsNotNone(_intent(dlg.data, CATEGORY))
            self.assertEqual(dlg.data["routes"][CATEGORY]["label"], CATEGORY)
            self.assertEqual(dlg.data["decision_model_categories"][CATEGORY],
                             DESCRIPTION)
        finally:
            dlg.close()


if __name__ == "__main__":
    unittest.main()
