"""Ticket 29 gate tests, adapted to ticket 30's one-box contract.

Ticket 30 replaces the two-editor merged block with one flat JSON editor, so the
two-box structure assertions from ticket 29 are rewritten to the new contract.
Everything ticket 29 guaranteed about behavior (derivation to disk, instructions
persisting, invalid JSON blocking OK, dirty gate) is preserved and re-gated here.
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox,  # noqa: E402
                              QLabel, QPlainTextEdit, QPushButton)
from clipllm02 import load_defaults  # noqa: E402
from clipllm02.studio import config as config_mod  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

CATEGORY = "票据29专属类别XYZ"
DESCRIPTION = "票据29专属判别描述：按笔画数分层解释"
INSTRUCTIONS = "票据29专属判别指令：只按能否直接解释输入判断，其余选通用解析。"


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _policy_widgets(dlg):
    page = dlg.policy_tab.widget()
    layout = page.layout()
    return [layout.itemAt(i).widget() for i in range(layout.count())
            if layout.itemAt(i).widget() is not None]


class TestMergedBlockStructure(unittest.TestCase):
    """The merged block stays first; it now owns exactly one editor."""

    def setUp(self):
        self.dlg = SettingsDialog(load_defaults())

    def tearDown(self):
        self.dlg.close()

    def test_first_policy_widget_is_merged_section_title(self):
        first = _policy_widgets(self.dlg)[0]
        self.assertIsInstance(first, QLabel, "拦截页第一个控件必须是区块标题")
        self.assertEqual(first.text(), "决策模型配置",
                         "拦截页顶部缺少总标题『决策模型配置』")
        self.assertEqual(first.objectName(), "section")

    def test_general_note_sits_at_top_before_editor(self):
        widgets = _policy_widgets(self.dlg)
        note = next((w for w in widgets if isinstance(w, QLabel)
                     and w.text().startswith("决策模型配置：")), None)
        self.assertIsNotNone(note, "拦截页缺少合并区块总说明")
        self.assertEqual(widgets.index(note), 1, "总说明必须紧贴总标题、位于区块最上方")
        self.assertLess(widgets.index(note),
                        widgets.index(self.dlg.decision_model_json_edit))

    def test_editor_and_apply_button_precede_rules_section(self):
        widgets = _policy_widgets(self.dlg)
        rules_idx = next(i for i, w in enumerate(widgets)
                         if isinstance(w, QLabel) and w.text() == "拦截规则")
        for name, widget in (("JSON 编辑框", self.dlg.decision_model_json_edit),
                             ("派生按钮", self.dlg.decision_model_json_apply)):
            self.assertIn(widget, widgets, f"拦截页顶层布局缺少{name}")
            self.assertLess(widgets.index(widget), rules_idx,
                            f"{name}必须位于『拦截规则』之前（合并区块置顶）")

    def test_no_duplicate_editors_or_apply_button(self):
        page = self.dlg.policy_tab.widget()
        self.assertEqual(
            len(page.findChildren(QPlainTextEdit, "decisionModelJsonEdit")), 1,
            "decisionModelJsonEdit 必须全页恰好一份")
        self.assertEqual(
            len(page.findChildren(QPlainTextEdit, "decisionModelInstructionsEdit")), 0,
            "一个框：不得再有独立 instructions 编辑框")
        apply_buttons = [b for b in page.findChildren(QPushButton)
                         if b.text() == "保存并派生模板"]
        self.assertEqual(len(apply_buttons), 1,
                         "『保存并派生模板』按钮必须全页恰好一个")


class TestTopBlockBehaviorRegression(unittest.TestCase):
    """Green-at-RED guards: ticket 29 behavior survives the one-box rewrite."""

    def setUp(self):
        self._old_xdg = os.environ.get("XDG_CONFIG_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        self._net = patch("urllib.request.urlopen",
                          side_effect=AssertionError("no real network"))
        self._net.start()
        self.dlg = SettingsDialog(load_defaults())

    def tearDown(self):
        self._net.stop()
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

    def _ok(self):
        self.dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok).click()

    def test_top_b_json_apply_button_derives_templates_to_disk(self):
        pack = json.loads(self.dlg.decision_model_json_edit.toPlainText())
        self.assertNotIn(CATEGORY, pack, "夹具类别必须不在出厂配置里")
        pack[CATEGORY] = DESCRIPTION
        self.dlg.decision_model_json_edit.setPlainText(
            json.dumps(pack, ensure_ascii=False))
        self.dlg.decision_model_json_apply.click()
        self.assertIn("请点右下角『确定』保存", self.dlg.status_hint.text())
        self._ok()
        self.assertEqual(self.dlg.result(), QDialog.Accepted)
        disk = self._disk()
        self.assertEqual(disk["decision_model_categories"][CATEGORY], DESCRIPTION)
        self.assertEqual(disk["routes"][CATEGORY]["label"], CATEGORY)
        self.assertTrue(str(disk["route_prompts"][CATEGORY]).strip())
        intent = next((i for i in disk["intents"]
                       if isinstance(i, dict) and i.get("id") == CATEGORY), None)
        self.assertIsNotNone(intent)
        self.assertEqual(intent["primary_route"], CATEGORY)

    def test_instructions_edit_persists_via_ok(self):
        self.assertNotEqual(
            self.dlg.data.get("decision_model_instructions"), INSTRUCTIONS)
        pack = json.loads(self.dlg.decision_model_json_edit.toPlainText())
        pack["_instructions"] = INSTRUCTIONS
        self.dlg.decision_model_json_edit.setPlainText(
            json.dumps(pack, ensure_ascii=False))
        self._ok()
        self.assertEqual(self._disk()["decision_model_instructions"], INSTRUCTIONS)

    def test_invalid_b_json_blocks_ok_and_writes_nothing(self):
        self.dlg.decision_model_json_edit.setPlainText("{ 这不是合法 JSON")
        self._ok()
        self.assertNotEqual(self.dlg.result(), QDialog.Accepted,
                            "解析错误时『确定』不得接受对话框")
        self.assertIn("⚠", self.dlg.status_hint.text())
        self.assertFalse(config_mod.config_path().exists(),
                         "解析错误时绝不写盘")

    def test_dirty_gate_does_not_overwrite_other_entry_point_change(self):
        cat = next(iter(self.dlg.data["decision_model_categories"]))
        self.assertNotEqual(
            self.dlg.data["decision_model_categories"][cat], DESCRIPTION)
        self.dlg.data["decision_model_categories"][cat] = DESCRIPTION
        self._ok()
        self.assertEqual(self.dlg.result(), QDialog.Accepted)
        self.assertEqual(
            self._disk()["decision_model_categories"][cat], DESCRIPTION,
            "未 dirty 的旧框文本不得覆盖其它入口的改动（dirty 门控）")


if __name__ == "__main__":
    unittest.main()
