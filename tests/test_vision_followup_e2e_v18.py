"""Ticket 34 end-to-end gates (v18): real offscreen app, no network.

① pinned LaTeX template -> one-click auto submit and LaTeX-templated request
② "auto" -> auto classify; "" -> first judgment only
④ settings dialog has no global switch (already gated in the removal file)

Uses the project's own make_app() (all API URLs blanked -> local preview only),
so on_intent never reaches the network.
"""
import os
import unittest
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402
from clipllm02 import InputEnvelope, UserSelection, build_request_prompt, new_id  # noqa: E402
from clipllm02.modality import MemoryAssetStore  # noqa: E402
from tests.test_studio_hardening import make_app, stop  # noqa: E402

LATEX_MARKER = "你是一位 LaTeX 数学公式解析专家"


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


OCR_TEXT = r"\(E = mc^2\)"


class TestOneClickEndToEnd(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def tearDown(self):
        stop(self.app)

    def _deliver(self, followup):
        png = b"\x89PNG" + b"0" * 64
        asset = MemoryAssetStore().put(png, "image/png")
        self.app._remember_asset(asset, png, "image/png")
        self.app._present_envelope(
            InputEnvelope.for_image(asset, source="屏幕截图"))
        tab = self.app.window.current_tab()
        seen = {}
        self.app.on_intent = lambda _tab, data: seen.update(data)
        self.app._on_vision_done((
            tab, SimpleNamespace(ok=True, text=OCR_TEXT),
            "image.ocr", "提取图片中的文字", followup))
        return tab, seen

    def test_1_pinned_latex_template_is_used_in_one_click(self):
        tab, seen = self._deliver("LaTeX 公式")
        self.assertIn("intent_id", seen, seen)
        intent = self.app.settings.intent(seen["intent_id"])
        self.assertIsNotNone(intent)
        self.assertEqual(intent.primary_route, "LaTeX 公式")
        self.assertNotIn("image", intent.domains)
        # the composed request really carries the LaTeX template
        selection = UserSelection(
            request_id=new_id("req"),
            envelope_id=seen.get("envelope_id") or new_id("env"),
            domain_id=seen.get("domain_id") or "*",
            intent_id=seen["intent_id"],
            format_id=seen.get("format_id") or "static")
        composed = build_request_prompt(selection, self.app.settings,
                                        content=OCR_TEXT)
        self.assertEqual(composed.query_type, "latex")
        self.assertIn(LATEX_MARKER, composed.prompt)
        self.assertEqual(tab.stack.currentIndex(), 1)  # result page, no 2nd click

    def test_2_auto_followup_submits_and_empty_waits(self):
        tab_auto, seen_auto = self._deliver("auto")
        self.assertIn("intent_id", seen_auto, seen_auto)
        self.assertNotIn("image", self.app.settings.intent(
            seen_auto["intent_id"]).domains)
        self.assertEqual(tab_auto.stack.currentIndex(), 1)

        tab_empty, seen_empty = self._deliver("")
        self.assertEqual(seen_empty, {}, seen_empty)  # no auto request
        self.assertEqual(tab_empty.stack.currentIndex(), 1)  # direct-show branch


if __name__ == "__main__":
    unittest.main()
