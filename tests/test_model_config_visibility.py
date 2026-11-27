"""Ticket 22 gates: main-model fields must show the config actually in use.

RED first: the settings dialog reads only main_model_config.*, so a user whose
real values live in desktop_* sees three empty boxes. Tests drive the real
offscreen SettingsDialog and read the real settings.json written by save_config
under a temporary XDG_CONFIG_HOME. No network calls: local config only.
"""
import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox  # noqa: E402

from clipllm02 import load_defaults  # noqa: E402
from clipllm02.desktop import gateway as gateway_mod  # noqa: E402
from clipllm02.studio import config as config_mod  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

BACKUP_CONFIG = ("/home/zhang/.config/clip-llm02.bak-20261127-102543"
                 "/settings.json")
LEGACY = {
    "main_model_api_url": "desktop_api_url",
    "main_model_key": "desktop_api_key",
    "main_model_name": "desktop_model",
}
LEGACY_VALUES = {
    "desktop_api_url": "https://ticket22.example.invalid/v1/chat/completions",
    "desktop_api_key": "TICKET22_LEGACY_KEY_NOT_IN_FACTORY",
    "desktop_model": "ticket22-legacy-model",
}


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


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


class TestMainModelBackfill(_TempConfigHome):
    def test_dialog_backfills_main_fields_from_legacy_when_new_fields_empty(self):
        settings = load_defaults()
        for key, value in LEGACY_VALUES.items():
            settings.data[key] = value
        for new_key in LEGACY:
            self.assertEqual(settings.data["main_model_config"][new_key], "",
                             "夹具要求新字段为空，旧字段有值")

        self.dlg = SettingsDialog(settings)

        for new_key, old_key in LEGACY.items():
            with self.subTest(new_key=new_key):
                self.assertEqual(self.dlg._edits[new_key].text(),
                                 LEGACY_VALUES[old_key],
                                 "设置页必须显示运行时真正在用的旧字段配置")


class TestModelConfigMigration(_TempConfigHome):
    def test_load_config_migrates_backup_config_copy_idempotently(self):
        self.assertTrue(Path(BACKUP_CONFIG).is_file(), "备份配置必须仍在")
        original = json.loads(Path(BACKUP_CONFIG).read_text(encoding="utf-8"))
        for new_key in LEGACY:
            self.assertEqual(original["main_model_config"][new_key], "")
        for old_key in LEGACY_VALUES:
            self.assertTrue(original[old_key], "备份里的旧字段必须有真实用户值")
        path = config_mod.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        config_mod.atomic_write(path, original)

        first = config_mod.load_config()
        disk1 = self._disk()
        for new_key, old_key in LEGACY.items():
            with self.subTest(new_key=new_key):
                self.assertEqual(disk1["main_model_config"][new_key],
                                 original[old_key],
                                 "新字段空时必须从旧字段一次性回填并落盘")
                self.assertEqual(disk1[old_key], original[old_key],
                                 "旧字段的用户值不能被迁移改写")
        self.assertEqual(first.main_model_config, disk1["main_model_config"])

        snapshot = path.read_text(encoding="utf-8")
        second = config_mod.load_config()
        self.assertEqual(path.read_text(encoding="utf-8"), snapshot,
                         "第二次加载必须幂等，不再改盘")
        self.assertEqual(second.main_model_config, first.main_model_config)


class TestMainModelEndToEnd(_TempConfigHome):
    def test_ok_persists_edit_then_restart_and_runtime_uses_new_fields(self):
        settings = load_defaults()
        for key, value in LEGACY_VALUES.items():
            settings.data[key] = value
        self.dlg = SettingsDialog(settings)
        edited_url = "https://ticket22-edited.example.invalid/v1/chat/completions"
        self.dlg._edits["main_model_api_url"].setText(edited_url)

        ok = self.dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        ok.click()  # 真实信号/槽接线，不直接调用 _apply
        self.assertEqual(self.dlg.result(), QDialog.Accepted)
        disk = self._disk()
        main = disk["main_model_config"]
        self.assertEqual(main["main_model_api_url"], edited_url)
        self.assertEqual(main["main_model_key"],
                         LEGACY_VALUES["desktop_api_key"])
        self.assertEqual(main["main_model_name"],
                         LEGACY_VALUES["desktop_model"])

        # 重启：从磁盘构造新设置对象与新对话框
        restarted = config_mod.load_config()
        reboot_dlg = SettingsDialog(restarted)
        try:
            self.assertEqual(reboot_dlg._edits["main_model_api_url"].text(),
                             edited_url)
            self.assertEqual(reboot_dlg._edits["main_model_key"].text(),
                             LEGACY_VALUES["desktop_api_key"])
        finally:
            reboot_dlg.close()

        # 运行时只认新字段：旧字段再改也不该盖掉用户已保存的新值
        restarted.data["desktop_api_url"] = "https://legacy-must-not-win.invalid"
        cfg = gateway_mod.GatewayConfig.from_settings(restarted)
        self.assertEqual(cfg.api_url, edited_url)


if __name__ == "__main__":
    unittest.main()
