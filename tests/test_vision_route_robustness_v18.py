"""Ticket 34 gate tests (v18): new image ways + anti-conflict guards.

RED first. Hand-derived from CHANGE-vision-followup-autosubmit-v18.md section 四:
  - a newly added image way that pins a text template must auto-submit
  - the three existing conflict mechanisms ①②③ are NOT to be changed:
    ① validate: followup target must exist, be enabled, and not be a vision template
    ② the followup dropdown auto-lists every eligible text template
    ③ a new image way with no explicit template is backfilled to "auto"

The first gate is the new behavior (RED at baseline); the other three are
regression guards (green at RED, each counter-proved before closing).
"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402
from clipllm02 import load_defaults  # noqa: E402
from clipllm02.settings import Settings, validate  # noqa: E402
from clipllm02.studio.app import StudioApp  # noqa: E402
from clipllm02.studio.config import settings_from_data  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

FUTURE_ROUTE = "FUTURE 模板"
OCR_TEXT = r"\(E = mc^2\)"


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def future_data():
    data = load_defaults().as_dict()
    data["routes"][FUTURE_ROUTE] = {
        "id": FUTURE_ROUTE, "label": FUTURE_ROUTE, "enabled": True,
        "prompt": "future prompt", "vision": False,
        "query_type": "future", "kind": "simple",
    }
    data["intents"] = list(data["intents"]) + [
        {"id": "future.text", "label": "FUTURE 查询", "enabled": True,
         "primary_route": FUTURE_ROUTE, "domains": [FUTURE_ROUTE],
         "order": 999, "default_format": "static", "custom": True},
        {"id": "image.future", "label": "FUTURE 图片方式", "enabled": True,
         "primary_route": "图片识别", "domains": ["image"], "order": 998,
         "prompt_override": "future vision prompt", "custom": True,
         "followup_route": FUTURE_ROUTE, "use_legacy_template": False},
    ]
    return data


def drive_with(settings):
    app = MagicMock()
    app.settings = settings
    seen = {}
    app.process_text = lambda text, **kw: seen.update(kw, text=text)
    tab = MagicMock()
    tab.request_generation = 0
    StudioApp._on_vision_done(app, (
        tab, SimpleNamespace(ok=True, text=OCR_TEXT),
        "image.future", "FUTURE 图片方式", FUTURE_ROUTE))
    return seen


class TestNewWayAutoSubmit(unittest.TestCase):
    def test_new_image_way_with_pinned_template_auto_submits(self):
        data = future_data()
        settings = settings_from_data(data)
        # fixture sanity: the new way really carries the new template
        future = settings.intent("image.future")
        self.assertIsNotNone(future)
        self.assertEqual(future.followup_route, FUTURE_ROUTE)
        seen = drive_with(settings)
        self.assertTrue(seen.get("auto_submit"), seen)
        self.assertEqual(seen.get("force_route"), FUTURE_ROUTE, seen)


class TestConflictMechanismsUntouched(unittest.TestCase):
    """Regression guards: green at RED; each counter-proved before closing."""

    def test_validate_rejects_vision_and_missing_followup_target(self):
        # ① settings.py:1156-1174
        for bad in ("图片识别", "no_such_route"):
            with self.subTest(bad=bad):
                data = load_defaults().as_dict()
                target = next(i for i in data["intents"] if i["id"] == "image.ocr")
                target["followup_route"] = bad
                errors = validate(Settings(data))
                self.assertTrue(any("followup_route" in e.field for e in errors),
                                bad)

    def test_new_image_way_without_template_is_backfilled_to_auto(self):
        # ③ settings.py:456-462
        data = load_defaults().as_dict()
        extra = dict(next(i for i in data["intents"] if i["id"] == "image.ocr"))
        extra.update({"id": "image.future2", "label": "FUTURE 图片方式2",
                      "custom": True, "prompt_override": "",
                      "followup_route": ""})
        data["intents"] = list(data["intents"]) + [extra]
        settings = settings_from_data(data)
        self.assertEqual(settings.intent("image.future2").followup_route,
                         "auto")

    def test_followup_dropdown_lists_new_template(self):
        # ② settings_dialog.py:642-656
        dialog = SettingsDialog(settings_from_data(future_data()))
        try:
            dialog._rebuild_followup_options()
            combo = dialog.intent_followup_combo
            labels = [combo.itemText(i) for i in range(combo.count())]
            data = [combo.itemData(i) for i in range(combo.count())]
            self.assertIn(FUTURE_ROUTE, data, labels)
            self.assertTrue(any(FUTURE_ROUTE in label for label in labels), labels)
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
