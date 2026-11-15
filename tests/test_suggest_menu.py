import unittest

from clipllm02 import (
    InputEnvelope, Preferences, build_menu, load_defaults, load_settings,
    preflight, sha256_text, suggest,
)


def make_menu(text, settings, prefs=None):
    env = InputEnvelope.for_text(text, source="test")
    policy = preflight(env, settings)
    s = suggest(env, policy, settings)
    return s, build_menu(s, settings, prefs or Preferences())


class TestDomainResolver(unittest.TestCase):
    def setUp(self):
        self.settings = load_defaults()

    def test_nacl_is_mixed(self):
        s, _ = make_menu("NaCl", self.settings)
        self.assertEqual(s.level, "MIXED")
        self.assertEqual({c.id for c in s.domain_candidates}, {"chem", "en"})

    def test_emc2_is_mixed_math_physics(self):
        s, _ = make_menu("E=mc²", self.settings)
        self.assertEqual(s.level, "MIXED")
        self.assertEqual({c.id for c in s.domain_candidates}, {"math", "physics"})

    def test_phrase_is_high(self):
        s, _ = make_menu("break the ice", self.settings)
        self.assertEqual(s.level, "HIGH")
        self.assertEqual(s.top_domain, "en")

    def test_undetermined_is_low(self):
        s, menu = make_menu("12345", self.settings)
        self.assertEqual(s.level, "LOW")
        self.assertTrue(menu.recommended)          # 自动出菜单，不强制两步
        self.assertTrue(menu.domain_chips)         # 但换领域的选项保留

    def test_scores_are_heuristic_not_probability(self):
        s, _ = make_menu("NaCl", self.settings)
        self.assertTrue(all(0 < c.score <= 1 for c in s.domain_candidates))
        self.assertTrue(all(c.evidence for c in s.domain_candidates))


class TestMenuBuilder(unittest.TestCase):
    def setUp(self):
        self.settings = load_defaults()

    def test_recommended_and_other_sections(self):
        _, menu = make_menu("NaCl", self.settings)
        self.assertTrue(menu.recommended)
        self.assertLessEqual(len(menu.recommended), 12)
        self.assertGreaterEqual(len(menu.recommended), 4)   # 领域专属选项充足
        ids = {o.intent.id for o in menu.recommended}
        self.assertIn("chem.legacy", ids)
        self.assertTrue(any(o.section == "other" for o in menu.others))
        self.assertTrue(all(o.intent.domains for o in menu.others))

    def test_labels_are_goal_language(self):
        _, menu = make_menu("NaCl", self.settings)
        for option in menu.recommended + menu.others:
            self.assertNotIn("route", option.intent.label.lower())
            self.assertTrue(option.intent.label)

    def test_disabled_domain_removes_chips_and_intents(self):
        settings = load_settings({"feature_switches": {"en_query": False}})
        s, menu = make_menu("NaCl", settings)
        self.assertNotIn("en", [c.id for c in menu.domain_chips])
        for option in menu.recommended + menu.others:
            self.assertNotIn("en", option.intent.domains)
        self.assertTrue(menu.recommended)      # 化学仍可用

    def test_disabling_phrase_route(self):
        settings = load_settings({"routes": {"en_phrase": {"enabled": False}}})
        _, menu = make_menu("break the ice", settings)
        enabled_routes = {o.intent.primary_route for o in menu.recommended + menu.others}
        self.assertNotIn("en_phrase", enabled_routes)
        grayed = {o.intent.id for o in menu.grayed}
        self.assertIn("en.phrase.legacy", grayed)

    def test_hidden_disabled_items(self):
        settings = load_settings({"menu": {"show_disabled_items": "hidden"}})
        _, menu = make_menu("NaCl", settings)
        self.assertEqual(menu.grayed, ())

    def test_preference_only_sets_default(self):
        text = "NaCl"
        prefs = Preferences(last={sha256_text(text): "chem.legacy"})
        _, menu = make_menu(text, self.settings, prefs)
        self.assertEqual(menu.default_intent, "chem.legacy")
        self.assertTrue(menu.recommended)       # 排序变化不影响选项集合

    def test_formats_and_tools_are_config_driven(self):
        _, menu = make_menu("NaCl", self.settings)
        self.assertIn("static", menu.formats)
        self.assertTrue(menu.tools)
        self.assertTrue(menu.free_input["enabled"])
        self.assertEqual(menu.free_input["modes"], ["instruction", "prompt"])
        self.assertTrue(menu.free_input["style_refs"])

    def test_image_menu_uses_image_intents(self):
        from clipllm02 import AssetRef, InputEnvelope, build_menu, preflight, suggest
        asset = AssetRef("a" * 16, "image/png", "a" * 16, "memory://a")
        env = InputEnvelope.for_image(asset)
        s = suggest(env, preflight(env, self.settings), self.settings)
        menu = build_menu(s, self.settings)
        ids = {o.intent.id for o in menu.recommended}
        self.assertTrue(ids.intersection({"image.ocr", "image.formula", "image.explain"}))
        self.assertIn("annotate", menu.formats)

    def test_menu_has_no_model_calls(self):
        # 纯本地对象构造：MenuModel 里不应出现任何 prompt 正文或 API 字段
        _, menu = make_menu("NaCl", self.settings)
        blob = str(menu.as_dict())
        self.assertNotIn("api_key", blob)
        self.assertNotIn("system_prompt", blob)


if __name__ == "__main__":
    unittest.main()
