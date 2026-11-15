import unittest

from clipllm02 import (
    InputEnvelope, build_menu, load_defaults, load_settings, preflight,
    structure_signature, suggest, validate,
)


class TestDefaults(unittest.TestCase):
    def test_factory_config_is_valid(self):
        errors = validate(load_defaults())
        self.assertEqual([e.as_dict() for e in errors], [])

    def test_defaults_cover_all_five_dimensions(self):
        s = load_defaults()
        self.assertEqual(s.trigger_policy, "menu")
        self.assertEqual(s.disabled_route_action, "hide_from_menu")
        self.assertTrue(s.domains)
        self.assertTrue(s.intents)
        self.assertTrue(s.routes)
        self.assertTrue(s.providers)
        self.assertEqual(s.get("image_text_conflict"), "image_only")

    def test_deep_merge_keeps_sibling_keys(self):
        s = load_settings({"menu": {"recommended_limit": 3}})
        self.assertEqual(s.menu["recommended_limit"], 3)
        self.assertTrue(s.menu["free_input_enabled"])
        self.assertEqual(s.trigger_policy, "menu")


class TestValidation(unittest.TestCase):
    def test_recommended_limit_bounded_by_number_keys(self):
        errors = validate(load_settings({"menu": {"recommended_limit": 13}}))
        self.assertTrue(any(e.field == "menu.recommended_limit" for e in errors))
        self.assertEqual(validate(load_settings({"menu": {"recommended_limit": 12}})), [])

    def test_menu_mode_rejects_auto_image_recognize(self):
        errors = validate(load_settings({
            "trigger_policy": "menu",
            "image": {"auto_recognize_in_menu": True},
        }))
        self.assertTrue(any(e.tab == "image" for e in errors))

    def test_search_provider_url_validation(self):
        bad = [{"id": "google", "label": "G", "url_template": "ftp://x/{q}",
                "mode": "default_browser", "enabled": True, "order": 1,
                "contexts": ["*"]}]
        errors = validate(load_settings({"search_providers": bad}))
        self.assertTrue(any(e.tab == "search" for e in errors))

    def test_disabled_browser_not_required_on_this_machine(self):
        # 出厂的 chrome_newtab 默认关闭，不应因本机无 google-chrome 而报错
        errors = validate(load_defaults())
        self.assertFalse(any("chrome_newtab" in e.field for e in errors))

    def test_disabled_route_does_not_block_saving(self):
        settings = load_settings({"routes": {"en_phrase": {"enabled": False}}})
        self.assertEqual([e.as_dict() for e in validate(settings)], [])
        env = InputEnvelope.for_text("break the ice")
        menu = build_menu(suggest(env, preflight(env, settings), settings), settings)
        enabled_routes = {o.intent.primary_route for o in menu.recommended + menu.others}
        self.assertNotIn("en_phrase", enabled_routes)
        self.assertIn("en.phrase.legacy", {o.intent.id for o in menu.grayed})

    def test_closing_phrase_query_is_valid(self):
        """规格 04_SETTINGS_SPEC.md §3.2：禁 intent + 禁 route 必须能直接保存。"""
        data = load_defaults().as_dict()
        data["routes"]["en_phrase"]["enabled"] = False
        for item in data["intents"]:
            if item.get("primary_route") == "en_phrase":
                item["enabled"] = False
        settings = load_settings(data)
        self.assertEqual([e.as_dict() for e in validate(settings)], [])
        env = InputEnvelope.for_text("break the ice")
        menu = build_menu(suggest(env, preflight(env, settings), settings), settings)
        self.assertFalse(any(o.intent.primary_route == "en_phrase"
                             for o in menu.recommended + menu.others))
        self.assertTrue(menu.recommended)     # 通用解释仍可用

    def test_unknown_feature_reported(self):
        settings = load_defaults()
        data = settings.as_dict()
        data["intents"][0]["applicable"] = {
            "feature": "not_a_feature", "op": "eq", "value": True}
        errors = validate(load_settings(data))
        self.assertTrue(any("未知特征" in e.message for e in errors))

    def test_unknown_domain_reported(self):
        settings = load_defaults()
        data = settings.as_dict()
        data["intents"][0]["domains"] = ["not_a_domain"]
        errors = validate(load_settings(data))
        self.assertTrue(any("未定义的领域" in e.message for e in errors))


class TestStructureSignature(unittest.TestCase):
    def test_signature_is_stable(self):
        self.assertEqual(structure_signature(load_defaults()),
                         structure_signature(load_defaults()))

    def test_policy_change_changes_signature(self):
        a = structure_signature(load_defaults())
        b = structure_signature(load_settings(
            {"policy": {"rules": {"url": {"level": "BLOCK"}}}}))
        self.assertNotEqual(a, b)

    def test_route_and_intent_changes_change_signature(self):
        a = structure_signature(load_defaults())
        b = structure_signature(load_settings(
            {"routes": {"en_word": {"enabled": False}}}))
        c = structure_signature(load_settings(
            {"menu": {"recommended_limit": 5}}))
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)


class TestProviders(unittest.TestCase):
    def test_context_filtering(self):
        s = load_defaults()
        ids = {p.id for p in s.enabled_search_providers(("*",))}
        self.assertEqual(len(ids), 7)
        for pid in ("baidu", "baidu_baike", "google", "google_wiki",
                    "alphaxiv", "arxiv", "scholar"):
            self.assertIn(pid, ids)

    def test_percent_s_placeholder(self):
        s = load_defaults()
        url = s.provider("baidu").build_url("NaCl & H2O")
        self.assertIn("wd=NaCl%20%26%20H2O", url)
        self.assertNotIn("%s", url)
        wiki = s.provider("google_wiki").build_url("NaCl")
        self.assertIn("site%3Aen.wikipedia.org%2Fwiki+NaCl", wiki)

    def test_url_encoding(self):
        s = load_defaults()
        url = s.provider("google").build_url("NaCl & H2O", max_chars=200)
        self.assertIn("NaCl%20%26%20H2O", url)

    def test_max_query_chars(self):
        s = load_defaults()
        url = s.provider("google").build_url("A" * 500, max_chars=10)
        self.assertIn("A" * 10, url)
        self.assertNotIn("A" * 11, url)


if __name__ == "__main__":
    unittest.main()
