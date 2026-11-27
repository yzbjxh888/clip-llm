"""Ticket 31 gate tests: v15 naming, three-group combo, delete protection.

Hand-derived from CHANGE-template-naming-groups-v15.md, not from the source.

Baseline (769 passed): intent_prompt_source returns 专属提示词/复用模板,
intent_route_combo is a flat list of route labels, and _delete_route only blocks
routes that are referenced by an intent. Every behavior gate below must fail for
that named reason. Gates that are green at RED are explicit regression guards;
each is counter-proven by a temporary production break before the ticket closes.

All dialogs are real SettingsDialog instances driven offscreen. urlopen is
patched everywhere: these tests must never make a real network call.
"""
import copy
import os
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402
from clipllm02 import load_defaults  # noqa: E402
from clipllm02.models import intent_prompt_source  # noqa: E402
from clipllm02.settings import Settings  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

HEADER_SOURCE = "提示词模板"
HEADER_CUSTOM = "自定义模板"
HEADER_IMAGE = "图片模板"
HEADERS = (HEADER_SOURCE, HEADER_CUSTOM, HEADER_IMAGE)
CUSTOM_ROUTE = "票据31模板"
SOURCE_ROUTE = "英语单词"
VISION_ROUTE = "图片识别"


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


def _data(*, with_custom=True, with_vision=True):
    data = load_defaults().as_dict()
    if with_custom and CUSTOM_ROUTE not in data["routes"]:
        data["routes"][CUSTOM_ROUTE] = {
            "id": CUSTOM_ROUTE, "label": CUSTOM_ROUTE, "enabled": True,
            "prompt": "票据31自定义模板提示词", "query_type": "free",
            "kind": "route", "sections": [], "fields": [],
        }
    if not with_vision:
        data["routes"].pop(VISION_ROUTE, None)
    return data


def _dialog(*, with_custom=True, with_vision=True):
    return SettingsDialog(Settings(
        _data(with_custom=with_custom, with_vision=with_vision)))


def _items(combo):
    model = combo.model()
    return [(combo.itemText(i), combo.itemData(i), bool(model.item(i).isEnabled()))
            for i in range(combo.count())]


def _headers(combo):
    return [text for text, data, _enabled in _items(combo) if data is None]


def _groups(combo):
    result = {}
    current = None
    for text, data, _enabled in _items(combo):
        if data is None:
            current = text
            result.setdefault(current, [])
        else:
            result.setdefault(current, []).append(data)
    return result


def _expected_group(route_id, routes):
    pack = routes[route_id]
    if pack.get("vision"):
        return HEADER_IMAGE
    if pack.get("source_from_decision_model"):
        return HEADER_SOURCE
    return HEADER_CUSTOM


class TestNamingLabels(_NoNetwork, unittest.TestCase):
    """CHANGE 1: display strings only."""

    def test_factory_with_override_is_custom(self):
        self.assertEqual(intent_prompt_source({"prompt_override": "写点东西"}),
                         "自定义")

    def test_factory_without_override_is_text_template(self):
        self.assertEqual(intent_prompt_source({"prompt_override": "  "}),
                         "文本模板")

    def test_custom_flag_still_custom(self):
        # Regression guard: unchanged by v15; counter-proven at close.
        self.assertEqual(intent_prompt_source({"custom": True}), "自定义")

    def test_free_input_still_free_input(self):
        # Regression guard: unchanged by v15; counter-proven at close.
        self.assertEqual(intent_prompt_source({"free_input_only": True}),
                         "自由输入")

    def test_label_lookup_does_not_mutate_data(self):
        raw = {"id": "image.table", "prompt_override": "", "custom": False,
               "free_input_only": False, "primary_route": VISION_ROUTE,
               "use_legacy_template": False}
        before = copy.deepcopy(raw)
        self.assertEqual(intent_prompt_source(raw), "文本模板")
        self.assertEqual(raw, before, "查询方式数据字段不得因显示文案被改")


class TestNamingUiCopy(_NoNetwork, unittest.TestCase):
    """CHANGE 1: every UI path that can show the retired words."""

    def setUp(self):
        super().setUp()
        self.dlg = _dialog()

    def tearDown(self):
        self.dlg.close()
        super().tearDown()

    def test_legacy_hint_without_override_says_text_template(self):
        self.dlg._load_intent("general.freeform")
        text = self.dlg.legacy_hint.text()
        self.assertIn("文本模板", text)
        self.assertIn("提示词留空即生效", text)
        self.assertNotIn("复用模板", text)

    def test_legacy_hint_with_override_says_custom(self):
        self.dlg._load_intent("chem.naming")
        text = self.dlg.legacy_hint.text()
        self.assertIn("自定义", text)
        self.assertNotIn("专属提示词", text)

    def test_editing_prompt_box_updates_hint_copy(self):
        self.dlg._load_intent("general.freeform")
        self.dlg.intent_prompt.setPlainText("票据31专属提示词")
        text = self.dlg.legacy_hint.text()
        self.assertIn("自定义", text)
        self.assertNotIn("专属提示词", text)

    def test_prompt_placeholder_says_text_template(self):
        text = self.dlg.intent_prompt.placeholderText()
        self.assertIn("文本模板", text)
        self.assertNotIn("复用主路由模板", text)

    def test_route_usage_hint_says_custom_prompt(self):
        data = _data()
        for item in data["intents"]:
            if item["id"] == "en.legacy":
                item["prompt_override"] = ""
            if item["id"] == "en.read":
                item["prompt_override"] = "票据31自己的提示词"
        dlg = SettingsDialog(Settings(data))
        try:
            index = dlg.route_combo.findData(SOURCE_ROUTE)
            self.assertGreaterEqual(index, 0)
            dlg.route_combo.setCurrentIndex(index)
            text = dlg.route_kind_hint.text()
            self.assertIn("自定义提示词", text)
            self.assertNotIn("专属提示词", text)
        finally:
            dlg.close()


class TestGroupedIntentCombo(_NoNetwork, unittest.TestCase):
    """CHANGE 2: three disabled group headers in the main route combo."""

    def setUp(self):
        super().setUp()
        self.dlg = _dialog()

    def tearDown(self):
        self.dlg.close()
        super().tearDown()

    def test_three_headers_present_ordered_and_disabled(self):
        # 构造时全量填充的路径（_sync 随后会按首个意图过滤，这里显式走一次）
        self.dlg._fill_intent_routes()
        combo = self.dlg.intent_route_combo
        heads = [(t, d, e) for t, d, e in _items(combo) if d is None]
        self.assertEqual([t for t, _d, _e in heads], list(HEADERS))
        self.assertTrue(all(not enabled for _t, _d, enabled in heads),
                        "组标题只是分隔，必须不可选")

    def test_each_route_sits_under_its_flag_group(self):
        self.dlg._fill_intent_routes()
        combo = self.dlg.intent_route_combo
        groups = _groups(combo)
        routes = self.dlg.data["routes"]
        for rid in routes:
            self.assertIn(rid, groups.get(_expected_group(rid, routes), []),
                          f"{rid} 归属分组错误")

    def test_within_group_order_follows_route_config(self):
        self.dlg._fill_intent_routes()
        groups = _groups(self.dlg.intent_route_combo)
        routes = self.dlg.data["routes"]
        for header in HEADERS:
            expected = [rid for rid in sorted(routes)
                        if _expected_group(rid, routes) == header]
            self.assertEqual(groups.get(header), expected, header)

    def test_text_intent_lists_only_two_text_groups(self):
        self.dlg._load_intent("zh.legacy")
        combo = self.dlg.intent_route_combo
        self.assertEqual(_headers(combo), [HEADER_SOURCE, HEADER_CUSTOM])
        self.assertNotIn(VISION_ROUTE, _groups(combo).get(HEADER_IMAGE, []))

    def test_image_intent_lists_only_picture_group(self):
        self.dlg._load_intent("image.ocr")
        combo = self.dlg.intent_route_combo
        self.assertEqual(_headers(combo), [HEADER_IMAGE])
        self.assertEqual([d for _t, d, _e in _items(combo) if d is not None],
                         [VISION_ROUTE])

    def test_selection_still_returns_route_id(self):
        self.dlg._load_intent("zh.legacy")
        combo = self.dlg.intent_route_combo
        index = combo.findData(CUSTOM_ROUTE)
        self.assertGreaterEqual(index, 0)
        self.assertIsNone(combo.itemData(index - 1), "选中项上方应是组标题")
        self.assertFalse(combo.model().item(index - 1).isEnabled())
        combo.setCurrentIndex(index)
        self.assertEqual(combo.currentData(), CUSTOM_ROUTE)
        self.assertEqual(self.dlg._intent_by_id("zh.legacy")["primary_route"],
                         CUSTOM_ROUTE)

    def test_empty_group_has_no_header(self):
        dlg = _dialog(with_vision=False)
        try:
            dlg._fill_intent_routes()
            self.assertEqual(_headers(dlg.intent_route_combo),
                             [HEADER_SOURCE, HEADER_CUSTOM],
                             "没有图片模板时不应显示图片模板组标题")
        finally:
            dlg.close()


class TestDeleteProtection(_NoNetwork, unittest.TestCase):
    """CHANGE 3: real delete button, real in-memory routes dict."""

    def setUp(self):
        super().setUp()
        self.dlg = _dialog()

    def tearDown(self):
        self.dlg.close()
        super().tearDown()

    def _click_delete(self, route_id):
        index = self.dlg.route_combo.findData(route_id)
        self.assertGreaterEqual(index, 0, route_id)
        self.dlg.route_combo.setCurrentIndex(index)
        self.dlg.del_route_button.click()

    def test_source_template_cannot_be_deleted(self):
        self._click_delete(SOURCE_ROUTE)
        self.assertIn(SOURCE_ROUTE, self.dlg.data["routes"], "提示词模板不得被删")
        self.assertIn("提示词模板（与决策模型绑定）不可删除",
                      self.dlg.legacy_hint.text())

    def test_protection_precedes_reference_check(self):
        self.assertTrue(any(i.get("primary_route") == SOURCE_ROUTE
                            for i in self.dlg.data["intents"]))
        self._click_delete(SOURCE_ROUTE)
        text = self.dlg.legacy_hint.text()
        self.assertIn("不可删除", text)
        self.assertNotIn("被引用", text)

    def test_vision_template_cannot_be_deleted(self):
        self._click_delete(VISION_ROUTE)
        self.assertIn(VISION_ROUTE, self.dlg.data["routes"], "图片识别模板不得被删")
        self.assertIn("图片识别模板不可删除", self.dlg.legacy_hint.text())

    def test_unreferenced_custom_template_still_deletable(self):
        # Regression guard: unchanged by v15; counter-proven at close.
        self._click_delete(CUSTOM_ROUTE)
        self.assertNotIn(CUSTOM_ROUTE, self.dlg.data["routes"])
        self.assertIn("已删除模板", self.dlg.legacy_hint.text())


class TestEndToEndV15(_NoNetwork, unittest.TestCase):
    """Handoff gates 1/2/3 against a real offscreen dialog."""

    def test_e2e_1_three_groups_on_real_dialog(self):
        dlg = _dialog()
        try:
            # 真实对话框的构造填充路径：三组全列、组标题不可选
            dlg._fill_intent_routes()
            combo = dlg.intent_route_combo
            self.assertEqual(_headers(combo), list(HEADERS))
            for t, _d, enabled in _items(combo):
                if t in HEADERS:
                    self.assertFalse(enabled, t)
            groups = _groups(combo)
            for rid, pack in dlg.data["routes"].items():
                expected = (HEADER_IMAGE if pack.get("vision") else
                            HEADER_SOURCE if pack.get("source_from_decision_model")
                            else HEADER_CUSTOM)
                self.assertIn(rid, groups[expected], rid)
            dlg._load_intent("zh.legacy")
            index = combo.findData(CUSTOM_ROUTE)
            combo.setCurrentIndex(index)
            self.assertEqual(combo.currentData(), CUSTOM_ROUTE)
            self.assertEqual(dlg._intent_by_id("zh.legacy")["primary_route"],
                             CUSTOM_ROUTE)
        finally:
            dlg.close()

    def test_e2e_2_names_and_data_fields_untouched(self):
        self.assertEqual(intent_prompt_source({"prompt_override": "写点东西"}),
                         "自定义")
        self.assertEqual(intent_prompt_source({"prompt_override": ""}),
                         "文本模板")
        self.assertEqual(intent_prompt_source({"custom": True}), "自定义")
        raw = next(i for i in load_defaults().as_dict()["intents"]
                   if i["id"] == "image.table")
        before = copy.deepcopy(raw)
        self.assertEqual(intent_prompt_source(raw), "自定义")
        self.assertEqual(raw, before)

    def test_e2e_3_delete_guards_via_real_button(self):
        dlg = _dialog()
        try:
            for rid, message in ((SOURCE_ROUTE,
                                  "提示词模板（与决策模型绑定）不可删除"),
                                 (VISION_ROUTE, "图片识别模板不可删除")):
                index = dlg.route_combo.findData(rid)
                dlg.route_combo.setCurrentIndex(index)
                dlg.del_route_button.click()
                self.assertIn(rid, dlg.data["routes"], rid)
                self.assertIn(message, dlg.legacy_hint.text())
            index = dlg.route_combo.findData(CUSTOM_ROUTE)
            dlg.route_combo.setCurrentIndex(index)
            dlg.del_route_button.click()
            self.assertNotIn(CUSTOM_ROUTE, dlg.data["routes"])
        finally:
            dlg.close()


if __name__ == "__main__":
    unittest.main()
