"""统一主窗口：查询栏 + 小图标工具栏 + 标签页 + 可配置快捷键。

顶栏只保留一个主查询按钮和五个小图标（搜索 / 新标签 / 图片 / 设置 / 输入），
紧凑不占空间；网页搜索移到顶部，与查询、新标签并列。
"""
from __future__ import annotations

import math
import time
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QIcon, QKeySequence, QPainter, QPalette,
                           QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton,
    QTabWidget, QToolButton, QVBoxLayout,
)

from ..desktop.window_base import ResizableWindow
from .config import DEFAULT_HOTKEYS
from .query_tab import IME_GRACE, ImeWatcher, QueryTab
from .theme_tokens import build_qss, tokens


def _draw_icon(kind: str, size: int = 18, color: str | None = None) -> QIcon:
    """自绘线条图标：不依赖系统图标主题，尺寸与颜色可控。"""
    painter_color = color or tokens()["text"]
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor(painter_color), 1.6, Qt.SolidLine,
                        Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    center = size / 2.0
    if kind == "find":                      # 放大镜：查询
        painter.drawEllipse(QRectF(1.6, 1.6, 10.2, 10.2))
        painter.drawLine(QPointF(10.8, 10.8), QPointF(16.2, 16.2))
    elif kind == "globe":                   # 地球：搜索（与放大镜区分）
        painter.drawEllipse(QRectF(1.6, 1.6, 14.8, 14.8))
        painter.drawEllipse(QRectF(6.2, 1.6, 5.6, 14.8))
        painter.drawLine(QPointF(1.6, center), QPointF(16.4, center))
    elif kind == "plus":                    # 加号：新标签
        painter.drawLine(QPointF(center, 2.6), QPointF(center, 15.4))
        painter.drawLine(QPointF(2.6, center), QPointF(15.4, center))
    else:                                   # 齿轮：设置
        painter.drawEllipse(QRectF(5.4, 5.4, 7.2, 7.2))
        for index in range(8):
            angle = math.radians(index * 45)
            painter.drawLine(
                QPointF(center + 5.9 * math.cos(angle),
                        center + 5.9 * math.sin(angle)),
                QPointF(center + 8.2 * math.cos(angle),
                        center + 8.2 * math.sin(angle)))
    painter.end()
    return QIcon(pixmap)


def app_icon() -> QIcon:
    """应用图标：源码旁 assets/png 优先，其次系统 hicolor 安装。

    ``__file__`` 是 ``<root>/src/clipllm02/studio/main_window.py``，
    项目根在 ``parents[3]``（studio → clipllm02 → src → root）。
    """
    root = Path(__file__).resolve().parents[3]
    for candidate in (
        root / "assets" / "png" / "clipllm02-256.png",
        Path("/usr/share/icons/hicolor/256x256/apps/clipllm02.png"),
    ):
        if candidate.exists():
            return QIcon(str(candidate))
    return QIcon.fromTheme("clipllm02")


class MainWindow(ResizableWindow):
    query_submitted = Signal(str)
    new_tab_requested = Signal(str)
    settings_requested = Signal()
    vision_image_requested = Signal()
    open_input_requested = Signal()
    search_provider_requested = Signal(str)   # 顶栏搜索：provider id
    requery_requested = Signal()
    back_requested = Signal()
    window_hidden = Signal()
    tab_changed = Signal(object)
    tab_created = Signal(object)

    def __init__(self, settings, parent=None):
        # 窗口类型只能在构造时决定：运行时改会重建 X 窗口（闪烁 / fcitx 重置）
        persistent = bool(settings.get("desktop_persistent_window", True))
        super().__init__(settings_key="studio", default_size=(780, 680),
                         parent=parent, persistent=persistent)
        self.settings = settings
        self.setWindowTitle("clip-llm · 内容解释")
        self._dark = bool(settings.get("desktop_dark_theme", False))
        self._tabs: list[QueryTab] = []
        self._shortcuts: list[QShortcut] = []
        self._ime_last = 0.0           # 最近一次拼音组词 / 打字的时刻
        self._ime_composing = False    # 是否正处于输入法组词中
        self._ime_watcher = None
        self._input_active = None      # 由 app 注入：独立输入进程是否活着

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(9)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.query_edit = QLineEdit()
        self.query_edit.setObjectName("query")
        self.query_edit.setFixedHeight(34)        # 与右侧 34×34 图标按钮等高
        self.query_edit.setPlaceholderText("粘贴或输入内容后回车查询…")
        self.query_edit.returnPressed.connect(self._submit_query)
        self._ime_watcher = ImeWatcher(self, self.query_edit)
        bar.addWidget(self.query_edit, 1)
        def icon_action(kind: str, tooltip: str, slot) -> QToolButton:
            """无外框图标的工具栏按钮，尺寸与查询框一致（34×34）。"""
            button = QToolButton()
            button.setObjectName("barAction")
            button.setProperty("icon_kind", kind)
            button.setToolTip(tooltip)
            button.setAutoRaise(True)                 # 去掉外框
            button.setFixedSize(34, 34)
            button.setIconSize(QSize(18, 18))
            button.setIcon(_draw_icon(kind))
            button.clicked.connect(slot)
            return button

        self.query_button = icon_action(
            "find", "查询（回车同效）", self._submit_query)
        bar.addWidget(self.query_button)

        self.search_button = icon_action(
            "globe", "用浏览器搜索当前内容", lambda: None)
        self.search_menu = QMenu(self)
        self.search_button.setMenu(self.search_menu)
        self.search_button.setPopupMode(QToolButton.InstantPopup)
        bar.addWidget(self.search_button)
        self.new_tab_button = icon_action(
            "plus", "在新标签查询", self._new_tab_from_bar)
        bar.addWidget(self.new_tab_button)
        # 定稿：顶栏只留查询 / 搜索 / 新标签三个图标。设置仍由 F3 与托盘进入，
        # 按钮对象与信号保留（隐藏而非删除），不改动任何连接。
        self.settings_button = icon_action("gear", "设置 (F3)",
                                           self.settings_requested.emit)
        self.settings_button.setVisible(False)
        bar.addWidget(self.settings_button)
        root.addLayout(bar)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setTabBarAutoHide(True)   # 单标签自动隐藏页签；多标签可点选切换
        # 一体化：不显示那条带左右箭头的横向标签导航条。
        # 多标签切换仍可用 Ctrl+Tab，关闭用各页内「关闭」/Esc，功能不丢。
        self.tabs.tabBar().hide()
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs, 1)

        self.new_tab("")
        self.apply_hotkeys()
        # 窗口未激活时 QShortcut 收不到 Esc；基座用 X11 键位图兜底后发到这里
        self.dismiss_requested.connect(self._on_dismiss)
        self.set_auto_hide(bool(settings.get("desktop_auto_hide", True)))
        self.watch_children([self.query_edit, self.tabs])
        self.apply_child_cursors()
        self.apply_theme(self._dark)

    # -- 快捷键 ------------------------------------------------------------
    def hotkeys(self) -> dict:
        merged = dict(DEFAULT_HOTKEYS)
        merged.update(self.settings.get("hotkeys") or {})
        return merged

    def apply_hotkeys(self) -> None:
        """设置保存后重新绑定，全部来自配置。"""
        for shortcut in self._shortcuts:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self._shortcuts = []
        keys = self.hotkeys()

        def bind(action: str, slot) -> None:
            sequence = QKeySequence(str(keys.get(action, "") or ""))
            if sequence.isEmpty():
                return
            shortcut = QShortcut(sequence, self)
            shortcut.setContext(Qt.ApplicationShortcut)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

        bind("dismiss", self._on_dismiss)
        bind("back_to_detect", self.back_requested.emit)
        bind("new_tab", self._new_tab_from_bar)
        bind("close_tab", lambda: self._close_tab(self.tabs.currentIndex()))
        bind("open_input", self.open_input_requested.emit)
        bind("settings", self.settings_requested.emit)
        bind("vision", self.vision_image_requested.emit)
        bind("requery", self.requery_requested.emit)
        bind("search", self._search_default)
        bind("switch_tab", self._cycle_tab)

    def set_input_activity_check(self, fn) -> None:
        """由 app 注入：独立输入进程（Ctrl+I）是否仍在运行。"""
        self._input_active = fn

    def _query_edit_veto(self) -> bool:
        """顶栏查询框：组词中不关；有文字时第一次 Esc 只清空。"""
        if QApplication.focusWidget() is not self.query_edit:
            return False
        if time.monotonic() - self._ime_last < IME_GRACE:
            return True
        if self.query_edit.text().strip():
            self.query_edit.clear()
            return True
        return False

    def _on_dismiss(self) -> None:
        """Esc：结果页回初判；初判页隐藏窗口（不是退出进程）。

        三种 Esc 入口（全局快捷键、X11 键位图兜底、页面关闭按钮）都汇到这里。
        拼音组词 / 输入框有字 / 独立输入进程活跃时，Esc 属于输入法而不是窗口。
        """
        if callable(self._input_active) and self._input_active():
            return
        if self._query_edit_veto():
            return
        tab = self.current_tab()
        if tab is not None and tab.esc_veto():
            return
        if tab is None or not tab.dismiss():
            self.hide()
            self.window_hidden.emit()

    def _search_default(self) -> None:
        pid = str(self.settings.get("search_default_provider", "google"))
        self.search_provider_requested.emit(pid)

    # -- 搜索菜单（由主控按设置填充） --------------------------------------
    def fill_search_menu(self, providers) -> None:
        self.search_menu.clear()
        for provider in providers:
            action = self.search_menu.addAction(provider.label)
            action.triggered.connect(
                lambda _=False, pid=provider.id:
                self.search_provider_requested.emit(pid))
        self.search_button.setEnabled(bool(providers))

    # -- 标签管理 ----------------------------------------------------------
    def new_tab(self, text: str = "", *, switch: bool = True) -> QueryTab:
        tab = QueryTab(self.settings)
        self._tabs.append(tab)
        index = self.tabs.addTab(tab, self._tab_title(text))
        if switch:
            self.tabs.setCurrentIndex(index)
        if text:
            tab.preview.setText(text[:120])
        self.tab_created.emit(tab)
        return tab

    @staticmethod
    def _tab_title(text: str) -> str:
        clean = (text or "").strip().replace("\n", " ")
        return (clean[:14] + "…") if len(clean) > 14 else (clean or "新查询")

    def _close_tab(self, index: int) -> None:
        if self.tabs.count() <= 1:
            return                          # 至少保留一个标签
        widget = self.tabs.widget(index)
        if isinstance(widget, QueryTab) and widget in self._tabs:
            self._tabs.remove(widget)
        self.tabs.removeTab(index)
        widget.deleteLater()

    def _on_tab_changed(self, _index: int) -> None:
        self.tab_changed.emit(self.current_tab())
        self._sync_window_title()

    def _cycle_tab(self) -> None:
        count = self.tabs.count()
        if count > 1:
            self.tabs.setCurrentIndex(
                (self.tabs.currentIndex() + 1) % count)

    def _sync_window_title(self) -> None:
        tab = self.tabs.currentWidget()
        name = self._tab_title(tab.current_text()) if isinstance(tab, QueryTab) else ""
        position = f"{self.tabs.currentIndex() + 1}/{self.tabs.count()}"
        self.setWindowTitle(f"clip-llm · {name or '内容解释'} ({position})")

    def current_tab(self) -> QueryTab:
        widget = self.tabs.currentWidget()
        if not isinstance(widget, QueryTab):
            return self.new_tab("", switch=True)
        return widget

    def _submit_query(self) -> None:
        text = self.query_edit.text().strip()
        if text:
            self.query_submitted.emit(text)

    def _new_tab_from_bar(self) -> None:
        text = self.query_edit.text().strip() or self.current_tab().current_text()
        tab = self.new_tab(text, switch=True)
        if text:
            self.query_submitted.emit(text)
        else:
            tab.preview.setText("等待输入…")

    # -- 由主控调用 --------------------------------------------------------
    def connect_tab(self, tab: QueryTab, handlers: dict) -> None:
        for name, slot in handlers.items():
            signal = getattr(tab, name, None)
            if signal is not None:
                signal.connect(slot)

    def apply_context(self, env, policy, suggestion, menu, *,
                      new_tab: bool = False) -> QueryTab:
        if new_tab or self.tabs.count() == 0:
            tab = self.new_tab(env.text or "", switch=True)
        else:
            tab = self.current_tab()
        tab.set_context(env, policy, suggestion, menu)
        index = self.tabs.indexOf(tab)
        if index >= 0:
            self.tabs.setTabText(index, self._tab_title(env.text or ""))
        self.query_edit.setText((env.text or "")[:200])
        self.apply_child_cursors()     # 动态创建的意图按钮也要显式光标
        return tab

    def apply_theme(self, dark: bool) -> None:
        """整体切换窗口 QSS / 底色 / 自绘图标颜色；不动任何信号与行为。"""
        self._dark = bool(dark)
        self.setStyleSheet(build_qss(self._dark))
        palette = self.palette()
        palette.setColor(QPalette.Window, QColor(tokens(self._dark)["background"]))
        self.setPalette(palette)
        icon_color = tokens(self._dark)["text"]
        for button in self.findChildren(QToolButton):
            if button.objectName() == "barAction":
                kind = str(button.property("icon_kind") or "find")
                button.setIcon(_draw_icon(kind, color=icon_color))

    def set_look(self, font_size: int, dark: bool) -> None:
        self.apply_theme(dark)
        for tab in self._tabs:
            tab.set_look(font_size, dark)

    def set_settings(self, settings) -> None:
        self.settings = settings
        for tab in self._tabs:
            tab.settings = settings
        self.set_look(int(settings.get("desktop_font_size", 14)),
                      bool(settings.get("desktop_dark_theme", False)))
        self.apply_hotkeys()
        self.set_auto_hide(bool(settings.get("desktop_auto_hide", True)))
        # desktop_persistent_window 决定窗口类型，只在构造时读取；此处绝不
        # 改 windowFlags()，否则会重建 X 窗口并触发既有的闪烁 / 输入法问题。
