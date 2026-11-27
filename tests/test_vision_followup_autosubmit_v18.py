"""Ticket 34 gate tests (v18): auto_submit decided by each way's followup_route.

RED first. Expected behavior is hand-derived from
.factory/00-decisions/CHANGE-vision-followup-autosubmit-v18.md section 四:
  concrete template -> auto_submit=True (pin that template)
  followup "auto"   -> auto_submit=True (auto classify)
  followup ""       -> auto_submit=False (show first judgment, user picks)
Legacy payload without the followup field + image.ocr still defaults to "auto".

Baseline 817 passed: factory image.auto_continue is False, so the old code
submits nothing even for a pinned "LaTeX 公式" template — that is the bug.

No Qt window, no network, no real config: lightweight stand-ins drive the real
StudioApp._on_vision_done method and capture process_text kwargs.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from clipllm02 import load_defaults
from clipllm02.studio.app import StudioApp

OCR_TEXT = r"\(E = mc^2\)"


def drive(followup, *, intent_id="image.ocr", include_followup=True,
          settings=None):
    """Run the real _on_vision_done and return the kwargs it gave process_text."""
    app = MagicMock()
    app.settings = settings if settings is not None else load_defaults()
    seen = {}
    app.process_text = lambda text, **kw: seen.update(kw, text=text)
    tab = MagicMock()
    tab.request_generation = 0
    payload = [tab, SimpleNamespace(ok=True, text=OCR_TEXT),
               intent_id, "提取图片中的文字"]
    if include_followup:
        payload.append(followup)
    StudioApp._on_vision_done(app, tuple(payload))
    return seen


class TestAutoSubmitByFollowup(unittest.TestCase):
    def test_concrete_template_submits_even_with_global_switch_off(self):
        # Hand-derived: factory default switch is off; the bug is that it wins.
        self.assertFalse((load_defaults().get("image") or {}).get(
            "auto_continue"), "fixture must reproduce the reported bug")
        seen = drive("LaTeX 公式")
        self.assertTrue(seen.get("auto_submit"), seen)
        self.assertEqual(seen.get("force_route"), "LaTeX 公式", seen)
        self.assertEqual(seen.get("source"), "vision", seen)
        self.assertTrue(seen.get("force_dedup_bypass"), seen)

    def test_auto_followup_submits_without_pinned_route(self):
        seen = drive("auto")
        self.assertTrue(seen.get("auto_submit"), seen)
        self.assertEqual(seen.get("force_route", ""), "", seen)

    def test_empty_followup_waits_for_user_choice(self):
        # Regression guard: green at RED; counter-proved before closing.
        seen = drive("", intent_id="image.explain")
        self.assertFalse(seen.get("auto_submit"), seen)
        self.assertFalse(seen, seen)  # process_text never called: no second request

    def test_legacy_payload_defaults_to_auto_and_submits(self):
        # Old payload has no followup field; image.ocr compat default is "auto".
        seen = drive(None, include_followup=False)
        self.assertTrue(seen.get("auto_submit"), seen)
        self.assertEqual(seen.get("force_route", ""), "", seen)


if __name__ == "__main__":
    unittest.main()
