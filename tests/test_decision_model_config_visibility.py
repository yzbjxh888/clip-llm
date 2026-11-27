"""Ticket 23 gates: decision URL/model configurable; Key stays env-only.

RED first: DecisionConfig/builder/client hardcode SYSTEMONE_API_URL / MODEL and
the settings dialog writes the decision Key box into the config. These tests
drive the real offscreen dialog and a structured urlopen double — no network.
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QDialogButtonBox, QLabel,
)

from clipllm02 import load_defaults  # noqa: E402
from clipllm02.desktop import gateway as gmod  # noqa: E402
from clipllm02.studio import config as config_mod  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

CONFIGURED_URL = "https://decision23.example.invalid/custom/systemone"
CONFIGURED_MODEL = "ticket23-configured-decision-model"
ENV_KEY = "TICKET23_ENV_KEY"
FORBIDDEN_KEY = "TICKET23_MUST_NOT_BE_PERSISTED"
CATEGORIES = {"英语单词": "英语单词，单个词", "通用解析": "兜底"}


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _Reply:
    """Complete structured SystemOne envelope; no socket is ever opened."""

    def __init__(self, data):
        self._body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _reply():
    return _Reply({"answers": {"category": {
        "choice": "英语单词", "probabilities": {"英语单词": 0.9}}},
        "usage": {"total_tokens": 5}})


def _with_configured_decision(settings):
    section = settings.data.setdefault("decision_model_config", {})
    section["decision_model_api_url"] = CONFIGURED_URL
    section["decision_model_name"] = CONFIGURED_MODEL
    return settings


class TestDecisionConfigFromSettings(unittest.TestCase):
    def test_config_reads_configured_url_and_model(self):
        settings = _with_configured_decision(load_defaults())
        cfg = gmod.DecisionConfig.from_settings(settings)
        self.assertEqual(cfg.api_url, CONFIGURED_URL,
                         "决策模型 URL 必须从 decision_model_config 读取")
        self.assertEqual(cfg.model, CONFIGURED_MODEL,
                         "决策模型名称必须从 decision_model_config 读取")
        self.assertNotEqual(cfg.api_url, gmod.SYSTEMONE_API_URL)
        self.assertNotEqual(cfg.model, gmod.SYSTEMONE_MODEL)


class TestSystemOneRequestOverrides(unittest.TestCase):
    def test_builder_uses_configured_model_from_settings(self):
        settings = _with_configured_decision(load_defaults())
        payload = gmod.build_systemone_request(
            "NaCl", CATEGORIES, settings=settings)
        self.assertEqual(payload["model"], CONFIGURED_MODEL,
                         "model 必须被配置覆盖，而不是写死 SYSTEMONE_MODEL")


    def test_client_posts_to_configured_url_with_env_key(self):
        settings = _with_configured_decision(load_defaults())
        with patch.dict(os.environ, {"AGNES_API_KEY": ENV_KEY}), \
                patch.object(gmod.urllib.request, "urlopen",
                             return_value=_reply()) as up:
            out = gmod.request_route(settings, state="NaCl",
                                     route_registry=settings.route_registry())
        up.assert_called_once()
        req = up.call_args.args[0]
        self.assertEqual(req.full_url, CONFIGURED_URL,
                         "SystemOne 请求必须发往用户配置的 URL")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {ENV_KEY}")
        self.assertEqual(json.loads(req.data)["model"], CONFIGURED_MODEL)
        self.assertEqual(json.loads(out)["selected_route_id"], "英语单词")


class _TempConfigHome(unittest.TestCase):
    def setUp(self):
        self._old_xdg = os.environ.get("XDG_CONFIG_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        self.dlg = None

    def tearDown(self):
        if self.dlg is not None:
            self.dlg.close()
        self._tmp.cleanup()
        if self._old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._old_xdg

    def _disk(self):
        path = config_mod.config_path()
        self.assertTrue(path.exists(), f"save_config 未写盘：{path}")
        return json.loads(path.read_text(encoding="utf-8"))


class TestDecisionDialogEndToEnd(_TempConfigHome):
    def test_ok_persists_url_model_and_runtime_request_uses_them(self):
        self.dlg = SettingsDialog(load_defaults())
        self.dlg._edits["decision_model_api_url"].setText(CONFIGURED_URL)
        self.dlg._edits["decision_model_name"].setText(CONFIGURED_MODEL)

        ok = self.dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        ok.click()  # 真实信号/槽 → _apply → save_config
        self.assertEqual(self.dlg.result(), QDialog.Accepted)
        disk = self._disk()["decision_model_config"]
        self.assertEqual(disk.get("decision_model_api_url"), CONFIGURED_URL)
        self.assertEqual(disk.get("decision_model_name"), CONFIGURED_MODEL)

        restarted = config_mod.load_config()  # 重启：从磁盘重新加载
        reboot = SettingsDialog(restarted)
        try:
            self.assertEqual(reboot._edits["decision_model_api_url"].text(),
                             CONFIGURED_URL)
            self.assertEqual(reboot._edits["decision_model_name"].text(),
                             CONFIGURED_MODEL)
        finally:
            reboot.close()

        with patch.dict(os.environ, {"AGNES_API_KEY": ENV_KEY}), \
                patch.object(gmod.urllib.request, "urlopen",
                             return_value=_reply()) as up:
            gmod.request_route(restarted, state="NaCl",
                               route_registry=restarted.route_registry())
        req = up.call_args.args[0]
        self.assertEqual(req.full_url, CONFIGURED_URL)
        self.assertEqual(json.loads(req.data)["model"], CONFIGURED_MODEL)


class TestDecisionKeyReadOnly(_TempConfigHome):
    def test_key_box_is_readonly_with_hint_and_never_persisted(self):
        self.dlg = SettingsDialog(load_defaults())
        key_box = self.dlg._edits["decision_model_key"]
        self.assertTrue(key_box.isReadOnly(),
                        "决策模型 Key 框必须只读，守住票据 08 的环境变量决策")
        hints = [label.text() for label in self.dlg.findChildren(QLabel)]
        self.assertTrue(any("AGNES_API_KEY" in text for text in hints),
                        "Key 框旁必须提示由 AGNES_API_KEY 提供")
        key_box.setText(FORBIDDEN_KEY)  # 程序写入模拟误操作；只读框照样发 textChanged

        with patch.dict(os.environ, {"AGNES_API_KEY": ENV_KEY}), \
                patch.object(gmod.Path, "home",
                             return_value=gmod.Path("/nonexistent")):
            self.assertEqual(gmod.agnes_api_key(), ENV_KEY,
                             "agnes_api_key() 必须仍只读环境变量")
            self.assertNotEqual(gmod.agnes_api_key(), FORBIDDEN_KEY)

        ok = self.dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        ok.click()
        self.assertEqual(self.dlg.result(), QDialog.Accepted)
        disk = self._disk()["decision_model_config"]
        self.assertNotIn("decision_model_key", disk,
                         "Key 框内容绝不能写进配置")
        self.assertNotIn(FORBIDDEN_KEY, json.dumps(disk, ensure_ascii=False),
                         "任何决策模型配置字段都不能落盘这个 Key")


if __name__ == "__main__":
    unittest.main()
