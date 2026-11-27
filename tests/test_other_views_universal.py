"""Ticket 15 gate tests: other_views are universal, independent of confidence.

RED first: build_menu's LOW branch never reads other_views and substitutes
"domain · template" options instead, so the bottom "其他视角" changes with
content. These tests exercise the real menu builder; no network/UI.
"""
import unittest

from clipllm02 import (
    InputEnvelope, Preferences, build_menu, load_defaults, load_settings,
    preflight, suggest,
)

VIEW_IDS = (
    "view.origin", "view.development", "view.interpretation",
    "view.debate", "view.application", "view.comparison",
)


def menu_for(text, settings, prefs=None):
    env = InputEnvelope.for_text(text, source="test")
    s = suggest(env, preflight(env, settings), settings)
    return s, build_menu(s, settings, prefs or Preferences())


def other_ids(menu):
    return tuple(o.intent.id for o in menu.others)


class TestOtherViewsAreUniversal(unittest.TestCase):
    def setUp(self):
        self.settings = load_defaults()

    def test_low_confidence_shows_configured_views_not_domain_templates(self):
        s, menu = menu_for("12345", self.settings)
        self.assertEqual(s.level, "LOW")
        self.assertEqual(other_ids(menu), VIEW_IDS)
        self.assertFalse(any(i.startswith(("math.", "chem.", "en.", "code."))
                             for i in other_ids(menu)))

    def test_views_identical_across_low_high_and_mixed(self):
        low = menu_for("12345", self.settings)[1]
        high = menu_for("break the ice", self.settings)[1]
        mixed = menu_for("NaCl", self.settings)[1]
        self.assertEqual(other_ids(low), VIEW_IDS)
        self.assertEqual(other_ids(high), VIEW_IDS)
        self.assertEqual(other_ids(mixed), VIEW_IDS)

    def test_low_confidence_uses_custom_view_not_hardcoded_six(self):
        """A distinct config must reach the LOW branch, not just the default six."""
        settings = load_settings({"other_views": {"items": [
            {"id": "custom_angle", "label": "专属解读角度",
             "prompt": "自定义提示词"},
        ]}})
        s, menu = menu_for("12345", settings)
        self.assertEqual(s.level, "LOW")
        self.assertEqual(other_ids(menu), ("view.custom_angle",))
        self.assertEqual(menu.others[0].intent.label, "专属解读角度")

    def test_changing_decision_model_does_not_change_views(self):
        """B is independent from other_views (user Q1=C)."""
        data = self.settings.as_dict()
        data["decision_model_categories"]["my_test_category"] = "测试判别描述"
        edited = load_settings(data)
        s, menu = menu_for("12345", edited)
        self.assertEqual(s.level, "LOW")
        self.assertEqual(other_ids(menu), VIEW_IDS)
        self.assertEqual(edited.data["other_views"], self.settings.data["other_views"])

    def test_domain_affinity_fallback_when_views_not_configured(self):
        settings = load_settings({"other_views": {"items": []}})
        self.assertEqual(settings.data["other_views"]["items"], [])
        low = menu_for("12345", settings)[1]
        high = menu_for("break the ice", settings)[1]
        self.assertTrue(low.others)
        self.assertTrue(high.others)
        self.assertFalse(any(i.startswith("view.") for i in other_ids(low)))
        self.assertFalse(any(i.startswith("view.") for i in other_ids(high)))


if __name__ == "__main__":
    unittest.main()
