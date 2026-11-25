"""设置：领域与意图（按领域分组）、快捷键、提示词模板、模型、拦截、搜索、行为。

所有意图（含复用旧脚本模板与用户自定义）都挂在「领域与意图」页的领域树下：
可在任意领域下新增/删除/改名/改提示词/改主路由/改排序，全部可插拔。
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QKeySequenceEdit, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QTabWidget, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from ..models import intent_prompt_source
from ..settings import Settings, load_defaults, validate
from .config import (
    DEFAULT_HOTKEYS, HOTKEY_LABELS, load_legacy, save_config,
)
from .theme_tokens import (
    QSS_SCROLL_AREA, TYPO_BODY_MD, TYPO_BODY_SM, TYPO_HEADLINE_LG,
    TYPO_HEADLINE_MD, TYPO_LABEL_SM, BORDER_WIDTH,
    SPACING_SM, SPACING_MD, SPACING_LG, SPACING_XS, RADIUS_DEFAULT,
    build_qss, tokens,
)

# 领域树顶层顺序（含通用）
DOMAIN_ORDER = (
    ("chem", "化学"), ("en", "英语 / 语言"), ("zh", "中文 / 术语"),
    ("math", "数学 / 公式"), ("physics", "物理"), ("code", "编程"),
    ("image", "图像内容"), ("*", "通用"),
)
# 领域总开关（多个领域可共享一个 switch，如 math/physics 共用 latex_query）
SWITCH_LABELS = (
    ("en_query", "英语 / 单词 / 词组"), ("zh_query", "中文 / 术语"),
    ("latex_query", "数学 / 物理 / 公式"), ("chem_query", "化学"),
    ("code_query", "编程"), ("image_intents", "图像意图"),
)
FORMAT_IDS = ("static", "table", "interactive", "annotate")


def settings_qss(dark: bool) -> str:
    """设置窗口专用的一层样式：正文 / 说明 / 分区标题三档字号，颜色取主题。

    只追加在 build_qss 之后，作用域是这个对话框，不会改动主窗口。
    """
    t = tokens(dark)
    return f"""
QDialog, QDialog * {{ font-size:{TYPO_BODY_MD[0]}px; }}
/* 设置页同样是一块连续面板：分区靠字阶 + 留白，不靠描边 */
QLabel#section {{ color:{t['text']}; font-size:{TYPO_HEADLINE_LG[0]}px;
  font-weight:600; padding-top:{SPACING_SM}px; padding-bottom:{SPACING_XS}px; }}
QLabel#sectionHint {{ color:{t['text_variant']};
  font-size:{TYPO_BODY_SM[0]}px; padding-bottom:{SPACING_SM}px; }}
QLabel#statusHint {{ color:{t['error']}; font-size:{TYPO_BODY_SM[0]}px; }}
QTabBar::tab {{ font-size:{TYPO_BODY_SM[0]}px; padding:{SPACING_SM}px {SPACING_MD}px; }}
/* 底部按钮：去掉系统自带的绿勾 / 红叉图标，统一成无图标文字按钮 */
QDialogButtonBox QPushButton {{ background:transparent; border:none;
  color:{t['text']}; padding:{SPACING_SM}px {SPACING_LG}px;
  min-height:32px; }}
QDialogButtonBox QPushButton:hover {{ background:{t['chip']};
  border-radius:{RADIUS_DEFAULT}px; }}
QDialogButtonBox QPushButton:default {{ font-weight:bold; }}
/* 表格 / 树：去掉网格线与交替行色，行间用极淡分隔，保持一体感 */
QTreeWidget, QTableWidget, QListWidget {{ background:transparent;
  border:none; outline:none; }}
QHeaderView::section {{ background:transparent; border:none;
  border-bottom:{BORDER_WIDTH}px solid {t['outline']};
  color:{t['text_variant']}; font-size:{TYPO_LABEL_SM[0]}px;
  padding:{SPACING_XS}px {SPACING_SM}px; }}
QTreeWidget::item, QTableWidget::item, QListWidget::item {{
  border:none; padding:{SPACING_XS}px {SPACING_SM}px; }}
QTreeWidget::item:hover, QTableWidget::item:hover, QListWidget::item:hover {{
  background:{t['chip']}; }}
QTreeWidget::item:selected, QTableWidget::item:selected, QListWidget::item:selected {{
  background:{t['chip']}; color:{t['text']}; }}
QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget {{
  background:{t['background']};
  }}
"""


def apply_settings_palette(widget, dark: bool) -> None:
    """深色主题下 QSS 改不动 QDialog 的底色，必须同步设置调色板。"""
    t = tokens(dark)
    palette = widget.palette()
    palette.setColor(QPalette.Window, QColor(t["background"]))
    palette.setColor(QPalette.WindowText, QColor(t["text"]))
    palette.setColor(QPalette.Base, QColor(t["surface_lowest"]))
    palette.setColor(QPalette.Text, QColor(t["text"]))
    widget.setPalette(palette)


def parse_includes(text: str) -> list[dict]:
    """``en_word:phonetic|etymology_roots, general:key_points`` → includes。"""
    result = []
    for chunk in (text or "").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        route_id, sections = chunk.split(":", 1)
        parts = [s.strip() for s in sections.split("|") if s.strip()]
        if route_id.strip() and parts:
            result.append({"route_id": route_id.strip(), "sections": parts,
                           "as": "补充片段"})
    return result


def format_includes(includes) -> str:
    return ", ".join(
        f"{inc.get('route_id', '')}:{'|'.join(inc.get('sections') or [])}"
        for inc in includes or [])


class SettingsDialog(QDialog):
    applied = Signal(object)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("clip-llm · 设置")
        # QDialog 的窗口类型是 Dialog，Wayland 下窗口管理器不给它最大化 /
        # 最小化按钮；改成普通 Window 类型并显式带上两个按钮提示。
        self.setWindowFlags(
            Qt.Window
            | Qt.WindowTitleHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint)
        self.setSizeGripEnabled(True)
        # 按屏幕可用区域自适应：小屏上也要能看见底部的确定 / 取消
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else None
        width = min(960, int(area.width() * 0.86)) if area else 960
        height = min(720, int(area.height() * 0.82)) if area else 720
        self.setMinimumSize(600, 460)
        self.resize(max(600, width), max(460, height))
        self.data = copy.deepcopy(settings.as_dict())
        self._dark = bool(self.data.get("desktop_dark_theme", False))
        self.setStyleSheet(build_qss(self._dark) + settings_qss(self._dark))
        apply_settings_palette(self, self._dark)
        self.data.setdefault("intents", [])
        self.data.setdefault("route_prompts", {})
        self._legacy = load_legacy()
        self._current_intent_id = ""
        self._loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(10)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)   # 弱化分页边框，整窗像一块连续面板
        # 顺序：外观 → 拦截 → 模板 → 查询方式 → 其他视角 → 搜索 → 模型 → 行为
        self.look_tab = self._scroll(self._build_look())
        self.tabs.addTab(self.look_tab, "外观")
        self.policy_tab = self._scroll(self._build_policy())
        self.tabs.addTab(self.policy_tab, "拦截")
        self.prompts_tab = self._scroll(self._build_prompts())
        self.tabs.addTab(self.prompts_tab, "提示词模板")
        self.intents_tab = self._scroll(self._build_domains_intents())
        self.tabs.addTab(self.intents_tab, "查询方式")
        self.other_tab = self._scroll(self._build_other_views())
        self.tabs.addTab(self.other_tab, "其他视角")
        self.search_tab = self._scroll(self._build_search())
        self.tabs.addTab(self.search_tab, "搜索")
        self.model_tab = self._scroll(self._build_model())
        self.tabs.addTab(self.model_tab, "模型与图片")
        self.library_tab = self._scroll(self._build_library())
        self.tabs.addTab(self.library_tab, "知识库")
        self.behavior_tab = self._scroll(self._build_behavior())
        self.tabs.addTab(self.behavior_tab, "行为")
        root.addWidget(self.tabs, 1)
        # 校验 / 冲突提示固定显示在按钮上方：无论当前在哪个页签都看得见
        self.status_hint = QLabel("")
        self.status_hint.setObjectName("statusHint")
        self.status_hint.setWordWrap(True)
        root.addWidget(self.status_hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Apply)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.Apply).setText("应用")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self._apply)
        root.addWidget(buttons)
        self._sync()

    # -- 外观 --------------------------------------------------------------
    def _scroll(self, page: QWidget) -> QScrollArea:
        """把页签内容放进滚动区：窗口再小也能滚到每个控件。"""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        # 滚动区默认会用 palette().base()（白）盖住页签，深色主题下更明显；
        # 让 viewport 与页面透明，露出窗口自身底色，避免出现白块。
        area.viewport().setAutoFillBackground(False)
        page.setAutoFillBackground(False)
        area.setStyleSheet(QSS_SCROLL_AREA)
        page_layout = page.layout()
        if page_layout is not None:          # 所有页签统一留白与行距
            page_layout.setContentsMargins(14, 12, 14, 12)
            page_layout.setSpacing(10)
        area.setWidget(page)
        return area

    @staticmethod
    def _section_title(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("section")
        return label

    @staticmethod
    def _note(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionHint")
        label.setWordWrap(True)
        return label

    def _build_look(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.font_spin = QSpinBox()
        self.font_spin.setRange(9, 28)
        self.font_spin.valueChanged.connect(
            lambda v: self.data.__setitem__("desktop_font_size", int(v)))
        self.dark_check = QCheckBox("深色主题")
        self.dark_check.toggled.connect(
            lambda v: self.data.__setitem__("desktop_dark_theme", bool(v)))
        hint = QLabel("结果排版、查询栏与标签页字号即时生效；主窗口可边缘缩放并记忆尺寸位置。")
        hint.setWordWrap(True)
        form.addRow("结果卡片字号", self.font_spin)
        form.addRow(self.dark_check)
        form.addRow(hint)
        return w

    # -- 领域与意图 --------------------------------------------------------
    def _build_domains_intents(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(8)

        # 顶部：类别总开关，数量 = 提示词模板数量，随模板增删自动变化
        layout.addWidget(QLabel("类别总开关（勾选启用模板，增删自动同步）："))
        self.switch_grid = QGridLayout()
        self.switch_grid.setSpacing(6)
        self.domain_checks: dict[str, QCheckBox] = {}
        self._rebuild_switches()
        layout.addLayout(self.switch_grid)

        body = QHBoxLayout()
        body.setSpacing(10)

        # 左：领域 → 意图 树
        left = QVBoxLayout()
        left.addWidget(QLabel("查询方式（按模板分组，勾选启用）"))
        self.prompt_tree = QTreeWidget()
        self.prompt_tree.setHeaderLabels(["查询方式 / 提示词", "模板"])
        self.prompt_tree.setColumnWidth(0, 320)
        self.prompt_tree.currentItemChanged.connect(self._on_tree_selection)
        self.prompt_tree.itemChanged.connect(self._on_tree_item_changed)
        left.addWidget(self.prompt_tree, 1)
        button_row = QHBoxLayout()
        self.add_intent_button = QPushButton("＋ 在当前模板下新增")
        self.add_intent_button.clicked.connect(self._add_intent)
        button_row.addWidget(self.add_intent_button)
        self.del_intent_button = QPushButton("删除选中（仅自定义）")
        self.del_intent_button.clicked.connect(self._delete_intent)
        button_row.addWidget(self.del_intent_button)
        left.addLayout(button_row)
        body.addLayout(left, 5)

        # 右：选中意图的编辑区
        form = QFormLayout()
        self.intent_enabled = QCheckBox("启用该查询方式")
        self.intent_enabled.toggled.connect(self._flush_intent)
        form.addRow(self.intent_enabled)
        self.intent_label_edit = QLineEdit()
        self.intent_label_edit.textChanged.connect(self._flush_intent)
        form.addRow("显示名", self.intent_label_edit)
        self.intent_hint_edit = QLineEdit()
        self.intent_hint_edit.textChanged.connect(self._flush_intent)
        form.addRow("说明", self.intent_hint_edit)
        routes = self.data.get("routes", {})
        self.intent_route_combo = QComboBox()
        for rid in sorted(routes):
            self.intent_route_combo.addItem(
                str(routes[rid].get("label") or rid), rid)
        if self.intent_route_combo.count() == 0:
            self.intent_route_combo.addItem("通用解析", "general")
        self.intent_route_combo.currentIndexChanged.connect(self._flush_intent)
        self.intent_route_combo.currentIndexChanged.connect(
            self._sync_followup_visibility)
        form.addRow("主路由（提示词来源）", self.intent_route_combo)
        self.intent_domain_combo = QComboBox()
        for did, label in DOMAIN_ORDER:
            self.intent_domain_combo.addItem(label, did)
        self.intent_domain_combo.currentIndexChanged.connect(self._flush_intent)
        form.addRow("所属领域", self.intent_domain_combo)
        self.intent_order_spin = QSpinBox()
        self.intent_order_spin.setRange(1, 999)
        self.intent_order_spin.valueChanged.connect(self._flush_intent)
        form.addRow("排序", self.intent_order_spin)
        self.intent_includes_edit = QLineEdit()
        self.intent_includes_edit.setPlaceholderText(
            "en_word:phonetic|etymology_roots, general:key_points")
        self.intent_includes_edit.textChanged.connect(self._flush_intent)
        form.addRow("借用其他路由片段", self.intent_includes_edit)
        self.intent_format_combo = QComboBox()
        self.intent_format_combo.addItems(FORMAT_IDS)
        self.intent_format_combo.currentTextChanged.connect(self._flush_intent)
        form.addRow("默认呈现", self.intent_format_combo)
        self.intent_prompt = QPlainTextEdit()
        self.intent_prompt.setPlaceholderText(
            "该查询方式的提示词；留空则直接复用主路由模板（含旧脚本 9 套完整 Prompt）")
        self.intent_prompt.textChanged.connect(self._flush_intent)
        form.addRow("提示词", self.intent_prompt)
        # 仅图片方式：识别完成后是否继续进入文本模板（选项里的模板动态来自
        # 提示词模板页，新增模板后这里立刻可选）
        self.intent_followup_combo = QComboBox()
        self.intent_followup_combo.currentIndexChanged.connect(self._flush_intent)
        form.addRow("识别后", self.intent_followup_combo)
        self.intent_followup_hint = QLabel("")
        self.intent_followup_hint.setObjectName("sectionHint")
        self.intent_followup_hint.setWordWrap(True)
        form.addRow(self.intent_followup_hint)
        self.intent_form = form
        self.legacy_hint = QLabel("")
        self.legacy_hint.setObjectName("sectionHint")
        self.legacy_hint.setWordWrap(True)
        form.addRow(self.legacy_hint)
        body.addLayout(form, 4)

        layout.addLayout(body, 1)
        return w

    # -- 领域树 ------------------------------------------------------------
    def _intent_by_id(self, intent_id: str) -> dict | None:
        return next((i for i in self.data["intents"]
                     if i.get("id") == intent_id), None)

    def _domain_label(self, domain_id: str) -> str:
        pack = (self.data.get("routes") or {}).get(domain_id)
        if pack:
            return str(pack.get("label") or domain_id)
        return dict(DOMAIN_ORDER).get(domain_id, domain_id)

    @property
    def intent_total(self) -> int:
        return len(self.data["intents"])

    def _reload_tree(self, *, keep_selection: bool = False) -> None:
        selected = self._current_intent_id if keep_selection else ""
        self.prompt_tree.blockSignals(True)
        self.prompt_tree.clear()
        # 第二层：查询方式按「提示词模板」分组，顶层数量恒等于模板数量
        grouped: dict[str, list[dict]] = {}
        for item in self.data["intents"]:
            route = str(item.get("primary_route") or "general")
            grouped.setdefault(route, []).append(item)
        for route_id, pack in (self.data.get("routes") or {}).items():
            label = str(pack.get("label") or route_id)
            intents = sorted(grouped.get(route_id, []),
                             key=lambda x: int(x.get("order", 100)))
            top = QTreeWidgetItem(self.prompt_tree,
                                  [f"{label}（{len(intents)}）", label])
            top.setData(0, Qt.UserRole, ("domain", route_id))
            for item in intents:
                enabled = bool(item.get("enabled", True))
                source = intent_prompt_source(item)
                child = QTreeWidgetItem(
                    top, [item.get("label", item.get("id")), source])
                child.setData(0, Qt.UserRole, ("intent", item.get("id")))
                child.setCheckState(0, Qt.Checked if enabled else Qt.Unchecked)
                child.setForeground(0, QColor(
                    tokens(self._dark)["text" if enabled else "text_variant"]))
                if item.get("id") == selected:
                    self.prompt_tree.setCurrentItem(child)
            top.setExpanded(True)
        self.prompt_tree.blockSignals(False)

    def select_first_intent(self) -> None:
        top = self.prompt_tree.topLevelItem(0)
        if top and top.childCount():
            self.prompt_tree.setCurrentItem(top.child(0))
        elif top:
            self.prompt_tree.setCurrentItem(top)

    def _on_tree_selection(self, current: QTreeWidgetItem | None,
                           _previous: QTreeWidgetItem | None) -> None:
        if current is None:
            return
        kind, value = current.data(0, Qt.UserRole)
        if kind == "intent":
            self._load_intent(str(value))
        elif kind == "domain":
            self._current_intent_id = ""
            self._set_editor_enabled(False)

    def _on_tree_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        kind, value = item.data(0, Qt.UserRole)
        if kind != "intent":
            return
        data = self._intent_by_id(str(value))
        if data is not None:
            data["enabled"] = item.checkState(0) == Qt.Checked
            item.setForeground(0, QColor(
                tokens(self._dark)["text" if data["enabled"] else "text_variant"]))

    def _set_editor_enabled(self, enabled: bool) -> None:
        for widget in (self.intent_enabled, self.intent_label_edit,
                       self.intent_hint_edit, self.intent_route_combo,
                       self.intent_domain_combo, self.intent_order_spin,
                       self.intent_includes_edit, self.intent_format_combo,
                       self.intent_prompt):
            widget.setEnabled(enabled)

    def _load_intent(self, intent_id: str) -> None:
        self._loading = True
        self._current_intent_id = intent_id
        data = self._intent_by_id(intent_id) or {}
        self._set_editor_enabled(True)
        self.intent_enabled.setChecked(bool(data.get("enabled", True)))
        self.intent_label_edit.setText(str(data.get("label", "")))
        self.intent_hint_edit.setText(str(data.get("hint", "")))
        self._fill_intent_routes(
            image_only="image" in (data.get("domains") or ()))
        index = self.intent_route_combo.findData(
            str(data.get("primary_route") or "general"))
        self.intent_route_combo.setCurrentIndex(max(0, index))
        domains = data.get("domains") or ["*"]
        index = self.intent_domain_combo.findData(domains[0])
        self.intent_domain_combo.setCurrentIndex(max(0, index))
        self.intent_order_spin.setValue(int(data.get("order", 100)))
        self.intent_includes_edit.setText(format_includes(data.get("includes")))
        self.intent_format_combo.setCurrentText(str(data.get("default_format", "static")))
        self.intent_prompt.setPlainText(str(data.get("prompt_override", "")))
        self._rebuild_followup_options()
        index = self.intent_followup_combo.findData(
            str(data.get("followup_route") or ""))
        self.intent_followup_combo.setCurrentIndex(max(0, index))
        self._sync_followup_visibility()
        if not data.get("prompt_override"):
            self.legacy_hint.setText("当前复用主路由模板（提示词留空即生效）")
        else:
            self.legacy_hint.setText("当前使用本条专属提示词")
        self._loading = False

    def _flush_intent(self, *_args) -> None:
        if self._loading or not self._current_intent_id:
            return
        data = self._intent_by_id(self._current_intent_id)
        if data is None:
            return
        data["enabled"] = self.intent_enabled.isChecked()
        data["label"] = self.intent_label_edit.text().strip() or data["id"]
        data["hint"] = self.intent_hint_edit.text().strip()
        data["primary_route"] = self.intent_route_combo.currentData() or None
        data["domains"] = [self.intent_domain_combo.currentData() or "*"]
        data["order"] = self.intent_order_spin.value()
        data["includes"] = parse_includes(self.intent_includes_edit.text())
        default_format = self.intent_format_combo.currentText()
        formats = list(data.get("supported_formats") or [])
        if default_format not in formats:
            formats.insert(0, default_format)
        data["default_format"] = default_format
        data["supported_formats"] = formats or [default_format]
        data["prompt_override"] = self.intent_prompt.toPlainText()
        data["followup_route"] = str(self.intent_followup_combo.currentData() or "")
        self._refresh_followup_hint()
        self.legacy_hint.setText(
            "当前复用主路由模板（提示词留空即生效）"
            if not data["prompt_override"] else "当前使用本条专属提示词")
        current = self.prompt_tree.currentItem()
        if current is not None:
            current.setText(0, data["label"])

    def _fill_intent_routes(self, *, image_only: bool) -> None:
        """主路由下拉只列同类模板：图片方式只能挂图片模板，反之亦然。"""
        combo = self.intent_route_combo
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for rid in sorted(self.data.get("routes", {})):
            pack = self.data["routes"][rid]
            if bool(pack.get("vision")) != image_only:
                continue
            combo.addItem(str(pack.get("label") or rid), rid)
        if combo.count() == 0:                     # 没有同类模板时不留空下拉
            combo.addItem("通用解析", "general")
        combo.blockSignals(False)
        if current:
            combo.setCurrentIndex(max(0, combo.findData(current)))

    def _route_accepts_followup(self, route_id: str) -> bool:
        """该模板下有没有可承接二次查询的启用方式（图片方式不算）。"""
        return any(i.get("enabled", True) and "image" not in (i.get("domains") or [])
                   and i.get("primary_route") == route_id
                   for i in self.data.get("intents", []))

    def _rebuild_followup_options(self) -> None:
        """识别后：直出 / 自动判类 / 指定某个文本模板（模板池动态生成）。"""
        combo = self.intent_followup_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("直接展示识别结果", "")
        combo.addItem("自动判断文本类别后继续", "auto")
        for rid in sorted(self.data.get("routes", {})):
            pack = self.data["routes"][rid]
            if pack.get("vision") or not pack.get("enabled", True):
                continue
            if not self._route_accepts_followup(rid):
                continue
            combo.addItem(f"指定模板：{pack.get('label') or rid}", rid)
        combo.blockSignals(False)

    def _sync_followup_visibility(self, *_args) -> None:
        """「识别后」只对图片方式有意义，文本方式直接整行隐藏。"""
        route = (self.data.get("routes", {})
                 .get(str(self.intent_route_combo.currentData() or "")) or {})
        vision = bool(route.get("vision"))
        if hasattr(self, "intent_form"):
            self.intent_form.setRowVisible(self.intent_followup_combo, vision)
            self.intent_form.setRowVisible(self.intent_followup_hint, vision)
        self._refresh_followup_hint()

    def _refresh_followup_hint(self) -> None:
        if not hasattr(self, "intent_followup_hint"):
            return
        value = str(self.intent_followup_combo.currentData() or "")
        if not value:
            text = ("视觉模型的结果直接展示。表格 / 解释 / 标注这类"
                    "「模型已给成品」的方式选这个。")
        elif value == "auto":
            text = ("按识别出的文字自动判类别，用该类别的默认查询方式继续；"
                    "中间那次初判自动完成，用户只点一次。")
        else:
            label = str((self.data.get("routes", {}).get(value) or {})
                        .get("label") or value)
            text = (f"固定用「{label}」模板下排序最前的那条查询方式继续，"
                    "不再让分类器判断。")
        self.intent_followup_hint.setText(text)

    def _current_domain_id(self) -> str:
        item = self.prompt_tree.currentItem()
        if item is None:
            return "*"
        kind, value = item.data(0, Qt.UserRole)
        if kind == "intent":
            parent = item.parent()
            if parent is not None:
                _, pvalue = parent.data(0, Qt.UserRole)
                return str(pvalue)
        elif kind == "domain":
            return str(value)
        return "*"

    def _add_intent(self) -> None:
        route_id = self._current_domain_id()
        # 领域沿用同模板已有查询方式，避免新增项落到不相关的领域
        sibling_domains = next(
            (i.get("domains") for i in self.data["intents"]
             if i.get("primary_route") == route_id and i.get("domains")), None)
        index = 1
        ids = {i.get("id") for i in self.data["intents"]}
        while f"custom.{index}" in ids:
            index += 1
        item = {
            "id": f"custom.{index}",
            "label": f"自定义查询 {index}",
            "hint": "",
            "domains": list(sibling_domains or ["*"]),
            "modalities": ["text", "image", "mixed"],
            "applicable": None, "primary_route": route_id,
            "prompt_override": "请解释这段内容，严格按 JSON 输出，至少包含 title 与 answer。",
            "includes": [], "default_format": "static",
            "supported_formats": ["static"], "order": 200, "enabled": True,
            "free_input_only": False, "custom": True,
            "conflict_policy": "primary_wins", "token_budget": 1400,
            "prompt_version": "2.0.0", "schema_version": "1.0.0",
        }
        self.data["intents"].append(item)
        self._reload_tree()
        # 选中新增项
        for i in range(self.prompt_tree.topLevelItemCount()):
            top = self.prompt_tree.topLevelItem(i)
            for j in range(top.childCount()):
                child = top.child(j)
                if child.data(0, Qt.UserRole) == ("intent", item["id"]):
                    self.prompt_tree.setCurrentItem(child)
                    return

    def _delete_intent(self) -> None:
        data = self._intent_by_id(self._current_intent_id)
        if not data or not data.get("custom"):
            self.legacy_hint.setText("只能删除自定义意图；出厂意图可停用或改提示词。")
            return
        self.data["intents"] = [i for i in self.data["intents"]
                                if i.get("id") != self._current_intent_id]
        self._current_intent_id = ""
        self._reload_tree()
        self.select_first_intent()

    # -- 快捷键 ------------------------------------------------------------
    def _pick_library_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择知识库目录")
        if path:
            self.library_dir_edit.setText(path)

    def _reset_hotkeys(self) -> None:
        for action, edit in self.hotkey_edits.items():
            edit.setKeySequence(QKeySequence(DEFAULT_HOTKEYS[action]))

    def _collect_hotkeys(self) -> tuple[dict, str]:
        result, seen, error = {}, {}, ""
        for action, edit in self.hotkey_edits.items():
            text = edit.keySequence().toString()
            result[action] = text
            if text and text in seen:
                error = f"快捷键 {text} 被「{HOTKEY_LABELS[seen[text]]}」和「{HOTKEY_LABELS[action]}」重复占用"
            seen[text] = action
        return result, error

    # -- 提示词模板 --------------------------------------------------------
    def _build_prompts(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(8)
        layout.addWidget(self._note(
            "文本模板决定「怎么解释」；图片模板就是识别图片时发给视觉模型的"
            "提示词。查询方式不填提示词，就会用所属模板这一份。"))
        row = QHBoxLayout()
        self.route_combo = QComboBox()
        self.route_combo.currentIndexChanged.connect(self._load_route_prompt)
        row.addWidget(self.route_combo, 1)
        self.add_route_button = QPushButton("＋ 新增模板")
        self.add_route_button.clicked.connect(self._add_route)
        row.addWidget(self.add_route_button)
        self.del_route_button = QPushButton("删除选中")
        self.del_route_button.clicked.connect(self._delete_route)
        row.addWidget(self.del_route_button)
        layout.addLayout(row)

        self.route_kind_hint = QLabel("")
        self.route_kind_hint.setObjectName("sectionHint")
        self.route_kind_hint.setWordWrap(True)
        layout.addWidget(self.route_kind_hint)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("显示名"))
        self.route_label_edit = QLineEdit()
        self.route_label_edit.setPlaceholderText("这个模板在界面上显示的名字")
        self.route_label_edit.textChanged.connect(self._save_route_label)
        name_row.addWidget(self.route_label_edit, 1)
        layout.addLayout(name_row)
        self.route_prompt = QPlainTextEdit()
        self.route_prompt.textChanged.connect(self._save_route_prompt)
        layout.addWidget(self.route_prompt, 2)

        vision_row = QHBoxLayout()
        vision_row.addWidget(QLabel("图片默认排最前"))
        self.image_default_combo = QComboBox()
        for intent_id, label in self._image_intents():
            self.image_default_combo.addItem(label, intent_id)
        current = str(self.data.get("desktop_vision_intent", "image.ocr"))
        index = self.image_default_combo.findData(current)
        self.image_default_combo.setCurrentIndex(max(index, 0))
        self.image_default_combo.currentIndexChanged.connect(
            self._save_vision_intent)
        vision_row.addWidget(self.image_default_combo, 1)
        layout.addLayout(vision_row)

        self._rebuild_route_combo()
        return w

    def _image_intents(self) -> list[tuple[str, str]]:
        """图片查询方式（截图后可选的那几条），按设置顺序。"""
        return [(i["id"], str(i.get("label") or i["id"]))
                for i in sorted(self.data.get("intents", []),
                                key=lambda i: i.get("order", 0))
                if "image" in (i.get("domains") or []) and i.get("enabled", True)]

    def _rebuild_route_combo(self, select: str | None = None) -> None:
        """按「文本模板 / 图片模板」分组重建下拉；组标题不可选。"""
        combo = self.route_combo
        current = select or combo.currentData()
        routes = self.data.get("routes", {})
        text_routes = [rid for rid in sorted(routes)
                       if not routes[rid].get("vision")]
        image_routes = [rid for rid in sorted(routes) if routes[rid].get("vision")]
        combo.blockSignals(True)
        combo.clear()
        for title, ids in (("—— 文本模板（分类器判定）——", text_routes),
                           ("—— 图片模板（截图后直接判定）——", image_routes)):
            if not ids:
                continue
            combo.addItem(title, None)
            model = combo.model()
            header = model.item(combo.count() - 1) if model is not None else None
            if header is not None:
                header.setEnabled(False)       # 组标题只是分隔，不可选中
            for rid in ids:
                combo.addItem(str(routes[rid].get("label") or rid), rid)
        combo.blockSignals(False)
        if current:
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)
        self._load_route_prompt(combo.currentIndex())

    def _save_vision_intent(self, _index: int) -> None:
        combo = getattr(self, "image_default_combo", None)
        intent_id = combo.currentData() if combo is not None else None
        if intent_id:
            self.data["desktop_vision_intent"] = str(intent_id)

    def _set_vision_enabled(self, value: bool) -> None:
        self.data.setdefault("image", {})["vision_enabled"] = bool(value)

    def _set_auto_continue(self, value: bool) -> None:
        self.data.setdefault("image", {})["auto_continue"] = bool(value)

    def _set_popup_max_mb(self, value: int) -> None:
        self.data.setdefault("image", {})["clipboard_popup_max_mb"] = int(value)

    def _load_route_prompt(self, _index: int) -> None:
        route_id = self.route_combo.currentData()
        if not route_id:
            return
        route = self.data.get("routes", {}).get(route_id, {})
        # 这一份模板到底有没有被用上：留空的查询方式复用它，写了专属提示词的不用
        members = [i for i in self.data.get("intents", [])
                   if i.get("primary_route") == route_id]
        reusing = [i for i in members
                   if not str(i.get("prompt_override") or "").strip()]
        text = self.data["route_prompts"].get(route_id, route.get("prompt", ""))
        if str(text).strip():
            usage = f"当前 {len(reusing)}/{len(members)} 条查询方式复用此模板"
            if len(reusing) < len(members):
                usage += "，其余用各自的专属提示词"
            usage += "。"
        else:
            usage = ("当前模板不提供提示词（留空）——每条查询方式必须自带"
                     "提示词，否则保存时会报错")
        if bool(route.get("vision")):
            default = (self.image_default_combo.currentText()
                       if hasattr(self, "image_default_combo") else "")
            self.route_kind_hint.setText(
                "图片识别：这是所有图片方式的兜底契约——只写跨方式都成立的"
                "规则（不要编造、公式用 LaTeX）；JSON 字段、坐标、包裹符这类"
                "格式要求写在各条查询方式自己的提示词里。" + usage
                + f"当前默认排在最前：{default}。")
        else:
            self.route_kind_hint.setText(
                "文本模板：文本查询时拼接的系统提示词（查询方式留空时使用）。"
                + usage)
        self.route_prompt.blockSignals(True)
        self.route_prompt.setPlainText(text)
        self.route_prompt.blockSignals(False)
        self.route_label_edit.blockSignals(True)
        self.route_label_edit.setText(str(route.get("label") or route_id))
        self.route_label_edit.blockSignals(False)

    def _save_route_label(self, text: str) -> None:
        """改显示名时同步下拉、类别开关与分组树，避免两页叫法不一致。"""
        route_id = self.route_combo.currentData()
        if not route_id:
            return
        label = text.strip() or route_id
        self.data.setdefault("routes", {}).setdefault(route_id, {})["label"] = label
        index = self.route_combo.currentIndex()
        if index >= 0:
            self.route_combo.setItemText(index, label)
        cb = self.domain_checks.get(route_id)
        if cb is not None:
            cb.setText(label)
            cb.setToolTip(f"提示词模板：{label}")
        for i in range(self.prompt_tree.topLevelItemCount()):
            top = self.prompt_tree.topLevelItem(i)
            if top.data(0, Qt.UserRole) == ("domain", route_id):
                top.setText(0, f"{label}（{top.childCount()}）")
                top.setText(1, label)
                break

    def _save_route_prompt(self) -> None:
        route_id = self.route_combo.currentData()
        if not route_id:
            return
        # 空也是有效状态：这份模板不提供提示词（校验会要求方式自带）
        self.data["route_prompts"][route_id] = self.route_prompt.toPlainText()

    def _add_route(self) -> None:
        text, ok = QInputDialog.getText(self, "新增模板",
                                        "模板 ID（英文标识符，如 my_route）：")
        if not ok:
            return
        route_id = text.strip()
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", route_id or ""):
            self.status_hint.setText("模板 ID 必须是英文标识符（字母/数字/下划线）。")
            return
        # 起始模板可直接复用旧脚本的 9 套完整提示词，不必从空白重写
        packs = self._legacy.get("packs") or {}
        sources = ["空白模板"] + [
            f"{rid} · {pack.get('label', rid)}" for rid, pack in packs.items()]
        source, picked = QInputDialog.getItem(
            self, "模板来源", "选择起始模板（可复用旧脚本 9 套提示词）：",
            sources, 0, False)
        if not picked:
            return
        base_prompt, base_label = "", route_id
        if source != "空白模板":
            pack = packs.get(source.split(" · ", 1)[0], {})
            base_prompt = str(pack.get("prompt") or "")
            base_label = f"{pack.get('label', route_id)}（副本）"
        routes = self.data.setdefault("routes", {})
        if route_id in routes:
            self.legacy_hint.setText(f"模板 {route_id} 已存在。")
            return
        routes[route_id] = {"id": route_id, "enabled": True,
                            "label": base_label,
                            "query_type": "free", "kind": "route",
                            "prompt": base_prompt,
                            "sections": [], "fields": []}
        self._rebuild_route_combo(route_id)
        self._rebuild_switches()
        self.legacy_hint.setText(
            f"已新增模板 {route_id}（{base_label}），类别总开关已同步。")

    def _delete_route(self) -> None:
        route_id = self.route_combo.currentData()
        if not route_id:
            return
        used = [i["id"] for i in self.data["intents"]
                if i.get("primary_route") == route_id
                or any(inc.get("route_id") == route_id
                       for inc in i.get("includes") or [])]
        if used:
            self.legacy_hint.setText(
                f"模板 {route_id} 被引用（{', '.join(used[:5])}…），"
                "请先在「领域与意图」里改这些条目的主路由。")
            return
        self.data.get("routes", {}).pop(route_id, None)
        self.data.get("route_prompts", {}).pop(route_id, None)
        self._rebuild_route_combo()
        self._rebuild_switches()
        self.legacy_hint.setText(f"已删除模板 {route_id}，类别总开关已同步。")

    # -- 第三层：其他视角（通用解读角度，与模板无关） ----------------------
    def _build_other_views(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(10)
        layout.addWidget(self._section_title("其他视角"))
        layout.addWidget(self._note(
            "通用解读角度，和提示词模板无关。勾选后出现在初判页「其他角度」。"))
        row = QHBoxLayout()
        row.addWidget(QLabel("最多显示"))
        self.other_max_spin = QSpinBox()
        self.other_max_spin.setRange(0, 24)
        self.other_max_spin.setValue(
            int((self.data.get("other_views") or {}).get("max_items", 6)))
        self.other_max_spin.valueChanged.connect(self._save_other_views)
        row.addWidget(self.other_max_spin)
        row.addStretch(1)
        self.add_view_button = QPushButton("＋ 新增视角")
        self.add_view_button.clicked.connect(self._add_other_view)
        row.addWidget(self.add_view_button)
        self.del_view_button = QPushButton("删除选中")
        self.del_view_button.clicked.connect(self._delete_other_view)
        row.addWidget(self.del_view_button)
        layout.addLayout(row)

        body = QHBoxLayout()
        body.setSpacing(10)
        self.other_view_list = QListWidget()
        self.other_view_list.currentRowChanged.connect(self._load_other_view)
        self.other_view_list.itemChanged.connect(self._on_other_view_toggled)
        body.addWidget(self.other_view_list, 2)

        form = QFormLayout()
        self.other_label_edit = QLineEdit()
        self.other_label_edit.textChanged.connect(self._flush_other_view)
        form.addRow("名称", self.other_label_edit)
        self.other_hint_edit = QLineEdit()
        self.other_hint_edit.textChanged.connect(self._flush_other_view)
        form.addRow("说明", self.other_hint_edit)
        self.other_domains_edit = QLineEdit()
        self.other_domains_edit.setPlaceholderText(
            "留空 = 所有类别；可填 zh, en, math")
        self.other_domains_edit.textChanged.connect(self._flush_other_view)
        form.addRow("适用领域", self.other_domains_edit)
        self.other_prompt_edit = QPlainTextEdit()
        self.other_prompt_edit.setPlaceholderText(
            "这个视角的提示词；要求返回 JSON。")
        self.other_prompt_edit.textChanged.connect(self._flush_other_view)
        form.addRow("提示词", self.other_prompt_edit)
        self.other_view_hint = QLabel("")
        self.other_view_hint.setObjectName("sectionHint")
        self.other_view_hint.setWordWrap(True)
        form.addRow(self.other_view_hint)
        body.addLayout(form, 3)
        layout.addLayout(body, 1)

        self._reload_other_views()
        return w

    def _other_items(self) -> list[dict]:
        views = self.data.setdefault("other_views", {})
        items = views.get("items")
        if not isinstance(items, list):
            items = views["items"] = []
        return items

    def _reload_other_views(self) -> None:
        if not hasattr(self, "other_view_list"):
            return
        self.other_view_list.blockSignals(True)
        self.other_view_list.clear()
        for view in self._other_items():
            item = QListWidgetItem(str(view.get("label") or view.get("id")
                                       or "视角"))
            item.setData(Qt.UserRole, str(view.get("id") or ""))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if view.get("enabled", True)
                               else Qt.Unchecked)
            self.other_view_list.addItem(item)
        self.other_view_list.blockSignals(False)
        self.other_view_list.setCurrentRow(
            0 if self.other_view_list.count() else -1)

    def _load_other_view(self, row: int) -> None:
        views = self._other_items()
        view = views[row] if 0 <= row < len(views) else None
        widgets = (self.other_label_edit, self.other_hint_edit,
                   self.other_domains_edit, self.other_prompt_edit)
        for widget in widgets:
            widget.blockSignals(True)
        if view is None:
            for widget in widgets[:3]:
                widget.clear()
            self.other_prompt_edit.clear()
            self.other_view_hint.setText("未选择视角")
        else:
            self.other_label_edit.setText(str(view.get("label") or ""))
            self.other_hint_edit.setText(str(view.get("hint") or ""))
            self.other_domains_edit.setText(", ".join(view.get("domains") or ()))
            self.other_prompt_edit.setPlainText(str(view.get("prompt") or ""))
            self.other_view_hint.setText(f"ID：{view.get('id') or '(未命名)'}")
        for widget in widgets:
            widget.blockSignals(False)

    def _flush_other_view(self, *_args) -> None:
        row = self.other_view_list.currentRow()
        views = self._other_items()
        if not (0 <= row < len(views)):
            return
        view = views[row]
        view["label"] = self.other_label_edit.text().strip()
        view["hint"] = self.other_hint_edit.text().strip()
        view["domains"] = [x.strip() for x in
                           self.other_domains_edit.text().split(",") if x.strip()]
        view["prompt"] = self.other_prompt_edit.toPlainText()
        item = self.other_view_list.item(row)
        if item is not None:
            item.setText(view["label"] or view.get("id") or "视角")

    def _on_other_view_toggled(self, item: QListWidgetItem) -> None:
        row = self.other_view_list.row(item)
        views = self._other_items()
        if 0 <= row < len(views):
            views[row]["enabled"] = item.checkState() == Qt.Checked

    def _add_other_view(self, *_args) -> None:
        views = self._other_items()
        index = 1
        ids = {str(v.get("id") or "") for v in views}
        while f"custom{index}" in ids:
            index += 1
        views.append({
            "id": f"custom{index}", "label": f"自定义视角 {index}",
            "hint": "", "domains": [], "order": 100 + index, "enabled": True,
            "prompt": "请从用户指定的角度解释输入内容。严格按 JSON 输出，"
                      "至少包含 title 与 answer。",
        })
        self._reload_other_views()
        self.other_view_list.setCurrentRow(len(views) - 1)

    def _delete_other_view(self, *_args) -> None:
        row = self.other_view_list.currentRow()
        views = self._other_items()
        if 0 <= row < len(views):
            views.pop(row)
            self._reload_other_views()

    def _save_other_views(self, *_args) -> None:
        self.data.setdefault("other_views", {})["max_items"] = \
            self.other_max_spin.value()

    # -- 模型 --------------------------------------------------------------
    def _build_model(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self._edits = {}
        for key, label in (("desktop_api_url", "文本模型 API URL"),
                           ("desktop_api_key", "文本模型 API Key"),
                           ("desktop_model", "文本模型名称"),
                           ("desktop_vision_api_url", "视觉模型 API URL（留空复用）"),
                           ("desktop_vision_api_key", "视觉模型 Key（留空复用）"),
                           ("desktop_vision_model", "视觉模型名称")):
            edit = QLineEdit(str(self.data.get(key, "")))
            if "key" in key:
                edit.setEchoMode(QLineEdit.Password)
            edit.textChanged.connect(lambda v, k=key: self.data.__setitem__(k, v))
            self._edits[key] = edit
            form.addRow(label, edit)
        self.vision_enabled_check = QCheckBox(
            "启用图片识别（截图/复制图片时进入图片查询方式，选模板后才识别）")
        self.vision_enabled_check.setChecked(
            bool((self.data.get("image") or {}).get("vision_enabled", True)))
        self.vision_enabled_check.toggled.connect(self._set_vision_enabled)
        form.addRow(self.vision_enabled_check)
        self.auto_continue_check = QCheckBox(
            "识别后自动用默认方式再解释一遍（默认关：先看这次识别的结果，"
            "要不要继续由你决定；开了则一次点击走完）")
        self.auto_continue_check.setChecked(
            bool((self.data.get("image") or {}).get("auto_continue", False)))
        self.auto_continue_check.toggled.connect(self._set_auto_continue)
        form.addRow(self.auto_continue_check)
        self.popup_max_mb_spin = QSpinBox()
        self.popup_max_mb_spin.setRange(0, 64)
        self.popup_max_mb_spin.setSpecialValueText("不限制")
        self.popup_max_mb_spin.setValue(int(
            (self.data.get("image") or {}).get("clipboard_popup_max_mb", 2)))
        self.popup_max_mb_spin.valueChanged.connect(self._set_popup_max_mb)
        form.addRow("剪贴板图片超过此大小不弹窗（MB）", self.popup_max_mb_spin)
        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(256, 32768)
        self.max_tokens_spin.setValue(int(self.data.get("desktop_max_tokens", 2048)))
        self.max_tokens_spin.valueChanged.connect(
            lambda v: self.data.__setitem__("desktop_max_tokens", int(v)))
        form.addRow("上下文长度 max_tokens", self.max_tokens_spin)
        form.addRow(self._note(
            "地址、密钥、模型名称任一项留空，就不会发送网络请求，只显示本地预览。"
            "上下文长度限制单次回答的长短。剪贴板图片超过设定大小时只保留在剪贴板，"
            "不弹初判窗口（默认 2 MB，设为「不限制」则总是弹窗）；"
            "手动按 Ctrl+Alt+S 截图不受此限制。"))
        return w

    # -- 拦截 --------------------------------------------------------------
    def _build_policy(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(10)
        layout.addWidget(self._section_title("拦截规则"))
        layout.addWidget(self._note(
            "勾选＝拦截（不弹窗、不发请求），取消勾选＝放行。"
            "标注「固定」的项是硬性上限，不可关闭；选中一行可以改它的命中方式。"))
        self.policy_tree = QTreeWidget()
        self.policy_tree.setColumnCount(3)
        self.policy_tree.setHeaderLabels(["规则", "说明", "拦截"])
        self.policy_tree.setRootIsDecorated(False)
        self.policy_tree.setAlternatingRowColors(False)   # 一体化：不要斑马纹
        self.policy_tree.setUniformRowHeights(True)
        header = self.policy_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.policy_tree.currentItemChanged.connect(self._on_policy_selected)
        self.policy_tree.itemChanged.connect(self._on_policy_item_changed)
        self.policy_rows: dict[str, QTreeWidgetItem] = {}
        layout.addWidget(self.policy_tree, 1)

        button_row = QHBoxLayout()
        add_rule = QPushButton("＋ 新增规则")
        add_rule.clicked.connect(self._add_policy_rule)
        button_row.addWidget(add_rule)
        del_rule = QPushButton("删除选中")
        del_rule.clicked.connect(self._delete_policy_rule)
        button_row.addWidget(del_rule)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        layout.addSpacing(8)
        layout.addWidget(self._section_title("规则设置"))
        layout.addWidget(self._note("选中上方一条规则后编辑，改动即时写入草稿。"))
        form = QFormLayout()
        form.setSpacing(8)
        self.policy_detector = QComboBox()
        self.policy_detector.addItems(
            ["regex", "feature", "threshold", "quota", "dedup"])
        self.policy_detector.currentTextChanged.connect(self._flush_policy_rule)
        form.addRow("命中方式", self.policy_detector)
        self.policy_pattern = QLineEdit()
        self.policy_pattern.setPlaceholderText(
            "正则表达式（命中方式 = regex 时生效，忽略大小写）")
        self.policy_pattern.textChanged.connect(self._flush_policy_rule)
        form.addRow("正则", self.policy_pattern)
        self.policy_feature = QLineEdit()
        self.policy_feature.setPlaceholderText(
            "特征名（命中方式 = feature/threshold 时生效，如 url、char_count）")
        self.policy_feature.textChanged.connect(self._flush_policy_rule)
        form.addRow("特征", self.policy_feature)
        self.policy_desc = QLineEdit()
        self.policy_desc.setPlaceholderText("规则说明（显示在本表格与拦截记录里）")
        self.policy_desc.textChanged.connect(self._flush_policy_rule)
        form.addRow("说明", self.policy_desc)
        layout.addLayout(form)

        self._current_policy_id = ""
        self._loading_policy = False
        self._populate_policy_table()
        return w

    def _populate_policy_table(self) -> None:
        self.policy_tree.blockSignals(True)
        self.policy_tree.clear()
        self.policy_rows = {}
        rules = self.data.get("policy", {}).get("rules", {})
        for rule_id, rule in rules.items():
            label = str(rule.get("label") or rule_id)
            desc = str(rule.get("description", ""))
            item = QTreeWidgetItem([label, desc, ""])
            item.setToolTip(0, f"{label}（{rule_id}）")
            item.setToolTip(1, desc)
            item.setData(0, Qt.UserRole, str(rule_id))
            locked = bool(rule.get("locked", False))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            enabled = rule.get("enabled", True) is not False or locked
            item.setCheckState(2, Qt.Checked if enabled else Qt.Unchecked)
            if locked:
                item.setToolTip(2, "硬性上限，不可关闭")
            self.policy_tree.addTopLevelItem(item)
            self.policy_rows[str(rule_id)] = item
        self.policy_tree.blockSignals(False)
        if self.policy_tree.topLevelItemCount():
            self.policy_tree.setCurrentItem(self.policy_tree.topLevelItem(0))

    def _on_policy_item_changed(self, item: QTreeWidgetItem,
                                column: int) -> None:
        """勾选＝拦截，取消勾选＝放行；locked 项不可改。"""
        if column != 2:
            return
        rid = str(item.data(0, Qt.UserRole) or "")
        rule = self.data.get("policy", {}).get("rules", {}).get(rid)
        if rule is None:
            return
        if rule.get("locked", False):
            # 硬性上限：点了也弹回勾选状态，不写入草稿
            self.policy_tree.blockSignals(True)
            item.setCheckState(2, Qt.Checked)
            self.policy_tree.blockSignals(False)
            return
        rule["enabled"] = item.checkState(2) == Qt.Checked

    def _on_policy_selected(self, current: QTreeWidgetItem | None,
                            _previous: QTreeWidgetItem | None) -> None:
        if current is None:
            return
        rid = str(current.data(0, Qt.UserRole))
        self._current_policy_id = rid
        rule = self.data["policy"]["rules"].get(rid, {})
        self._loading_policy = True
        self.policy_detector.setCurrentText(str(rule.get("detector", "regex")))
        self.policy_pattern.setText(str(rule.get("pattern") or ""))
        self.policy_feature.setText(str(rule.get("feature") or ""))
        self.policy_desc.setText(str(rule.get("description", "")))
        self._loading_policy = False
        self._sync_policy_fields()

    def _sync_policy_fields(self) -> None:
        detector = self.policy_detector.currentText()
        self.policy_pattern.setEnabled(detector == "regex")
        self.policy_feature.setEnabled(detector in ("feature", "threshold"))

    def _flush_policy_rule(self, *_args) -> None:
        if getattr(self, "_loading_policy", False):
            return
        rid = getattr(self, "_current_policy_id", "")
        rule = self.data["policy"]["rules"].get(rid)
        if rule is None:
            return
        rule["detector"] = self.policy_detector.currentText()
        rule["pattern"] = self.policy_pattern.text().strip()
        rule["feature"] = self.policy_feature.text().strip() or None
        rule["description"] = self.policy_desc.text()
        self._sync_policy_fields()
        current = self.policy_tree.currentItem()
        if current is not None:
            current.setText(1, rule["description"])
            current.setToolTip(1, rule["description"])

    def _add_policy_rule(self) -> None:
        rules = self.data.setdefault("policy", {}).setdefault("rules", {})
        index = 1
        while f"custom_{index}" in rules:
            index += 1
        rid = f"custom_{index}"
        rules[rid] = {"label": "自定义规则", "detector": "regex", "pattern": "",
                      "level": "BLOCK", "enabled": True,
                      "description": "新规则：命中即不弹窗"}
        self._populate_policy_table()
        for i in range(self.policy_tree.topLevelItemCount()):
            item = self.policy_tree.topLevelItem(i)
            if item.data(0, Qt.UserRole) == rid:
                self.policy_tree.setCurrentItem(item)
                return

    def _delete_policy_rule(self) -> None:
        rid = getattr(self, "_current_policy_id", "")
        if not rid:
            return
        self.data["policy"]["rules"].pop(rid, None)
        # 墓碑：deep_merge 以出厂规则为底，会把删掉的键原样补回来
        policy = self.data.setdefault("policy", {})
        removed = set(policy.get("removed_rules") or ())
        removed.add(rid)
        policy["removed_rules"] = sorted(removed)
        self._current_policy_id = ""
        self._populate_policy_table()
        self.legacy_hint.setText(f"已删除拦截规则 {rid}。")

    # -- 搜索 --------------------------------------------------------------
    def _build_search(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(10)
        layout.addWidget(self._section_title("网页搜索"))
        layout.addWidget(self._note(
            "勾选后出现在顶栏搜索菜单和结果页右键菜单，保存后立即生效。"
            "双击名称或网址可以修改，网址里用 %s 或 {q} 表示查询词。"))
        self.provider_table = QTableWidget()
        self.provider_table.setColumnCount(3)
        self.provider_table.setHorizontalHeaderLabels(["启用", "名称", "网址"])
        self.provider_table.verticalHeader().setVisible(False)
        self.provider_table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        header = self.provider_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.provider_table.itemChanged.connect(self._on_provider_changed)
        row_buttons = QHBoxLayout()
        add = QPushButton("＋ 新增搜索")
        add.clicked.connect(self._add_provider)
        row_buttons.addWidget(add)
        delete = QPushButton("删除选中")
        delete.clicked.connect(self._delete_provider)
        row_buttons.addWidget(delete)
        row_buttons.addStretch(1)
        layout.addLayout(row_buttons)
        layout.addWidget(self.provider_table, 1)
        self._reload_provider_table()
        return w

    def _reload_provider_table(self) -> None:
        providers = self.data.get("search_providers", [])
        self.provider_table.blockSignals(True)
        self.provider_table.setRowCount(len(providers))
        for row, item in enumerate(providers):
            check = QTableWidgetItem("")
            check.setFlags(check.flags() | Qt.ItemIsUserCheckable)
            check.setCheckState(Qt.Checked if item.get("enabled", True)
                               else Qt.Unchecked)
            self.provider_table.setItem(row, 0, check)
            self.provider_table.setItem(row, 1, QTableWidgetItem(
                str(item.get("label") or item.get("id"))))
            url = str(item.get("url_template") or "")
            url_cell = QTableWidgetItem(url)
            url_cell.setToolTip(url)
            self.provider_table.setItem(row, 2, url_cell)
        self.provider_table.blockSignals(False)

    def _on_provider_changed(self, cell) -> None:
        providers = self.data.get("search_providers", [])
        row = cell.row()
        if not (0 <= row < len(providers)):
            return
        item = providers[row]
        if cell.column() == 0:
            item["enabled"] = cell.checkState() == Qt.Checked
        elif cell.column() == 1:
            item["label"] = cell.text().strip() or item.get("id")
        elif cell.column() == 2:
            item["url_template"] = cell.text().strip()

    def _add_provider(self) -> None:
        name, ok = QInputDialog.getText(self, "新增搜索", "名称（例如：豆瓣）：")
        if not ok or not name.strip():
            return
        url, ok = QInputDialog.getText(
            self, "新增搜索", "网址（用 %s 或 {q} 表示查询词）：",
            text="https://example.com/search?q=%s")
        if not ok:
            return
        if not url.strip() or ("%s" not in url and "{q}" not in url):
            self.status_hint.setText("网址里要有 %s 或 {q}，查询词才能填进去。")
            return
        providers = self.data.setdefault("search_providers", [])
        ids = {str(p.get("id")) for p in providers}
        index = 1
        while f"custom.{index}" in ids:
            index += 1
        providers.append({
            "id": f"custom.{index}", "label": name.strip(),
            "url_template": url.strip(), "mode": "default_browser",
            "enabled": True, "order": 100 + index, "contexts": ["*"],
        })
        self._reload_provider_table()
        self.status_hint.setText("")

    def _delete_provider(self) -> None:
        row = self.provider_table.currentRow()
        providers = self.data.get("search_providers", [])
        if not (0 <= row < len(providers)):
            self.status_hint.setText("先选中一行，再点删除。")
            return
        providers.pop(row)
        self._reload_provider_table()
        self.status_hint.setText("")

    # -- 行为 --------------------------------------------------------------
    # -- 知识库 ------------------------------------------------------------
    def _library_root(self) -> Path:
        """与运行时一致：填了自定义目录就用它，否则 XDG 数据目录。"""
        custom = str(self.data.get("desktop_library_dir") or "").strip()
        if custom:
            return Path(custom).expanduser()
        base = os.environ.get("XDG_DATA_HOME") or str(
            Path.home() / ".local" / "share")
        return Path(base) / "clip-llm02" / "library"

    def _on_library_dir_changed(self, text: str) -> None:
        self.data["desktop_library_dir"] = text.strip()
        self._reload_library_folders()

    def _open_library_dir(self) -> None:
        root = self._library_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.status_hint.setText(f"⚠ 无法创建目录：{exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    def _legacy_folder_map(self) -> dict[str, str]:
        """英文 route id → 中文分类名（旧版本用裸 id 建过文件夹）。"""
        try:
            routes = load_defaults().routes
        except Exception:               # 配置异常时不影响设置窗口
            return {}
        return {rid: str(pack.get("label")) for rid, pack in routes.items()
                if pack.get("label") and str(pack.get("label")) != rid}

    def _tidy_library_folders(self) -> None:
        """把英文 id 目录里的笔记并到中文分类，空的旧目录顺手删掉。"""
        root = self._library_root()
        moved = 0
        for old, new in self._legacy_folder_map().items():
            source = root / old
            if not source.is_dir():
                continue
            target = root / new
            target.mkdir(parents=True, exist_ok=True)
            for item in sorted(source.glob("*.md")):
                destination = target / item.name
                if destination.exists():      # 同名不覆盖，留给用户判断
                    continue
                try:
                    item.rename(destination)
                except OSError:
                    continue
                moved += 1
            try:
                source.rmdir()                # 清空了才删
            except OSError:
                pass
        self._reload_library_folders()
        self.status_hint.setText(
            f"已整理 {moved} 篇到中文分类。" if moved else "没有需要整理的旧分类。")

    def _reload_library_folders(self) -> None:
        """扫描库根，列出「分类文件夹 → 笔记数」，让用户看清结构。"""
        root = self._library_root()
        self.library_actual.setText(f"当前实际路径：{root}")
        rows: list[tuple[str, int]] = []
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir():
                    rows.append((child.name, sum(1 for _ in child.glob("*.md"))))
        self.library_table.setRowCount(len(rows))
        for index, (name, count) in enumerate(rows):
            self.library_table.setItem(index, 0, QTableWidgetItem(name))
            cell = QTableWidgetItem(str(count))
            cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.library_table.setItem(index, 1, cell)
        total = sum(count for _, count in rows)
        legacy = [name for name, _ in rows if name in self._legacy_folder_map()]
        if not rows:
            self.library_hint.setText(
                "目录还是空的：成功查询一次后会自动按分类建文件夹。")
            return
        note = f"共 {len(rows)} 个分类、{total} 篇笔记。"
        note += "「通用」是自由输入，「通用解析」是 general 模板。"
        if legacy:
            note += f" 其中 {('、'.join(legacy))} 是旧版本留下的英文目录，可点「整理旧分类」合并。"
        self.library_hint.setText(note)

    def _build_library(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        intro = QLabel(
            "每次查询成功后，结果会写成一篇 Markdown：\n"
            "    <库根> / <分类> / 日期_标题.md\n"
            "分类取「① 提示词模板」的名称（LaTeX 公式 / 化学物质 / 英语单词…），"
            "自定义查询落在「通用」，追问会追加到同一天的同一篇笔记。"
            "同名文件直接覆盖，Obsidian / Logseq 可直接把库根添加为仓库。")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.library_check = QCheckBox("保存查询结果到知识库（默认开启）")
        self.library_check.toggled.connect(
            lambda v: self.data.__setitem__("desktop_library_enabled", bool(v)))
        layout.addWidget(self.library_check)

        form = QFormLayout()
        self.library_dir_edit = QLineEdit()
        self.library_dir_edit.setPlaceholderText(
            "留空 = ~/.local/share/clip-llm02/library")
        self.library_dir_edit.textChanged.connect(self._on_library_dir_changed)
        dir_row = QHBoxLayout()
        dir_row.setSpacing(6)
        dir_row.addWidget(self.library_dir_edit, 1)
        self.library_browse = QPushButton("选择…")
        self.library_browse.clicked.connect(self._pick_library_dir)
        dir_row.addWidget(self.library_browse)
        self.library_open = QPushButton("打开")
        self.library_open.setToolTip("在文件管理器里打开知识库目录")
        self.library_open.clicked.connect(self._open_library_dir)
        dir_row.addWidget(self.library_open)
        form.addRow("知识库目录", dir_row)
        layout.addLayout(form)

        self.library_actual = QLabel("")
        self.library_actual.setWordWrap(True)
        self.library_actual.setObjectName("count")
        layout.addWidget(self.library_actual)

        head = QHBoxLayout()
        head.addWidget(QLabel("现有分类"))
        head.addStretch(1)
        self.library_refresh = QPushButton("刷新")
        self.library_refresh.clicked.connect(self._reload_library_folders)
        head.addWidget(self.library_refresh)
        self.library_tidy = QPushButton("整理旧分类")
        self.library_tidy.setToolTip(
            "把旧版本用英文 id 建出的文件夹（如 chem_substance）"
            "合并到对应中文分类，同名笔记不覆盖")
        self.library_tidy.clicked.connect(self._tidy_library_folders)
        head.addWidget(self.library_tidy)
        layout.addLayout(head)

        self.library_table = QTableWidget(0, 2)
        self.library_table.setHorizontalHeaderLabels(["分类文件夹", "笔记数"])
        self.library_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self.library_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents)
        self.library_table.verticalHeader().setVisible(False)
        self.library_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.library_table, 1)

        self.library_hint = QLabel("")
        self.library_hint.setWordWrap(True)
        layout.addWidget(self.library_hint)

        self._reload_library_folders()
        return page

    def _build_behavior(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(10)
        self.behavior_hint = QLabel("")
        self.behavior_hint.setObjectName("statusHint")
        self.behavior_hint.setWordWrap(True)
        layout.addWidget(self.behavior_hint)

        layout.addWidget(self._section_title("快捷键"))
        layout.addWidget(self._note(
            "均可修改，冲突会在保存时拦下；图片识别还可注册为 GNOME 全局键。"))
        hotkey_form = QFormLayout()
        hotkey_form.setSpacing(8)
        self.hotkey_edits: dict[str, QKeySequenceEdit] = {}
        hotkeys = dict(DEFAULT_HOTKEYS)
        hotkeys.update(self.data.get("hotkeys") or {})
        for action, label in HOTKEY_LABELS.items():
            edit = QKeySequenceEdit(QKeySequence(str(hotkeys.get(action, ""))))
            self.hotkey_edits[action] = edit
            hotkey_form.addRow(label, edit)
        layout.addLayout(hotkey_form)
        row = QHBoxLayout()
        reset = QPushButton("恢复默认")
        reset.clicked.connect(self._reset_hotkeys)
        row.addWidget(reset)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(self._note(
            "全局图片识别快捷键：clipllm02-desktop --register-shortcuts"))

        layout.addSpacing(8)
        layout.addWidget(self._section_title("窗口行为"))
        form = QFormLayout()
        form.setSpacing(8)
        self.persistent_check = QCheckBox("固定窗口（常驻，出现在 Alt+Tab）")
        self.persistent_check.toggled.connect(
            lambda v: self.data.__setitem__("desktop_persistent_window", bool(v)))
        form.addRow(self.persistent_check)
        # 知识库相关控件已独立成「知识库」页签，这里不再重复
        self.new_tab_check = QCheckBox("新查询在新标签页打开")
        self.new_tab_check.toggled.connect(
            lambda v: self.data.__setitem__("desktop_new_query_in_new_tab", bool(v)))
        self.auto_hide_check = QCheckBox("鼠标离开约 0.4 秒后自动隐藏")
        self.auto_hide_check.toggled.connect(
            lambda v: self.data.__setitem__("desktop_auto_hide", bool(v)))
        self.dup_spin = QSpinBox()
        self.dup_spin.setRange(0, 3600)
        self.dup_spin.valueChanged.connect(
            lambda v: self.data.__setitem__("desktop_dup_ttl", int(v)))
        form.addRow(self.new_tab_check)
        form.addRow(self.auto_hide_check)
        form.addRow("相同内容去重（秒）", self.dup_spin)
        layout.addLayout(form)
        layout.addWidget(self._note(
            "不勾「固定窗口」，查完就自动隐藏；按 Esc 关闭初判窗口。"))
        layout.addStretch(1)
        return w

    # -- 类别总开关 --------------------------------------------------------
    def _rebuild_switches(self) -> None:
        """开关数量恒等于 routes 数量；增删模板后调用即自动同步。"""
        while self.switch_grid.count():
            item = self.switch_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.domain_checks.clear()
        routes = self.data.get("routes", {})
        columns = max(3, min(5, self.width() // 170))
        for index, (rid, pack) in enumerate(routes.items()):
            label = str(pack.get("label") or rid)
            cb = QCheckBox(label)
            cb.setToolTip(f"提示词模板：{label}")
            cb.setChecked(bool(pack.get("enabled", True)))
            cb.toggled.connect(
                lambda v, k=rid: self.data["routes"][k].__setitem__(
                    "enabled", bool(v)))
            self.domain_checks[rid] = cb
            self.switch_grid.addWidget(cb, index // columns, index % columns)
        self._reload_other_views()

    # -- 事务 --------------------------------------------------------------
    def _sync(self) -> None:
        self.font_spin.setValue(int(self.data.get("desktop_font_size", 14)))
        self.dark_check.setChecked(bool(self.data.get("desktop_dark_theme", False)))
        self._rebuild_switches()
        self.persistent_check.setChecked(
            bool(self.data.get("desktop_persistent_window", True)))
        self.library_check.setChecked(
            bool(self.data.get("desktop_library_enabled", True)))
        self.library_dir_edit.setText(
            str(self.data.get("desktop_library_dir") or ""))
        self._reload_library_folders()
        self.new_tab_check.setChecked(
            bool(self.data.get("desktop_new_query_in_new_tab", False)))
        self.auto_hide_check.setChecked(
            bool(self.data.get("desktop_auto_hide", True)))
        self.dup_spin.setValue(int(self.data.get("desktop_dup_ttl", 1)))
        self.max_tokens_spin.setValue(int(self.data.get("desktop_max_tokens", 2048)))
        self._reload_tree()
        self.select_first_intent()
        self._load_route_prompt(self.route_combo.currentIndex())

    def _apply(self) -> bool:
        hotkeys, conflict = self._collect_hotkeys()
        if conflict:
            # 切到行为页 + 底部固定提示，当前在哪一页都看得见
            self.tabs.setCurrentWidget(self.behavior_tab)
            self.behavior_hint.setText("⚠ " + conflict)
            self.legacy_hint.setText(conflict)
            self.status_hint.setText("⚠ " + conflict)
            return False
        self.data["hotkeys"] = hotkeys
        # 图片提示词的唯一编辑入口在「其他视角」页（写入 route_prompts），
        # desktop_vision_prompt 仅作模板为空时的兜底，不再由设置页覆盖。
        errors = validate(Settings(self.data))
        if errors:
            message = "配置校验失败：" + "；".join(e.message for e in errors[:3])
            self.legacy_hint.setText(message)
            self.status_hint.setText("⚠ " + message)
            return False
        save_config(self.data)
        self._dark = self.dark_check.isChecked()
        self.setStyleSheet(build_qss(self._dark) + settings_qss(self._dark))
        apply_settings_palette(self, self._dark)
        self.status_hint.setText("")
        self.applied.emit(copy.deepcopy(self.data))
        return True

    def _accept(self) -> None:
        if self._apply():
            self.accept()
