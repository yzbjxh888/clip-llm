"""Ticket 27 gates (v11): internal source_from_decision_model tag + unified UI badge.

RED first. Expected values are hand-derived from
``.factory/00-decisions/CHANGE-decision-model-tag-v11.md`` and
``spec/builtin_settings.json`` (the authority), never from the code under test.
The new tag is orthogonal to ``derived_template_source``: the UI badge must
follow the new tag, while the three old deletion/migration sites must keep
following ``derived_template_source``. No real network calls; Qt offscreen;
the real user config is never touched (temp XDG_CONFIG_HOME + mtime assertions).
"""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox  # noqa: E402

from clipllm02 import load_defaults  # noqa: E402
from clipllm02 import settings as settings_mod  # noqa: E402
from clipllm02.desktop import gateway as gateway_mod  # noqa: E402
from clipllm02.studio import config as config_mod  # noqa: E402
from clipllm02.studio.config import settings_from_data  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

BADGE = " · 来自决策模型"
TAG = "source_from_decision_model"
IMAGE_ID = "图片识别"                    # not in B; never tagged
DERIVED_ID = "英语句子"                  # B-new route in the real v11 config
FACTORY_IDS = [
    "英语单词", "英语短语", "中文词语", "中文句解", "中文答疑", "中文段落",
    "LaTeX 公式", "代码", "化学物质", "通用解析",
]
NEW_CATEGORY = "票27新增类别"
NEW_CRITERIA = "票27：英文完整句子，含主谓宾结构与时态（新增类别判别描述）"
ROOT = Path(__file__).resolve().parents[1]


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _factory_b():
    """B categories from the authority spec (not from production code)."""
    spec = json.loads((ROOT / "spec" / "builtin_settings.json").read_text(encoding="utf-8"))
    return dict(spec["decision_model_categories"])


def _real_shaped_data(version=11, tagged=False):
    """Exact shape of the v11 user config: 10 factory + 图片识别 + 英语句子.

    Only 英语句子 carries derived_template_source=decision_model; the other
    ten B routes have no marker at all (the reported root cause).
    """
    data = load_defaults().as_dict()
    b = _factory_b()
    b[DERIVED_ID] = "英文完整句子（用户新增类别）"
    data["decision_model_categories"] = b
    data["routes"][DERIVED_ID] = {
        "id": DERIVED_ID, "label": DERIVED_ID, "enabled": True,
        "query_type": "free", "kind": "route", "prompt": "翻译成中文。",
        "sections": {}, "fields": [],
        "derived_template_source": "decision_model",
    }
    data.setdefault("route_prompts", {})[DERIVED_ID] = "翻译成中文。"
    data["config_version"] = version
    if tagged:
        for rid in list(b):
            data["routes"][rid][TAG] = True
    return data


def _marker():
    """getattr keeps collection green; RED is a named assertion, not ImportError."""
    return getattr(settings_mod, "mark_decision_model_source_routes", None)


def _guard_network(test):
    p = patch.object(gateway_mod.urllib.request, "urlopen",
                     side_effect=AssertionError("票据27不得发起真实网络调用"))
    p.start()
    test.addCleanup(p.stop)


class TestBuiltinSpecTag(unittest.TestCase):
    """Change 1a: the shipped spec itself carries the new tag."""

    def test_spec_tags_all_ten_b_routes_and_not_image(self):
        spec = json.loads((ROOT / "spec" / "builtin_settings.json").read_text(encoding="utf-8"))
        routes = spec["routes"]
        b = spec["decision_model_categories"]
        self.assertEqual(set(FACTORY_IDS), set(b), "出厂 B 清单与 hand-derived 清单不符")
        for rid in FACTORY_IDS:
            self.assertIs(routes[rid].get(TAG), True, f"出厂 spec 未给 {rid} 打新标记")
        self.assertNotIn(TAG, routes[IMAGE_ID], "图片识别不在 B，不得打标")


class TestTagMigration(unittest.TestCase):
    """Change 1b: idempotent config migration 11 -> 12."""

    def setUp(self):
        self.mark = _marker()

    def test_marker_function_exists(self):
        self.assertTrue(callable(self.mark), "缺少 mark_decision_model_source_routes")

    def test_marker_tags_eleven_b_routes_idempotent(self):
        self.assertIsNotNone(self.mark)
        data = _real_shaped_data()
        self.assertTrue(self.mark(data), "首次迁移应报告改动")
        b = set(data["decision_model_categories"])
        tagged = {rid for rid, pack in data["routes"].items() if pack.get(TAG) is True}
        self.assertEqual(tagged, b, "应且只应给 B 类别清单里的路由打标")
        self.assertEqual(len(tagged), 11)
        self.assertNotIn(IMAGE_ID, tagged)
        self.assertIs(data["routes"][DERIVED_ID].get("derived_template_source"),
                      "decision_model", "不得改动旧的 derived 标记")
        once = copy.deepcopy(data)
        self.assertFalse(self.mark(data), "重复迁移不能再改动配置")
        self.assertEqual(data, once)

    def test_marker_also_uses_id_field_when_key_is_not_a_b_category(self):
        self.assertIsNotNone(self.mark)
        data = _real_shaped_data()
        data["routes"]["custom.key"] = {
            "id": "中文词语", "label": "别名", "enabled": True,
            "query_type": "free", "kind": "route", "prompt": "",
            "sections": {}, "fields": [],
        }
        self.mark(data)
        self.assertIs(data["routes"]["custom.key"].get(TAG), True,
                      "id 字段在 B 清单里的路由也应打标")

    def test_config_version_is_bumped_to_13(self):
        # v16 票据 32 把 12→13；v11 的 11→12 已被后续版本取代。
        self.assertEqual(config_mod.CONFIG_VERSION, 13, "配置版本应从 11/12 升到 13")


class TestSyncTag(unittest.TestCase):
    """Change 1c: B add/delete keeps the new tag in sync."""

    def test_add_category_derived_route_tagged_and_delete_removes_it(self):
        data = load_defaults().as_dict()
        b = dict(data["decision_model_categories"])
        b[NEW_CATEGORY] = NEW_CRITERIA
        out, errors = settings_mod.apply_decision_model_json(
            data, json.dumps(b, ensure_ascii=False))
        self.assertEqual(errors, [])
        route = out["routes"].get(NEW_CATEGORY)
        self.assertIsNotNone(route, "B 新类别必须派生同名路由")
        self.assertIs(route.get(TAG), True, "派生路由必须带新标记")

        b.pop(NEW_CATEGORY)
        out2, errors2 = settings_mod.apply_decision_model_json(
            out, json.dumps(b, ensure_ascii=False))
        self.assertEqual(errors2, [])
        self.assertNotIn(NEW_CATEGORY, out2["routes"], "B 删类别应连带删除派生路由")
        self.assertIn(IMAGE_ID, out2["routes"], "图片识别不得被误删")

    def test_delete_category_never_removes_untagged_factory_route(self):
        """Regression: the old derived_template_source delete logic is intact."""
        data = _real_shaped_data()
        b = dict(data["decision_model_categories"])
        b.pop("英语单词")                      # factory route id removed from B
        b.pop(DERIVED_ID)                      # user-added route removed from B
        out, errors = settings_mod.apply_decision_model_json(
            data, json.dumps(b, ensure_ascii=False))
        self.assertEqual(errors, [])
        self.assertIn("英语单词", out["routes"],
                      "出厂路由无 derived 标记，删类别时不得被误删")
        self.assertNotIn(DERIVED_ID, out["routes"],
                         "带 derived 标记的派生路由应随类别删除")


class _TempConfigHome(unittest.TestCase):
    def setUp(self):
        _guard_network(self)
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
        self.assertTrue(path.exists(), f"load_config 未写盘：{path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _assert_real_config_untouched(self):
        mtime = (self.real_config.stat().st_mtime_ns
                 if self.real_config.exists() else None)
        self.assertEqual(mtime, self.real_mtime, "真实用户配置被测试改动了")


class TestMigrationEndToEnd(_TempConfigHome):
    """Hard gate ②: real config copy migrates 11 -> 12 and is idempotent."""

    def test_full_chain_v11_config_migrates_then_dialog_shows_eleven_badges(self):
        """Real v11 config -> load_config migration -> real dialog -> 11 badges."""
        path = config_mod.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_real_shaped_data(version=11), ensure_ascii=False,
                                   indent=2), encoding="utf-8")

        settings = config_mod.load_config()
        dlg = SettingsDialog(settings)
        try:
            labels = [dlg.route_combo.itemText(i)
                      for i in range(dlg.route_combo.count())]
            self.assertEqual(sum(BADGE in t for t in labels), 11,
                             f"迁移后的真实配置应显示 11 个标注：{labels}")
            self.assertFalse(any(BADGE in t and IMAGE_ID in t for t in labels))
            for rid in ("英语单词", "中文词语", DERIVED_ID):
                idx = dlg.route_combo.findData(rid)
                dlg.route_combo.setCurrentIndex(idx)
                self.assertIn("来自决策模型", dlg.route_kind_hint.text(),
                              f"{rid} 上方说明未统一")
        finally:
            dlg.close()
        self._assert_real_config_untouched()

    def test_real_config_copy_migrates_persists_and_survives_restart(self):
        path = config_mod.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_real_shaped_data(version=11), ensure_ascii=False,
                                   indent=2), encoding="utf-8")

        settings = config_mod.load_config()
        disk = self._disk()
        b = set(disk["decision_model_categories"])
        tagged = {rid for rid, pack in disk["routes"].items() if pack.get(TAG) is True}
        self.assertEqual(tagged, b)
        self.assertEqual(len(tagged), 11)
        self.assertNotIn(IMAGE_ID, tagged)
        self.assertEqual(disk["config_version"], 13)
        self.assertEqual(settings.routes[DERIVED_ID].get(TAG), True)
        self.assertNotIn(TAG, settings.routes[IMAGE_ID])

        restarted = config_mod.load_config()
        self.assertEqual(self._disk(), disk, "重启后磁盘配置发生漂移")
        self.assertEqual(restarted.routes["英语单词"].get(TAG), True)
        self._assert_real_config_untouched()


class TestUiBadge(unittest.TestCase):
    """Change 2: all three UI sites follow the new tag, not the old one."""

    def setUp(self):
        _guard_network(self)
        self.dlg = SettingsDialog(settings_from_data(_real_shaped_data(tagged=True)))

    def tearDown(self):
        self.dlg.close()

    def _labels(self):
        return [self.dlg.route_combo.itemText(i)
                for i in range(self.dlg.route_combo.count())]

    def _select(self, rid):
        idx = self.dlg.route_combo.findData(rid)
        self.assertGreaterEqual(idx, 0, f"下拉里找不到 {rid}")
        self.dlg.route_combo.setCurrentIndex(idx)

    def test_dropdown_badges_eleven_text_routes_not_image(self):
        labels = self._labels()
        badged = [t for t in labels if BADGE in t]
        self.assertEqual(len(badged), 11, f"应有 11 个文本标签带标注，实际：{badged}")
        image = [t for t in labels if IMAGE_ID in t]
        self.assertEqual(len(image), 1)
        self.assertNotIn(BADGE.strip(), image[0], "图片识别不得带标注")

    def test_edit_area_note_unified_for_all_constrained_routes(self):
        for rid in ("中文词语", "英语单词", DERIVED_ID):
            self._select(rid)
            self.assertIn("来自决策模型", self.dlg.route_kind_hint.text(),
                          f"{rid} 的上方说明未统一显示「来自决策模型」")

    def test_rename_via_name_field_keeps_badge(self):
        """Second rename path (显示名 field) must not drop the badge either."""
        self._select("英语单词")
        self.dlg.route_label_edit.setText("英语单词（改名）")
        idx = self.dlg.route_combo.findData("英语单词")
        self.assertEqual(self.dlg.route_combo.itemText(idx),
                         "英语单词（改名）" + BADGE, "改名后标注丢失")

    def test_rename_in_editor_keeps_badge(self):
        self._select("英语单词")
        pack = json.loads(self.dlg.route_prompt.toPlainText())
        pack["label"] = "英语单词（改名后）"
        self.dlg.route_prompt.setPlainText(json.dumps(pack, ensure_ascii=False, indent=2))
        idx = self.dlg.route_combo.findData("英语单词")
        self.assertEqual(self.dlg.route_combo.itemText(idx),
                         "英语单词（改名后）" + BADGE, "改名后标注丢失")


class TestEndToEndDialogAndBSync(_TempConfigHome):
    """Hard gates ① and ③ on a real offscreen SettingsDialog."""

    def setUp(self):
        super().setUp()
        self.dlg = SettingsDialog(settings_from_data(_real_shaped_data(tagged=True)))

    def tearDown(self):
        self.dlg.close()
        super().tearDown()

    def test_gate1_dropdown_and_note_on_real_dialog(self):
        labels = [self.dlg.route_combo.itemText(i)
                  for i in range(self.dlg.route_combo.count())]
        self.assertEqual(sum(BADGE in t for t in labels), 11)
        self.assertFalse(any(BADGE in t and IMAGE_ID in t for t in labels))
        idx = self.dlg.route_combo.findData("中文答疑")
        self.dlg.route_combo.setCurrentIndex(idx)
        self.assertIn("来自决策模型", self.dlg.route_kind_hint.text())
        ok = self.dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        self.assertIsNotNone(ok)
        ok.click()
        self.assertEqual(self.dlg.result(), QDialog.Accepted)
        self._assert_real_config_untouched()

    def test_gate3_b_add_shows_badge_then_delete_removes_route(self):
        b = json.loads(self.dlg.decision_model_json_edit.toPlainText())
        self.assertNotIn(NEW_CATEGORY, b)
        b[NEW_CATEGORY] = NEW_CRITERIA
        self.dlg.decision_model_json_edit.setPlainText(
            json.dumps(b, ensure_ascii=False, indent=2))
        self.dlg.decision_model_json_apply.click()

        route = self.dlg.data["routes"].get(NEW_CATEGORY)
        self.assertIsNotNone(route)
        self.assertIs(route.get(TAG), True)
        idx = self.dlg.route_combo.findData(NEW_CATEGORY)
        self.assertGreaterEqual(idx, 0)
        self.assertIn(BADGE.strip(), self.dlg.route_combo.itemText(idx),
                      "B 新增类别的派生路由在下拉里应显示标注")
        self._select = lambda rid: self.dlg.route_combo.setCurrentIndex(
            self.dlg.route_combo.findData(rid))
        self._select(NEW_CATEGORY)
        self.assertIn("来自决策模型", self.dlg.route_kind_hint.text())

        # 删类别：派生路由连带删除，旧 derived 删除逻辑未破坏，图片识别仍在
        b.pop(NEW_CATEGORY)
        self.dlg.decision_model_json_edit.setPlainText(
            json.dumps(b, ensure_ascii=False, indent=2))
        self.dlg.decision_model_json_apply.click()
        self.assertNotIn(NEW_CATEGORY, self.dlg.data["routes"])
        self.assertIn(IMAGE_ID, self.dlg.data["routes"])
        self._assert_real_config_untouched()


if __name__ == "__main__":
    unittest.main()
