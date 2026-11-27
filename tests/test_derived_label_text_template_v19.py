"""Ticket 35 / v19 change-1 gates: derived query methods display 文本模板.

RED first, hand-derived from .factory/00-decisions/CHANGE-derived-label-and-ocr-guard-v19.md.
Current production order puts ``if custom`` before the derived check, so a
decision-model-derived intent with an empty override is wrongly labelled
自定义. Every expected string below is hand-derived from the spec, not from
the code under test.

Gates green at RED are explicit regression guards; each is counter-proven by a
temporary production break before the ticket closes. All dialogs are real
offscreen SettingsDialogs; urlopen is patched everywhere (no real network).
"""
import copy
import inspect
import os
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import load_defaults  # noqa: E402
from clipllm02.models import intent_prompt_source  # noqa: E402
from clipllm02.settings import Settings, sync_decision_model_templates  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

DERIVED_ID = "英语句子"
OVERRIDE = "票35：这条派生方式改过专属提示词"
TEXT_TEMPLATE = "文本模板"
CUSTOM = "自定义"
FREE_INPUT = "自由输入"


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _NoNetwork:
    def setUp(self):
        self._net = patch("urllib.request.urlopen",
                          side_effect=AssertionError("no real network"))
        self._net.start()
        self.addCleanup(self._net.stop)


def _derived_data(*, override=""):
    """Real B→A derivation path, then optionally user edits the override."""
    data = load_defaults().as_dict()
    before = dict(data.get("decision_model_categories") or {})
    categories = dict(before)
    categories[DERIVED_ID] = "英语句子，需要整句解释"
    data["decision_model_categories"] = categories
    out = sync_decision_model_templates(data, before)
    intent = next(i for i in out["intents"] if i.get("id") == DERIVED_ID)
    intent["prompt_override"] = override
    return out


def _derived_item(*, override=""):
    return next(i for i in _derived_data(override=override)["intents"]
                if i.get("id") == DERIVED_ID)


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


class TestDerivedLabelUnit(_NoNetwork, unittest.TestCase):
    """Change 1: the display decision itself."""

    def test_derived_with_empty_override_is_text_template(self):
        # RED today: returns 自定义 because `if custom` wins over the derived check.
        item = _derived_item()
        self.assertTrue(item.get("custom"), "fixture must be a derived custom intent")
        self.assertEqual(str(item.get("prompt_override") or "").strip(), "")
        self.assertEqual(item.get("derived_template_source"), "decision_model")
        self.assertEqual(intent_prompt_source(item), TEXT_TEMPLATE)

    def test_derived_with_override_is_custom(self):
        # Regression guard (green at RED): an edited derived override stays 自定义.
        item = _derived_item(override=OVERRIDE)
        self.assertEqual(intent_prompt_source(item), CUSTOM)

    def test_plain_custom_without_derived_marker_is_custom(self):
        # Regression guard (green at RED): user-created intents stay 自定义.
        self.assertEqual(intent_prompt_source({"custom": True}), CUSTOM)

    def test_free_input_still_wins(self):
        # Regression guard (green at RED).
        self.assertEqual(intent_prompt_source(
            {"custom": True, "free_input_only": True,
             "derived_template_source": "decision_model"}), FREE_INPUT)

    def test_label_lookup_does_not_mutate_derived_data(self):
        # Regression guard (green at RED).
        item = _derived_item()
        before = copy.deepcopy(item)
        self.assertEqual(intent_prompt_source(item), TEXT_TEMPLATE)
        self.assertEqual(item, before)

    def test_docstring_explains_derived_reuse(self):
        # v19 requires the docstring to be synchronised with the new precedence.
        doc = inspect.getdoc(intent_prompt_source) or ""
        self.assertIn("派生", doc, "docstring 未说明决策模型派生判定")


class TestDerivedLabelE2E(_NoNetwork, unittest.TestCase):
    """Change 1 hard gate ①: the real offscreen dialog tree's 模板 column."""

    def setUp(self):
        super().setUp()
        data = _derived_data()
        data["intents"].append({
            "id": "custom.v19", "label": "用户自建查询方式",
            "custom": True, "prompt_override": "", "primary_route": "通用解析",
            "free_input_only": False, "enabled": True,
        })
        self.dlg = SettingsDialog(Settings(data))
        self.addCleanup(self.dlg.close)

    def _source_column(self, intent_id):
        item = _find_tree_item(self.dlg.prompt_tree, intent_id)
        self.assertIsNotNone(item, f"领域树里找不到 {intent_id}")
        return item.text(1)

    def test_e2e_derived_child_shows_text_template(self):
        self.assertEqual(self._source_column(DERIVED_ID), TEXT_TEMPLATE)

    def test_e2e_derived_with_override_child_shows_custom(self):
        # Regression guard (green at RED).
        item = next(i for i in self.dlg.data["intents"]
                    if i.get("id") == DERIVED_ID)
        item["prompt_override"] = OVERRIDE
        self.dlg._reload_tree()
        self.assertEqual(self._source_column(DERIVED_ID), CUSTOM)

    def test_e2e_plain_custom_child_shows_custom(self):
        # Regression guard (green at RED): no derived marker -> 自定义.
        self.assertEqual(self._source_column("custom.v19"), CUSTOM)
