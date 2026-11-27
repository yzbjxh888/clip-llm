"""Ticket 06 gate tests: one-way route/template sync (T025-T029).

RED first: no route_registry/route_template_sync API, no default prompt
injection, no json_writeback_forbidden guard. All tests use in-memory dicts;
no network and no UI.
"""
import copy
import unittest

from clipllm02 import Settings, load_defaults
from clipllm02 import prompts as prompts_mod
from clipllm02 import settings as settings_mod

DEFAULT_PROMPT = getattr(
    prompts_mod, "GENERIC_TEMPLATE", {}).get("prompt", "__missing_generic_template__")


class TestT026RegistryIsSingleSource(unittest.TestCase):
    def setUp(self):
        self.settings = load_defaults()

    def test_settings_exposes_route_registry(self):
        fn = getattr(self.settings, "route_registry", None)
        self.assertIsNotNone(fn, "Settings must expose route_registry")
        self.assertEqual(set(fn()), set(self.settings.enabled_route_ids()))

    def test_add_route_syncs_template(self):
        fn = getattr(settings_mod, "sync_route_templates", None)
        self.assertIsNotNone(fn, "settings.py must expose sync_route_templates")
        data = self.settings.as_dict()
        data["routes"]["my_route"] = {
            "id": "my_route", "label": "我的路由", "enabled": True,
            "query_type": "general", "fields": ["title", "answer"],
            "prompt": "", "sections": {},
        }
        out = fn(data)
        self.assertIn("my_route", out["route_prompts"])
        self.assertIn(DEFAULT_PROMPT, out["route_prompts"]["my_route"])

    def test_delete_route_removes_template(self):
        fn = getattr(settings_mod, "sync_route_templates", None)
        self.assertIsNotNone(fn)
        data = self.settings.as_dict()
        data["routes"].pop("英语单词")
        out = fn(data)
        self.assertNotIn("英语单词", out.get("route_prompts", {}))


class TestT028DefaultPrompt(unittest.TestCase):
    def test_new_template_gets_default_prompt(self):
        fn = getattr(prompts_mod, "new_template_prompt", None)
        self.assertIsNotNone(fn, "prompts.py must expose new_template_prompt")
        self.assertEqual(fn(), DEFAULT_PROMPT)

    def test_default_prompt_constant_in_settings(self):
        self.assertEqual(getattr(settings_mod, "NEW_TEMPLATE_DEFAULT_PROMPT", None),
                         DEFAULT_PROMPT)


class TestT025NoJsonWriteback(unittest.TestCase):
    def test_edit_template_does_not_change_registry(self):
        fn = getattr(settings_mod, "apply_template_edit", None)
        self.assertIsNotNone(fn, "settings.py must expose apply_template_edit")
        settings = load_defaults()
        before = list(settings.route_registry())
        result = fn(settings, "英语单词", "改过的模板")
        after = list(result.route_registry())
        self.assertEqual(after, before, "route_registry must never be written back")
        self.assertEqual(result.route_prompts["英语单词"], "改过的模板")

    def test_json_writeback_forbidden_flag(self):
        flag = getattr(settings_mod, "JSON_WRITEBACK_FORBIDDEN", None)
        self.assertIs(flag, True)
        fn = getattr(settings_mod, "assert_no_json_writeback", None)
        self.assertIsNotNone(fn)
        before = copy.deepcopy(load_defaults().as_dict())
        fn(before, {"route_prompts": {"英语单词": "changed"}})
        self.assertEqual(before["routes"], load_defaults().as_dict()["routes"])

    def test_validation_flags_template_without_route(self):
        data = load_defaults().as_dict()
        data.setdefault("route_prompts", {})["orphan_route"] = DEFAULT_PROMPT
        errors = settings_mod.validate(Settings(data))
        self.assertTrue(any(e.tab == "prompts" for e in errors),
                        "template without a route must be reported")
