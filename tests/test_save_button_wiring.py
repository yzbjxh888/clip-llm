"""Ticket 20 gate tests: the bottom-right OK button must persist B edits.

RED first: the B JSON edit box has no signal wiring, so typing a new category
and clicking only OK writes the *old* in-memory data to disk. These tests read
the real on-disk settings.json written by save_config (temporary XDG_CONFIG_HOME),
never just self.data. No network calls: only local config + dialog code paths.
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
from tests._json_editor_v10 import set_editor_prompt  # noqa: E402

CATEGORY = "英语句子"
DESCRIPTION = "英文句子，含主谓宾结构的完整句子"


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class TestOkButtonPersistsBEdit(unittest.TestCase):
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

    def test_ok_button_persists_b_edit_and_derived_chain_to_disk(self):
        b = json.loads(self.dlg.decision_model_json_edit.toPlainText())
        self.assertNotIn(CATEGORY, b, "测试夹具必须用一个出厂配置里没有的新类别")
        b[CATEGORY] = DESCRIPTION
        self.dlg.decision_model_json_edit.setPlainText(
            json.dumps(b, ensure_ascii=False))

        # 用户只点右下角「确定」：Ok → _accept → _apply；不碰小按钮。
        ok = self.dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        ok.click()

        self.assertEqual(self.dlg.result(), QDialog.Accepted)
        disk = self._disk()  # 真实磁盘文件，不是内存 self.data

        # B 类别本身
        self.assertIn(CATEGORY, disk["decision_model_categories"],
                      "用户编辑的 B 类别没有写盘")
        self.assertEqual(disk["decision_model_categories"][CATEGORY], DESCRIPTION)

        # 约束链第 1→2 环：criteria(B) → 模板(route_prompts)
        self.assertIn(CATEGORY, disk["routes"])
        self.assertEqual(disk["routes"][CATEGORY]["label"], CATEGORY)
        self.assertIn(CATEGORY, disk["route_prompts"])
        self.assertTrue(str(disk["route_prompts"][CATEGORY]).strip())

        # 第 3 环：查询方式 intent，三处中文同名 + 复用主路由模板（空 override）
        intent = next((i for i in disk["intents"]
                       if isinstance(i, dict) and i.get("id") == CATEGORY), None)
        self.assertIsNotNone(intent, "B 派生 intent 没有写盘")
        self.assertEqual(intent["label"], CATEGORY)
        self.assertEqual(intent["primary_route"], CATEGORY)
        self.assertEqual(intent.get("prompt_override"), "")

        self.assertEqual(set(validate(Settings(disk))), set())


    def test_invalid_b_json_blocks_ok_and_writes_nothing(self):
        self.dlg.decision_model_json_edit.setPlainText("{ 这不是合法 JSON")

        ok = self.dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        ok.click()

        self.assertNotEqual(self.dlg.result(), QDialog.Accepted,
                            "解析错误时『确定』不得接受对话框")
        self.assertIn("⚠", self.dlg.status_hint.text(), "必须显示解析错误")
        self.assertFalse(config_mod.config_path().exists(),
                         "解析错误时绝不写盘")


    def test_route_label_edit_is_persisted_by_ok_button(self):
        # v9：routes JSON 编辑框已移除；Ok 仍需把草稿里的 routes 写盘。
        route_id = next(iter(self.dlg.data["routes"]))
        new_label = "用户改过的路由名XYZ"
        self.assertNotEqual(self.dlg.data["routes"][route_id]["label"], new_label)
        self.dlg.data["routes"][route_id]["label"] = new_label

        ok = self.dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        ok.click()

        disk = self._disk()
        self.assertEqual(disk["routes"][route_id]["label"], new_label,
                         "Ok 没把 routes 草稿写盘")

    def test_invalid_routes_json_is_rejected_without_writing_anything(self):
        # v9：UI 入口已移除，但数据层对非法 JSON 的拒绝契约必须保留。
        before = copy.deepcopy(self.dlg.data)
        out, errors = settings_mod.apply_routes_json(
            self.dlg.data, "{ 这不是合法 JSON")
        self.assertTrue(errors)
        self.assertEqual(out["routes"], before["routes"],
                         "非法 JSON 不得改动 routes")
        self.assertFalse(config_mod.config_path().exists(),
                         "非法 JSON 绝不写盘")


    def test_small_button_message_does_not_claim_disk_save(self):
        b = json.loads(self.dlg.decision_model_json_edit.toPlainText())
        b[CATEGORY] = DESCRIPTION
        self.dlg.decision_model_json_edit.setPlainText(
            json.dumps(b, ensure_ascii=False))

        self.dlg.decision_model_json_apply.click()

        hint = self.dlg.status_hint.text()
        self.assertIn("请点右下角『确定』保存", hint)
        self.assertNotIn("已保存", hint, "小按钮只改内存，不能声称已保存")


TEMPLATE_TEXT = "【Ticket20 四环专属模板】按汉字结构分层解释。"


class TestConstraintChainFourRings(unittest.TestCase):
    """端到端四环：criteria(B) → 模板 → 查询方式 → 解释请求读模板。"""

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

    def test_four_rings_survive_ok_button_to_disk_and_compose_prompt(self):
        # 小按钮：B → 派生模板 / 查询方式（内存）
        b = json.loads(self.dlg.decision_model_json_edit.toPlainText())
        b[CATEGORY] = DESCRIPTION
        self.dlg.decision_model_json_edit.setPlainText(
            json.dumps(b, ensure_ascii=False))
        self.dlg.decision_model_json_apply.click()

        # 用户在模板页把派生模板改成自己的文案（真实控件 textChanged → self.data）
        idx = self.dlg.route_combo.findData(CATEGORY)
        self.assertGreaterEqual(idx, 0)
        self.dlg.route_combo.setCurrentIndex(idx)
        # v10：编辑区是完整 JSON，走真实控件只改 prompt 字段
        set_editor_prompt(self.dlg, TEMPLATE_TEXT)
        self.assertEqual(self.dlg.data["route_prompts"][CATEGORY], TEMPLATE_TEXT)

        # 再只点右下角『确定』：两处编辑都要落盘
        ok = self.dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        ok.click()

        path = config_mod.config_path()
        self.assertTrue(path.exists())
        disk = json.loads(path.read_text(encoding="utf-8"))

        # 环 1：criteria(B)
        self.assertEqual(disk["decision_model_categories"][CATEGORY], DESCRIPTION)
        # 环 2：模板 route_prompts，且用户文案未被覆盖
        self.assertEqual(disk["route_prompts"][CATEGORY], TEMPLATE_TEXT)
        # 环 3：查询方式 intent 指向该路由、复用模板
        intent = next((i for i in disk["intents"]
                       if isinstance(i, dict) and i.get("id") == CATEGORY), None)
        self.assertIsNotNone(intent)
        self.assertEqual(intent["primary_route"], CATEGORY)
        self.assertEqual(intent.get("prompt_override"), "")
        self.assertEqual(disk["routes"][CATEGORY]["label"], CATEGORY)

        # 环 4：解释请求真的读到这份模板
        settings = Settings(disk)
        composed = compose_prompt(settings.intent(CATEGORY), settings).prompt
        self.assertTrue(composed.startswith(TEMPLATE_TEXT),
                        f"compose_prompt 没读到 route_prompts：{composed[:80]}")
        self.assertEqual(set(validate(settings)), set())


if __name__ == "__main__":
    unittest.main()
