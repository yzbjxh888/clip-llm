"""Ticket 10 gate tests: decision model drives short-tier initial judgment.

RED first: `suggest()` always uses the local regex classification and the
Studio call site never consults short_classifier_source. The SystemOne call is
mocked; the StudioApp runs offscreen and never sends a network request.
"""
import json
import os
import unittest
from importlib import import_module
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import (  # noqa: E402
    InputEnvelope, build_menu, load_defaults, preflight, suggest,
)
from clipllm02 import settings as settings_mod  # noqa: E402
from clipllm02.desktop import gateway as gateway_mod  # noqa: E402
from clipllm02.studio.app import StudioApp  # noqa: E402

# `clipllm02.suggest` is re-exported as a function; import the module itself.
suggest_mod = import_module("clipllm02.suggest")

LONG_TEXT = "这是一段关于机器学习梯度下降的中文段落，讨论了收敛条件与学习率的选择。" * 3
DECISION_ID = "中文答疑"


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _decision_settings():
    return settings_mod.load_settings({"short_classifier_source": "decision_model"})


class TestShortSourceSelection(unittest.TestCase):
    def test_decision_id_replaces_local_regex_category(self):
        s = _decision_settings()
        env = InputEnvelope.for_text("NaCl")
        policy = preflight(env, s)
        with patch.object(gateway_mod, "request_route", return_value=json.dumps(
                {"selected_route_id": DECISION_ID})) as req:
            out = suggest_mod.suggest_with_short_source(env, policy, s)
        self.assertEqual(req.call_args.kwargs["state"], "NaCl")
        self.assertIsNotNone(out.classification)
        self.assertEqual(out.classification.category, DECISION_ID)
        self.assertEqual(out.classification.reason, "decision_model")

    def test_local_source_keeps_existing_regex_classification(self):
        s = load_defaults()
        env = InputEnvelope.for_text("NaCl")
        policy = preflight(env, s)
        base = suggest(env, policy, s)
        with patch.object(gateway_mod, "request_route") as req:
            out = suggest_mod.suggest_with_short_source(env, policy, s)
        req.assert_not_called()
        self.assertEqual(out.classification, base.classification)

    def test_decision_failure_is_explicit_no_result(self):
        s = _decision_settings()
        env = InputEnvelope.for_text("NaCl")
        policy = preflight(env, s)
        with patch.object(gateway_mod, "request_route", return_value=""):
            out = suggest_mod.suggest_with_short_source(env, policy, s)
        self.assertIsNone(out.classification, "no silent fallback to regex")
        self.assertTrue(any("决策模型" in w for w in out.warnings))

    def test_mid_tier_never_calls_decision_model(self):
        s = _decision_settings()
        env = InputEnvelope.for_text(LONG_TEXT)
        policy = preflight(env, s)
        base = suggest(env, policy, s)
        with patch.object(gateway_mod, "request_route") as req:
            out = suggest_mod.suggest_with_short_source(env, policy, s)
        req.assert_not_called()
        self.assertEqual(out.classification, base.classification)


class TestMenuHighlight(unittest.TestCase):
    def test_menu_highlights_route_returned_by_decision_model(self):
        s = _decision_settings()
        env = InputEnvelope.for_text("NaCl")
        policy = preflight(env, s)
        with patch.object(gateway_mod, "request_route", return_value=json.dumps(
                {"selected_route_id": DECISION_ID})):
            out = suggest_mod.suggest_with_short_source(env, policy, s)
        menu = build_menu(out, s)
        self.assertEqual(menu.category, DECISION_ID)
        self.assertTrue(menu.category_chips)
        self.assertEqual(menu.category_chips[0].id, DECISION_ID)


class TestStudioWiring(unittest.TestCase):
    def setUp(self):
        settings = _decision_settings()
        for key in ("desktop_api_url", "desktop_api_key", "desktop_model",
                    "desktop_vision_api_url", "desktop_vision_api_key",
                    "desktop_vision_model"):
            settings.data[key] = ""
        self.app = StudioApp(settings, poll_interval=5.0)

    def tearDown(self):
        self.app._alive = False
        self.app.clip_worker._active = False

    def test_process_text_uses_decision_model_category(self):
        with patch.object(gateway_mod, "request_route", return_value=json.dumps(
                {"selected_route_id": DECISION_ID})) as req:
            self.app.process_text("NaCl", source="test")
        self.assertEqual(req.call_count, 1)
        tab = self.app.window.current_tab()
        self.assertEqual(tab.suggestion.classification.category, DECISION_ID)


if __name__ == "__main__":
    unittest.main()
