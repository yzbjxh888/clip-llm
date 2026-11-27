"""Ticket 33 gate tests: concise knowledge-base intro copy (v17).

RED first. Expected values are hand-derived from
.factory/00-decisions/CHANGE-library-intro-short-v17.md — never from the code
under test.

Baseline (814 passed): the 知识库 page intro QLabel carries the verbose
~130-char copy. The two text gates below must fail for that named reason.
wordWrap is a deliberate regression guard: green at RED, then counter-proven
by a temporary production break before the ticket closes.

All dialogs are real offscreen SettingsDialog instances. urlopen is patched:
these tests must never make a real network call. The real user config is never
touched (temp XDG_CONFIG_HOME).
"""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402
from clipllm02 import load_defaults  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402

# Hand-derived verbatim from the v17 decision doc, section 三.
EXPECTED_INTRO = (
    "查询结果按「库根/分类/日期_标题.md」存成 Markdown，可用 Obsidian / Logseq 打开。\n"
    "自定义查询归「通用」，追问追加到同日笔记。"
)
# Old sentences the v17 change must eliminate (decision doc section 五).
OLD_FRAGMENTS = (
    "每次查询成功后",
    "LaTeX 公式 / 化学物质",
    "同名文件直接覆盖",
)


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _SafeEnv(unittest.TestCase):
    """No real network, no real user config."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="lib-intro-v17-")
        self._xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        self._net = patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("no real network"),
        )
        self._net.start()

    def tearDown(self):
        self._net.stop()
        if self._xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._xdg
        self._tmp.cleanup()
        super().tearDown()


class TestLibraryIntro(_SafeEnv):
    """The 知识库 page intro QLabel via a real offscreen dialog."""

    def setUp(self):
        super().setUp()
        self.dlg = SettingsDialog(load_defaults())

    def tearDown(self):
        self.dlg.close()
        super().tearDown()

    def _intro(self):
        # library_tab is a QScrollArea (_scroll); the page is its widget and
        # the intro QLabel is the first widget in the page's QVBoxLayout.
        return self.dlg.library_tab.widget().layout().itemAt(0).widget()

    def test_intro_text_is_concise_verbatim(self):
        self.assertEqual(self._intro().text(), EXPECTED_INTRO)

    def test_intro_drops_verbose_old_sentences(self):
        text = self._intro().text()
        for fragment in OLD_FRAGMENTS:
            self.assertNotIn(fragment, text, f"旧文案残留: {fragment}")

    def test_intro_word_wrap_still_enabled(self):
        self.assertTrue(self._intro().wordWrap())
