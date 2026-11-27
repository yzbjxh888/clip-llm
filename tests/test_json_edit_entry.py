"""Ticket 11 gate tests: settings routes JSON data entry (FR-023 / SC-004).

RED first: no apply_routes_json helper and no JSON editor in the dialog.
v9 ticket 25: the dialog editor was removed by the user's explicit decision;
the data-layer one-way apply gates below remain authoritative. No network;
Qt runs offscreen. Expectations are hand-derived literals.
"""
import copy
import json
import os
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import Settings, load_defaults  # noqa: E402
from clipllm02 import prompts as prompts_mod  # noqa: E402
from clipllm02 import settings as settings_mod  # noqa: E402

DEFAULT_PROMPT = getattr(
    prompts_mod, "GENERIC_TEMPLATE", {}).get("prompt", "__missing_generic_template__")
NEW_ROUTE = {"my_route": {"label": "我的路由", "enabled": True}}


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _json(routes, prompts=None):
    return json.dumps({"routes": routes, "route_prompts": prompts or {}},
                      ensure_ascii=False)


class TestOneWayJsonApply(unittest.TestCase):
    def setUp(self):
        self.base = load_defaults().as_dict()

    def test_new_route_gets_default_template(self):
        routes = dict(self.base["routes"])
        routes.update(NEW_ROUTE)
        out, errors = settings_mod.apply_routes_json(self.base, _json(routes))
        self.assertEqual(errors, [])
        self.assertEqual(set(out["routes"]),
                         set(self.base["routes"]) | {"my_route"})
        self.assertEqual(out["route_prompts"]["my_route"], DEFAULT_PROMPT)

    def test_delete_route_removes_its_template(self):
        base = copy.deepcopy(self.base)
        base.setdefault("route_prompts", {})["英语单词"] = "旧模板"
        kept = {k: v for k, v in base["routes"].items() if k != "英语单词"}
        out, errors = settings_mod.apply_routes_json(base, _json(kept))
        self.assertEqual(errors, [])
        self.assertNotIn("英语单词", out["routes"])
        self.assertNotIn("英语单词", out["route_prompts"])

    def test_bad_json_reports_error_and_keeps_original(self):
        base = copy.deepcopy(self.base)
        base["route_prompts"] = {"英语单词": "原模板"}
        out, errors = settings_mod.apply_routes_json(base, "{ not json")
        self.assertTrue(errors)
        self.assertEqual(out["routes"], base["routes"])
        self.assertEqual(out["route_prompts"], base["route_prompts"])

    def test_missing_routes_reports_error(self):
        out, errors = settings_mod.apply_routes_json(
            self.base, json.dumps({"route_prompts": {}}))
        self.assertTrue(errors)
        self.assertEqual(out, self.base)

    def test_template_edit_never_writes_back_routes(self):
        out, errors = settings_mod.apply_routes_json(self.base, _json(NEW_ROUTE))
        self.assertEqual(errors, [])
        edited = settings_mod.apply_template_edit(
            Settings(out), "my_route", "改过的模板")
        self.assertEqual(edited.as_dict()["routes"], out["routes"])
        self.assertEqual(edited.route_prompts["my_route"], "改过的模板")

    def test_new_json_route_does_not_change_b_candidates(self):
        # ADR-010: B is the single source. Editing the routes JSON (A) must
        # never write back to B's categories/criteria.
        out, errors = settings_mod.apply_routes_json(self.base, _json(NEW_ROUTE))
        self.assertEqual(errors, [])
        self.assertIn("my_route", out["routes"])
        b = out.get("decision_model_categories") or {}
        self.assertNotIn("my_route", b)
        self.assertEqual(b, self.base["decision_model_categories"])


class TestDialogJsonEditor(unittest.TestCase):
    """v9 票据 25：设置页的 routes JSON 编辑框已移除（UI 契约被用户推翻）；
    数据层 apply_routes_json 的单向落地契约不受影响，在这里继续把门。"""

    def test_routes_data_entry_still_applies_without_ui_editor(self):
        base = load_defaults().as_dict()
        base.setdefault("route_prompts", {})["英语单词"] = "用户写过的英语单词模板"
        routes = dict(base["routes"])
        routes.update(NEW_ROUTE)
        out, errors = settings_mod.apply_routes_json(
            base, _json(routes, base["route_prompts"]))
        self.assertEqual(errors, [])
        self.assertIn("my_route", out["routes"])
        self.assertEqual(out["route_prompts"]["my_route"], DEFAULT_PROMPT)
        self.assertEqual(out["route_prompts"]["英语单词"], "用户写过的英语单词模板",
                         "数据层 apply 不得覆盖用户改过的出厂模板")

    def test_invalid_json_reports_error_and_keeps_settings_without_ui(self):
        base = load_defaults().as_dict()
        base.setdefault("route_prompts", {})["英语单词"] = "原模板"
        before = copy.deepcopy(base)
        out, errors = settings_mod.apply_routes_json(base, "{ broken")
        self.assertTrue(errors)
        self.assertEqual(out["routes"], before["routes"])
        self.assertEqual(out["route_prompts"], before["route_prompts"])


if __name__ == "__main__":
    unittest.main()
