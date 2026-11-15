"""结果卡片：只读、简洁、可缩放、尺寸位置记忆、不抢焦点。"""
from __future__ import annotations

import html

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QFrame, QMenu, QTextBrowser, QVBoxLayout,
)

from ..models import SearchProvider
from ..studio.theme_tokens import tokens
from .window_base import ResizableWindow


class _ContextForwarder(QObject):
    def __init__(self, window: "ResultWindow"):
        super().__init__(window)
        self.window = window

    def eventFilter(self, _obj, event) -> bool:
        if event.type() == QEvent.Type.ContextMenu:
            self.window.show_context_menu(event.globalPos())
            return True
        return False


class ResultWindow(ResizableWindow):
    change_intent_requested = Signal()
    requery_requested = Signal()
    settings_requested = Signal()
    search_requested = Signal(str, str)
    clipboard_written = Signal(str)
    closed = Signal()

    def __init__(self, providers: tuple[SearchProvider, ...] = (), parent=None):
        super().__init__(settings_key="result", default_size=(520, 420), parent=parent)
        self.providers = providers
        self.setWindowTitle("clip-llm 0.2 · 结果")

        self.view = QTextBrowser(self)
        self.view.setReadOnly(True)
        self.view.setUndoRedoEnabled(False)
        self.view.setOpenLinks(False)
        self.view.setOpenExternalLinks(False)
        self.view.setFrameShape(QFrame.NoFrame)
        self.view.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.view.setFocusPolicy(Qt.NoFocus)
        self.view.setAttribute(Qt.WA_InputMethodEnabled, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.addWidget(self.view, 1)

        forwarder = _ContextForwarder(self)
        for child in (self.view, self.view.viewport()):
            child.installEventFilter(forwarder)
            child.setContextMenuPolicy(Qt.DefaultContextMenu)
        self.watch_children([self.view, self.view.viewport()])

        self.menu = QMenu(self)
        self.search_menu = QMenu("搜索", self.menu)
        self.menu.addMenu(self.search_menu)
        self.copy_selection_action = self.menu.addAction("复制选中内容")
        self.copy_selection_action.triggered.connect(self._copy_selection)
        self.menu.addSeparator()
        self.menu.addAction("换一种解释", self.change_intent_requested.emit)
        self.menu.addAction("重新查询", self.requery_requested.emit)
        self.menu.addAction("复制结果", self._copy_all)
        self.menu.addAction("重置窗口", self.reset_geometry)
        self.menu.addSeparator()
        self.menu.addAction("设置 (F3)", self.settings_requested.emit)
        self.menu.addAction("关闭 (Esc)", self.close)

        self._shortcuts: list[QShortcut] = []
        for key, slot in ((Qt.Key_Escape, self.close),
                          (Qt.Key_F3, self.settings_requested.emit),
                          (Qt.Key_R, self.requery_requested.emit)):
            sc = QShortcut(QKeySequence(key), self)
            sc.activated.connect(slot)
            self._shortcuts.append(sc)
        for seq, slot in (("Ctrl+R", self.requery_requested.emit),
                          ("Ctrl+Shift+C", self._copy_all),
                          ("Ctrl+C", self._copy_selection_or_all)):
            sc = QShortcut(QKeySequence(seq), self)
            sc.activated.connect(slot)
            self._shortcuts.append(sc)

    # -- 内容 --------------------------------------------------------------
    def show_loading(self, text: str, hint: str = "") -> None:
        safe = html.escape((text or "")[:80], quote=False)
        muted = tokens()["text_variant"]
        self.view.setHtml(
            f"<p style='color:{muted}'>正在处理…</p>"
            f"<p>{safe}</p><p><i>{html.escape(hint)}</i></p>")
        self.view.verticalScrollBar().setValue(0)
        self.show_without_focus()

    def show_html(self, body_html: str, *, title: str = "", error: str = "") -> None:
        head = f"<h3>{html.escape(title)}</h3>" if title else ""
        warn = (f"<p style='color:{tokens()['error']}'>{html.escape(error)}</p>"
                if error else "")
        self.view.setHtml(head + warn + body_html)
        self.view.verticalScrollBar().setValue(0)
        self.show_without_focus()

    # -- 选择与复制 --------------------------------------------------------
    def selected_text(self) -> str:
        try:
            return self.view.textCursor().selectedText().strip()
        except (AttributeError, RuntimeError):
            return ""

    def current_plain_text(self) -> str:
        return self.view.toPlainText()

    def _copy_selection(self) -> None:
        text = self.selected_text()
        if text:
            QApplication.clipboard().setText(text)
            self.clipboard_written.emit(text)

    def _copy_all(self) -> None:
        text = self.current_plain_text().strip()
        if text:
            QApplication.clipboard().setText(text)
            self.clipboard_written.emit(text)

    def _copy_selection_or_all(self) -> None:
        if self.selected_text():
            self._copy_selection()
        else:
            self._copy_all()

    # -- 右键菜单 ----------------------------------------------------------
    def show_context_menu(self, global_pos) -> None:
        query = self.selected_text() or self.current_plain_text()[:200]
        self.copy_selection_action.setEnabled(bool(self.selected_text()))
        self.search_menu.clear()
        for provider in self.providers:
            if provider.enabled:
                action = self.search_menu.addAction(provider.label)
                action.triggered.connect(
                    lambda _=False, pid=provider.id, q=query:
                    self.search_requested.emit(pid, q))
        self.search_menu.setEnabled(bool(query) and bool(self.providers))
        self.menu.exec(global_pos)

    def contextMenuEvent(self, event) -> None:
        self.show_context_menu(event.globalPos())

    def closeEvent(self, event) -> None:
        self.save_geometry()
        self.closed.emit()
        super().closeEvent(event)
