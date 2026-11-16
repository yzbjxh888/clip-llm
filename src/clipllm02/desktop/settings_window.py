"""设置窗口：把 0.2 的所有能力暴露成可插拔选项。

草稿事务：所有控件只改 ``self.draft``，取消整体丢弃，确定/应用才校验、原子写盘、
通过 ``applied`` 信号让主进程热重载。
"""
from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from ..settings import validate
from ..studio.config import config_path, load_config, save_config


def user_settings_path() -> Path:
    """兼容旧调用：配置路径由 studio.config 统一管理。"""
    return config_path()


def load_user_settings():
    """主进程用：读用户配置；升级/迁移统一走 studio.config。"""
    return load_config()


class SettingsWindow(QWidget):
    applied = Signal(object)             # 新的完整配置 dict
    validation_failed = Signal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._original = copy.deepcopy(settings.as_dict())
        self.draft = copy.deepcopy(self._original)
        self.settings = settings
        self.setWindowTitle("clip-llm 0.2 · 设置")
        self.resize(640, 620)

        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_general(), "通用")
        self.tabs.addTab(self._build_intents(), "意图与领域")
        self.tabs.addTab(self._build_policy(), "输入与拦截")
        self.tabs.addTab(self._build_search(), "搜索与跳转")
        self.tabs.addTab(self._build_model(), "模型")
        root.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Apply)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.Apply).setText("应用")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self._reject)
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self._apply)
        root.addWidget(buttons)

        self._sync_from_draft()

    # -- 通用 --------------------------------------------------------------
    def _build_general(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.cb_hide_preview = QCheckBox("隐藏输入预览（隐私）")
        self.cb_show_hints = QCheckBox("显示快捷键提示")
        self.cb_auto_submit = QCheckBox("高置信时自动提交（不推荐）")
        self.combo_disabled = QComboBox()
        self.combo_disabled.addItems(["grayed", "hidden", "hidden_in_fold"])
        self.combo_default = QComboBox()
        self.combo_default.addItems(["last_used", "first", "none"])
        self.combo_trigger = QComboBox()
        self.combo_trigger.addItems(["auto", "menu", "confirm", "hotkey"])
        self.combo_conflict = QComboBox()
        self.combo_conflict.addItems(["image_only", "text_only", "mixed", "ask"])
        form.addRow(self.cb_hide_preview)
        form.addRow(self.cb_show_hints)
        form.addRow(self.cb_auto_submit)
        form.addRow("已禁用项显示方式", self.combo_disabled)
        form.addRow("默认意图策略", self.combo_default)
        form.addRow("触发策略", self.combo_trigger)
        form.addRow("图片/文本冲突策略", self.combo_conflict)

        self.cb_hide_preview.toggled.connect(
            lambda v: self.draft.__setitem__("desktop_hide_preview", bool(v)))
        self.cb_show_hints.toggled.connect(
            lambda v: self.draft.__setitem__("desktop_show_hints", bool(v)))
        self.cb_auto_submit.toggled.connect(
            lambda v: self.draft["menu"].__setitem__("auto_submit", bool(v)))
        self.combo_disabled.currentTextChanged.connect(
            lambda v: self.draft["menu"].__setitem__("show_disabled_items", v))
        self.combo_default.currentTextChanged.connect(
            lambda v: self.draft["menu"].__setitem__("default_intent_policy", v))
        self.combo_trigger.currentTextChanged.connect(
            lambda v: self.draft.__setitem__("trigger_policy", v))
        self.combo_conflict.currentTextChanged.connect(
            lambda v: self.draft.__setitem__("image_text_conflict", v))
        return w

    # -- 意图与领域 --------------------------------------------------------
    def _build_intents(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("取消勾选即可关闭某个解释方式（例如「关闭词组查询」）"))
        self.intent_list = QListWidget()
        for intent in self.settings.intents:
            item = QListWidgetItem(f"{intent.label}　[{intent.id}]")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if intent.enabled else Qt.Unchecked)
            item.setData(Qt.UserRole, intent.id)
            self.intent_list.addItem(item)
        self.intent_list.itemChanged.connect(self._on_intent_changed)
        layout.addWidget(self.intent_list, 1)

        group = QGroupBox("领域总闸（关闭英语查询 = 取消 en_query）")
        grid = QVBoxLayout(group)
        self.domain_checks: dict[str, QCheckBox] = {}
        for key, label in (("en_query", "英语查询"), ("zh_query", "中文查询"),
                           ("latex_query", "数学 / 公式"), ("chem_query", "化学"),
                           ("code_query", "编程"), ("image_intents", "图片意图")):
            cb = QCheckBox(label)
            cb.toggled.connect(
                lambda v, k=key: self.draft["feature_switches"].__setitem__(k, bool(v)))
            self.domain_checks[key] = cb
            grid.addWidget(cb)
        layout.addWidget(group)
        return w

    def _on_intent_changed(self, item) -> None:
        intent_id = str(item.data(Qt.UserRole))
        enabled = item.checkState() == Qt.Checked
        for entry in self.draft.get("intents", []):
            if entry.get("id") == intent_id:
                entry["enabled"] = enabled
                break

    # -- 拦截 --------------------------------------------------------------
    def _build_policy(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        form = QFormLayout(inner)
        self.policy_checks: dict[str, QCheckBox] = {}
        for rule_id, rule in self.settings.policy.get("rules", {}).items():
            cb = QCheckBox("拦截")
            locked = bool(rule.get("locked", False))
            cb.setChecked(rule.get("enabled", True) is not False or locked)
            cb.setEnabled(not locked)
            cb.toggled.connect(
                lambda v, rid=rule_id:
                self.draft["policy"]["rules"][rid].__setitem__("enabled", bool(v)))
            label = QLabel(f"{rule.get('label') or rule_id}　"
                           f"{rule.get('description', '')}")
            label.setWordWrap(True)
            form.addRow(label, cb)
            self.policy_checks[rule_id] = cb
        scroll.setWidget(inner)
        return scroll

    # -- 搜索 --------------------------------------------------------------
    def _build_search(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("启用的搜索源会出现在菜单工具区与结果卡片右键菜单"))
        self.provider_list = QListWidget()
        for provider in self.settings.providers:
            item = QListWidgetItem(f"{provider.label}　[{provider.id}]")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if provider.enabled else Qt.Unchecked)
            item.setData(Qt.UserRole, provider.id)
            self.provider_list.addItem(item)
        self.provider_list.itemChanged.connect(self._on_provider_changed)
        layout.addWidget(self.provider_list, 1)
        note = QLabel("URL 模板必须含 {q} 且为 http(s)；显式浏览器模式要求本机存在该可执行文件。")
        note.setWordWrap(True)
        layout.addWidget(note)
        return w

    def _on_provider_changed(self, item) -> None:
        provider_id = str(item.data(Qt.UserRole))
        enabled = item.checkState() == Qt.Checked
        for entry in self.draft.get("search_providers", []):
            if entry.get("id") == provider_id:
                entry["enabled"] = enabled
                break

    # -- 模型 --------------------------------------------------------------
    def _build_model(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.edit_api_url = QLineEdit()
        self.edit_api_url.setPlaceholderText("https://.../v1/chat/completions")
        self.edit_api_key = QLineEdit()
        self.edit_api_key.setEchoMode(QLineEdit.Password)
        self.edit_model = QLineEdit()
        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(5, 600)
        self.spin_max_tokens = QSpinBox()
        self.spin_max_tokens.setRange(256, 32768)
        form.addRow("API URL", self.edit_api_url)
        form.addRow("API Key", self.edit_api_key)
        form.addRow("模型名", self.edit_model)
        form.addRow("超时（秒）", self.spin_timeout)
        form.addRow("max_tokens", self.spin_max_tokens)
        note = QLabel("留空任何一项都不会发起网络请求，卡片将显示「本地请求预览」。\n"
                      "Key 只写入本机配置（0600），不进日志。")
        note.setWordWrap(True)
        form.addRow(note)
        for edit, key in ((self.edit_api_url, "desktop_api_url"),
                          (self.edit_api_key, "desktop_api_key"),
                          (self.edit_model, "desktop_model")):
            edit.textChanged.connect(lambda v, k=key: self.draft.__setitem__(k, v))
        self.spin_timeout.valueChanged.connect(
            lambda v: self.draft.__setitem__("desktop_timeout", int(v)))
        self.spin_max_tokens.valueChanged.connect(
            lambda v: self.draft.__setitem__("desktop_max_tokens", int(v)))
        return w

    # -- 草稿事务 ----------------------------------------------------------
    def _sync_from_draft(self) -> None:
        general = self.draft
        menu = general.get("menu", {})
        widgets = [
            (self.cb_hide_preview, general.get("desktop_hide_preview", False)),
            (self.cb_show_hints, general.get("desktop_show_hints", True)),
            (self.cb_auto_submit, menu.get("auto_submit", False)),
        ]
        for widget, value in widgets:
            widget.blockSignals(True)
            widget.setChecked(bool(value))
            widget.blockSignals(False)
        for combo, value in ((self.combo_disabled, menu.get("show_disabled_items", "grayed")),
                             (self.combo_default, menu.get("default_intent_policy", "last_used")),
                             (self.combo_trigger, general.get("trigger_policy", "auto")),
                             (self.combo_conflict, general.get("image_text_conflict", "image_only"))):
            combo.blockSignals(True)
            combo.setCurrentText(str(value))
            combo.blockSignals(False)
        for key, cb in self.domain_checks.items():
            cb.blockSignals(True)
            cb.setChecked(bool(general.get("feature_switches", {}).get(key, True)))
            cb.blockSignals(False)
        for edit, key in ((self.edit_api_url, "desktop_api_url"),
                          (self.edit_api_key, "desktop_api_key"),
                          (self.edit_model, "desktop_model")):
            edit.blockSignals(True)
            edit.setText(str(general.get(key, "")))
            edit.blockSignals(False)
        self.spin_timeout.blockSignals(True)
        self.spin_timeout.setValue(int(general.get("desktop_timeout", 60)))
        self.spin_timeout.blockSignals(False)
        self.spin_max_tokens.blockSignals(True)
        self.spin_max_tokens.setValue(int(general.get("desktop_max_tokens", 2048)))
        self.spin_max_tokens.blockSignals(False)

    def _validate(self) -> bool:
        from ..settings import Settings
        errors = validate(Settings(self.draft))
        if errors:
            message = "；".join(f"[{e.tab}] {e.field}: {e.message}" for e in errors[:5])
            self.validation_failed.emit(message)
            return False
        return True

    def _apply(self) -> bool:
        if not self._validate():
            return False
        save_config(self.draft)
        self._original = copy.deepcopy(self.draft)
        self.applied.emit(copy.deepcopy(self.draft))
        return True

    def _accept(self) -> None:
        if self._apply():
            self.close()

    def _reject(self) -> None:
        """取消：整体丢弃草稿，不写盘、不热重载。"""
        self.draft = copy.deepcopy(self._original)
        self._sync_from_draft()
        self.close()
