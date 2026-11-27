"""Ticket 07 gate tests: unified model config + final acceptance (T030-T040).

RED first: gateway still reads desktop_* split keys, vision still prefers
desktop_vision_*, no ruleset audit report, no NC-001 doc trace, no final
acceptance record. Network-free: all assertions are on config resolution.
"""
import json
import unittest
from pathlib import Path

from clipllm02 import load_defaults
from clipllm02.desktop import gateway as gateway_mod
from clipllm02.studio import vision as vision_mod

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = Path("/home/zhang/.hermes/workspace/.factory")


class TestT034GatewayUsesMainConfig(unittest.TestCase):
    def test_gateway_config_reads_main_model_config(self):
        s = load_defaults()
        cfg = gateway_mod.GatewayConfig.from_settings(s)
        main = s.main_model_config
        self.assertEqual(cfg.api_url, main["main_model_api_url"])
        self.assertEqual(cfg.api_key, main["main_model_key"])
        self.assertEqual(cfg.model, main["main_model_name"])


class TestT035VisionUsesMainConfig(unittest.TestCase):
    def test_vision_profile_no_longer_prefers_desktop_vision(self):
        s = load_defaults()
        s.data["desktop_vision_api_url"] = "https://legacy.invalid"
        s.data["desktop_vision_api_key"] = "LEGACY"
        s.data["desktop_vision_model"] = "legacy-model"
        profile = vision_mod.vision_profile(s)
        main = s.main_model_config
        self.assertEqual(profile["api_url"], main["main_model_api_url"])
        self.assertEqual(profile["api_key"], main["main_model_key"])
        self.assertEqual(profile["model"], main["main_model_name"])

    def test_text_and_image_share_same_config(self):
        s = load_defaults()
        gw = gateway_mod.GatewayConfig.from_settings(s)
        vp = vision_mod.vision_profile(s)
        self.assertEqual(gw.api_url, vp["api_url"])
        self.assertEqual(gw.api_key, vp["api_key"])
        self.assertEqual(gw.model, vp["model"])


class TestT030ConfigPageEntries(unittest.TestCase):
    def test_model_form_has_one_main_and_one_decision_entry(self):
        from clipllm02.studio.settings_dialog import SettingsDialog
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        dlg = SettingsDialog(load_defaults())
        try:
            keys = set(getattr(dlg, "_edits", {}))
            self.assertIn("main_model_api_url", keys)
            self.assertIn("decision_model_api_url", keys)
            self.assertNotIn("desktop_vision_api_url", keys)
            self.assertNotIn("desktop_vision_model", keys)
        finally:
            dlg.close()


class TestT037RulesetAudit(unittest.TestCase):
    def test_audit_report_lists_12_kept_and_2_removed(self):
        from clipllm02 import settings as settings_mod
        fn = getattr(settings_mod, "ruleset_audit", None)
        self.assertIsNotNone(fn, "settings.py must expose ruleset_audit")
        report = fn(load_defaults())
        self.assertEqual(len(report["kept"]), 12)
        self.assertEqual(set(report["removed"]), {"pem_block", "daily_limit"})
        self.assertTrue(report["no_conflict"])


class TestT038NC001Evidence(unittest.TestCase):
    def test_decisions_doc_registers_nc001_closure(self):
        text = (ROOT / "docs/07_DECISIONS.md").read_text(encoding="utf-8")
        self.assertIn("NC-001", text)
        self.assertIn("research.md", text)

    def test_explain_text_trace_covers_veto(self):
        from clipllm02.classify import explain_text
        s = load_defaults()
        trace = explain_text("这是一段中文自然句，里面夹着一个 f(x)=x^2 公式。", s)
        self.assertIn("L2 特征", trace)
        self.assertIn("判定类别", trace)


class TestT040PlanFinalAcceptance(unittest.TestCase):
    def test_plan_has_complexity_tracking_and_final_result(self):
        plan = (WORKSPACE / "03-plan/plan.md").read_text(encoding="utf-8")
        self.assertIn("Complexity Tracking", plan)
        self.assertIn("最终验收", plan)


if __name__ == "__main__":
    unittest.main()
