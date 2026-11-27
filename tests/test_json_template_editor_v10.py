"""Ticket 26 gates (v10): full-JSON template editor + id alignment + new-label fill.

RED first. Every expected literal is hand-derived from CHANGE-json-template-editor-v10.md
and spec/builtin_settings.json (the authority), not from the implementation under test.
Production functions are fetched with getattr so a missing function is a named
assertion failure instead of an ImportError. No real network calls; Qt runs offscreen.
The real user config is never touched (tests use a temporary XDG_CONFIG_HOME).
"""
import copy
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox,  # noqa: E402
                               QLineEdit, QPlainTextEdit)

from clipllm02 import Settings, load_defaults  # noqa: E402
from clipllm02 import settings as settings_mod  # noqa: E402
from clipllm02.desktop import gateway as gateway_mod  # noqa: E402
from clipllm02.prompts import compose_prompt  # noqa: E402
from clipllm02.studio import config as config_mod  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

# unicode-escaped so no source file reintroduces the retired placeholder literal.
POLLUTED = "\u8fd9\u662f\u4ec0\u4e48?"

# v10 §3.1 / CHANGE-generic-template-v9.md §5 — hand-derived, distinctive.
EN_FIELDS = ["title", "phonetic", "definitions", "etymology_roots",
             "phrases", "examples", "memory_tip"]
EN_SECTION_KEYS = ["phonetic", "definitions", "etymology_roots", "phrases",
                   "examples", "memory_tip"]
GENERIC_FIELDS = ["title", "what_is_it", "core_meaning", "structure", "examples",
                  "background", "perspectives", "caveats"]
GENERIC_SECTION_KEYS = ["what_is_it", "core_meaning", "structure", "examples",
                        "background", "perspectives", "caveats"]
DISTINCT_SECTION = "【v10票26用户手改的音标段落·音标重定义XYZ】不要照抄出厂文案。"
DISTINCT_PROMPT = "【v10票26改后指令】只按本段输出，忽略出厂那套词汇解释。"
LABEL_FROM_EDIT = "v10显示名由编辑框改XYZ"
LABEL_FROM_JSON = "v10显示名由JSON改XYZ"
BAD_ID = "票26里B没有的类别XYZ"
EN_WORD = "英语单词"


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _serialize():
    return getattr(settings_mod, "route_template_json", None)


def _apply():
    return getattr(settings_mod, "apply_route_template_json", None)


def _restore_derived():
    return getattr(settings_mod, "restore_derived_route_templates", None)


def _editor_pack(dlg):
    """Parse the editor as JSON; return None on failure so RED is a named assert."""
    try:
        return json.loads(dlg.route_prompt.toPlainText())
    except (ValueError, TypeError):
        return None


def _set_editor(dlg, **updates):
    pack = _editor_pack(dlg)
    if pack is None:
        raise AssertionError("编辑区当前不是合法 JSON，无法构造编辑输入")
    pack.update(updates)
    dlg.route_prompt.setPlainText(json.dumps(pack, ensure_ascii=False, indent=2))


def _select(dlg, rid):
    idx = dlg.route_combo.findData(rid)
    assert idx >= 0, f"路由 {rid} 不在下拉里"
    dlg.route_combo.setCurrentIndex(idx)
    assert dlg.route_combo.currentData() == rid


def _ok(dlg):
    return dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)


def _legacy_id_data():
    """真实老配置形状：key 仍是 en_word，id 字段也是 en_word，intents 全引用它。"""
    data = load_defaults().as_dict()
    original = copy.deepcopy(data["routes"].pop(EN_WORD))
    original["id"] = "en_word"
    original["label"] = EN_WORD
    data["routes"]["en_word"] = original
    prompts = data.setdefault("route_prompts", {})
    prompts.pop(EN_WORD, None)
    prompts["en_word"] = "用户写过的英语单词文案"
    for intent in data["intents"]:
        for field in ("primary_route", "followup_route"):
            if intent.get(field) == EN_WORD:
                intent[field] = "en_word"
        for inc in intent.get("includes") or []:
            if inc.get("route_id") == EN_WORD:
                inc["route_id"] = "en_word"
    return data


class TestDataLayerGates(unittest.TestCase):
    def setUp(self):
        self.data = load_defaults().as_dict()
        self.data.setdefault("route_prompts", {})

    def test_serializer_shows_formatted_full_json(self):
        fn = _serialize()
        self.assertIsNotNone(fn, "缺少 route_template_json 序列化函数")
        text = fn(self.data, EN_WORD)
        self.assertIsInstance(text, str)
        pack = json.loads(text)
        self.assertEqual(list(pack.get("fields") or []), EN_FIELDS)
        self.assertEqual(list(pack.get("sections") or {}), EN_SECTION_KEYS)
        self.assertIn("音标", pack["sections"]["phonetic"])
        for key in ("id", "label", "enabled", "query_type", "kind",
                    "prompt", "sections", "fields"):
            self.assertIn(key, pack, f"完整 JSON 缺字段 {key}")
        self.assertIn('\n  "id":', text, "必须 indent=2 格式化")
        self.assertIn(EN_WORD, text, "必须 ensure_ascii=False，中文不转义")

    def test_apply_edits_sections_and_prompt(self):
        fn = _apply()
        self.assertIsNotNone(fn, "缺少 apply_route_template_json 函数")
        before = copy.deepcopy(self.data)
        pack = copy.deepcopy(before["routes"][EN_WORD])
        pack["sections"]["phonetic"] = DISTINCT_SECTION
        pack["prompt"] = DISTINCT_PROMPT
        out, errors = fn(self.data, EN_WORD,
                         json.dumps(pack, ensure_ascii=False))
        self.assertEqual(errors, [], f"合法编辑不该报错：{errors}")
        self.assertEqual(out["routes"][EN_WORD]["sections"]["phonetic"],
                         DISTINCT_SECTION)
        self.assertEqual(out["routes"][EN_WORD]["prompt"], DISTINCT_PROMPT)
        self.assertEqual(out["route_prompts"][EN_WORD], DISTINCT_PROMPT)
        self.assertEqual(self.data, before, "绝不能就地改动传入的配置")

    def test_invalid_json_returns_error_and_unchanged_data(self):
        fn = _apply()
        self.assertIsNotNone(fn)
        before = copy.deepcopy(self.data)
        out, errors = fn(self.data, EN_WORD, "{这不是 JSON")
        self.assertTrue(errors, "非法 JSON 必须报错")
        self.assertIn("JSON", errors[0])
        self.assertEqual(out, before, "非法 JSON 绝不落盘/改内存")

    def test_bad_structure_returns_error_and_unchanged_data(self):
        fn = _apply()
        self.assertIsNotNone(fn)
        base = copy.deepcopy(self.data["routes"][EN_WORD])
        missing = copy.deepcopy(base)
        missing.pop("fields")
        wrong_type = copy.deepcopy(base)
        wrong_type["sections"] = []
        for name, payload in (("顶层不是对象", []),
                              ("缺必要字段", missing),
                              ("字段值类型不对", wrong_type)):
            with self.subTest(name):
                before = copy.deepcopy(self.data)
                out, errors = fn(self.data, EN_WORD,
                                 json.dumps(payload, ensure_ascii=False))
                self.assertTrue(errors, f"{name} 必须报错")
                self.assertEqual(out, before)

    def test_id_outside_b_is_rejected_without_change(self):
        fn = _apply()
        self.assertIsNotNone(fn)
        self.assertNotIn(BAD_ID, self.data["decision_model_categories"])
        pack = copy.deepcopy(self.data["routes"][EN_WORD])
        pack["id"] = BAD_ID
        before = copy.deepcopy(self.data)
        out, errors = fn(self.data, EN_WORD,
                         json.dumps(pack, ensure_ascii=False))
        self.assertTrue(errors, "改成 B 里没有的 id 必须报错")
        self.assertIn("decision_model_categories", "".join(errors))
        self.assertEqual(out, before)

    def test_stale_id_field_left_alone_is_not_treated_as_id_change(self):
        """真实用户配置：key=英语单词 但 id 字段=en_word，只改 sections 应放行。"""
        fn = _apply()
        self.assertIsNotNone(fn)
        data = _legacy_id_data()
        # key 已迁移为中文，但 id 字段仍旧英文（真实用户配置形状）
        data["routes"][EN_WORD] = data["routes"].pop("en_word")
        data["route_prompts"][EN_WORD] = data["route_prompts"].pop("en_word")
        pack = copy.deepcopy(data["routes"][EN_WORD])
        pack["sections"]["phonetic"] = DISTINCT_SECTION
        out, errors = fn(data, EN_WORD, json.dumps(pack, ensure_ascii=False))
        self.assertEqual(errors, [], f"未改 id 不该被拦：{errors}")
        self.assertEqual(out["routes"][EN_WORD]["id"], "en_word")
        self.assertEqual(out["routes"][EN_WORD]["sections"]["phonetic"],
                         DISTINCT_SECTION)

    def test_id_rename_syncs_keys_and_every_intent_reference(self):
        fn = _apply()
        self.assertIsNotNone(fn)
        data = _legacy_id_data()
        original_data = copy.deepcopy(data)
        self.assertIn(EN_WORD, data["decision_model_categories"])
        self.assertIn("en_word", data["routes"])
        pack = copy.deepcopy(data["routes"]["en_word"])
        pack["id"] = EN_WORD
        pack["label"] = EN_WORD
        out, errors = fn(data, "en_word", json.dumps(pack, ensure_ascii=False))
        self.assertEqual(errors, [], f"合法 id 改名不该报错：{errors}")

        self.assertNotIn("en_word", out["routes"])
        self.assertNotIn("en_word", out["route_prompts"])
        self.assertIn(EN_WORD, out["routes"])
        self.assertIn(EN_WORD, out["route_prompts"])
        self.assertEqual(out["routes"][EN_WORD]["id"], EN_WORD)
        self.assertEqual(out["routes"][EN_WORD]["label"], EN_WORD)
        # v10：编辑区 JSON 的 prompt 是权威值，写回 route_prompts（compose_prompt 优先级）
        self.assertEqual(out["route_prompts"][EN_WORD], pack["prompt"])
        self.assertEqual(data, original_data, "原配置绝不能被就地改动")

        for intent in out["intents"]:
            for field in ("primary_route", "followup_route"):
                self.assertNotEqual(intent.get(field), "en_word", intent["id"])
            for inc in intent.get("includes") or []:
                self.assertNotEqual(inc.get("route_id"), "en_word", intent["id"])
        # 无悬空引用：primary_route 与 includes.route_id 必须指向存在的路由。
        # followup_route 有的是 intent id（如 auto），不能按路由 id 校验。
        for intent in out["intents"]:
            rid = intent.get("primary_route")
            if rid:
                self.assertIn(rid, out["routes"], f"{intent['id']}.primary_route")
            for inc in intent.get("includes") or []:
                if inc.get("route_id"):
                    self.assertIn(inc["route_id"], out["routes"])

    def test_id_rename_rejects_existing_target_route(self):
        fn = _apply()
        self.assertIsNotNone(fn)
        data = _legacy_id_data()
        pack = copy.deepcopy(data["routes"]["en_word"])
        pack["id"] = "英语短语"      # 在 B 里已存在，且 routes 里也已有
        out, errors = fn(data, "en_word", json.dumps(pack, ensure_ascii=False))
        self.assertTrue(errors, "目标 id 已被占用必须报错")
        self.assertEqual(out, data)


class TestDerivedRouteMigrationGates(unittest.TestCase):
    def test_derived_route_gets_generic_template_and_keeps_user_text(self):
        fn = _restore_derived()
        self.assertIsNotNone(fn, "缺少 restore_derived_route_templates 迁移函数")
        data = load_defaults().as_dict()
        data["routes"]["英语句子"] = {
            "id": "英语句子", "label": "英语句子", "enabled": True,
            "query_type": "free", "kind": "route", "prompt": POLLUTED,
            "sections": {}, "fields": [],
            "derived_template_source": "decision_model",
        }
        data.setdefault("route_prompts", {})["英语句子"] = "翻译成中文。"
        self.assertTrue(fn(data), "被污染的新增路由必须报告已迁移")
        pack = data["routes"]["英语句子"]
        self.assertEqual(list(pack["fields"]), GENERIC_FIELDS)
        self.assertEqual(list(pack["sections"]), GENERIC_SECTION_KEYS)
        self.assertNotEqual(pack["prompt"], POLLUTED)
        self.assertIn("通用解析专家", pack["prompt"])
        # 用户改过的文案绝不覆盖
        self.assertEqual(data["route_prompts"]["英语句子"], "翻译成中文。")
        self.assertFalse(fn(data), "重复迁移必须幂等")

    def test_factory_routes_are_untouched_by_derived_migration(self):
        fn = _restore_derived()
        self.assertIsNotNone(fn)
        data = load_defaults().as_dict()
        before = copy.deepcopy(data["routes"][EN_WORD])
        self.assertFalse(fn(data), "没有待补的新增路由时不应报告改动")
        self.assertEqual(data["routes"][EN_WORD], before)


class _TempConfigHome(unittest.TestCase):
    def setUp(self):
        self._old_xdg = os.environ.get("XDG_CONFIG_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        self.real_config = Path.home() / ".config" / "clip-llm02" / "settings.json"
        self.real_mtime = (self.real_config.stat().st_mtime_ns
                           if self.real_config.exists() else None)

    def tearDown(self):
        self._tmp.cleanup()
        if self._old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._old_xdg

    def _disk(self):
        path = config_mod.config_path()
        self.assertTrue(path.exists(), f"配置未写盘：{path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _assert_real_config_untouched(self):
        mtime = (self.real_config.stat().st_mtime_ns
                 if self.real_config.exists() else None)
        self.assertEqual(mtime, self.real_mtime, "真实用户配置被测试改动了")

    def _no_network(self):
        return patch.object(gateway_mod.urllib.request, "urlopen",
                            side_effect=AssertionError("票据26不得发起真实网络调用"))


class TestMigrationPersists(_TempConfigHome):
    def test_config_version_10_to_13_migration_writes_and_is_idempotent(self):
        data = load_defaults().as_dict()
        data["routes"]["英语句子"] = {
            "id": "英语句子", "label": "英语句子", "enabled": True,
            "query_type": "free", "kind": "route", "prompt": POLLUTED,
            "sections": {}, "fields": [],
            "derived_template_source": "decision_model",
        }
        data.setdefault("route_prompts", {})["英语句子"] = "翻译成中文。"
        data["config_version"] = 10
        path = config_mod.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")

        with self._no_network():
            config_mod.load_config()
            disk = self._disk()
            self.assertEqual(disk["config_version"], 13, "迁移必须升到当前版本 13")
            pack = disk["routes"]["英语句子"]
            self.assertEqual(list(pack["fields"]), GENERIC_FIELDS)
            self.assertEqual(list(pack["sections"]), GENERIC_SECTION_KEYS)
            self.assertNotEqual(pack["prompt"], POLLUTED)
            self.assertEqual(disk["route_prompts"]["英语句子"], "翻译成中文。")
            # 出厂路由各归各位（票25）保持不动
            self.assertEqual(list(disk["routes"][EN_WORD]["fields"]), EN_FIELDS)

            config_mod.load_config()   # 重启：不得再漂移
            self.assertEqual(self._disk(), disk)

        self._assert_real_config_untouched()


class TestFullJsonEditorEndToEnd(_TempConfigHome):
    def test_sections_edit_goes_through_ok_to_disk_restart_and_compose(self):
        with self._no_network():
            dlg = SettingsDialog(load_defaults())
            try:
                _select(dlg, EN_WORD)
                pack = _editor_pack(dlg)
                self.assertIsNotNone(pack, "编辑区未显示完整 JSON")
                self.assertEqual(list(pack.get("fields") or []), EN_FIELDS)
                self.assertIn("phonetic", pack.get("sections") or {})
                self.assertIn("音标", pack["sections"]["phonetic"])

                _set_editor(dlg, sections=dict(
                    pack["sections"], phonetic=DISTINCT_SECTION),
                    prompt=DISTINCT_PROMPT)
                self.assertEqual(
                    dlg.data["routes"][EN_WORD]["sections"]["phonetic"],
                    DISTINCT_SECTION)
                self.assertEqual(dlg.data["route_prompts"][EN_WORD],
                                 DISTINCT_PROMPT)
                self.assertEqual(_ok(dlg).isEnabled(), True)
                _ok(dlg).click()
                self.assertEqual(dlg.result(), QDialog.Accepted)
            finally:
                dlg.close()

            disk = self._disk()
            self.assertEqual(
                disk["routes"][EN_WORD]["sections"]["phonetic"],
                DISTINCT_SECTION)
            self.assertEqual(disk["route_prompts"][EN_WORD], DISTINCT_PROMPT)

            restarted = config_mod.load_config()
            intent = next(i for i in restarted.intents
                          if i.primary_route == EN_WORD)
            probe = replace(intent, includes=[{
                "route_id": EN_WORD, "as": "音标", "sections": ["phonetic"]}])
            composed = compose_prompt(probe, restarted).prompt
            self.assertIn(DISTINCT_PROMPT, composed, "compose_prompt 没拿到改后指令")
            self.assertIn(DISTINCT_SECTION, composed, "compose_prompt 没拿到改后片段")

            reboot = SettingsDialog(restarted)
            try:
                _select(reboot, EN_WORD)
                again = _editor_pack(reboot)
                self.assertIsNotNone(again)
                self.assertEqual(again["sections"]["phonetic"], DISTINCT_SECTION)
            finally:
                reboot.close()

        self._assert_real_config_untouched()

    def test_invalid_json_and_unknown_id_block_ok_without_disk_write(self):
        with self._no_network():
            dlg = SettingsDialog(load_defaults())
            try:
                _select(dlg, EN_WORD)
                dlg.route_prompt.setPlainText("{这不是 JSON")
                _ok(dlg).click()
                self.assertNotEqual(dlg.result(), QDialog.Accepted,
                                    "非法 JSON 不得接受对话框")
                self.assertIn("JSON", dlg.status_hint.text())
                self.assertFalse(config_mod.config_path().exists(),
                                 "非法 JSON 绝不写盘")

                pack = copy.deepcopy(load_defaults().routes[EN_WORD])
                pack["id"] = BAD_ID
                dlg.route_prompt.setPlainText(
                    json.dumps(pack, ensure_ascii=False, indent=2))
                _ok(dlg).click()
                self.assertNotEqual(dlg.result(), QDialog.Accepted,
                                    "B 里没有的 id 不得接受对话框")
                self.assertFalse(config_mod.config_path().exists())
                self.assertIn("decision_model_categories", dlg.status_hint.text())
            finally:
                dlg.close()

        self._assert_real_config_untouched()

    def test_id_rename_via_editor_aligns_three_places_on_disk(self):
        with self._no_network():
            dlg = SettingsDialog(Settings(_legacy_id_data()))
            try:
                _select(dlg, "en_word")
                pack = _editor_pack(dlg)
                self.assertIsNotNone(pack)
                self.assertEqual(pack["id"], "en_word")
                _set_editor(dlg, id=EN_WORD, label=EN_WORD)
                self.assertNotIn("en_word", dlg.data["routes"])
                self.assertNotIn("en_word", dlg.data["route_prompts"])
                _ok(dlg).click()
                self.assertEqual(dlg.result(), QDialog.Accepted)
            finally:
                dlg.close()

            disk = self._disk()
            self.assertNotIn("en_word", disk["routes"])
            self.assertNotIn("en_word", disk["route_prompts"])
            self.assertEqual(disk["routes"][EN_WORD]["id"], EN_WORD)
            self.assertEqual(disk["routes"][EN_WORD]["label"], EN_WORD)
            self.assertIn(EN_WORD, disk["decision_model_categories"])
            for intent in disk["intents"]:
                self.assertNotEqual(intent.get("primary_route"), "en_word")
                self.assertNotEqual(intent.get("followup_route"), "en_word")
                for inc in intent.get("includes") or []:
                    self.assertNotEqual(inc.get("route_id"), "en_word")

        self._assert_real_config_untouched()

    def test_label_edit_and_json_label_sync_both_ways(self):
        with self._no_network():
            dlg = SettingsDialog(load_defaults())
            try:
                _select(dlg, EN_WORD)
                label_edit = dlg.findChildren(QLineEdit)
                self.assertIn(dlg.route_label_edit, label_edit)
                dlg.route_label_edit.setText(LABEL_FROM_EDIT)
                pack = _editor_pack(dlg)
                self.assertIsNotNone(pack)
                self.assertEqual(pack["label"], LABEL_FROM_EDIT,
                                 "显示名改动必须同步进 JSON")

                _set_editor(dlg, label=LABEL_FROM_JSON)
                self.assertEqual(dlg.route_label_edit.text(), LABEL_FROM_JSON,
                                 "JSON label 改动必须同步回显示名")
            finally:
                dlg.close()

        self._assert_real_config_untouched()


if __name__ == "__main__":
    unittest.main()
