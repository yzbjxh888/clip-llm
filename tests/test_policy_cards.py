"""Ticket 13 gate tests: 拦截 rules as a compact card list (FR-027 / SC-010).

RED first: the 拦截 tab still uses a 3-column QTreeWidget. No network; Qt is
offscreen. Expectations are hand-derived from ADR-010 / .impeccable.md.
The construct-and-wire checks matter: a card must actually be parented into the
container's layout, not merely instantiated.
"""
import os
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QVBoxLayout  # noqa: E402

from clipllm02 import load_defaults  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog, settings_qss  # noqa: E402

LOCKED_RULES = {"empty", "too_long"}


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class TestPolicyCardStructure(unittest.TestCase):
    def setUp(self):
        self.dialog = SettingsDialog(load_defaults())
        self.rules = self.dialog.data["policy"]["rules"]

    def tearDown(self):
        self.dialog.close()

    def test_tree_widget_is_replaced_by_card_container(self):
        self.assertFalse(hasattr(self.dialog, "policy_tree"),
                         "拦截 tab must no longer use a QTreeWidget")
        self.assertIsInstance(self.dialog.policy_cards.layout(), QVBoxLayout)

    def test_one_card_per_rule_and_all_are_wired_into_layout(self):
        layout = self.dialog.policy_cards.layout()
        wired = [layout.itemAt(i).widget() for i in range(layout.count())]
        wired = [w for w in wired if w is not None]
        self.assertEqual(len(wired), len(self.rules))
        self.assertEqual(set(self.dialog.policy_card_widgets), set(self.rules))
        for card in wired:
            self.assertIs(card.parent(), self.dialog.policy_cards)

    def test_each_card_has_toggle_and_short_description(self):
        for rid, card in self.dialog.policy_card_widgets.items():
            self.assertIsInstance(card.toggle, QCheckBox)
            self.assertIsInstance(card.summary, QLabel)
            self.assertEqual(card.summary.text(),
                             str(self.rules[rid].get("label") or rid))
            self.assertEqual(card.toggle.isChecked(),
                             self.rules[rid].get("enabled", True) is not False)

    def test_card_tooltip_carries_full_description(self):
        for rid, card in self.dialog.policy_card_widgets.items():
            self.assertEqual(card.toolTip(),
                             str(self.rules[rid].get("description") or ""))

    def test_cards_are_uniform_and_compact(self):
        heights = {card.minimumHeight()
                   for card in self.dialog.policy_card_widgets.values()}
        self.assertEqual(len(heights), 1, "all cards must share one height")
        self.assertLessEqual(next(iter(heights)), 52, "cards must stay compact")


class TestPolicyCardBehavior(unittest.TestCase):
    def setUp(self):
        self.dialog = SettingsDialog(load_defaults())
        self.rules = self.dialog.data["policy"]["rules"]

    def tearDown(self):
        self.dialog.close()

    def test_locked_rules_cannot_be_switched_off_and_look_different(self):
        for rid in LOCKED_RULES:
            card = self.dialog.policy_card_widgets[rid]
            self.assertFalse(card.toggle.isEnabled())
            self.assertTrue(card.locked)
            self.assertEqual(card.objectName(), "policyCardLocked")
            self.assertTrue(card.toggle.isChecked())

    def test_unlocked_rule_toggle_writes_through(self):
        rid = "url"
        card = self.dialog.policy_card_widgets[rid]
        self.assertTrue(card.toggle.isEnabled())
        card.toggle.setChecked(False)
        self.assertIs(self.rules[rid]["enabled"], False)

    def test_locked_rule_toggle_never_writes_through(self):
        rid = "empty"
        card = self.dialog.policy_card_widgets[rid]
        before = dict(self.rules[rid])
        card.toggle.setChecked(False)   # disabled control; signal may still fire
        self.assertEqual(self.rules[rid], before)

    def test_selecting_a_card_loads_its_edit_form(self):
        card = self.dialog.policy_card_widgets["url"]
        card.selected.emit("url")
        self.assertEqual(self.dialog._current_policy_id, "url")
        self.assertEqual(self.dialog.policy_desc.text(),
                         str(self.rules["url"].get("description") or ""))

    def test_add_and_delete_rule_rebuild_cards(self):
        self.dialog._add_policy_rule()
        rid = self.dialog._current_policy_id
        self.assertIn(rid, self.dialog.policy_card_widgets)
        self.dialog._delete_policy_rule()
        self.assertNotIn(rid, self.dialog.policy_card_widgets)

    def test_cards_use_tokenized_object_names_not_hardcoded_styles(self):
        # Every card carries a semantic object name; styling lives in theme_tokens.
        for card in self.dialog.policy_card_widgets.values():
            self.assertIn(card.objectName(), ("policyCard", "policyCardLocked"))


class TestPolicyCardCritiqueFixes(unittest.TestCase):
    """P1 fixes from the impeccable critique: keyboard, feedback, discoverability."""

    def setUp(self):
        self.dialog = SettingsDialog(load_defaults())
        self.rules = self.dialog.data["policy"]["rules"]

    def tearDown(self):
        self.dialog.close()

    def test_cards_are_keyboard_focusable_and_arrow_navigable(self):
        card = self.dialog.policy_card_widgets["empty"]
        self.assertEqual(card.focusPolicy(), Qt.StrongFocus)
        card.setFocus()
        card.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right,
                                     Qt.NoModifier))
        self.assertEqual(self.dialog._current_policy_id, "no_letters")

    def test_card_exposes_accessible_name_and_description(self):
        card = self.dialog.policy_card_widgets["url"]
        self.assertIn("只是一个网址", card.accessibleName())
        self.assertEqual(card.accessibleDescription(),
                         str(self.rules["url"].get("description") or ""))

    def test_focus_and_selection_have_visible_qss(self):
        qss = settings_qss(False)
        self.assertIn('QFrame#policyCard:focus', qss)
        self.assertIn('QFrame#policyCard[selected="true"]', qss)
        self.assertIn('QFrame#policyCardLocked:hover', qss)

    def test_card_shows_persistent_one_line_detail(self):
        for rid, card in self.dialog.policy_card_widgets.items():
            self.assertIsInstance(card.detail, QLabel)
            self.assertEqual(card.detail.text(),
                             str(self.rules[rid].get("description") or "")[:40])
            self.assertGreaterEqual(len(card.detail.text()), 1)

    def test_toggle_hit_target_is_at_least_24px(self):
        for card in self.dialog.policy_card_widgets.values():
            self.assertGreaterEqual(card.toggle.minimumHeight(), 24)

    def test_delete_feedback_shows_in_visible_status_and_is_undoable(self):
        self.dialog._add_policy_rule()
        rid = self.dialog._current_policy_id
        before_legacy = self.dialog.legacy_hint.text()
        self.dialog._delete_policy_rule()
        self.assertIn(rid, self.dialog.status_hint.text())
        self.assertEqual(self.dialog.legacy_hint.text(), before_legacy)
        self.assertTrue(not self.dialog.policy_undo_button.isHidden())
        self.dialog.policy_undo_button.click()
        self.assertIn(rid, self.dialog.data["policy"]["rules"])
        self.assertNotIn(rid, self.dialog.data["policy"].get("removed_rules") or [])
        self.assertTrue(self.dialog.policy_undo_button.isHidden())


if __name__ == "__main__":
    unittest.main()
