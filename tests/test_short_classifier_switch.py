"""Ticket 09 gate tests: mutually-exclusive short classifier source (FR-021 / SC-007).

RED first: no short_classifier_source on Settings, no config normalizer, no
UI switches. Qt is offscreen; no network calls are made.
"""
import json
import os
import unittest
from pathlib import Path

from clipllm02 import InputEnvelope, load_defaults, route_mid_text, run_flow
from clipllm02 import settings as settings_mod

ROOT = Path(__file__).resolve().parents[1]


class TestConfigLayer(unittest.TestCase):
    def test_builtin_settings_registers_local_as_default(self):
        data = json.loads((ROOT / "spec/builtin_settings.json").read_text(encoding="utf-8"))
        self.assertEqual(data.get("short_classifier_source"), "local_classifier")
        self.assertEqual(load_defaults().short_classifier_source, "local_classifier")

    def test_decision_model_value_is_honored(self):
        s = settings_mod.load_settings({"short_classifier_source": "decision_model"})
        self.assertEqual(s.short_classifier_source, "decision_model")

    def test_invalid_value_falls_back_to_local(self):
        s = settings_mod.load_settings({"short_classifier_source": "bogus"})
        self.assertEqual(s.short_classifier_source, "local_classifier")

    def test_both_flags_true_normalizes_to_local_with_notice(self):
        source, notice = settings_mod.normalize_short_classifier_source({
            "local_classifier_enabled": True,
            "decision_model_enabled": True,
            "short_classifier_source": "decision_model",
        })
        self.assertEqual(source, "local_classifier")
        self.assertTrue(notice, "normalization must be visible to the user")

    def test_load_settings_applies_normalization(self):
        s = settings_mod.load_settings({
            "local_classifier_enabled": True,
            "decision_model_enabled": True,
        })
        self.assertEqual(s.short_classifier_source, "local_classifier")
        self.assertTrue(s.short_source_notice)

    def test_single_flag_selects_that_source(self):
        source, _ = settings_mod.normalize_short_classifier_source(
            {"decision_model_enabled": True})
        self.assertEqual(source, "decision_model")


class TestSwitchScope(unittest.TestCase):
    def test_switch_leaves_mid_tier_and_diagnostics_untouched(self):
        text = "这是一段关于机器学习梯度下降的中文段落，讨论了收敛条件与学习率的选择。" * 3
        env = InputEnvelope.for_text(text)
        local = load_defaults()
        decision = settings_mod.load_settings(
            {"short_classifier_source": "decision_model"})

        a = route_mid_text(env, local)
        b = route_mid_text(env, decision)
        self.assertEqual(a.classification_result, b.classification_result)
        self.assertTrue(b.local_classifier_enabled_for_mid)
        self.assertFalse(b.state_sent)
        self.assertEqual(run_flow(env, local).classification_result,
                         run_flow(env, decision).classification_result)


class TestDialogExclusivity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.qapp = QApplication.instance() or QApplication([])
        from clipllm02.studio.settings_dialog import SettingsDialog
        cls.dialog_cls = SettingsDialog

    def test_opening_one_switch_closes_the_other(self):
        dlg = self.dialog_cls(load_defaults())
        try:
            self.assertTrue(dlg.short_source_local.isChecked())
            dlg.short_source_decision.setChecked(True)
            self.assertFalse(dlg.short_source_local.isChecked())
            self.assertEqual(dlg.data["short_classifier_source"], "decision_model")
            self.assertIs(dlg.data["local_classifier_enabled"], False)

            dlg.short_source_local.setChecked(True)
            self.assertFalse(dlg.short_source_decision.isChecked())
            self.assertEqual(dlg.data["short_classifier_source"], "local_classifier")
        finally:
            dlg.close()

    def test_dialog_shows_saved_source(self):
        saved = settings_mod.load_settings(
            {"short_classifier_source": "decision_model"})
        dlg = self.dialog_cls(saved)
        try:
            self.assertTrue(dlg.short_source_decision.isChecked())
            self.assertFalse(dlg.short_source_local.isChecked())
        finally:
            dlg.close()


if __name__ == "__main__":
    unittest.main()
