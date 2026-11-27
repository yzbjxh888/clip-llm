"""Ticket 32 gate tests: intents delete tombstone + "自定义" factory deletion.

RED first. Expected values are hand-derived from
.factory/00-decisions/CHANGE-intent-delete-tombstone-v16.md and the shipped
spec, never from the code under test.

Baseline (793 passed): CONFIG_VERSION is 12; builtin_settings.json has no
removed_intents; _delete_intent only lets custom=True intents be deleted and
writes no tombstone; settings_from_data applies only policy.removed_rules.
Every behavior gate below must fail for that named reason. Gates that are green
at RED are explicit regression guards; each is counter-proven by a temporary
production break before the ticket closes.

All dialogs are real offscreen SettingsDialog instances driven through their
real tree selection and real delete button. urlopen is patched everywhere:
these tests must never make a real network call. The real user config is never
touched (temp XDG_CONFIG_HOME + mtime assertions).
"""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import load_defaults  # noqa: E402
from clipllm02.models import intent_prompt_source  # noqa: E402
from clipllm02.settings import Settings, apply_decision_model_json  # noqa: E402
from clipllm02.studio import config as config_mod  # noqa: E402
from clipllm02.studio.config import (  # noqa: E402
    save_config, settings_from_data)
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec" / "builtin_settings.json"

REMOVED_KEY = "removed_intents"
EXPECTED_VERSION = 13
FACTORY_OVERRIDE_ID = "en.read"      # 30 条显示"自定义"的出厂意图之一
FACTORY_STAY_ID = "en.synonyms"      # 同组、不删的出厂意图
FACTORY_TEXT_ID = "zh.legacy"        # 纯"文本模板"出厂意图（prompt_override 为空）
CUSTOM_ID = "custom.9901"            # 用户自定义意图（custom=True）
BLOCK_HINT = "要删除请先给它填专属提示词"
FACTORY_HINT = "这是出厂意图"


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _NoNetwork:
    def setUp(self):
        self._net = patch("urllib.request.urlopen",
                          side_effect=AssertionError("no real network"))
        self._net.start()

    def tearDown(self):
        self._net.stop()


def _factory_data():
    return load_defaults().as_dict()


def _original_override_intent(intent_id):
    return copy.deepcopy(next(i for i in _factory_data()["intents"]
                              if i["id"] == intent_id))


def _custom_intent():
    return {
        "id": CUSTOM_ID, "label": "票32用户意图", "hint": "",
        "domains": ["*"], "modalities": ["text"], "applicable": None,
        "primary_route": "通用解析", "prompt_override": "票32用户专属提示词",
        "includes": [], "default_format": "static",
        "supported_formats": ["static"], "order": 200, "enabled": True,
        "free_input_only": False, "custom": True,
        "conflict_policy": "primary_wins", "token_budget": 1400,
        "prompt_version": "2.0.0", "schema_version": "1.0.0",
    }


def _find_tree_item(tree, intent_id):
    def walk(parent):
        for i in range(parent.childCount()):
            child = parent.child(i)
            if child.data(0, Qt.UserRole) == ("intent", intent_id):
                return child
            found = walk(child)
            if found is not None:
                return found
        return None

    for i in range(tree.topLevelItemCount()):
        found = walk(tree.topLevelItem(i))
        if found is not None:
            return found
    return None


class _DialogMixin(_NoNetwork, unittest.TestCase):
    """Drive the real offscreen dialog through real tree selection."""

    def setUp(self):
        super().setUp()
        self.dlg = SettingsDialog(Settings(_factory_data()))

    def tearDown(self):
        self.dlg.close()
        super().tearDown()

    def select(self, intent_id):
        item = _find_tree_item(self.dlg.prompt_tree, intent_id)
        self.assertIsNotNone(item, f"领域树里找不到 {intent_id}")
        self.dlg.prompt_tree.setCurrentItem(item)
        self.assertEqual(self.dlg._current_intent_id, intent_id,
                         "选中意图后 _current_intent_id 未同步")

    def click_delete(self):
        self.dlg.del_intent_button.click()

    def ids(self):
        return [i.get("id") for i in self.dlg.data["intents"]]

    def tombstone(self):
        return self.dlg.data.get(REMOVED_KEY)


class TestFactorySpecField(unittest.TestCase):
    """Change 1a: the shipped factory spec carries the empty tombstone."""

    def test_spec_has_empty_removed_intents(self):
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        self.assertIn(REMOVED_KEY, spec, "出厂 spec 缺少 removed_intents 默认")
        self.assertEqual(spec[REMOVED_KEY], [], "出厂墓碑默认必须是空列表")


class TestConfigVersionAndMigration(unittest.TestCase):
    """Change 1b: idempotent config migration 12 -> 13."""

    def test_config_version_is_bumped_to_13(self):
        self.assertEqual(config_mod.CONFIG_VERSION, EXPECTED_VERSION,
                         "配置版本应从 12 升到 13")


class _TempConfigHome(unittest.TestCase):
    def setUp(self):
        self._net = patch("urllib.request.urlopen",
                          side_effect=AssertionError("票据32不得发起真实网络调用"))
        self._net.start()
        self.addCleanup(self._net.stop)
        self._old_xdg = os.environ.get("XDG_CONFIG_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        self.addCleanup(self._restore_xdg)
        self.real_config = Path.home() / ".config" / "clip-llm02" / "settings.json"
        self.real_mtime = (self.real_config.stat().st_mtime_ns
                           if self.real_config.exists() else None)

    def _restore_xdg(self):
        self._tmp.cleanup()
        if self._old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._old_xdg

    def _write_v12(self, data):
        path = config_mod.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return path

    def _disk(self):
        path = config_mod.config_path()
        self.assertTrue(path.exists(), f"load_config 未写盘：{path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _assert_real_config_untouched(self):
        mtime = (self.real_config.stat().st_mtime_ns
                 if self.real_config.exists() else None)
        self.assertEqual(mtime, self.real_mtime, "真实用户配置被测试改动了")


class TestMigrationEndToEnd(_TempConfigHome):
    """Change 1b: real v12 config -> load_config -> disk, then restart."""

    def _v12_data(self):
        data = _factory_data()
        data.pop(REMOVED_KEY, None)
        data["config_version"] = 12
        return data

    def test_v12_config_gains_empty_tombstone_and_bumps_version(self):
        self._write_v12(self._v12_data())
        config_mod.load_config()
        disk = self._disk()
        self.assertIn(REMOVED_KEY, disk, "12→13 迁移未补 removed_intents 字段")
        self.assertEqual(disk[REMOVED_KEY], [])
        self.assertEqual(disk["config_version"], EXPECTED_VERSION)
        self._assert_real_config_untouched()

    def test_migration_is_idempotent_on_restart(self):
        self._write_v12(self._v12_data())
        config_mod.load_config()
        once = self._disk()
        config_mod.load_config()                       # 模拟再次重启
        self.assertEqual(self._disk(), once, "重启后磁盘配置发生漂移")
        self._assert_real_config_untouched()

    def test_migration_preserves_existing_tombstone_ids(self):
        data = self._v12_data()
        data[REMOVED_KEY] = [FACTORY_OVERRIDE_ID]
        self._write_v12(data)
        config_mod.load_config()
        disk = self._disk()
        self.assertEqual(disk[REMOVED_KEY], [FACTORY_OVERRIDE_ID],
                         "迁移不得丢掉用户已删除的墓碑 id")
        self.assertEqual(disk["config_version"], EXPECTED_VERSION)
        self._assert_real_config_untouched()


class TestLoadTombstone(unittest.TestCase):
    """Change 1c: deep_merge cannot resurrect a tombstoned factory intent."""

    def test_tombstone_removes_runtime_intent_and_persisted_data(self):
        data = _factory_data()
        data.setdefault(REMOVED_KEY, []).append(FACTORY_OVERRIDE_ID)
        settings = settings_from_data(data)
        self.assertNotIn(
            FACTORY_OVERRIDE_ID,
            [i.id for i in settings.intents],
            "墓碑未从运行期 settings.intents 删除该意图")
        self.assertNotIn(
            FACTORY_OVERRIDE_ID,
            [i.get("id") for i in settings.data["intents"]],
            "墓碑未从 settings.data[intents] 删除：下次保存会把意图写回盘")
        self.assertIn(FACTORY_STAY_ID, [i.id for i in settings.intents],
                      "未删的出厂意图必须仍在")

    def test_tombstone_beats_factory_merge_on_load(self):
        """Disk still carries the id (factory re-added); runtime must not."""
        data = _factory_data()
        self.assertIn(FACTORY_OVERRIDE_ID, [i["id"] for i in data["intents"]])
        data[REMOVED_KEY] = [FACTORY_OVERRIDE_ID]
        settings = settings_from_data(data)
        self.assertNotIn(FACTORY_OVERRIDE_ID, [i.id for i in settings.intents],
                         "deep_merge 按 id 补回的出厂意图未再被墓碑删除")

    def test_no_tombstone_keeps_all_factory_intents(self):
        # Regression guard (green at RED): counter-proven at close.
        data = _factory_data()
        data.pop(REMOVED_KEY, None)
        settings = settings_from_data(data)
        self.assertIn(FACTORY_OVERRIDE_ID, [i.id for i in settings.intents])

    def test_missing_tombstone_field_is_safe(self):
        # Regression guard (green at RED): counter-proven at close.
        data = _factory_data()
        data.pop(REMOVED_KEY, None)
        settings = settings_from_data(data)
        self.assertIn(FACTORY_STAY_ID, [i.id for i in settings.intents])

    def test_policy_removed_rules_tombstone_still_works(self):
        # Regression guard (green at RED): counter-proven at close.
        data = _factory_data()
        data.setdefault("policy", {}).setdefault("rules", {})["empty"] = {
            "enabled": True}
        data["policy"]["removed_rules"] = ["empty"]
        settings = settings_from_data(data)
        self.assertNotIn("empty", settings.policy["rules"],
                         "policy.removed_rules 墓碑行为不得被本票破坏")


class TestDeleteLogic(_DialogMixin):
    """Change 2: display-"自定义" factory intents are deletable + tombstoned."""

    def test_factory_override_intent_deleted_via_real_button(self):
        self.assertEqual(intent_prompt_source(
            _original_override_intent(FACTORY_OVERRIDE_ID)), "自定义")
        self.select(FACTORY_OVERRIDE_ID)
        self.click_delete()
        self.assertNotIn(FACTORY_OVERRIDE_ID, self.ids(),
                         "显示自定义的出厂意图仍删不掉")
        tomb = self.tombstone()
        self.assertIsNotNone(tomb, "删除出厂意图应写 removed_intents 字段")
        self.assertIn(FACTORY_OVERRIDE_ID, tomb, "删出厂意图未打墓碑")
        self.assertIn(FACTORY_STAY_ID, self.ids(), "未删的出厂意图不得消失")

    def test_tombstone_ids_accumulate_without_duplicates(self):
        # Pre-existing tombstone for this very id: must not be duplicated.
        self.dlg.data[REMOVED_KEY] = [FACTORY_OVERRIDE_ID]
        self.dlg.data["intents"].append(
            _original_override_intent(FACTORY_OVERRIDE_ID))
        self.dlg._reload_tree()
        self.select(FACTORY_OVERRIDE_ID)
        self.click_delete()
        self.assertNotIn(FACTORY_OVERRIDE_ID, self.ids())
        self.assertEqual(self.tombstone().count(FACTORY_OVERRIDE_ID), 1,
                         "墓碑 id 必须去重")

    def test_text_template_factory_intent_blocked_with_human_hint(self):
        self.assertEqual(intent_prompt_source(
            _original_override_intent(FACTORY_TEXT_ID)), "文本模板")
        self.select(FACTORY_TEXT_ID)
        before = self.tombstone()
        self.click_delete()
        self.assertIn(FACTORY_TEXT_ID, self.ids(),
                      "纯文本模板的出厂意图不得被删")
        text = self.dlg.legacy_hint.text()
        self.assertIn(BLOCK_HINT, text, f"挡删提示要说人话，实际：{text}")
        self.assertIn(FACTORY_HINT, text)
        self.assertEqual(self.tombstone(), before, "被挡时不得写墓碑")

    def test_custom_user_intent_deleted_without_tombstone(self):
        # Regression guard (green at RED): counter-proven at close.
        self.dlg.data["intents"].append(_custom_intent())
        self.dlg._reload_tree()
        self.select(CUSTOM_ID)
        self.click_delete()
        self.assertNotIn(CUSTOM_ID, self.ids(), "用户自定义意图应照旧可删")
        tomb = self.tombstone() or []
        self.assertNotIn(CUSTOM_ID, tomb, "删用户自定义意图不得写墓碑")

    def test_delete_still_selects_first_intent(self):
        # Regression guard (green at RED): counter-proven at close.
        self.select(FACTORY_OVERRIDE_ID)
        self.click_delete()
        self.assertNotEqual(self.dlg._current_intent_id, FACTORY_OVERRIDE_ID,
                            "删除后应重新选中一条意图")


class TestEndToEnd(_TempConfigHome):
    """Hard gates ①②③④: real dialog + real save_config + real load_config."""

    def setUp(self):
        super().setUp()
        # 真实用户路径：磁盘配置 -> load_config（补齐默认键）-> 真实对话框。
        # 若直接用 load_defaults().as_dict() 构造 Settings，会缺
        # desktop_* / hotkeys 等键，重启时触发无关迁移并让磁盘漂移。
        path = config_mod.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_factory_data(), ensure_ascii=False,
                                   indent=2), encoding="utf-8")
        self.dlg = SettingsDialog(config_mod.load_config())
        self.addCleanup(self.dlg.close)

    def _select(self, intent_id):
        item = _find_tree_item(self.dlg.prompt_tree, intent_id)
        self.assertIsNotNone(item, intent_id)
        self.dlg.prompt_tree.setCurrentItem(item)

    def _ids(self):
        return [i.get("id") for i in self.dlg.data["intents"]]

    def test_e2e_1_delete_display_custom_factory_intent(self):
        self._select(FACTORY_OVERRIDE_ID)
        self.dlg.del_intent_button.click()
        self.assertNotIn(FACTORY_OVERRIDE_ID, self._ids())
        self.assertIn(REMOVED_KEY, self.dlg.data)
        self.assertIn(FACTORY_OVERRIDE_ID, self.dlg.data[REMOVED_KEY])
        self.assertIn(FACTORY_STAY_ID, self._ids())
        self._assert_real_config_untouched()

    def test_e2e_2_saved_config_survives_restart_without_resurrection(self):
        self._select(FACTORY_OVERRIDE_ID)
        self.dlg.del_intent_button.click()
        save_config(self.dlg.data)                      # 真实写盘
        on_disk = self._disk()
        self.assertIn(REMOVED_KEY, on_disk, "删除出厂意图未写盘墓碑字段")
        self.assertIn(FACTORY_OVERRIDE_ID, on_disk[REMOVED_KEY])

        restarted = config_mod.load_config()             # 模拟重启
        self.assertNotIn(
            FACTORY_OVERRIDE_ID, [i.id for i in restarted.intents],
            "重启后 deep_merge 把已删出厂意图补回来了")
        self.assertIn(FACTORY_STAY_ID, [i.id for i in restarted.intents],
                      "未删的出厂意图重启后必须还在")
        self.assertEqual(on_disk, self._disk(), "重启导致磁盘配置漂移")
        self._assert_real_config_untouched()

    def test_e2e_3_text_template_factory_intent_is_blocked(self):
        self._select(FACTORY_TEXT_ID)
        self.dlg.del_intent_button.click()
        self.assertIn(FACTORY_TEXT_ID, self._ids())
        text = self.dlg.legacy_hint.text()
        self.assertIn(BLOCK_HINT, text)
        self.assertIn(FACTORY_HINT, text)
        self._assert_real_config_untouched()

    def test_e2e_4_custom_intent_deleted_without_tombstone(self):
        self.dlg.data["intents"].append(_custom_intent())
        self.dlg._reload_tree()
        self._select(CUSTOM_ID)
        self.dlg.del_intent_button.click()
        self.assertNotIn(CUSTOM_ID, self._ids())
        self.assertNotIn(CUSTOM_ID, self.dlg.data.get(REMOVED_KEY) or [])
        self._assert_real_config_untouched()


class TestUntouchedRegressions(_NoNetwork, unittest.TestCase):
    """Change 4 guards (green at RED); each counter-proven before close."""

    def test_v15_display_semantics_unchanged(self):
        self.assertEqual(intent_prompt_source(
            _original_override_intent(FACTORY_OVERRIDE_ID)), "自定义")
        self.assertEqual(intent_prompt_source(
            _original_override_intent(FACTORY_TEXT_ID)), "文本模板")
        self.assertEqual(intent_prompt_source({"custom": True}), "自定义")
        self.assertEqual(intent_prompt_source({"free_input_only": True}),
                         "自由输入")

    def test_derived_template_delete_logic_unchanged(self):
        data = _factory_data()
        cats = dict(data["decision_model_categories"])
        derived = "票32派生类别"
        cats[derived] = "票32：用于验证派生路由删类别连带删除"
        out, errors = apply_decision_model_json(
            data, json.dumps(cats, ensure_ascii=False))
        self.assertEqual(errors, [])
        self.assertIn(derived, out["routes"])
        cats.pop(derived)
        out2, errors2 = apply_decision_model_json(
            out, json.dumps(cats, ensure_ascii=False))
        self.assertEqual(errors2, [])
        self.assertNotIn(derived, out2["routes"])
        self.assertIn("英语单词", out2["routes"],
                      "B 删类别不得误删无 derived 标记的出厂路由")


if __name__ == "__main__":
    unittest.main()
