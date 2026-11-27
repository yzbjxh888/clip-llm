"""Ticket 21 gate tests: template-page edits must survive the bottom-right Ok.

v9 ticket 25: the stale routes JSON box that caused the rollback was removed by
the user's decision; the real-control template edit gates and the surviving
data-layer one-way apply gate remain. These tests drive the real offscreen
SettingsDialog and the real Ok button, then read the on-disk settings.json
written by the real save_config (temporary XDG_CONFIG_HOME). No network calls.
"""
import copy
import json
import os
import tempfile
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox  # noqa: E402

from clipllm02 import Settings, compose_prompt, load_defaults, validate  # noqa: E402
from clipllm02 import settings as settings_mod  # noqa: E402
from clipllm02.studio import config as config_mod  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402
from tests._json_editor_v10 import editor_json, set_editor_prompt  # noqa: E402

ORIGINAL_ROUTE = "英语单词"
DERIVED_ROUTE = "英语句子"
ORIGINAL_TEXT = "【票21 原有模板】按词根拆解法讲解单词。"
DERIVED_TEXT = "【票21 派生模板】按句子成分逐层讲解。"
NEW_ROUTE_LABEL = "【票21 JSON框改过的路由名】"


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _DialogCase(unittest.TestCase):
    def setUp(self):
        self._old_xdg = os.environ.get("XDG_CONFIG_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        self.dlg = SettingsDialog(load_defaults())

    def tearDown(self):
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

    @staticmethod
    def _select_route(dlg, rid):
        idx = dlg.route_combo.findData(rid)
        assert idx >= 0, f"路由 {rid} 不在下拉里"
        dlg.route_combo.setCurrentIndex(idx)
        assert dlg.route_combo.currentData() == rid

    def _edit_template(self, rid, text):
        self._select_route(self.dlg, rid)
        # v10：编辑区是完整 JSON，走真实控件 textChanged → _save_route_prompt
        # 只改 JSON 里的 prompt 字段；其余字段原样保留。
        set_editor_prompt(self.dlg, text)
        self.assertEqual(self.dlg.data["route_prompts"][rid], text)

    def _derive_route(self):
        b = json.loads(self.dlg.decision_model_json_edit.toPlainText())
        self.assertNotIn(DERIVED_ROUTE, b, "夹具类别必须是出厂配置里没有的")
        b[DERIVED_ROUTE] = "英文句子，含主谓宾结构的完整句子"
        self.dlg.decision_model_json_edit.setPlainText(
            json.dumps(b, ensure_ascii=False))
        self.dlg.decision_model_json_apply.click()
        self._select_route(self.dlg, DERIVED_ROUTE)

    def _click_ok(self):
        ok = self.dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        ok.click()
        self.assertEqual(self.dlg.result(), QDialog.Accepted)


class TestTemplateEditSurvivesOkButton(_DialogCase):
    def test_original_route_template_edit_survives_ok_button(self):
        self._edit_template(ORIGINAL_ROUTE, ORIGINAL_TEXT)
        self._click_ok()

        disk = self._disk()
        self.assertIn("route_prompts", disk)
        self.assertIn(ORIGINAL_ROUTE, disk["route_prompts"])
        self.assertEqual(disk["route_prompts"][ORIGINAL_ROUTE], ORIGINAL_TEXT,
                         "模板页编辑被旧 routes JSON 框回滚了")

    def test_derived_route_template_edit_survives_ok_button(self):
        self._derive_route()
        self._edit_template(DERIVED_ROUTE, DERIVED_TEXT)
        self._click_ok()

        disk = self._disk()
        self.assertIn(DERIVED_ROUTE, disk["route_prompts"])
        self.assertEqual(disk["route_prompts"][DERIVED_ROUTE], DERIVED_TEXT,
                         "派生模板页编辑被旧 routes JSON 框回滚了")

    def test_restart_reloads_custom_template_and_constraint_chain(self):
        self._derive_route()
        self._edit_template(DERIVED_ROUTE, DERIVED_TEXT)
        self._click_ok()

        disk = self._disk()
        self.assertEqual(disk["route_prompts"][DERIVED_ROUTE], DERIVED_TEXT)

        # 重启：用磁盘内容构造新 dialog，模板页必须仍显示自定义文案
        restarted = SettingsDialog(Settings(disk))
        try:
            self._select_route(restarted, DERIVED_ROUTE)
            self.assertEqual(editor_json(restarted)["prompt"], DERIVED_TEXT,
                             "重启后模板页没有加载磁盘上的自定义文案")
        finally:
            restarted.close()

        settings = Settings(disk)
        composed = compose_prompt(settings.intent(DERIVED_ROUTE), settings).prompt
        self.assertTrue(composed.startswith(DERIVED_TEXT),
                        f"约束链末环没读到自定义模板：{composed[:80]}")
        self.assertEqual(set(validate(settings)), set())

    def test_routes_data_entry_still_applies_without_ui_box(self):
        # v9：旧 routes JSON 框已移除；数据层单向 apply 契约不回退。
        before = copy.deepcopy(self.dlg.data)
        routes = copy.deepcopy(before["routes"])
        routes[ORIGINAL_ROUTE]["label"] = NEW_ROUTE_LABEL
        text = json.dumps({"routes": routes,
                           "route_prompts": before.get("route_prompts") or {}},
                          ensure_ascii=False)
        out, errors = settings_mod.apply_routes_json(before, text)
        self.assertEqual(errors, [])
        self.assertEqual(out["routes"][ORIGINAL_ROUTE]["label"], NEW_ROUTE_LABEL)
        self.assertEqual(out["route_prompts"], before["route_prompts"],
                         "apply 不得回滚用户改过的模板")


if __name__ == "__main__":
    unittest.main()
