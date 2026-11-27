"""Ticket 02 gate tests: entry flow skeleton (T004-T008).

RED first: every test here fails against the pre-ticket tree. Expectations are
hand-derived from schema.json, spec.md, research.md NC-001 and the ticket.
No network access: run_flow is a pure orchestration over preflight + classify.
"""
import json
import unittest
from pathlib import Path

import clipllm02
from clipllm02 import InputEnvelope, Settings, load_defaults, settings as settings_mod
from clipllm02 import policy as policy_mod

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec"


class TestT004ContractFields(unittest.TestCase):
    def test_flow_decision_carries_all_contract_fields(self):
        cls = getattr(clipllm02, "FlowDecision", None)
        self.assertIsNotNone(cls, "models.py must define FlowDecision")
        decision = cls(
            input_kind="text", local_regex_applied=True, content_length=7,
            length_tier="short_0_80", classification_result="english_word:english",
            hard_block=False, block_suppresses_popup=False,
            block_suppresses_request=False,
            flow_order=("local_regex_hard_block", "length_split",
                        "route_or_classify_or_block"),
            decision_model_fallback_enabled=False,
        )
        for field in ("input_kind", "local_regex_applied", "content_length",
                      "length_tier", "classification_result", "hard_block"):
            self.assertTrue(hasattr(decision, field), f"missing contract field {field}")
        self.assertEqual(decision.input_kind, "text")
        self.assertEqual(decision.length_tier, "short_0_80")
        self.assertIs(decision.decision_model_fallback_enabled, False)


class TestT005ModelConfigEntry(unittest.TestCase):
    def test_settings_exposes_both_model_configs(self):
        s = load_defaults()
        main = getattr(s, "main_model_config", None)
        decision = getattr(s, "decision_model_config", None)
        self.assertIsInstance(main, dict)
        self.assertIsInstance(decision, dict)
        for key in ("main_model_api_url", "main_model_key", "main_model_name"):
            self.assertIn(key, main)
            self.assertIn(key, decision)

    def test_validation_rejects_broken_model_config(self):
        data = load_defaults().as_dict()
        data["main_model_config"] = {"main_model_api_url": "https://x"}
        errors = settings_mod.validate(Settings(data))
        self.assertTrue(any(e.tab == "models" for e in errors),
                        "missing main_model_config keys must be reported")


class TestT006FactorySettings(unittest.TestCase):
    def test_text_max_chars_is_2000(self):
        data = json.loads((SPEC / "builtin_settings.json").read_text(encoding="utf-8"))
        self.assertEqual(data["thresholds"]["text_max_chars"], 2000)
        self.assertEqual(data["thresholds"]["code_max"], 80)
        self.assertEqual(data["thresholds"]["code_block_max"], 1000)

    def test_factory_has_two_model_config_sections(self):
        data = json.loads((SPEC / "builtin_settings.json").read_text(encoding="utf-8"))
        for section in ("main_model_config", "decision_model_config"):
            self.assertIsInstance(data.get(section), dict)
            for key in ("main_model_api_url", "main_model_key", "main_model_name"):
                self.assertIn(key, data[section])


class TestT007RuleSet(unittest.TestCase):
    RULES = {
        "empty", "no_letters", "single_latin", "url", "path", "email",
        "filename", "token_blob", "fake_word", "secret", "too_long", "duplicate",
    }

    def test_builtin_policy_keeps_exactly_12_rules(self):
        data = json.loads((SPEC / "builtin_policy.json").read_text(encoding="utf-8"))
        self.assertEqual(set(data["rules"]), self.RULES)
        self.assertNotIn("pem_block", data["rules"])
        self.assertNotIn("daily_limit", data["rules"])

    def test_secret_rule_covers_pem_block(self):
        result = clipllm02.preflight(
            InputEnvelope.for_text("-----BEGIN RSA PRIVATE KEY-----"),
            load_defaults())
        self.assertEqual(result.level, "BLOCK")
        self.assertIn("secret", result.block_reasons)


class TestT008FlowOrder(unittest.TestCase):
    ORDER = ("local_regex_hard_block", "length_split", "route_or_classify_or_block")

    def setUp(self):
        self.settings = load_defaults()
        self.run_flow = getattr(policy_mod, "run_flow", None)
        self.tier_of = getattr(policy_mod, "length_tier_of", None)

    def test_length_tier_boundaries(self):
        self.assertIsNotNone(self.tier_of)
        self.assertEqual(self.tier_of(0), "short_0_80")
        self.assertEqual(self.tier_of(80), "short_0_80")
        self.assertEqual(self.tier_of(81), "mid_80_2000")
        self.assertEqual(self.tier_of(2000), "mid_80_2000")
        self.assertEqual(self.tier_of(2001), "long_over_2000")

    def test_flow_order_is_fixed(self):
        self.assertEqual(tuple(policy_mod.FLOW_ORDER), self.ORDER)

    def _run(self, text, **kw):
        self.assertIsNotNone(self.run_flow, "policy.py must define run_flow")
        return self.run_flow(InputEnvelope.for_text(text), self.settings, **kw)

    def test_short_text_gets_classified_and_not_blocked(self):
        d = self._run("NaCl")
        self.assertEqual(d.length_tier, "short_0_80")
        self.assertIs(d.hard_block, False)
        self.assertTrue(d.classification_result)
        self.assertEqual(d.input_kind, "text")
        self.assertIs(d.local_regex_applied, True)
        self.assertIs(d.block_suppresses_popup, False)
        self.assertIs(d.block_suppresses_request, False)

    def test_blocked_text_still_produces_classification(self):
        d = self._run("https://example.com")
        self.assertIs(d.hard_block, True)
        self.assertTrue(d.classification_result, "classification before suppression")
        self.assertIs(d.block_suppresses_popup, True)
        self.assertIs(d.block_suppresses_request, True)

    def test_over_2000_is_blocked(self):
        d = self._run("A" * 2001)
        self.assertEqual(d.length_tier, "long_over_2000")
        self.assertIs(d.hard_block, True)
        self.assertIs(d.block_suppresses_request, True)

    def test_decision_model_fallback_is_never_enabled(self):
        d = self._run("break the ice.")
        self.assertIs(d.decision_model_fallback_enabled, False)

    def test_image_skips_local_regex(self):
        asset = clipllm02.AssetRef("a" * 16, "image/png", "b" * 16, "memory://a")
        d = self.run_flow(InputEnvelope.for_image(asset), self.settings)
        self.assertEqual(d.input_kind, "image_screenshot")
        self.assertIs(d.local_regex_applied, False)
        self.assertIs(d.hard_block, False)


if __name__ == "__main__":
    unittest.main()
