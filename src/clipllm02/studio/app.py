"""Studio 主控：剪贴板线程 → 初判 → 标签页 → 请求 → 精美排版。

前台进程，不注册 systemd。主进程禁用 text-input，中文输入与设置走独立进程；
所有 UI 更新都在主线程，模型/视觉请求在工作线程。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QMenu, QSystemTrayIcon,
)

from ..desktop.clipboard_worker import ClipboardWorker  # noqa: F401 (serve 兼容)
from .clipboard_monitor import ClipboardMonitor
from ..desktop.gateway import Gateway, GatewayConfig, GatewayResult, ResultCache
from ..features import extract_features
from ..models import (
    DomainCandidate, InputEnvelope, Preferences, Suggestion, UserSelection,
    new_id, sha256_text,
)
from ..policy import preflight
from ..prompts import build_request_prompt, followup_system
from ..serve import ClipboardReader
from ..settings import Settings, structure_signature
from ..suggest import build_menu, suggest
from .config import load_config, settings_from_data
from . import __main__ as studio_build
from .input_bridge import InputBridge
from .json_render import (RENDER_SCHEMA, render_json_result,
                          render_local_preview, render_notice)
from .main_window import MainWindow
from .vision import (
    capture_screenshot, read_clipboard_image, read_image_file, recognize,
)

_IMAGE_REF = re.compile(r"@image:[^\s]+")


def strip_image_refs(text: str) -> str:
    """去掉 IDE / 视觉模型塞进文本里的 @image:路径 图片引用标记。

    这类标记不是用户要查的内容，混进查询会把结果变成一长串本地路径。
    """
    return _IMAGE_REF.sub("", text or "").strip()


class _Bridge(QObject):
    finished = Signal(object)


@dataclass
class StudioState:
    prefs: Preferences = field(default_factory=Preferences)
    generation: int = 0
    errors: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=lambda: {
        "events": 0, "blocked": 0, "tabs": 0, "requests": 0,
        "previews": 0, "vision": 0, "duplicates": 0})
    quota_day: str = ""
    requests_today: int = 0


class StudioApp(QObject):
    bridge_vision = Signal(object)

    def __init__(self, settings: Settings, *, poll_interval: float = 1.0,
                 parent=None):
        super().__init__(parent)
        self.settings = settings
        self.state = StudioState()
        self._alive = True
        self._sigint = False
        self._seen: dict[str, float] = {}
        self._self_copy: set[str] = set()
        self._cancel: threading.Event | None = None

        self.bridge = _Bridge()
        self.bridge.finished.connect(self._on_worker_done)
        self.bridge_vision.connect(self._on_vision_done)
        self.gateway = Gateway(GatewayConfig.from_settings(settings))
        self.cache = ResultCache()
        self.reader = ClipboardReader()

        self.window = MainWindow(settings)
        self.input_bridge = InputBridge(self)
        self._child_windows = 0               # 打开的独立子窗口数（置顶协调）
        self.input_bridge.text_ready.connect(self._on_input_text)
        self.input_bridge.settings_ready.connect(self.reload_settings)
        self.input_bridge.failed.connect(self._on_input_failed)

        # CopyQ 式事件驱动 + Wayland watch 监听（文本 + 图片）
        self.clip_monitor = ClipboardMonitor(self)
        self.clip_monitor.text_found.connect(self._on_clipboard_text)
        self.clip_monitor.image_found.connect(self._on_clipboard_image)

        self.window.tab_created.connect(self._connect_tab)
        self.window.query_submitted.connect(self.on_query_submitted)
        self.window.new_tab_requested.connect(self.on_new_tab)
        self.window.settings_requested.connect(self.open_settings)
        self.window.vision_image_requested.connect(self.on_vision)
        self.window.open_input_requested.connect(self.open_input)
        # 独立输入进程开着时，Esc 归那个窗口，主窗口不响应
        self.window.set_input_activity_check(self.input_bridge.has_active)
        self.window.search_provider_requested.connect(self.on_search_provider)
        self.window.requery_requested.connect(self.on_requery)
        self.window.back_requested.connect(self.on_back)
        self._connect_tab(self.window.current_tab())
        self.window.fill_search_menu(
            self.settings.enabled_search_providers(("*",)))
        self.window.set_look(int(settings.get("desktop_font_size", 14)),
                             bool(settings.get("desktop_dark_theme", False)))

        # 全局快捷键（gsettings）通过 IPC 文件把请求送进常驻进程
        self.ipc_dir = Path(os.environ.get("XDG_CACHE_HOME",
                                          str(Path.home() / ".cache"))) / "clip-llm02" / "ipc"
        self.ipc_timer = QTimer(self)
        self.ipc_timer.setInterval(500)
        self.ipc_timer.timeout.connect(self._check_ipc)
        self._paused = False

        # 托盘入口：窗口无焦点（焦点防护开启）时，快捷键收不到，
        # 托盘菜单是永可达的设置 / 截图入口。
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self._tray_icon(), self)
            menu = QMenu()
            menu.addAction("⚙ 设置 (F3)", self.open_settings)
            menu.addAction("🖼 截图 / 图片识别", self.on_vision)
            menu.addAction("显示窗口", self.show_window)
            menu.addSeparator()
            menu.addAction("退出", self.shutdown)
            self.tray.setContextMenu(menu)
            self.tray.setToolTip("clip-llm · 复制内容自动初判\n"
                                 f"build {studio_build.BUILD}")
            self.tray.activated.connect(self._on_tray_activated)
            self.tray.show()

    @staticmethod
    def _tray_icon():
        from .main_window import app_icon
        icon = app_icon()
        if not icon.isNull():
            return icon
        # 兜底：没有图标资源时画一个简单的方块，避免托盘空白
        from PySide6.QtGui import QIcon, QPixmap
        from .theme_tokens import tokens
        t = tokens()
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.transparent)
        from PySide6.QtGui import QPainter, QColor, QFont
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(t["primary"]))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(2, 2, 28, 28, 7, 7)
        painter.setPen(QColor(t["on_primary"]))
        painter.setFont(QFont("sans", 15, QFont.Bold))
        painter.drawText(pixmap.rect(), Qt.AlignCenter, "释")
        painter.end()
        return QIcon(pixmap)

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.window.show_without_focus(bring_to_front=True)

    # -- 诊断与策略上下文 ---------------------------------------------------
    def _diag(self, event: str, **fields) -> None:
        """结构化单行诊断；仅在 CLIPLLM_DIAG 下输出，绝不打印原文与 Key。"""
        if not os.environ.get("CLIPLLM_DIAG"):
            return
        parts = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
        print(f"[diag] {time.time():.3f} {event} {parts}", flush=True)

    def _policy_runtime(self) -> dict:
        """配额按自然日重置，避免长驻进程把累计请求数当成当日用量。"""
        today = time.strftime("%Y-%m-%d")
        if self.state.quota_day != today:
            self.state.quota_day = today
            self.state.requests_today = 0
        return {
            "requests_today": self.state.requests_today,
            "daily_limit": int(self.settings.get("daily_request_limit", 0)),
        }

    def _sensitive_patterns(self) -> tuple[str, ...]:
        raw = self.settings.policy.get("sensitive_patterns") or ()
        return tuple(str(p) for p in raw if p)

    # -- 标签信号 ----------------------------------------------------------
    def _connect_tab(self, tab) -> None:
        if getattr(tab, "_studio_connected", False):
            return
        tab._studio_connected = True
        tab.request_generation = getattr(tab, "request_generation", 0)
        tab.intent_selected.connect(lambda data, t=tab: self.on_intent(t, data))
        tab.domain_changed.connect(lambda did, t=tab: self.on_domain(t, did))
        tab.category_changed.connect(
            lambda cid, t=tab: self.on_category(t, cid))
        tab.new_tab_requested.connect(self.on_new_tab)
        tab.search_requested.connect(self.on_search)
        tab.settings_requested.connect(self.open_settings)
        tab.copy_query_requested.connect(self.on_copy_query)
        tab.vision_image_requested.connect(self.on_vision)
        tab.dismiss_requested.connect(self.window._on_dismiss)
        tab.ime_requested.connect(self.open_free_input)
        tab.self_copy_requested.connect(self.clip_monitor.suppress_text)
        tab.followup_requested.connect(
            lambda text, t=tab: self.on_followup(t, text))

    # -- 剪贴板 / IPC ------------------------------------------------------
    @property
    def clip_worker(self):
        """兼容旧测试：事件驱动监听器同样支持 `_active` 开关语义。"""
        return self.clip_monitor

    def show_window(self) -> None:
        """托盘「显示窗口」：唤起并提到最前（不强制重映射，避免闪烁）。"""
        window = self.window
        window._ensure_on_screen()
        window.show()
        if QApplication.platformName() != "offscreen":
            window.raise_()
            window.activateWindow()
        print(f"[clip-llm] 显示窗口: visible={window.isVisible()} "
              f"geometry={window.geometry()}", flush=True)

    def start(self) -> None:
        self.ipc_timer.start()
        self.window.show_without_focus()

    def _check_ipc(self) -> None:
        request = self.ipc_dir / "vision.cmd"
        if request.exists():
            try:
                request.unlink(missing_ok=True)
            except OSError:
                pass
            self.on_vision()

    def _on_clipboard_text(self, text: str) -> None:
        if self._alive and not self._paused:
            self.process_text(text, source="clipboard")

    def _on_clipboard_image(self) -> None:
        """GNOME 截图 / 复制图片进入剪贴板 → 自动识别（截图即用户主动动作）。"""
        if self._alive and not self._paused:
            self.on_vision()

    # -- 子窗口反馈 --------------------------------------------------------
    def _child_opened(self) -> None:
        self._child_windows += 1

    def _child_closed(self) -> None:
        self._child_windows = max(0, self._child_windows - 1)

    def _on_input_failed(self, message: str) -> None:
        self.state.errors.append(f"输入进程：{message}")
        if self.tray is not None:
            self.tray.showMessage("clip-llm", f"子窗口打开失败：{message}",
                                  QSystemTrayIcon.MessageIcon.Warning, 5000)
        self._child_closed()

    # -- 独立输入进程 ------------------------------------------------------
    def open_input(self) -> None:
        self._child_opened()
        self.input_bridge.request_text(
            "query", title="输入要查询的内容（可用中文输入法）",
            placeholder="粘贴或输入内容…",
            initial=self.window.query_edit.text())

    def open_free_input(self) -> None:
        self._child_opened()
        self.input_bridge.request_text(
            "free", title="描述你想怎么解释（可用中文输入法）",
            placeholder="例如：请从动力系统角度分析…", multiline=True)

    def _on_input_text(self, purpose: str, text: str) -> None:
        self._child_closed()
        if not text.strip():
            return
        if purpose == "query":
            self.window.query_edit.setText(text[:200])
            self.process_text(text, source="input_host", force_dedup_bypass=True)
        elif purpose == "free":
            tab = self.window.current_tab()
            self.on_intent(tab, {
                "envelope_id": tab.env.envelope_id if tab.env else "",
                "domain_id": "*", "intent_id": None, "format_id": "static",
                "custom_instruction": text,
                "free_input_mode": "instruction", "style_ref": None,
                "allowed_route_ids": [],
            })

    def on_query_submitted(self, text: str) -> None:
        self.process_text(text, source="query_bar", force_dedup_bypass=True)

    def on_new_tab(self, text: str) -> None:
        self.process_text(text, source="tab", new_tab=True,
                          force_dedup_bypass=True)

    def on_copy_query(self, text: str) -> None:
        self.process_text(text, source="copy", force_dedup_bypass=True)

    def on_back(self) -> None:
        self.window.current_tab().dismiss()

    def on_requery(self) -> None:
        tab = self.window.current_tab()
        data = getattr(tab, "last_selection", None)
        if data:
            self._diag("requery", gen=tab.request_generation)
            self.on_intent(tab, dict(data))

    def on_search_default(self) -> None:
        text = self.window.current_tab().current_text()
        provider_id = str(self.settings.get("search_default_provider", "google"))
        self.on_search(provider_id, text)

    def _duplicate(self, digest: str) -> bool:
        now = time.time()
        if digest in self._self_copy:
            return True
        ttl = float(self.settings.get("desktop_dup_ttl", 300))
        last = self._seen.get(digest)
        if last is not None and ttl > 0 and now - last < ttl:
            return True
        self._seen[digest] = now
        if len(self._seen) > 512:
            self._seen = {k: v for k, v in self._seen.items() if now - v < ttl}
        return False

    # -- 第一层 ------------------------------------------------------------
    def process_text(self, text: str, *, source: str = "clipboard",
                     new_tab: bool = False, force_dedup_bypass: bool = False) -> None:
        text = strip_image_refs(text)
        if not self._alive or not text:
            return
        try:
            digest = sha256_text(text)
            if force_dedup_bypass:
                self._seen.pop(digest, None)
            if self._duplicate(digest):
                self.state.stats["duplicates"] += 1
                return
            self.state.generation += 1
            self.state.stats["events"] += 1
            env = InputEnvelope.for_text(text, source=source)
            policy = preflight(env, self.settings,
                               runtime=self._policy_runtime(),
                               sensitive_patterns=self._sensitive_patterns())
            self._diag("preflight", source=source, level=policy.level,
                       reasons=",".join(policy.block_reasons) or "-")
            if not policy.allowed:
                # 硬拦截完全静默：不弹窗、不抢注意力、不改变窗口显隐。
                # 但必须作废当前标签的待发请求能力，否则上一轮残留的
                # 意图按钮仍能带着旧内容发出请求（BLOCK 形同虚设）。
                self.state.stats["blocked"] += 1
                tab = self.window.current_tab()
                tab.invalidated = True
                tab.last_selection = None
                self._diag("blocked", gen=tab.request_generation,
                           reasons=",".join(policy.block_reasons) or "-")
                return
            s = suggest(env, policy, self.settings)
            if s.classification:
                self._diag("classify", category=s.classification.category,
                           reason=s.classification.reason)
            menu = build_menu(s, self.settings, self.state.prefs)
            use_new_tab = new_tab or bool(
                self.settings.get("desktop_new_query_in_new_tab", False))
            tab = self.window.apply_context(env, policy, s, menu,
                                            new_tab=use_new_tab)
            self._connect_tab(tab)
            self.state.stats["tabs"] = self.window.tabs.count()
            # 新查询必须把常驻窗口带到前面，否则只会被 Chrome 盖住，
            # 用户只能看到 GNOME 的「已就绪」通知
            self.window.show_without_focus(bring_to_front=True)
        except Exception:
            self.state.errors.append(traceback.format_exc(limit=3))

    def on_domain(self, tab, domain_id: str) -> None:
        try:
            env = tab.env
            if env is None:
                return
            # 换领域同样要重跑策略：此前直接复用旧 PolicyResult，规则改严后
            # 换一次领域就能绕过硬否决。
            policy = preflight(env, self.settings,
                               runtime=self._policy_runtime(),
                               sensitive_patterns=self._sensitive_patterns())
            if not policy.allowed:
                tab.invalidated = True
                self._diag("reject-domain",
                           reasons=",".join(policy.block_reasons) or "-")
                return
            label = next((d["label"] for d in self.settings.domains
                          if d["id"] == domain_id), domain_id)
            candidate = DomainCandidate(id=domain_id, label=label, score=1.0,
                                        evidence=("用户手动选择领域",))
            s = Suggestion(
                envelope_id=env.envelope_id, modality=env.modality,
                features=(tab.suggestion.features if tab.suggestion
                          else extract_features(env, self.settings.thresholds)),
                domain_candidates=(candidate,), level="HIGH",
                warnings=policy.warnings,
                classification=(tab.suggestion.classification
                                if tab.suggestion else None))
            tab.set_context(env, policy, s,
                            build_menu(s, self.settings, self.state.prefs))
        except Exception:
            self.state.errors.append(traceback.format_exc(limit=3))

    def on_category(self, tab, category_id: str) -> None:
        """用户从初判层切换到其他提示词模板类别：只重建菜单，不发请求。"""
        try:
            if tab.env is None or tab.suggestion is None or tab.policy is None:
                return
            menu = build_menu(tab.suggestion, self.settings,
                              self.state.prefs, category_id=category_id)
            tab.set_context(tab.env, tab.policy, tab.suggestion, menu)
            self._diag("category", value=category_id)
        except Exception as exc:
            self.state.errors.append(traceback.format_exc(limit=3))
            self._diag("error", where="on_category", exc=type(exc).__name__)

    # -- 第二层 ------------------------------------------------------------
    def on_intent(self, tab, data: dict) -> None:
        try:
            env = tab.env
            if env is None:
                return
            # 追问：本轮正文只是本次追问，不改动 tab.env，
            # 这样「复制再查询」仍然拿原始输入。
            # 追问走真正的多轮：system 沿用上一轮领域设定（输出契约已换成
            # 自由作答），历史消息由 chat_history 提供。pop 而不只是 get，
            # 避免下次普通查询误用追问内容。
            followup = tab.__dict__.pop("_followup", None)
            if followup:
                content_text = followup["question"]
                chat_history = followup["history"]
                system_override = followup["system"]
            else:
                content_text = env.text or ""
                chat_history = None
                system_override = ""
            if getattr(tab, "invalidated", False):
                self._diag("reject-invalidated", gen=tab.request_generation)
                return
            # 硬否决统一防线：自由输入 / Ctrl+R / 旧标签残留 / 领域切换 /
            # 改设置后不重启全部收敛到这里，请求之前不再存在旁路。
            policy = preflight(env, self.settings,
                               runtime=self._policy_runtime(),
                               sensitive_patterns=self._sensitive_patterns())
            if not policy.allowed:
                tab.invalidated = True
                self._diag("reject-blocked", gen=tab.request_generation,
                           reasons=",".join(policy.block_reasons) or "-")
                return
            # CONFIRM：不新开窗口、不新增控件。首次点击只把已有的黄色提示条
            # 改成授权说明并留在初判层；再次点击同一入口才算显式授权。
            level_actions = self.settings.policy.get("levels") or {}
            if policy.level == "CONFIRM" and level_actions.get(
                    "CONFIRM", {}).get("confirm", True):
                signature = json.dumps(data, sort_keys=True, ensure_ascii=False)
                if tab.pending_confirmation != signature:
                    tab.pending_confirmation = signature
                    tab.last_selection = dict(data)
                    tab.warn.setVisible(True)
                    tab.warn.setText(
                        "⚠ 命中「需授权」策略，尚未发送任何网络请求；"
                        "再次点击同一查询方式即视为授权（或按 Ctrl+R）。")
                    self._diag("confirm-pending", level=policy.level)
                    return
                tab.pending_confirmation = None
            tab.last_selection = dict(data)
            tab.request_generation += 1
            generation = tab.request_generation
            self._diag("submit", gen=generation)
            selection = UserSelection(
                request_id=new_id("req"), envelope_id=env.envelope_id,
                domain_id=str(data.get("domain_id") or "*"),
                intent_id=data.get("intent_id"),
                format_id=str(data.get("format_id") or "static"),
                custom_instruction=data.get("custom_instruction"),
                free_input_mode=str(data.get("free_input_mode") or "instruction"),
                style_ref=data.get("style_ref"),
                allowed_route_ids=tuple(data.get("allowed_route_ids") or ()),
            )
            if selection.intent_id:
                self.state.prefs.record(env.text_hash, selection.intent_id,
                                        selection.domain_id)
            composed = build_request_prompt(selection, self.settings,
                                            content=content_text)
            if system_override:
                composed = replace(composed, prompt=system_override)
            tab._pending_content = content_text
            tab._pending_system = composed.prompt
            tab._followup_pending = bool(followup)
            font_size = int(self.settings.get("desktop_font_size", 14))
            dark = bool(self.settings.get("desktop_dark_theme", False))
            tab.show_loading(content_text, hint="正在请求模型..")
            key = composed.cache_key(
                model=self.gateway.config.model, route_id=composed.query_type,
                intent_id=selection.intent_id or "freeform",
                format_id=selection.format_id, schema_version=RENDER_SCHEMA,
                ruleset_version=structure_signature(self.settings)[:16],
                query_hash=env.query_hash, selection_salt=selection.cache_salt())

            if not self.gateway.config.enabled:
                self.state.stats["previews"] += 1
                tab.show_result(render_local_preview(
                    composed, content_text, font_size=font_size, dark=dark),
                    title="查询预览", preview_only=True)
                return
            # 追问的缓存键与上一轮相同（原始输入没变），不能命中旧结果
            cached = None if followup else self.cache.get(key)
            if cached:
                # 缓存只存模型原始文本，按当前字号/主题现场渲染；
                # 改外观无需清缓存，也不会读到上一版渲染器生成的 HTML。
                # 命中仍要记会话并落盘：否则 chat_history 还留着上一轮，
                # 追问会带错上下文，知识库文件也会停在旧会话。
                self._record_chat(tab, cached)
                self._save_library(tab, cached, False)
                tab.show_result(
                    render_json_result(cached, font_size=font_size, dark=dark),
                    title="解释结果")
                return

            self.state.stats["requests"] += 1
            self.state.requests_today += 1
            cancel = threading.Event()
            self._cancel = cancel
            alive = self._alive

            def work():
                try:
                    result = self.gateway.explain(composed.prompt,
                                                  content_text,
                                                  history=chat_history,
                                                  cancel=cancel)
                except Exception:
                    result = GatewayResult(html="", ok=False, error="模型调用异常")
                if alive and self._alive:
                    # 票据随回包携带：标签 / 上下文 / 请求三者必须全部匹配。
                    self.bridge.finished.emit((
                        tab, generation, result, key, font_size, dark,
                        env.envelope_id, selection.request_id))

            threading.Thread(target=work, daemon=True).start()
            self._diag("request", gen=generation,
                       rid=selection.request_id, eid=env.envelope_id)
        except Exception as exc:
            self.state.errors.append(traceback.format_exc(limit=3))
            self._diag("error", where="on_intent", exc=type(exc).__name__)
            tab.show_result(render_notice("错误", "<p>请求构造失败，请重试。</p>"),
                            title="错误", error="构造失败")

    def _save_library(self, tab, raw: str, was_followup: bool) -> None:
        """把本轮结果写成 Markdown；失败只记录，不影响结果展示。"""
        try:
            from .library import save_entry
            selection = getattr(tab, "last_selection", None) or {}
            intent_id = selection.get("intent_id")
            route_id = ""
            intent_label = ""
            if intent_id:
                intent = self.settings.intent(str(intent_id))
                if intent is not None:
                    route_id = intent.primary_route or ""
                    intent_label = intent.label
            pack = (self.settings.routes or {}).get(route_id) or {}
            route_label = str(pack.get("label") or route_id or "通用")
            followup = ""
            if was_followup:
                followup = getattr(tab, "_pending_content", "") or ""
            path = save_entry(
                self.settings,
                query=tab.current_text() or "",
                query_type=str(pack.get("query_type") or route_id),
                route_label=route_label,
                intent_label=intent_label or ("自由输入" if not intent_id else ""),
                followup=followup,
                raw=raw,
                is_followup=was_followup,
                session_id=getattr(tab, "session_id", "") or "",
            )
            if path is not None:
                self._diag("library", file=str(path.name))
        except Exception:
            self.state.errors.append(traceback.format_exc(limit=3))
            self._diag("error", where="save_library")

    def _record_chat(self, tab, raw: str) -> None:
        """记录本轮问答，供追问时作为多轮上下文（只保留最近 4 轮）。"""
        try:
            content = getattr(tab, "_pending_content", "") or ""
            history = list(getattr(tab, "chat_history", []) or [])
            if content:
                history.append({"role": "user", "content": content})
                history.append({"role": "assistant",
                                "content": (raw or "")[:6000]})
            tab.chat_history = history[-8:]
            tab.chat_system = getattr(tab, "_pending_system", "") or ""
        except Exception:
            self.state.errors.append(traceback.format_exc(limit=3))

    def on_followup(self, tab, text: str) -> None:
        """在同一标签内继续追问：保留领域设定与问答历史，放开输出格式。

        不是"换一段正文重新查"，而是把上一轮的用户输入与模型原始返回作为
        ``history`` 一起发给模型。system 保留上一轮的领域 / 角色 / 角度，
        只把末尾的「严格 JSON」输出契约换成自由作答规则——第一次查询要
        结构化字段，追问要的是一段接得上话的自然回答。
        """
        try:
            question = (text or "").strip()
            if not question or tab not in self.window._tabs:
                return
            history = list(getattr(tab, "chat_history", []) or [])
            if not history:
                # 没有可用的上一轮（本地预览 / 请求失败）：退化成普通查询
                self.on_query_submitted(question)
                return
            tab._followup = {
                "question": question,
                "history": history[-6:],
                "system": followup_system(getattr(tab, "chat_system", "") or ""),
            }
            self.on_intent(tab, dict(tab.last_selection or {}))
            self._diag("followup", chars=len(question), turns=len(history))
        except Exception:
            self.state.errors.append(traceback.format_exc(limit=3))
            self._diag("error", where="on_followup")

    def _on_worker_done(self, payload) -> None:
        try:
            # 前 6 项保持旧格式，后 2 项为票据；兼容测试构造的短 payload。
            tab, generation, result, key, font_size, dark = payload[:6]
            envelope_id = payload[6] if len(payload) > 6 else None
            request_id = payload[7] if len(payload) > 7 else None
            if not self._alive:
                self._diag("drop", reason="shutdown")
                return
            if tab not in self.window._tabs:
                # 标签已关闭：C++ 对象可能已被 deleteLater，不能再触碰。
                self._diag("drop", reason="closed-tab", rid=request_id)
                return
            if generation != tab.request_generation:
                self._diag("drop", reason="stale-generation", rid=request_id,
                           got=generation, want=tab.request_generation)
                return
            if envelope_id is not None and (
                    tab.env is None or envelope_id != tab.env.envelope_id):
                self._diag("drop", reason="stale-envelope", rid=request_id)
                return
            self._diag("render", gen=generation, rid=request_id, ok=result.ok)
            if result.ok and result.raw:
                html_text = render_json_result(
                    result.raw, font_size=font_size, dark=dark)
                was_followup = bool(getattr(tab, "_followup_pending", False))
                if not was_followup:
                    self.cache.put(key, result.raw)
                tab._followup_pending = False
                self._record_chat(tab, result.raw)
                self._save_library(tab, result.raw, was_followup)
                tab.show_result(
                    html_text, title="解释结果",
                    append=was_followup,
                    user_text=((getattr(tab, "_pending_content", "") or "")
                               if was_followup else ""),
                )
            else:
                from html import escape as _escape
                from .theme_tokens import tokens
                tab.show_result(render_notice(
                    "请求失败",
                    f"<p style='color:{tokens(dark)['error']};'>"
                    f"{_escape(str(result.error))}</p>",
                    font_size=font_size, dark=dark),
                    title="错误", error=result.error)
        except Exception as exc:
            self.state.errors.append(traceback.format_exc(limit=3))
            self._diag("error", where="_on_worker_done", exc=type(exc).__name__)

    # -- 图片识别 / 截图 ----------------------------------------------------
    def on_vision(self) -> None:
        try:
            # 从托盘菜单触发时窗口可能已隐藏，需要主动提到最前
            self.window.show_without_focus(bring_to_front=True)
            # 每次识别都是全新一屏：不清的话第二张图的等待卡片会挂在
            # 上一轮结果下面，看起来像在旧对话里继续。
            self.window.current_tab().begin_new_session()
            max_bytes = int(self.settings.get("image", {}).get("max_bytes",
                                                               8 * 1024 * 1024))
            found = self.clip_monitor.take_image()
            origin = "剪贴板图片"
            if found is None:
                found = read_clipboard_image(max_bytes)   # 外部命令兜底
            if found is None:
                tab = self.window.current_tab()
                tab.show_loading("屏幕截图", hint="正在请求截图..")
                QApplication.processEvents()
                found = capture_screenshot(max_bytes)
                origin = "屏幕截图"
            if found is None:
                self._notice("截图未完成",
                             "没有拿到截图（可能取消了选择，或桌面截图界面未响应）。"
                             "也可以先复制图片，再点顶栏的图片按钮重试。")
                return
            image_bytes, mime = found
            prompt = str(self.settings.get("desktop_vision_prompt", ""))
            self.state.stats["vision"] += 1
            tab = self.window.current_tab()
            tab.show_loading(origin, hint="正在识别图片..")

            def work():
                result = recognize(image_bytes, mime, prompt, self.settings)
                if self._alive:
                    self.bridge_vision.emit((tab, result))

            threading.Thread(target=work, daemon=True).start()
        except Exception:
            self.state.errors.append(traceback.format_exc(limit=3))

    def _on_vision_done(self, payload) -> None:
        tab, result = payload
        if not result.ok:
            self._notice("图片识别失败", result.error)
            if self.tray is not None:
                self.tray.showMessage("clip-llm 图片识别失败", result.error,
                                      QSystemTrayIcon.MessageIcon.Warning, 6000)
            return
        text = strip_image_refs(result.text)
        if not text:
            self._notice("图片识别没有返回文字",
                         "视觉模型只返回了图片引用或空内容，已跳过本次查询。")
            return
        self.window.query_edit.setText(text[:200])
        self.process_text(text, source="vision", force_dedup_bypass=True)

    def _notice(self, title: str, message: str) -> None:
        self.window.current_tab().show_result(render_notice(
            title, f"<p>{message}</p>",
            font_size=int(self.settings.get("desktop_font_size", 14)),
            dark=bool(self.settings.get("desktop_dark_theme", False))),
            title=title)

    # -- 搜索 / 设置 -------------------------------------------------------
    def on_search(self, provider_id: str, query: str) -> None:
        pid = provider_id or str(self.settings.get("search_default_provider", "google"))
        provider = self.settings.provider(pid)
        if provider:
            QDesktopServices.openUrl(QUrl(provider.build_url(
                query, max_chars=int(self.settings.get("search_max_query_chars", 200)))))

    def on_search_provider(self, provider_id: str) -> None:
        """顶栏搜索：用选中文本或当前内容。"""
        text = self.window.current_tab()._search_query() \
            or self.window.query_edit.text().strip()
        if text:
            self.on_search(provider_id, text)

    def open_settings(self) -> None:
        """设置窗口在独立进程里打开（首次启动约 1~2 秒）。

        不覆盖当前标签：子进程取消或崩溃时没有回传信号，若先把标签刷成
        loading，原内容就再也恢复不了。提示改走托盘气泡，不触碰窗口焦点。
        """
        self._child_opened()
        if self.tray is not None:
            self.tray.showMessage(
                "clip-llm", "正在打开设置窗口（独立进程，首次约 1~2 秒）…",
                QSystemTrayIcon.MessageIcon.Information, 3000)
        self.input_bridge.request_settings(self.settings.as_dict())

    def reload_settings(self, data: dict) -> None:
        self._child_closed()
        try:
            self.settings = settings_from_data(data)
            self.gateway = Gateway(GatewayConfig.from_settings(self.settings))
            self.window.set_settings(self.settings)
            self.window.fill_search_menu(
                self.settings.enabled_search_providers(("*",)))
            # 配置变更后旧选择与旧判定一律失效，已打开的标签就地重算策略
            for tab in self.window._tabs:
                tab.last_selection = None
                tab.pending_confirmation = None
                if tab.env is not None:
                    fresh = preflight(
                        tab.env, self.settings,
                        runtime=self._policy_runtime(),
                        sensitive_patterns=self._sensitive_patterns())
                    if fresh.allowed:
                        # 查询方式 / 其他视角 / 模板的改动必须立即反映到已打开的
                        # 标签里，而不是只换掉 settings 等下一次复制才生效。
                        sug = suggest(tab.env, fresh, self.settings)
                        menu = build_menu(sug, self.settings, self.state.prefs)
                        tab.refresh_menu(tab.env, fresh, sug, menu)
                    tab.policy = fresh
                    tab.invalidated = not fresh.allowed
            self._diag("settings-reloaded", tabs=len(self.window._tabs))
        except Exception as exc:
            self.state.errors.append(traceback.format_exc(limit=3))
            self._diag("error", where="reload_settings", exc=type(exc).__name__)

    # -- 生命周期 ----------------------------------------------------------
    def shutdown(self) -> None:
        self._alive = False
        if self._cancel is not None:
            self._cancel.set()
        self.ipc_timer.stop()
        if self.tray is not None:
            self.tray.hide()
        self.input_bridge.shutdown()
        self.clip_monitor.stop()
        try:
            self.clip_monitor.text_found.disconnect()
            self.bridge.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.window.hide()
        QApplication.quit()
