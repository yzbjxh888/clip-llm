"""Ticket 03 T013: Studio 入口与经典桌面行为一致（SC-002）。

RED first: StudioApp 尚未记录 FlowDecision，桥接文本也未声明模态。
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import AssetRef, InputEnvelope, load_defaults  # noqa: E402
from clipllm02.studio.app import StudioApp  # noqa: E402


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class TestStudioEntryFlow(unittest.TestCase):
    def _app(self):
        return StudioApp(load_defaults(), poll_interval=5.0)

    def test_text_entry_records_text_modality(self):
        app = self._app()
        try:
            app.process_text("NaCl", source="test")
            flow = getattr(app.state, "flow", None)
            self.assertIsNotNone(flow, "studio entry must record a FlowDecision")
            self.assertEqual(flow.input_kind, "text")
            self.assertIs(flow.local_regex_applied, True)
            self.assertIs(flow.hard_block, False)
        finally:
            app.shutdown()

    def test_blocked_text_keeps_diagnostic_classification(self):
        app = self._app()
        try:
            app.process_text("https://example.com", source="test")
            flow = getattr(app.state, "flow", None)
            self.assertIsNotNone(flow)
            self.assertIs(flow.hard_block, True)
            self.assertTrue(flow.classification_result)
        finally:
            app.shutdown()

    def test_image_entry_skips_regex(self):
        app = self._app()
        try:
            asset = AssetRef("a" * 16, "image/png", "b" * 16, "memory://a")
            app._present_envelope(InputEnvelope.for_image(asset, source="screenshot"))
            flow = getattr(app.state, "flow", None)
            self.assertIsNotNone(flow)
            self.assertEqual(flow.input_kind, "image_screenshot")
            self.assertIs(flow.local_regex_applied, False)
        finally:
            app.shutdown()


class TestBridgeModalityDeclaration(unittest.TestCase):
    def test_bridge_text_is_declared_as_text_modality(self):
        from clipllm02.studio import input_bridge
        fn = getattr(input_bridge, "bridge_input_kind", None)
        self.assertIsNotNone(fn, "input_bridge must declare its modality")
        self.assertEqual(fn(), "text")


if __name__ == "__main__":
    unittest.main()
