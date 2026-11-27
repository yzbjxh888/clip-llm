"""Ticket 16 gate tests: user-editable SystemOne `instructions`.

RED first: build_systemone_request hardcodes SYSTEMONE_INSTRUCTIONS and the
拦截 dialog offers no editor, so the user cannot change the decision-model
question's instructions. urlopen is mocked; assertions target our payload,
our dialog state and the schema artifact. No real network.
"""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import load_defaults  # noqa: E402
from clipllm02.desktop import gateway as gmod  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

CUSTOM_INSTRUCTIONS = "只按「能否直接解释输入」判类：能则选最贴合的类别，否则选 general。"
FACTORY_SCHEMA = (Path.home() / ".hermes/workspace/.factory/01-spec/schema.json")


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _Reply:
    def __init__(self, data):
        self._body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _send(settings, state="construct"):
    with patch.dict(os.environ, {"AGNES_API_KEY": "ENV_KEY"}), \
            patch.object(gmod.urllib.request, "urlopen",
                         return_value=_Reply({"answers": {"category": {
                             "choice": "英语单词", "probabilities": {"英语单词": 0.9}}}})) as up:
        gmod.request_route(settings, state=state,
                           route_registry=settings.route_registry())
    return json.loads(up.call_args.args[0].data)


class TestConfiguredInstructions(unittest.TestCase):
    def test_request_payload_uses_user_configured_instructions(self):
        settings = load_defaults()
        settings.data["decision_model_instructions"] = CUSTOM_INSTRUCTIONS
        payload = _send(settings)
        self.assertEqual(
            payload["questions"]["category"]["instructions"], CUSTOM_INSTRUCTIONS)

    def test_absent_field_falls_back_to_constant(self):
        settings = load_defaults()
        settings.data.pop("decision_model_instructions", None)
        payload = _send(settings)
        self.assertEqual(
            payload["questions"]["category"]["instructions"],
            gmod.SYSTEMONE_INSTRUCTIONS)

    def test_empty_field_falls_back_to_constant(self):
        settings = load_defaults()
        settings.data["decision_model_instructions"] = ""
        payload = _send(settings)
        self.assertEqual(
            payload["questions"]["category"]["instructions"],
            gmod.SYSTEMONE_INSTRUCTIONS)

    def test_instructions_do_not_change_criteria_from_b(self):
        settings = load_defaults()
        settings.data["decision_model_instructions"] = CUSTOM_INSTRUCTIONS
        payload = _send(settings)
        self.assertEqual(
            payload["questions"]["category"]["criteria"],
            gmod.systemone_categories(settings.data["decision_model_categories"]))


class TestInstructionsEditor(unittest.TestCase):
    def test_dialog_exposes_editable_instructions_field(self):
        dlg = SettingsDialog(load_defaults())
        try:
            # v14 票据 30：独立 instructions 框已并入一个平铺 JSON 框，
            # 通过 _instructions 行编辑，再走真实的「保存并派生模板」接线。
            editor = dlg.decision_model_json_edit
            pack = json.loads(editor.toPlainText())
            self.assertIn("_instructions", pack, "统一框缺少 _instructions 行")
            pack["_instructions"] = CUSTOM_INSTRUCTIONS
            editor.setPlainText(json.dumps(pack, ensure_ascii=False))
            dlg.decision_model_json_apply.click()
            self.assertEqual(
                dlg.data.get("decision_model_instructions"), CUSTOM_INSTRUCTIONS)
        finally:
            dlg.close()


class TestSchemaField(unittest.TestCase):
    def test_schema_declares_instructions_field(self):
        self.assertTrue(FACTORY_SCHEMA.is_file(), str(FACTORY_SCHEMA))
        schema = json.loads(FACTORY_SCHEMA.read_text(encoding="utf-8"))
        field = schema["properties"].get("decision_model_instructions")
        self.assertIsInstance(field, dict, "schema 缺少 decision_model_instructions")
        self.assertEqual(field.get("type"), "string")


if __name__ == "__main__":
    unittest.main()
