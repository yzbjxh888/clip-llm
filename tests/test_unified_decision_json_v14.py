"""Ticket 30 gate tests: one flat JSON editor for instructions + categories.

Baseline (757 passed): the 拦截 tab has two editors (decisionModelJsonEdit with
categories only, decisionModelInstructionsEdit with raw instructions) and the
v13 two-part note. Every structure gate below must fail for that named reason.

Four behavior guards are green at RED because the underlying save/derive/dirty
logic is deliberately unchanged: category derivation to disk, invalid JSON
blocking OK, non-string category blocking OK, and the dirty gate. They are
classified as regression guards and each is counter-proven by a temporary
production break before the ticket closes. urlopen is patched everywhere;
assertions read the real file written under a temporary XDG_CONFIG_HOME.
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
from clipllm02.desktop import gateway as gmod  # noqa: E402
from clipllm02.studio import config as config_mod  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

# Hand-derived from CHANGE-unified-decision-json-v14.md (not read from source).
DEFAULT_INSTRUCTIONS = "从候选类别中选择与输入最匹配的一个；无法确定时选 通用解析。"
CUSTOM_INSTRUCTIONS = "票据30专属指令：只按能否直接解释输入判类，其余一律选通用解析。"
CATEGORY = "票据30专属类别ABC"
DESCRIPTION = "票据30专属判别描述：按笔画数分层解释"
V13_NOTE_START = "决策模型配置：这里决定软件如何给内容分类。分两部分——"
V14_INSTR_SHORT_NOTE = (
    "告诉决策模型如何判别类别；留空时回退内置默认指令。"
    "只影响请求题，不写入提示词模板、不回写类别清单。")
GENERAL_NOTE_V14 = (
    '决策模型配置：这一个框管两件事。\n'
    '① 第一行 "_instructions" 管"怎么判"：改引号里的文字就能改判别指令。\n'
    '② 其余每行 "类别名": "判别描述" 管"有哪些类"：新增一行＝新增一个类别（模板页自动生成同名派生模板，使用默认通用结构化模板，可再改）；删掉一行＝连带删掉那个模板；改判别描述＝只改变决策模型按什么判断，不改模板文案。含「通用解析」兜底，别删。\n'
    '删掉 "_instructions" 这一行或留空 → 保存后回退内置默认指令，不会写死进配置。\n'
    '写错 JSON 格式只会提示、不会保存。改完点「保存并派生模板」，再点右下角「确定」才真正存盘。'
)


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _policy_widgets(dlg):
    page = dlg.policy_tab.widget()
    layout = page.layout()
    return [layout.itemAt(i).widget() for i in range(layout.count())
            if layout.itemAt(i).widget() is not None]


def _labels(dlg):
    return [w for w in _policy_widgets(dlg) if isinstance(w, QLabel)]


def _flat(dlg):
    return json.loads(dlg.decision_model_json_edit.toPlainText())


def _set_flat(dlg, pack):
    dlg.decision_model_json_edit.setPlainText(
        json.dumps(pack, ensure_ascii=False, indent=2))


def _pack(dlg, *, instructions=DEFAULT_INSTRUCTIONS, add=None, drop=()):
    """Build a complete flat payload with _instructions always present."""
    pack = {"_instructions": instructions}
    for key, value in (dlg.data.get("decision_model_categories") or {}).items():
        if key not in drop:
            pack[key] = value
    if add:
        pack.update(add)
    return pack


class TestUnifiedEditorStructure(unittest.TestCase):
    """RED gates: one editor, flat payload, v14 note."""

    def setUp(self):
        self.dlg = SettingsDialog(load_defaults())

    def tearDown(self):
        self.dlg.close()

    def test_exactly_one_json_editor_and_no_instructions_editor(self):
        page = self.dlg.policy_tab.widget()
        self.assertEqual(
            len(page.findChildren(QPlainTextEdit, "decisionModelJsonEdit")), 1,
            "决策模型配置必须只有一个 decisionModelJsonEdit")
        self.assertEqual(
            len(page.findChildren(QPlainTextEdit, "decisionModelInstructionsEdit")), 0,
            "decisionModelInstructionsEdit 必须已移除（一个框）")
        self.assertFalse(hasattr(self.dlg, "decision_model_instructions_edit"),
                         "对话框不应再暴露独立 instructions 编辑控件")

    def test_editor_shows_flat_json_with_default_instructions(self):
        pack = _flat(self.dlg)
        self.assertIsInstance(pack, dict, "编辑框内容必须是平铺 JSON 对象")
        self.assertIn("_instructions", pack, "平铺 JSON 缺少 _instructions 行")
        self.assertEqual(pack["_instructions"], DEFAULT_INSTRUCTIONS,
                         "_instructions 行必须显示内置默认（框非空可见）")
        self.assertEqual(next(iter(pack)), "_instructions",
                         "_instructions 必须是框里的第一行")
        for key, value in (load_defaults().data["decision_model_categories"]).items():
            self.assertIn(key, pack, f"平铺 JSON 缺少类别 {key}")
            self.assertEqual(pack[key], value)

    def test_general_note_is_verbatim_v14_text(self):
        texts = [w.text() for w in _labels(self.dlg)]
        self.assertIn(GENERAL_NOTE_V14, texts,
                      "总说明缺失或文字与 v14 第四节不逐字一致")

    def test_editor_sits_directly_after_general_note(self):
        widgets = _policy_widgets(self.dlg)
        note = next((w for w in widgets if isinstance(w, QLabel)
                     and w.text() == GENERAL_NOTE_V14), None)
        self.assertIsNotNone(note, "拦截页缺少 v14 逐字总说明")
        self.assertEqual(widgets.index(note) + 1,
                         widgets.index(self.dlg.decision_model_json_edit),
                         "唯一编辑框必须紧跟总说明（中间无第二个框的说明）")

    def test_legacy_two_box_copy_is_gone(self):
        texts = [w.text() for w in _labels(self.dlg)]
        self.assertFalse(any(V13_NOTE_START in t for t in texts),
                         "票据29 的两部分总说明必须被 v14 说明替换")
        self.assertNotIn(V14_INSTR_SHORT_NOTE, texts,
                         "独立 instructions 框的短说明必须随控件一起移除")
        apply_buttons = [b for b in self.dlg.policy_tab.widget().findChildren(QPushButton)
                         if b.text() == "保存并派生模板"]
        self.assertEqual(len(apply_buttons), 1, "派生按钮必须全页恰好一个")


class TestUnifiedEditorBehavior(unittest.TestCase):
    """Drive the real editor and real buttons; read the real persisted file."""

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

    # --- RED: new flat-split behavior ---------------------------------------
    def test_add_category_derives_four_rings_and_instructions_never_category(self):
        self.assertNotIn(CATEGORY, self.dlg.data["decision_model_categories"],
                         "夹具类别必须不在出厂配置里")
        _set_flat(self.dlg, _pack(self.dlg, add={CATEGORY: DESCRIPTION}))
        self.dlg.decision_model_json_apply.click()
        self.assertIn("请点右下角『确定』保存", self.dlg.status_hint.text())
        self._ok()
        self.assertEqual(self.dlg.result(), QDialog.Accepted)
        disk = self._disk()
        self.assertEqual(disk["decision_model_categories"][CATEGORY], DESCRIPTION)
        self.assertNotIn("_instructions", disk["decision_model_categories"],
                         "_instructions 绝不能被当成类别写进 B")
        self.assertEqual(disk["routes"][CATEGORY]["label"], CATEGORY)
        self.assertTrue(str(disk["route_prompts"][CATEGORY]).strip())
        intent = next((i for i in disk["intents"]
                       if isinstance(i, dict) and i.get("id") == CATEGORY), None)
        self.assertIsNotNone(intent)
        self.assertEqual(intent["primary_route"], CATEGORY)

    def test_changed_instructions_persist_and_reach_request_payload(self):
        self.assertNotIn(CUSTOM_INSTRUCTIONS,
                         str(self.dlg.data.get("decision_model_instructions") or ""))
        _set_flat(self.dlg, _pack(self.dlg, instructions=CUSTOM_INSTRUCTIONS))
        self.dlg.decision_model_json_apply.click()
        self._ok()
        self.assertEqual(self.dlg.result(), QDialog.Accepted)
        disk = self._disk()
        self.assertEqual(disk["decision_model_instructions"], CUSTOM_INSTRUCTIONS,
                         "改 _instructions 行必须写盘到 decision_model_instructions")
        payload = gmod.build_systemone_request(
            "construct", disk["decision_model_categories"], settings=disk)
        self.assertEqual(payload["questions"]["category"]["instructions"],
                         CUSTOM_INSTRUCTIONS)
        self.assertEqual(payload["questions"]["category"]["criteria"],
                         gmod.systemone_categories(disk["decision_model_categories"]))

    def test_removing_instructions_line_falls_back_and_refills_default(self):
        _set_flat(self.dlg, _pack(self.dlg, instructions=CUSTOM_INSTRUCTIONS))
        self.dlg.decision_model_json_apply.click()
        self._ok()
        self.assertEqual(self._disk()["decision_model_instructions"],
                         CUSTOM_INSTRUCTIONS)
        pack = _flat(self.dlg)
        self.assertIn("_instructions", pack)
        del pack["_instructions"]
        _set_flat(self.dlg, pack)
        self.dlg.decision_model_json_apply.click()
        self.assertIn("请点右下角『确定』保存", self.dlg.status_hint.text(),
                      "删掉 _instructions 行必须保存成功")
        self._ok()
        self.assertEqual(self.dlg.result(), QDialog.Accepted)
        self.assertEqual(self._disk()["decision_model_instructions"], "",
                         "删掉 _instructions 行后回退空（运行时用内置默认）")
        self.assertEqual(_flat(self.dlg)["_instructions"], DEFAULT_INSTRUCTIONS,
                         "回填的统一框必须重新显示内置默认指令")

    def test_ok_only_path_applies_unified_editor(self):
        _set_flat(self.dlg, _pack(self.dlg, instructions=CUSTOM_INSTRUCTIONS,
                                 add={CATEGORY: DESCRIPTION}))
        self._ok()  # dirty gate must drive the new apply path
        self.assertEqual(self.dlg.result(), QDialog.Accepted)
        disk = self._disk()
        self.assertEqual(disk["decision_model_instructions"], CUSTOM_INSTRUCTIONS)
        self.assertEqual(disk["decision_model_categories"][CATEGORY], DESCRIPTION)

    def test_delete_category_removes_derived_templates(self):
        _set_flat(self.dlg, _pack(self.dlg, add={CATEGORY: DESCRIPTION}))
        self.dlg.decision_model_json_apply.click()
        self.assertIn(CATEGORY, self.dlg.data["routes"])
        pack = _flat(self.dlg)
        del pack[CATEGORY]
        _set_flat(self.dlg, pack)
        self.dlg.decision_model_json_apply.click()
        self._ok()
        disk = self._disk()
        self.assertNotIn(CATEGORY, disk["decision_model_categories"])
        self.assertNotIn(CATEGORY, disk["routes"], "删类别须连带删派生路由")
        self.assertNotIn(CATEGORY, disk["route_prompts"], "删类别须连带删派生模板")
        self.assertFalse(any(isinstance(i, dict) and i.get("id") == CATEGORY
                             for i in disk["intents"]), "删类别须连带删查询方式")
        self.assertNotIn("_instructions", disk["decision_model_categories"])

    # --- GREEN-at-RED regression guards (unchanged behavior) ----------------
    def test_only_category_change_does_not_write_default_instructions(self):
        _set_flat(self.dlg, _pack(self.dlg, instructions=DEFAULT_INSTRUCTIONS,
                                 add={CATEGORY: DESCRIPTION}))
        self.dlg.decision_model_json_apply.click()
        self._ok()
        disk = self._disk()
        self.assertIn("decision_model_instructions", disk)
        self.assertEqual(disk["decision_model_instructions"], "",
                         "只改类别不得把内置默认写死进配置")
        self.assertEqual(disk["decision_model_categories"][CATEGORY], DESCRIPTION)

    def test_invalid_json_blocks_ok_and_writes_nothing(self):
        self.dlg.decision_model_json_edit.setPlainText("{ 这不是合法 JSON")
        self._ok()
        self.assertNotEqual(self.dlg.result(), QDialog.Accepted,
                            "解析错误时『确定』不得接受对话框")
        self.assertIn("⚠", self.dlg.status_hint.text())
        self.assertFalse(config_mod.config_path().exists(), "解析错误时绝不写盘")

    def test_category_value_non_string_blocks_ok_and_writes_nothing(self):
        pack = _pack(self.dlg)
        pack[CATEGORY] = 12345
        _set_flat(self.dlg, pack)
        self._ok()
        self.assertNotEqual(self.dlg.result(), QDialog.Accepted,
                            "类别值非字符串时『确定』不得接受对话框")
        self.assertIn("⚠", self.dlg.status_hint.text())
        self.assertFalse(config_mod.config_path().exists(), "校验失败时绝不写盘")

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
