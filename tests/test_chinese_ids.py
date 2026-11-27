"""Ticket 17 gate tests: Chinese category/route ids everywhere (破坏性改动).

RED first: builtin routes/categories still use English ids, user configs
carry old ids, and the SystemOne return path only accepts English ids.
All network calls are mocked; assertions target our ids, payload and migration.
"""
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from clipllm02 import InputEnvelope, Preferences, build_menu, load_defaults, preflight, suggest
from clipllm02.desktop import gateway as gmod
from clipllm02.studio import config as config_mod

EXPECTED_ROUTE_IDS = {
    "英语单词", "英语短语", "中文词语", "中文句解", "中文答疑", "中文段落",
    "LaTeX 公式", "代码", "化学物质", "通用解析", "图片识别",
}
EXPECTED_TEXT_CATEGORIES = EXPECTED_ROUTE_IDS - {"图片识别"}
VIEW_IDS = ("origin", "development", "interpretation", "debate",
            "application", "comparison")


class _Reply:
    def __init__(self, data):
        self._body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class TestChineseIdsInDefaults(unittest.TestCase):
    def setUp(self):
        self.settings = load_defaults()

    def test_route_ids_are_chinese_and_match_labels(self):
        self.assertEqual(set(self.settings.routes), EXPECTED_ROUTE_IDS)
        for rid, pack in self.settings.routes.items():
            self.assertEqual(pack.get("label"), rid, rid)

    def test_criteria_keys_are_chinese_text_categories(self):
        categories = self.settings.data["decision_model_categories"]
        self.assertEqual(set(categories), EXPECTED_TEXT_CATEGORIES)
        self.assertIn("通用解析", categories)      # catch-all 也改中文

    def test_intent_primary_routes_point_to_chinese_routes(self):
        for intent in self.settings.intents:
            if intent.primary_route:
                self.assertIn(intent.primary_route, self.settings.routes, intent.id)

    def test_legacy_assets_packs_use_chinese_route_ids(self):
        legacy = config_mod.load_legacy()
        self.assertTrue(set(legacy["packs"]) <= EXPECTED_ROUTE_IDS)
        self.assertIn("英语单词", legacy["packs"])

    def test_other_view_ids_are_untouched(self):
        views = self.settings.data["other_views"]["items"]
        self.assertEqual(tuple(v["id"] for v in views), VIEW_IDS)


class TestChineseIdsAtRuntime(unittest.TestCase):
    def test_systemone_accepts_chinese_returned_id(self):
        settings = load_defaults()
        reply = _Reply({"answers": {"category": {
            "choice": "英语单词", "probabilities": {"英语单词": 0.9}}}})
        with patch.dict(os.environ, {"AGNES_API_KEY": "ENV_KEY"}), \
                patch.object(gmod.urllib.request, "urlopen", return_value=reply) as up:
            out = gmod.request_route(settings, state="construct",
                                     route_registry=settings.route_registry())
        payload = json.loads(up.call_args.args[0].data)
        self.assertEqual(set(payload["questions"]["category"]["criteria"]),
                         EXPECTED_TEXT_CATEGORIES)
        self.assertEqual(json.loads(out)["selected_route_id"], "英语单词")

    def test_compose_prompt_uses_chinese_route(self):
        intent = load_defaults().intent("en.legacy")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.primary_route, "英语单词")

    def test_menu_other_section_still_uses_common_views(self):
        settings = load_defaults()
        env = InputEnvelope.for_text("12345", source="test")
        s = suggest(env, preflight(env, settings), settings)
        menu = build_menu(s, settings, Preferences())
        self.assertEqual(tuple(o.intent.id for o in menu.others),
                         tuple("view." + v for v in VIEW_IDS))


class TestUserConfigMigration(unittest.TestCase):
    def test_old_english_ids_are_migrated_and_persisted(self):
        old = {
            "config_version": 7,
            "decision_model_categories": {"en_word": "英语单词，单个词", "general": "兜底"},
            "routes": {"en_word": {"label": "英语单词", "enabled": True}},
            "route_prompts": {"en_word": "用户改过的文案"},
            "intents": [{"id": "custom.q", "label": "自定义",
                         "primary_route": "en_word"}],
        }
        with TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "clip-llm02"
            cfg_dir.mkdir()
            path = cfg_dir / "settings.json"
            path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
                settings = config_mod.load_config()
                saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("英语单词", settings.data["decision_model_categories"])
        self.assertNotIn("en_word", settings.data["decision_model_categories"])
        self.assertIn("英语单词", settings.data["route_prompts"])
        self.assertEqual(settings.data["route_prompts"]["英语单词"], "用户改过的文案")
        custom = next(i for i in settings.intents if i.id == "custom.q")
        self.assertEqual(custom.primary_route, "英语单词")
        self.assertIn("英语单词", saved["decision_model_categories"])
        self.assertNotIn("en_word", saved["decision_model_categories"])


if __name__ == "__main__":
    unittest.main()
