"""Ticket 04 gate tests: short-text decision router (T014-T018).

RED first: no decision-model request helper, no registry validation, no
template application by selected route id. The gateway must be the only
network client; these tests never call the network (badge config / fake reply).
"""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from clipllm02 import InputEnvelope, Settings, FLOW_ORDER, load_defaults
from clipllm02 import policy as policy_mod
from clipllm02.desktop import gateway as gateway_mod

ROOT = Path(__file__).resolve().parents[1]


class TestT014DecisionRequest(unittest.TestCase):
    def setUp(self):
        self.settings = load_defaults()

    def test_gateway_has_decision_request_entry(self):
        fn = getattr(gateway_mod, "request_route", None)
        self.assertIsNotNone(fn, "gateway.py must expose request_route")

    def test_decision_config_drives_gateway_config(self):
        cls = getattr(gateway_mod, "DecisionConfig", None)
        self.assertIsNotNone(cls, "gateway.py must expose DecisionConfig")
        cfg = cls.from_settings(self.settings)
        self.assertEqual(cfg.api_url, self.settings.decision_model_config.get(
            "main_model_api_url"))
        self.assertEqual(cfg.model, self.settings.decision_model_config.get(
            "main_model_name"))

    def test_short_tier_sends_state_to_decision_model(self):
        fn = getattr(policy_mod, "route_short_text", None)
        self.assertIsNotNone(fn, "policy.py must expose route_short_text")
        reply = json.dumps({"selected_route_id": "英语单词"})
        with patch.object(gateway_mod, "request_route", return_value=reply) as req:
            chosen = fn(InputEnvelope.for_text("NaCl"), self.settings)
        self.assertEqual(chosen, "英语单词")
        self.assertEqual(req.call_count, 1)
        payload = req.call_args.kwargs.get("state")
        self.assertEqual(payload, "NaCl")


class TestT015RejectUnknownRoute(unittest.TestCase):
    def test_id_outside_registry_rejected(self):
        fn = getattr(policy_mod, "route_short_text", None)
        self.assertIsNotNone(fn)
        with patch.object(gateway_mod, "request_route",
                          return_value=json.dumps({"selected_route_id": "no_such_route"})):
            chosen = fn(InputEnvelope.for_text("NaCl"), load_defaults())
        self.assertIsNone(chosen, "unknown route id must not be accepted")


class TestT016SingleGatewayNoFallback(unittest.TestCase):
    def test_no_second_network_module_created(self):
        # No new client module: only gateway.py owns urllib.request.
        src = (ROOT / "src/clipllm02/desktop/gateway.py").read_text(encoding="utf-8")
        self.assertIn("urllib.request", src)

    def test_fallback_stays_disabled(self):
        d = policy_mod.run_flow(InputEnvelope.for_text("NaCl"), load_defaults())
        self.assertIs(d.decision_model_fallback_enabled, False)


class TestT018ComposeBySelectedRoute(unittest.TestCase):
    def test_selected_route_id_maps_to_prompt(self):
        fn = getattr(policy_mod, "compose_for_route", None)
        self.assertIsNotNone(fn, "policy.py must expose compose_for_route")
        composed = fn("英语单词", load_defaults())
        self.assertEqual(composed.query_type, "english_word")
        self.assertTrue(composed.prompt)

    def test_unknown_route_has_no_prompt(self):
        fn = getattr(policy_mod, "compose_for_route", None)
        self.assertIsNotNone(fn)
        with self.assertRaises(ValueError):
            fn("no_such_route", load_defaults())


class TestShortTierGate(unittest.TestCase):
    """FR-006: state 只在短段且未硬拦截时发往决策模型。"""

    def test_mid_tier_never_sends_state(self):
        fn = getattr(policy_mod, "route_short_text", None)
        self.assertIsNotNone(fn)
        with patch.object(gateway_mod, "request_route",
                          return_value=json.dumps({"selected_route_id": "英语单词"})) as req:
            chosen = fn(InputEnvelope.for_text("A" * 100), load_defaults())
        self.assertIsNone(chosen)
        self.assertEqual(req.call_count, 0)

    def test_blocked_short_text_never_sends_state(self):
        fn = getattr(policy_mod, "route_short_text", None)
        self.assertIsNotNone(fn)
        with patch.object(gateway_mod, "request_route",
                          return_value=json.dumps({"selected_route_id": "英语单词"})) as req:
            chosen = fn(InputEnvelope.for_text("https://example.com"), load_defaults())
        self.assertIsNone(chosen)
        self.assertEqual(req.call_count, 0)


if __name__ == "__main__":
    unittest.main()
