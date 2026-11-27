"""Studio 主控：剪贴板线程 → 初判 → 标签页 → 请求 → 精美排版。

前台进程，不注册 systemd。主进程禁用 text-input，中文输入与设置走独立进程；
所有 UI 更新都在主线程，模型/视觉请求在工作线程。
"""
from __future__ import annotations

import hashlib
import html
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
    QApplication, QMenu, QSystemTrayIcon,
)

from ..desktop.clipboard_worker import ClipboardWorker  # noqa: F401 (serve 兼容)
from .clipboard_monitor import ClipboardMonitor
from ..desktop.gateway import Gateway, GatewayConfig, GatewayResult, ResultCache
from ..features import extract_features
from ..modality import AssetStore
from ..models import (
    DomainCandidate, FlowDecision, InputEnvelope, Preferences, Suggestion, UserSelection,
    new_id, sha256_text,
)
from ..policy import preflight, run_flow
from ..prompts import build_request_prompt, followup_system
from ..serve import ClipboardReader
from ..settings import Settings, structure_signature
from ..suggest import (auto_followup_intent, build_menu, suggest,
                       suggest_with_short_source)
from .config import DEFAULT_DESKTOP_DUP_TTL, load_config, settings_from_data
from . import __main__ as studio_build
from .input_bridge import InputBridge
from .json_render import (RENDER_SCHEMA, render_json_result,
                          render_local_preview, render_notice)
from .main_window import MainWindow
from .vision import (
    capture_screenshot, intent_vision_prompt, read_clipboard_image,
    recognize, vision_enabled, vision_route,
    VisionResult, VISION_FOLLOWUP_SYSTEM,
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
    flow: FlowDecision | None = None


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
        self._cancels: set[threading.Event] = set()
        self._vision_cancels: set[threading.Event] = set()

        self.bridge = _Bridge()
        self.bridge.finished.connect(self._on_worker_done)
        self.bridge_vision.connect(self._on_vision_done)
        self.gateway = Gateway(GatewayConfig.from_settings(settings))
        self.cache = ResultCache()
        self.reader = ClipboardReader()
        # 图片是一等模态：原图落 AssetStore，数据对象只带 asset_id/摘要；
        # 最近几张图在内存里留一份，用户点菜单时不必回读磁盘。
        self.assets = AssetStore(
            max_bytes=int(settings.image.get("max_bytes", 8 * 1024 * 1024)))
        self._vision_assets: dict[str, tuple[bytes, str]] = {}
        # 刚提交过的正文：查询框里如果还是它，说明是残留，不能当成新截图的
        # 「附加要求」——否则视觉模型会照着上一段正文答，而不是看图。
        self._last_query_text = ""
        # 已经用过的图片摘要（最近 8 张）。剪贴板里如果还是老图——截图工具常把
        # 截图写回剪贴板，直接读剪贴板又永远读到同一张——必须当作「没有新图」，
        # 否则会出现「查不同公式，返回的总是上一个」。
        self._used_image_digests: list[str] = []

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
        self.window.vision_image_requested.connect(
            lambda: self.on_vision(source="screenshot"))
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
            menu.addAction("🖼 截图 / 图片查询（先选模板）",
                           lambda: self.on_vision(source="screenshot"))
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

        def _on_tab_intent(data, t=tab):
            env = getattr(t, "env", None)
            self._diag(
                "intent-selected",
                source=str(getattr(env, "source", "-") or "-"),
                assets=len(getattr(env, "assets", ()) or ()),
                intent=str(data.get("intent_id") or "-"),
                invalidated=bool(getattr(t, "invalidated", False)))
            self.on_intent(t, data)

        tab.intent_selected.connect(_on_tab_intent)
        tab.domain_changed.connect(lambda did, t=tab: self.on_domain(t, did))
        tab.category_changed.connect(
            lambda cid, t=tab: self.on_category(t, cid))
        tab.new_tab_requested.connect(self.on_new_tab)
        tab.search_requested.connect(self.on_search)
        tab.settings_requested.connect(self.open_settings)
        tab.copy_query_requested.connect(self.on_copy_query)
        tab.vision_image_requested.connect(
            lambda: self.on_vision(source="screenshot"))
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
        # 上次退出前没被消费的 vision.cmd（全局键 / 崩溃残留）会在首次
        # 轮询时触发一次系统截图框。启动时先清掉，避免「一开就弹截图」。
        stale = self.ipc_dir / "vision.cmd"
        if stale.exists():
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                self._diag("ipc", reason="stale-unlink-failed")
        self.ipc_timer.start()
        self.window.show_without_focus()

    def _check_ipc(self) -> None:
        request = self.ipc_dir / "vision.cmd"
        if not request.exists():
            return
        try:
            request.unlink(missing_ok=True)
        except OSError:
            # 删不掉就别执行：否则每 500ms 都会再触发一次系统截图。
            self._diag("ipc", reason="unlink-failed")
            return
        self.on_vision(source="screenshot")

    def _on_clipboard_text(self, text: str) -> None:
        if self._alive and not self._paused:
            self.process_text(text, source="clipboard")

    def _on_clipboard_image(self) -> None:
        """剪贴板出现图片 → 和文本一样弹出初判窗口（不自动识别）。

        Win+Shift+S 等截图工具也是把图放进剪贴板，因此这条入口同样弹窗；
        但它只读剪贴板，绝不触发系统截图，避免和手动截图入口互相顶替。
        总开关 ``image.vision_enabled`` 关闭后直接返回。
        """
        if not (self._alive and not self._paused):
            return
        if not vision_enabled(self.settings):
            self._diag("vision-skip", reason="image.vision_enabled=false")
            return
        # 大图往往是整页截图 / 导出图，用户只是想留在剪贴板，不希望弹窗。
        # 0 表示不限制；默认 2 MB 来自 image.clipboard_popup_max_mb。
        limit_mb = int((self.settings.get("image", {}) or {}).get(
            "clipboard_popup_max_mb", 2))
        pending = getattr(self.clip_monitor, "last_image", None)
        if limit_mb > 0 and pending and len(pending[0] or b"") > limit_mb * 1024 * 1024:
            self._diag("vision-skip", reason="clipboard-image-too-large",
                       kb=len(pending[0] or b"") // 1024, limit_mb=limit_mb)
            self._notice(
                "大图未自动弹出",
                f"这张图约 {len(pending[0]) // 1024 // 1024} MB，超过 "
                f"{limit_mb} MB，按设置留在剪贴板、不弹初判。"
                "要用它查询就点顶栏图片按钮或按 Ctrl+Alt+S；"
                "想让大图也自动弹窗，到「设置 → 模型与图片」调大上限或设为不限制。")
            return
        self.on_vision(source="clipboard")

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
        ttl = float(self.settings.get("desktop_dup_ttl", DEFAULT_DESKTOP_DUP_TTL))
        last = self._seen.get(digest)
        if last is not None and ttl > 0 and now - last < ttl:
            return True
        self._seen[digest] = now
        if len(self._seen) > 512:
            self._seen = {k: v for k, v in self._seen.items() if now - v < ttl}
        return False

    # -- 第一层 ------------------------------------------------------------
    def process_text(self, text: str, *, source: str = "clipboard",
                     new_tab: bool = False, force_dedup_bypass: bool = False,
                     auto_submit: bool = False,
                     notify_block: bool = False,
                     force_route: str = "") -> None:
        """文本查询主入口。

        ``auto_submit``：初判后不等用户点选，直接用默认查询方式发请求
        （图片识别回流走这条，做到「截图 → 选一次 → 出答案」）。
        ``notify_block``：被硬拦截时给一行提示，而不是静默（用户主动触发的
        流程里，静默等于没反应）。
        """
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
            flow = run_flow(env, self.settings, runtime=self._policy_runtime(),
                            sensitive_patterns=self._sensitive_patterns())
            self.state.flow = flow
            policy = preflight(env, self.settings,
                               runtime=self._policy_runtime(),
                               sensitive_patterns=self._sensitive_patterns())
            self._diag("flow", kind=flow.input_kind,
                       regex=flow.local_regex_applied, tier=flow.length_tier)
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
                if notify_block:
                    self._notice(
                        "识别结果没有通过检查",
                        "识别出的文字命中了硬拦截规则（"
                        + "、".join(policy.block_reasons) + "），已停止本次查询。")
                return
            s = suggest_with_short_source(env, policy, self.settings)
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
            # 与 _present_envelope 一致：正文已经进了标签页，查询框不能留残留，
            # 否则下一次截图会把它当成「附加要求」拼进视觉提示词（串扰）。
            if (env.text or "").strip():
                # 只记有正文的那一次：图片信封不能把这条记忆冲掉，
                # 否则旧正文又被当成新要求。
                self._last_query_text = env.text.strip()
            self.window.query_edit.clear()
            if auto_submit:
                # 同一事件循环里直接续跑：初判菜单不会被真正显示出来
                self._auto_submit(tab, force_route=force_route)
        except Exception:
            self.state.errors.append(traceback.format_exc(limit=3))

    def _pinned_intent(self, route_id: str) -> str | None:
        """图片方式指定的下游文本模板 → 该模板下第一条可用查询方式。"""
        if not route_id:
            return None
        candidates = [i for i in self.settings.intents
                      if i.enabled and i.primary_route == route_id
                      and "image" not in i.domains]
        if not candidates:
            return None
        return sorted(candidates, key=lambda i: i.order)[0].id

    def _auto_submit(self, tab, force_route: str = "") -> None:
        """用初判的默认查询方式直接发请求，替用户省掉第二次点击。

        只用于「图片识别 → 文本模板」这条回流链路；用户手动操作时菜单
        仍然照常停留，等他选。
        """
        try:
            menu = getattr(tab, "menu", None)
            env = getattr(tab, "env", None)
            intent_id = self._pinned_intent(force_route) \
                or auto_followup_intent(menu, self.settings)
            if not intent_id or env is None:
                return
            intent = self.settings.intent(intent_id)
            if intent is None:
                return
            self._diag("auto-submit", intent=intent_id,
                       category=getattr(menu, "category", None) or "-")
            if hasattr(tab, "result_label"):
                tab.result_label.setText(intent.label)
            if hasattr(tab, "_goto_result"):
                tab._goto_result()
            self.on_intent(tab, {
                "envelope_id": env.envelope_id,
                "domain_id": (menu.domain_chips[0].id
                              if menu.domain_chips else "*"),
                "intent_id": intent_id,
                "format_id": intent.default_format,
                "custom_instruction": None,
                "free_input_mode": "instruction",
                "style_ref": None,
                "allowed_route_ids": ([intent.primary_route]
                                      if intent.primary_route else []),
            })
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
                            build_menu(s, self.settings, self.state.prefs),
                            fresh=False)
        except Exception:
            self.state.errors.append(traceback.format_exc(limit=3))

    def on_category(self, tab, category_id: str) -> None:
        """用户从初判层切换到其他提示词模板类别：只重建菜单，不发请求。"""
        try:
            if tab.env is None or tab.suggestion is None or tab.policy is None:
                return
            menu = build_menu(tab.suggestion, self.settings,
                              self.state.prefs, category_id=category_id)
            tab.set_context(tab.env, tab.policy, tab.suggestion, menu,
                            fresh=False)
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
                # BLOCK 全程静默是既有约定（见 test_block_invalidates_stale_tab）：
                # 拦截时已提示过，这里再点按钮不再触碰窗口。
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
                self._notice("查询被拦截",
                             "命中规则：" + "、".join(policy.block_reasons),
                             tab=tab)
                return
            # 两级策略：BLOCK 已在上面静默拒绝，走到这里就是 ALLOW，
            # 不存在「二次确认」这类会额外弹窗的中间态。
            # 图片模态：选中的是图片模板就发视觉请求；纯图片必须先选一套模板。
            intent = (self.settings.intent(str(data.get("intent_id")))
                      if data.get("intent_id") else None)
            route_id = str(getattr(intent, "primary_route", "") or "") if intent else ""
            is_vision_route = bool(
                route_id and (self.settings.routes.get(route_id) or {}).get("vision"))
            # 其他视角是「通用解读角度」：知识型回答交给文本模型，图片随消息附上
            is_view = bool(intent is not None
                           and str(intent.id).startswith("view."))
            extra = str(data.get("custom_instruction") or "")
            if env.assets and not followup and not extra.strip():
                # 双模态：查询框里写的文字就是本次要求，之前只认
                # custom_instruction，导致「图 + 一句问题」里的问题被丢掉。
                extra = (env.text or "").strip()
            # 追问是对「已有答案」的文本提问：带着 history 直接走文本模型，
            # 不再重新识别图片（此前会二次发起视觉请求，且忽略 history）。
            if env.assets and not followup:
                has_text = bool((env.text or "").strip())
                if not is_vision_route and not has_text \
                        and not extra.strip() and not is_view:
                    self._notice(
                        "请选择一种图片查询方式",
                        "图片已就绪：在查询方式里选一条（文字 / 表格 / 解释 / "
                        "标注）即可识别；也可以在查询框写一句要求。")
                    return
                # 自由输入（没有选方式，只有一句要求）也要走视觉：
                # 双模态时同样以图片为主，文字只是要求本身。
                if (is_vision_route or intent is None or not has_text) \
                        and not is_view:
                    tab.last_selection = dict(data)
                    self._start_vision(
                        tab, env,
                        route_id if is_vision_route
                        else vision_route(self.settings),
                        extra, intent,
                        mode=str(data.get("free_input_mode") or "instruction"))
                    return
            if is_view and env.assets:
                content_text = (content_text or "").strip() or \
                    f"请从「{getattr(intent, 'label', '')}」的角度解读这张图片。"
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
            tab.show_pending("正在生成解释…")
            # 捕获网关快照：key 与实际请求必须来自同一份配置。热重载会整体
            # 替换 self.gateway，旧请求若读到新模型会把结果缓存到旧键下。
            gw = self.gateway
            key = composed.cache_key(
                model=gw.config.model, route_id=composed.query_type,
                intent_id=selection.intent_id or "freeform",
                format_id=selection.format_id, schema_version=RENDER_SCHEMA,
                ruleset_version=structure_signature(self.settings)[:16],
                query_hash=env.query_hash, selection_salt=selection.cache_salt())

            if not gw.config.enabled:
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

            attach_image = self._attach_image(followup, is_view, env)
            self.state.stats["requests"] += 1
            self.state.requests_today += 1
            cancel = threading.Event()
            self._cancels.add(cancel)
            alive = self._alive

            def work():
                try:
                    result = gw.explain(composed.prompt,
                                        content_text,
                                        history=chat_history,
                                        image=attach_image,
                                        cancel=cancel)
                except Exception:
                    result = GatewayResult(html="", ok=False, error="模型调用异常")
                finally:
                    self._cancels.discard(cancel)
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

    def _attach_image(self, followup, is_view, env):
        """追问与「其他视角」都基于图片：把原图随文本请求一起发（默认开）。"""
        if not getattr(env, "assets", ()):
            return None
        if not (followup or is_view):
            return None
        if not bool(self.settings.get("desktop_followup_image", True)):
            return None
        return self._asset_bytes(env.assets[0])

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
    @staticmethod
    def _image_identity(data: bytes) -> str:
        """图片身份：按**像素**算，不按字节。

        同一张图被重新编码（wl-paste / xclip 每次重新出 PNG）后字节会变，
        但像素不变——按字节记账会认不出「还是上次那张」，于是又串扰。
        """
        from PySide6.QtGui import QImage
        image = QImage.fromData(data)
        if image.isNull():
            return hashlib.sha256(data).hexdigest()
        small = image.scaled(256, 256, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        small = small.convertToFormat(QImage.Format.Format_RGB32)
        digest = hashlib.sha256()
        digest.update(f"{small.width()}x{small.height()}|".encode())
        for y in range(small.height()):
            digest.update(bytes(small.constScanLine(y))[:small.bytesPerLine()])
        return digest.hexdigest()

    def _fresh_clipboard_image(self, found):
        """校验剪贴板图可用（只检查格式与大小，不按内容拒绝）。

        早先按「最近 8 张」摘要拦截，本意是防截图工具把同一张图写回
        剪贴板造成死循环；但两条取图路径拆分后已无此问题，反而让用户
        「重新复制同一张图再查一次」被静默吞掉。这里只做基本校验。
        """
        if found is None:
            return None
        if not found[0]:
            self._diag("vision-skip-empty", reason="clipboard-empty-image")
            return None
        return found

    def _mark_image_used(self, digest: str) -> None:
        """记账：同一张图不会第二次被当成「新图」。"""
        if not digest or digest in self._used_image_digests:
            return
        self._used_image_digests.append(digest)
        del self._used_image_digests[:-8]

    def on_vision(self, source: str = "clipboard") -> None:
        """截图/复制图片 → 建图片信封 → 出菜单；识别在用户选模板后才发生。

        两条取图路径互不兜底：``screenshot`` 只走系统截图（不读剪贴板），
        ``clipboard`` 只读剪贴板（不弹截图框）。Win+Shift+S 的截图本来就在
        剪贴板里，因此走 clipboard 分支，不会被上一次复制的图片顶掉。
        """
        try:
            # 置顶交给 _present_envelope 统一做一次：这里再调一次会让
            # 剪贴板图片 / 截图都二次弹到最前，表现为「频繁弹窗」。
            if not vision_enabled(self.settings):
                self._notice("图片识别已关闭",
                             "到「设置 → 模型」勾选「启用图片识别」后再试。")
                return
            max_bytes = int(self.settings.get("image", {}).get(
                "max_bytes", 8 * 1024 * 1024))
            if source == "clipboard":
                found = self.clip_monitor.take_image()
                origin = "剪贴板图片"
                if found is None:
                    found = read_clipboard_image(max_bytes)
                found = self._fresh_clipboard_image(found)
                if found is None:
                    self._diag("vision-skip", reason="clipboard-no-new-image")
                    return                    # 同一张图不重复打扰
            else:
                tab = self.window.current_tab()
                tab.show_pending("正在等待屏幕截图…")
                QApplication.processEvents()
                found = capture_screenshot(max_bytes)
                origin = "屏幕截图"
            if found is None:
                self._notice("截图未完成",
                             "没有拿到截图（可能取消了选择，或桌面截图界面未响应）。"
                             "也可以按 Ctrl+Alt+S 重新截图。")
                return

            image_bytes, mime = found
            try:
                asset = self.assets.put(image_bytes, mime)
            except ValueError as exc:
                self._notice("图片不可用", str(exc))
                return
            self._remember_asset(asset, image_bytes, mime)
            # 资产真正可用后才记账，失败的图不该被永久判为「旧图」
            self._mark_image_used(self._image_identity(image_bytes))

            # 新图 = 新一屏：旧结果不留在同一张卡片下面
            self.window.current_tab().begin_new_session()
            self.state.stats["events"] += 1
            # 查询框有文字 → 双模态：图片交给图片模板，文字作为附加要求。
            # 但如果那就是上一次提交的正文（查询框没清干净），它是残留而不是
            # 要求：照单全收会让视觉模型去解释上一段文字（串扰）。
            query_text = self.window.query_edit.text().strip()
            if query_text and query_text == self._last_query_text:
                self._diag("vision-drop-residual-text",
                           chars=len(query_text))
                self.window.query_edit.clear()
                query_text = ""
            env = (InputEnvelope.for_image(asset, source=origin, text=query_text)
                   if query_text
                   else InputEnvelope.for_image(asset, source=origin))
            self._diag("vision-menu", asset=asset.asset_id[:8], mime=mime,
                       modality=env.modality, text_chars=len(query_text))
            self._present_envelope(env)
        except Exception:
            self.state.errors.append(traceback.format_exc(limit=3))

    def _remember_asset(self, asset, data: bytes, mime: str) -> None:
        self._vision_assets[asset.asset_id] = (data, mime)
        while len(self._vision_assets) > 8:
            self._vision_assets.pop(next(iter(self._vision_assets)), None)

    def _asset_bytes(self, asset) -> tuple[bytes, str] | None:
        hit = self._vision_assets.get(asset.asset_id)
        if hit is not None:
            return hit
        try:
            return Path(asset.local_path).read_bytes(), asset.mime
        except OSError:
            return None

    def _present_envelope(self, env, *, new_tab: bool = False) -> None:
        """图片信封复用文本的闸门/初判/菜单，只是推荐区换成图片查询方式。"""
        flow = run_flow(env, self.settings, runtime=self._policy_runtime(),
                        sensitive_patterns=self._sensitive_patterns())
        self.state.flow = flow
        self._diag("flow", kind=flow.input_kind,
                   regex=flow.local_regex_applied, tier=flow.length_tier)
        policy = preflight(env, self.settings,
                           runtime=self._policy_runtime(),
                           sensitive_patterns=self._sensitive_patterns())
        self._diag("preflight", source=env.source, level=policy.level,
                   reasons=",".join(policy.block_reasons) or "-")
        if not policy.allowed:
            self.state.stats["blocked"] += 1
            tab = self.window.current_tab()
            tab.invalidated = True
            tab.last_selection = None
            self._notice("这张图没有通过检查",
                         "命中拦截规则（" + "、".join(policy.block_reasons)
                         + "），本次图片查询已停止。", tab=tab)
            return
        suggestion = suggest(env, policy, self.settings)
        menu = build_menu(suggestion, self.settings, self.state.prefs)
        use_new_tab = new_tab or bool(
            self.settings.get("desktop_new_query_in_new_tab", False))
        tab = self.window.apply_context(env, policy, suggestion, menu,
                                        new_tab=use_new_tab)
        self._connect_tab(tab)
        # 图片查询的附带要求已经进了 env；查询框不再保留，否则下一张截图
        # 会把上一句当成新要求（串扰）
        if (env.text or "").strip():
            self._last_query_text = env.text.strip()
        self.window.query_edit.clear()
        self.state.stats["tabs"] = self.window.tabs.count()
        self.window.show_without_focus(bring_to_front=True)
        # 图片初判需要用户再点一次菜单，置顶窗口留长一点（进窗后自动保持）
        self.window.extend_flash(1500)

    def _start_vision(self, tab, env, route_id: str, extra: str,
                      intent=None, mode: str = "instruction") -> None:
        """按用户选中的图片查询方式发视觉请求（模板提示词 + 附加要求）。

        查询方式自带 ``prompt_override``（如图片公式 / 图片翻译）时优先用它，
        否则用所属图片模板的提示词。
        """
        if not env.assets:
            self._notice("这张图已经失效",
                         "当前输入里没有图片，请重新截图或复制图片后再试。")
            return
        found = self._asset_bytes(env.assets[0])
        if found is None:
            self._notice("原图已失效", "缓存里找不到这张图，请重新截图。")
            return
        data, mime = found
        prompt = intent_vision_prompt(self.settings, route_id, intent, extra,
                                      mode)
        intent_id = str(getattr(intent, "id", "") or "")
        followup = str(getattr(intent, "followup_route", "") or "")
        label = (str(getattr(intent, "label", "") or "")
                 or ("自定义" if intent is None
                     else str((self.settings.routes.get(route_id) or {})
                              .get("label") or route_id)))
        self.state.stats["vision"] += 1
        tab.request_generation += 1
        # 图片请求也要带票据：慢的旧请求返回时不能盖掉新截图的结果
        generation = tab.request_generation
        envelope_id = str(getattr(env, "envelope_id", "") or "")
        # 直出的结果页也要能追问：把这一轮的「输入」和「system」记下来，
        # 否则 chat_history 是空的，追问会退化成一句孤立的新查询。
        tab._pending_content = (f"[图片] {label}"
                                + (f"；附加要求：{extra.strip()}"
                                   if extra.strip() else ""))
        tab._pending_system = VISION_FOLLOWUP_SYSTEM
        tab.show_pending(f"正在识别图片：{label}…")
        self._diag("vision-start", intent=intent_id, chars=len(prompt),
                   asset=str(getattr(env.assets[0], "sha256", "") or "")[:8],
                   gen=generation)

        cancel = threading.Event()
        self._vision_cancels.add(cancel)
        settings_snapshot = self.settings

        def work():
            try:
                result = recognize(data, mime, prompt, settings_snapshot,
                                   cancel=cancel)
            except Exception as exc:
                # 没有兜底时线程会静默死亡，标签永远停在「正在识别图片..」。
                self.state.errors.append(traceback.format_exc(limit=3))
                result = VisionResult(error=f"图片识别异常：{exc}", mime=mime)
            finally:
                self._vision_cancels.discard(cancel)
            if self._alive:
                self.bridge_vision.emit((tab, result, intent_id, label,
                                         followup, prompt, generation,
                                         envelope_id))

        threading.Thread(target=work, daemon=True).start()

    def _on_vision_done(self, payload) -> None:
        payload = tuple(payload)
        tab, result = payload[0], payload[1]
        from .query_tab import QueryTab
        if isinstance(tab, QueryTab) and tab not in self.window._tabs:
            self._diag("drop", reason="closed-tab")
            return
        # 票据校验：截图之后用户又换了一张图 / 又点了一次，旧请求迟到必须丢弃，
        # 否则「这张图在讲什么」显示的是上一张图的答案（串扰）。
        generation = payload[6] if len(payload) > 6 else None
        if generation is not None and generation != tab.request_generation:
            self._diag("drop", reason="stale-vision", got=generation,
                       want=tab.request_generation)
            return
        envelope_id = str(payload[7]) if len(payload) > 7 else ""
        if envelope_id and (tab.env is None
                            or envelope_id != tab.env.envelope_id):
            self._diag("drop", reason="stale-vision-envelope")
            return
        intent_id = str(payload[2]) if len(payload) > 2 else "image.ocr"
        intent_label = str(payload[3]) if len(payload) > 3 else intent_id
        has_followup = len(payload) > 4
        followup = str(payload[4] or "") if has_followup else ""
        if not has_followup and intent_id == "image.ocr":
            # 只有「老载荷根本没有这个字段」时才走兼容默认；
            # 用户显式把某条方式设成「直接展示」不能被改回去。
            followup = "auto"
        if not result.ok:
            self._notice("图片识别失败", result.error, tab=tab)
            if self.tray is not None:
                self.tray.showMessage("clip-llm 图片识别失败", result.error,
                                      QSystemTrayIcon.MessageIcon.Warning, 6000)
            return
        text = strip_image_refs(result.text)
        if not text:
            self._notice("图片识别没有返回文字",
                         "视觉模型只返回了图片引用或空内容，已跳过本次查询。")
            return
        # 「提取图片中的文字」：识别结果作为查询内容进初判，再解释一遍。
        # 表格 / 解释 / 标注：模型返回的已经是答案本身，直接渲染结果，
        # 不再拿它当文本再请求一次（否则表格 JSON 会被当成一个新问题）。
        if not str(getattr(tab, "_pending_content", "") or "").strip():
            tab._pending_content = f"[图片] {intent_label}"
        if not str(getattr(tab, "_pending_system", "") or "").strip():
            tab._pending_system = VISION_FOLLOWUP_SYSTEM
        if not followup:
            tab.show_result(render_json_result(
                text,
                font_size=int(self.settings.get("desktop_font_size", 14)),
                dark=bool(self.settings.get("desktop_dark_theme", False))),
                title=intent_label)
            self._record_chat(tab, text)
            self.state.stats["requests"] += 1
            return
        # v18：续跑由本条图片方式的「识别后」下拉决定，不再读全局开关：
        # 指定具体模板 / auto → 自动组装+请求；空 → 出初判让用户选。
        # 对未来新增的图片方式同样生效（只要 followup_route 有值）。
        auto_submit = bool(followup)
        self.window.query_edit.setText(text[:200])
        self.process_text(
            text, source="vision", force_dedup_bypass=True,
            auto_submit=auto_submit, notify_block=True,
            force_route="" if followup == "auto" else followup)

    def _notice(self, title: str, message: str, tab=None) -> None:
        (tab or self.window.current_tab()).show_result(render_notice(
            title, f"<p>{html.escape(str(message))}</p>",
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
            self.assets.max_bytes = int(self.settings.image.get(
                "max_bytes", 8 * 1024 * 1024))
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
                        sug = suggest_with_short_source(tab.env, fresh, self.settings)
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
        for cancel in tuple(self._cancels):
            cancel.set()
        for cancel in tuple(self._vision_cancels):
            cancel.set()
        self.ipc_timer.stop()
        if self.tray is not None:
            self.tray.hide()
        self.input_bridge.shutdown()
        self.clip_monitor.stop()
        for signal in (self.clip_monitor.text_found, self.bridge.finished,
                       self.bridge_vision):
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass
        self.window.hide()
        QApplication.quit()
