"""Ticket 35 / v19 change-2 gates: protect image.ocr from deletion.

RED first, hand-derived from .factory/00-decisions/CHANGE-derived-label-and-ocr-guard-v19.md.
Today image.ocr is a factory intent with an override, so v16's
``is_custom or has_override`` rule lets the real delete button tombstone and
remove it. The spec protects only image.ocr; other image ways stay deletable.

Gates green at RED are explicit regression guards; each is counter-proven by a
temporary production break before the ticket closes. All dialogs are real
offscreen SettingsDialogs driven through the real tree and real delete button;
urlopen is patched everywhere (no real network). The real user config is never
touched.
"""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import load_defaults  # noqa: E402
from clipllm02.settings import Settings  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

PROTECTED = "image.ocr"
OTHER_IMAGE = ("image.table", "image.explain", "image.annotate")
GUARD_FRAGMENT = "不可删除"
GUARD_LABEL = "公式识别"
REMOVED_KEY = "removed_intents"
REAL_CONFIG = Path.home() / ".config" / "clip-llm02" / "settings.json"


def setUpModule():
    global _APP, _REAL_MTIME
    _APP = QApplication.instance() or QApplication([])
    _REAL_MTIME = REAL_CONFIG.stat().st_mtime_ns if REAL_CONFIG.exists() else None


def tearDownModule():
    now = REAL_CONFIG.stat().st_mtime_ns if REAL_CONFIG.exists() else None
    assert now == _REAL_MTIME, "真实用户配置被本 gate 文件改动"


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


class _DialogMixin:
    def setUp(self):
        self._net = patch("urllib.request.urlopen",
                          side_effect=AssertionError("no real network"))
        self._net.start()
        self.addCleanup(self._net.stop)
        self.dlg = SettingsDialog(Settings(load_defaults().as_dict()))
        self.addCleanup(self.dlg.close)

    def ids(self):
        return [i.get("id") for i in self.dlg.data["intents"]]

    def tombstone(self):
        return list(self.dlg.data.get(REMOVED_KEY) or [])

    def select(self, intent_id):
        item = _find_tree_item(self.dlg.prompt_tree, intent_id)
        self.assertIsNotNone(item, f"领域树里找不到 {intent_id}")
        self.dlg.prompt_tree.setCurrentItem(item)
        self.assertEqual(self.dlg._current_intent_id, intent_id)

    def click_delete(self):
        self.dlg.del_intent_button.click()


class TestOcrDeleteGuard(_DialogMixin, unittest.TestCase):
    """Change 2: image.ocr cannot be removed through the real button."""

    def test_ocr_delete_is_blocked(self):
        # RED today: image.ocr is a factory intent with an override -> deleted.
        self.assertIn(PROTECTED, self.ids())
        self.select(PROTECTED)
        self.click_delete()
        self.assertIn(PROTECTED, self.ids(), "公式识别被误删")

    def test_ocr_delete_shows_guard_hint(self):
        # RED today: hint is empty (or the fallback 出厂意图 message).
        self.select(PROTECTED)
        self.click_delete()
        hint = self.dlg.legacy_hint.text()
        self.assertIn(GUARD_FRAGMENT, hint, f"挡删提示不对：{hint}")
        self.assertIn(GUARD_LABEL, hint, f"挡删提示没点名公式识别：{hint}")

    def test_ocr_delete_writes_no_tombstone(self):
        # RED today: the delete path tombstones image.ocr.
        before = self.tombstone()
        self.select(PROTECTED)
        self.click_delete()
        self.assertEqual(self.tombstone(), before,
                         "挡删时不得写墓碑")
        self.assertNotIn(PROTECTED, self.tombstone())


class TestOtherDeletesUnchanged(_DialogMixin, unittest.TestCase):
    """Regression guards (green at RED): only image.ocr is protected."""

    def test_image_table_still_deletable_with_tombstone(self):
        self.select(OTHER_IMAGE[0])
        self.click_delete()
        self.assertNotIn(OTHER_IMAGE[0], self.ids())
        self.assertIn(OTHER_IMAGE[0], self.tombstone())

    def test_other_image_ways_still_deletable(self):
        for intent_id in OTHER_IMAGE[1:]:
            with self.subTest(intent_id=intent_id):
                self.select(intent_id)
                self.click_delete()
                self.assertNotIn(intent_id, self.ids())
                self.assertIn(intent_id, self.tombstone())

    def test_plain_text_template_factory_still_blocked(self):
        intent_id = "zh.legacy"
        self.select(intent_id)
        self.click_delete()
        self.assertIn(intent_id, self.ids())
        self.assertIn("这是出厂意图", self.dlg.legacy_hint.text())

    def test_user_custom_intent_still_deletable_without_tombstone(self):
        data = load_defaults().as_dict()
        data["intents"].append({
            "id": "custom.v19", "label": "票35用户自建", "custom": True,
            "prompt_override": "用户提示词", "primary_route": "通用解析",
            "free_input_only": False, "enabled": True,
        })
        self.dlg.close()
        self.dlg = SettingsDialog(Settings(data))
        self.select("custom.v19")
        self.click_delete()
        self.assertNotIn("custom.v19", self.ids())
        self.assertNotIn("custom.v19", self.tombstone())
