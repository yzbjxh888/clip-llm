"""Ticket 34 gate tests (v18): the global auto_continue switch is gone.

RED first. Hand-derived from CHANGE-vision-followup-autosubmit-v18.md section 四:
  - settings dialog: no _set_auto_continue, no auto_continue_check widget
  - config migration: no longer writes user.image.auto_continue
  - factory spec: keeps the field for data compatibility (regression guard)

Real offscreen SettingsDialog under a temp XDG_CONFIG_HOME; urlopen guarded so
a network call is an AssertionError. The real user config is never touched.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # noqa: E402

from PySide6.QtWidgets import QApplication, QCheckBox  # noqa: E402
from clipllm02 import load_defaults  # noqa: E402
from clipllm02.studio import config as config_mod  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROD_FILES = (
    "src/clipllm02/studio/app.py",
    "src/clipllm02/studio/config.py",
    "src/clipllm02/studio/settings_dialog.py",
)
FORBIDDEN_LABEL = "自动用默认方式再解释一遍"


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _SafeEnv(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="autocont-v18-")
        self._xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        self._net = patch("urllib.request.urlopen",
                          side_effect=AssertionError("no real network"))
        self._net.start()

    def tearDown(self):
        self._net.stop()
        if self._xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._xdg
        self._tmp.cleanup()
        super().tearDown()


class TestAutoContinueSwitchRemoved(_SafeEnv):
    def test_settings_dialog_has_no_auto_continue_checkbox(self):
        dialog = SettingsDialog(load_defaults())
        try:
            self.assertFalse(hasattr(dialog, "auto_continue_check"))
            self.assertFalse(hasattr(dialog, "_set_auto_continue"))
            texts = [cb.text() for cb in dialog.findChildren(QCheckBox)]
            self.assertFalse(any(FORBIDDEN_LABEL in t for t in texts), texts)
        finally:
            dialog.close()

    def test_config_migration_no_longer_writes_auto_continue(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            path.write_text(json.dumps({
                "config_version": 7,
                "image": {"max_bytes": 12345},
            }), encoding="utf-8")
            with patch.object(config_mod, "config_path", return_value=path):
                config_mod.load_config()
            # The runtime Settings deep-merges the factory spec, which keeps the
            # field for data compatibility. The migration's own write is what
            # must disappear — assert on the on-disk user config it writes.
            image = (json.loads(path.read_text(encoding="utf-8"))
                     .get("image") or {})
            self.assertNotIn("auto_continue", image, image)
            self.assertEqual(image.get("max_bytes"), 12345, image)
            # reload: still absent (no resurrection on restart)
            with patch.object(config_mod, "config_path", return_value=path):
                config_mod.load_config()
            image2 = (json.loads(path.read_text(encoding="utf-8"))
                      .get("image") or {})
            self.assertNotIn("auto_continue", image2, image2)

    def test_legacy_auto_continue_key_is_preserved_not_overwritten(self):
        """Existing user key survives the merge for data compat; it is just ignored."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            path.write_text(json.dumps({
                "config_version": 7,
                "image": {"auto_continue": True, "max_bytes": 12345},
            }), encoding="utf-8")
            with patch.object(config_mod, "config_path", return_value=path):
                config_mod.load_config()
            image = (json.loads(path.read_text(encoding="utf-8"))
                     .get("image") or {})
            self.assertTrue(image.get("auto_continue"), image)
            self.assertEqual(image.get("max_bytes"), 12345, image)

    def test_production_source_has_no_auto_continue_literal(self):
        for rel in PROD_FILES:
            with self.subTest(path=rel):
                text = (ROOT / rel).read_text(encoding="utf-8")
                count = text.count("auto_continue")
                self.assertEqual(count, 0, f"{rel}: {count} literal hits")

    def test_factory_spec_keeps_field_for_data_compatibility(self):
        # Regression guard: green at RED; counter-proved before closing.
        spec = json.loads((ROOT / "spec/builtin_settings.json").read_text(
            encoding="utf-8"))
        self.assertIn("image", spec)
        self.assertIn("auto_continue", spec["image"])
        self.assertFalse(spec["image"]["auto_continue"])


if __name__ == "__main__":
    unittest.main()
