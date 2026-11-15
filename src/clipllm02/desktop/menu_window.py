"""意图菜单窗口：简洁、不抢焦点、可缩放、尺寸位置可记忆。

性能与稳定性要点（对应六个反馈问题）：
- 所有控件**只创建一次**，``_render()`` 只改文本与可见性，不再重建/销毁控件，
  彻底消除 ``deleteLater()`` 导致的旧控件残留、访问已删除 C++ 对象的闪退与卡顿；
- 不调用 ``activateWindow()`` / ``raise_()``，自动弹出绝不抢焦点；
- 只显示该领域常用的查询类型，通用兜底放到「更多」里；
- 次要信息（呈现形态、其他角度、置灰项、自由输入）默认折叠。
"""
from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPushButton, QRadioButton, QVBoxLayout,
)

from ..models import InputEnvelope, MenuModel, PolicyResult, Suggestion
from ..studio.theme_tokens import RADIUS_DEFAULT, SPACING_XS, tokens
from .window_base import ResizableWindow

MAX_INTENTS = 24
MAX_OTHERS = 12
FORMATS = ("static", "table", "interactive", "annotate")


class MenuWindow(ResizableWindow):
    """复制内容后弹出的意图菜单；用户选择前不产生任何模型请求。"""

    selection_made = Signal(object)
    cancelled = Signal()
    settings_requested = Signal()
    search_requested = Signal(str, str)
    clipboard_written = Signal(str)
    domain_changed = Signal(str)

    def __init__(self, settings, envelope: InputEnvelope, policy: PolicyResult,
                 suggestion: Suggestion, menu: MenuModel, parent=None):
        super().__init__(settings_key="menu", default_size=(520, 430), parent=parent)
        self.settings = settings
        self.envelope = envelope
        self.policy = policy
        self.suggestion = suggestion
        self.menu = menu
        self._active_domain = menu.domain_chips[0].id if menu.domain_chips else "*"
        self._shortcuts: list[QShortcut] = []

        self.setWindowTitle("clip-llm 0.2 · 意图菜单")
        self.setObjectName("clipLlmCard")

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(8)

        # 头部：预览 + 档位 + 换领域 + 关闭
        head = QHBoxLayout()
        self.header = QLabel()
        self.header.setObjectName("muted")
        self.header.setWordWrap(False)
        head.addWidget(self.header, 1)
        self.detection_label = QLabel()
        self.detection_label.setObjectName("chip")
        head.addWidget(self.detection_label)
        self.domain_combo = QComboBox()
        self.domain_combo.setToolTip("换领域")
        self.domain_combo.currentIndexChanged.connect(self._on_domain_combo)
        head.addWidget(self.domain_combo)
        self.close_button = QPushButton("×")
        self.close_button.setObjectName("tool")
        self.close_button.setFixedWidth(24)
        self.close_button.clicked.connect(self._cancel)
        head.addWidget(self.close_button)
        root.addLayout(head)

        self.warning_label = QLabel()
        self.warning_label.setObjectName("muted")
        self.warning_label.setWordWrap(True)
        t = tokens()
        self.warning_label.setStyleSheet(
            "QLabel {{ background:{0}; color:{1}; padding:{2}px; "
            "border-radius:{3}px; }}".format(
                t["warning_container"], t["warning_text"],
                SPACING_XS, RADIUS_DEFAULT))
        root.addWidget(self.warning_label)

        # 主区：领域专属查询类型（控件预创建，复用）
        self.recommended_box = QFrame()
        self.recommended_layout = QVBoxLayout(self.recommended_box)
        self.recommended_layout.setContentsMargins(0, 0, 0, 0)
        self.recommended_layout.setSpacing(6)
        self._intent_buttons: list[QPushButton] = []
        for idx in range(MAX_INTENTS):
            button = QPushButton()
            button.setObjectName("intent")
            button.setProperty("intent_index", idx)
            button.clicked.connect(lambda _=False, i=idx: self._submit_index(i + 1))
            button.setVisible(False)
            self._intent_buttons.append(button)
            self.recommended_layout.addWidget(button)
        self.recommended_layout.addStretch(1)
        root.addWidget(self.recommended_box, 1)

        # 折叠区：其他角度 + 置灰说明 + 呈现形态
        self.other_toggle = QPushButton("更多选项 ▸")
        self.other_toggle.setObjectName("tool")
        self.other_toggle.setCheckable(True)
        self.other_toggle.toggled.connect(self._on_toggle_others)
        root.addWidget(self.other_toggle)

        self.other_box = QFrame()
        self.other_layout = QVBoxLayout(self.other_box)
        self.other_layout.setContentsMargins(6, 4, 6, 4)
        self.other_layout.setSpacing(4)
        self._other_buttons: list[QPushButton] = []
        self._other_options: list = []
        for idx in range(MAX_OTHERS):
            button = QPushButton()
            button.setObjectName("tool")
            button.setVisible(False)
            button.clicked.connect(lambda _=False, i=idx: self._submit_other(i))
            self._other_buttons.append(button)
            self.other_layout.addWidget(button)
        self.grayed_label = QLabel()
        self.grayed_label.setObjectName("muted")
        self.grayed_label.setWordWrap(True)
        self.other_layout.addWidget(self.grayed_label)

        self.format_box = QFrame()
        self.format_layout = QHBoxLayout(self.format_box)
        self.format_layout.setContentsMargins(0, 2, 0, 2)
        self._format_buttons: list[QRadioButton] = []
        for fmt in FORMATS:
            radio = QRadioButton(fmt)
            radio.setProperty("format_id", fmt)
            radio.setVisible(False)
            self._format_buttons.append(radio)
            self.format_layout.addWidget(radio)
        self.format_layout.addStretch(1)
        self.other_layout.addWidget(self.format_box)
        root.addWidget(self.other_box)
        self.other_box.setVisible(False)

        # 自由输入（默认折叠）
        self.free_box = QFrame()
        free_layout = QVBoxLayout(self.free_box)
        free_layout.setContentsMargins(0, 2, 0, 2)
        free_layout.setSpacing(5)
        row = QHBoxLayout()
        self.free_edit = QLineEdit()
        self.free_edit.setPlaceholderText("描述你想要什么…")
        # 未展开时不向合成器注册 text-input surface，避免焦点串扰；
        # 用户主动点「自定义」时再启用输入法。
        self.free_edit.setAttribute(Qt.WA_InputMethodEnabled, False)
        self.free_edit.returnPressed.connect(self._submit_free)
        row.addWidget(self.free_edit, 1)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("描述需求", "instruction")
        self.mode_combo.addItem("写提示词", "prompt")
        row.addWidget(self.mode_combo)
        self.style_combo = QComboBox()
        self.style_combo.addItem("风格：默认", "")
        row.addWidget(self.style_combo)
        self.free_send = QPushButton("发送")
        self.free_send.setObjectName("tool")
        self.free_send.clicked.connect(self._submit_free)
        row.addWidget(self.free_send)
        free_layout.addLayout(row)
        root.addWidget(self.free_box)
        self.free_box.setVisible(False)

        # 工具行
        tools = QHBoxLayout()
        self.free_toggle = QPushButton("自定义")
        self.free_toggle.setObjectName("tool")
        self.free_toggle.setCheckable(True)
        self.free_toggle.toggled.connect(self._on_toggle_free)
        tools.addWidget(self.free_toggle)
        self.search_button = QPushButton("搜索")
        self.search_button.setObjectName("tool")
        self.search_menu = QMenu(self)
        self.search_button.setMenu(self.search_menu)
        tools.addWidget(self.search_button)
        self.settings_button = QPushButton("设置")
        self.settings_button.setObjectName("tool")
        self.settings_button.clicked.connect(self.settings_requested.emit)
        tools.addWidget(self.settings_button)
        self.reset_button = QPushButton("重置窗口")
        self.reset_button.setObjectName("tool")
        self.reset_button.clicked.connect(self.reset_geometry)
        tools.addWidget(self.reset_button)
        tools.addStretch(1)
        self.footer = QLabel("1-9 选择 · ⏎ 默认 · Esc 关闭")
        self.footer.setObjectName("muted")
        tools.addWidget(self.footer)
        root.addLayout(tools)

        self.watch_children([self.recommended_box, self.other_box,
                             self.free_box, self.warning_label,
                             self.grayed_label, self.footer])
        self._render()
        self._bind_shortcuts()

    # -- 渲染（只改属性，不重建控件）--------------------------------------
    def update_model(self, envelope: InputEnvelope, policy: PolicyResult,
                     suggestion: Suggestion, menu: MenuModel) -> None:
        self.envelope = envelope
        self.policy = policy
        self.suggestion = suggestion
        self.menu = menu
        self._active_domain = menu.domain_chips[0].id if menu.domain_chips else "*"
        self._render()

    def _render(self) -> None:
        preview = (self.envelope.text or "").replace("\n", " ")[:48]
        if self.settings.get("desktop_hide_preview", False):
            preview = "（预览已隐藏）"
        self.header.setText(f"{self.envelope.modality} · {len(self.envelope.text or '')} 字  "
                            f"{html.escape(preview)}")
        self.detection_label.setText(f"{self.menu.level} · "
                                     + " / ".join(c.label.split(" / ")[0]
                                                  for c in self.menu.domain_chips[:2]))

        self.domain_combo.blockSignals(True)
        self.domain_combo.clear()
        for chip in self.menu.domain_chips:
            self.domain_combo.addItem(chip.label, chip.id)
        self.domain_combo.blockSignals(False)
        self.domain_combo.setHidden(self.menu.level == "HIGH" and
                                    len(self.menu.domain_chips) <= 1)

        warnings = list(self.policy.warnings) + list(self.policy.confirmations)
        self.warning_label.setVisible(bool(warnings))
        self.warning_label.setText("⚠ " + "；".join(warnings) if warnings else "")

        for idx, button in enumerate(self._intent_buttons):
            if idx < len(self.menu.recommended):
                option = self.menu.recommended[idx]
                button.setText(f"  {idx + 1}   {option.intent.label}")
                button.setToolTip(option.intent.hint or option.intent.id)
                is_default = self.menu.default_intent == option.intent.id
                button.setProperty("default", "true" if is_default else "false")
                button.style().unpolish(button)
                button.style().polish(button)
                button.setVisible(True)
            else:
                button.setVisible(False)

        self._other_options = list(self.menu.others[:MAX_OTHERS])
        for idx, button in enumerate(self._other_buttons):
            if idx < len(self._other_options):
                option = self._other_options[idx]
                button.setText(option.intent.label)
                button.setToolTip(option.intent.hint or "")
                button.setVisible(True)
            else:
                button.setVisible(False)
        self.grayed_label.setText(
            f"{len(self.menu.grayed)} 项在设置中关闭或不适用"
            if self.menu.grayed else "")
        self.other_toggle.setVisible(bool(self.menu.others or self.menu.grayed
                                          or len(self.menu.formats) > 1))
        self.other_toggle.setText("更多选项 ▾" if self.other_toggle.isChecked()
                                  else "更多选项 ▸")
        self.other_box.setVisible(self.other_toggle.isChecked())

        formats = self.menu.formats or ("static",)
        for radio in self._format_buttons:
            fmt = str(radio.property("format_id"))
            radio.setVisible(fmt in formats)
            if fmt == formats[0]:
                radio.setChecked(True)

        self.style_combo.blockSignals(True)
        self.style_combo.clear()
        self.style_combo.addItem("风格：默认", "")
        for route_id in self.menu.free_input.get("style_refs", [])[:6]:
            self.style_combo.addItem(route_id, route_id)
        self.style_combo.blockSignals(False)

        self.search_menu.clear()
        for provider in self.menu.tools[:6]:
            action = self.search_menu.addAction(provider.label)
            action.triggered.connect(
                lambda _=False, pid=provider.id:
                self.search_requested.emit(pid, self.envelope.text or ""))
        self.search_button.setEnabled(bool(self.menu.tools))
        self.footer.setVisible(self.settings.get("desktop_show_hints", True))

    # -- 交互 --------------------------------------------------------------
    def current_format(self) -> str:
        for button in self._format_buttons:
            if button.isVisible() and button.isChecked():
                return str(button.property("format_id"))
        return "static"

    def _submit_other(self, index: int) -> None:
        if 0 <= index < len(self._other_options):
            self._submit_intent(self._other_options[index])

    def _submit_intent(self, option) -> None:
        intent = option.intent
        if intent.free_input_only or not intent.primary_route:
            self._open_free()
            return
        self._emit(intent_id=intent.id,
                   route_ids=(intent.primary_route,) if intent.primary_route else ())

    def _submit_free(self) -> None:
        custom = self.free_edit.text().strip()
        if not custom:
            return
        self._emit(intent_id=None, custom=custom,
                   mode=str(self.mode_combo.currentData()),
                   style_ref=str(self.style_combo.currentData()) or None)

    def _emit(self, *, intent_id, custom=None, mode="instruction",
              style_ref=None, route_ids=()) -> None:
        self.selection_made.emit({
            "envelope_id": self.envelope.envelope_id,
            "domain_id": self._active_domain,
            "intent_id": intent_id,
            "format_id": self.current_format(),
            "custom_instruction": custom,
            "free_input_mode": mode,
            "style_ref": style_ref,
            "allowed_route_ids": list(route_ids),
        })

    def _open_free(self) -> None:
        self.free_toggle.setChecked(True)
        self.free_box.setVisible(True)
        self.free_edit.setAttribute(Qt.WA_InputMethodEnabled, True)
        self.free_edit.setFocus()

    def _cancel(self) -> None:
        self.hide()
        self.cancelled.emit()

    def _on_toggle_others(self, checked: bool) -> None:
        self.other_box.setVisible(checked)
        self.other_toggle.setText("更多选项 ▾" if checked else "更多选项 ▸")

    def _on_toggle_free(self, checked: bool) -> None:
        self.free_box.setVisible(checked)

    def _on_domain_combo(self, _index: int) -> None:
        domain_id = self.domain_combo.currentData()
        if domain_id and domain_id != self._active_domain:
            self.domain_changed.emit(str(domain_id))

    def _bind_shortcuts(self) -> None:
        for idx in range(1, 10):
            sc = QShortcut(QKeySequence(str(idx)), self)
            sc.activated.connect(lambda i=idx: self._submit_index(i))
            self._shortcuts.append(sc)
            sc2 = QShortcut(QKeySequence(f"Ctrl+{idx}"), self)
            sc2.activated.connect(lambda i=idx: self._select_domain(i))
            self._shortcuts.append(sc2)
        enter = QShortcut(QKeySequence(Qt.Key_Return), self)
        enter.activated.connect(self._submit_default)
        escape = QShortcut(QKeySequence(Qt.Key_Escape), self)
        escape.activated.connect(self._cancel)
        f3 = QShortcut(QKeySequence(Qt.Key_F3), self)
        f3.activated.connect(self.settings_requested.emit)
        self._shortcuts.extend((enter, escape, f3))

    def _submit_index(self, index: int) -> None:
        if 1 <= index <= len(self.menu.recommended):
            self._submit_intent(self.menu.recommended[index - 1])

    def _submit_default(self) -> None:
        for option in self.menu.recommended:
            if option.intent.id == self.menu.default_intent:
                self._submit_intent(option)
                return
        if self.menu.recommended:
            self._submit_intent(self.menu.recommended[0])

    def _select_domain(self, index: int) -> None:
        if 1 <= index <= self.domain_combo.count():
            self.domain_combo.setCurrentIndex(index - 1)

    def closeEvent(self, event) -> None:
        self.save_geometry()
        # 析构期间不 emit，避免槽在主对象已销毁时被调用导致闪退
        super().closeEvent(event)
