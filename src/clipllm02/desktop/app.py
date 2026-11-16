"""桌面应用主控：托盘、剪贴板线程、generation 防乱序、请求调度。

前台进程运行；不注册 systemd、不启动守护服务、不写 ~/.config/systemd。

针对六个反馈问题的修复：
1. 不抢焦点：窗口一律 ``show_without_focus()``，不 activate、不 raise；
2. 不卡顿：剪贴板读取在 QThread，UI 线程只收信号；
3. 领域专属：菜单由 ``build_menu`` 按领域生成，提示词由意图自带；
4. 必弹且覆盖：去重带 TTL、自我复制单独标记、每次新输入强制 update_model；
5. 初判自动但保留选项：LOW 也出菜单 + 换领域；
6. 不闪退：所有槽函数异常受控，退出时停止线程、断开信号，析构期不 emit。
"""
from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap, QColor, QPainter
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from ..features import extract_features
from ..models import (
    DomainCandidate, InputEnvelope, Preferences, Suggestion, UserSelection,
    new_id, sha256_text,
)
from ..policy import preflight
from ..prompts import build_request_prompt
from ..serve import ClipboardReader
from ..settings import Settings, load_settings
from ..suggest import build_menu, suggest
from .clipboard_worker import ClipboardWorker
from .gateway import Gateway, GatewayConfig, GatewayResult, ResultCache
from .menu_window import MenuWindow
from .result_window import ResultWindow
from .settings_window import SettingsWindow


def make_icon() -> QIcon:
    from ..studio.theme_tokens import tokens
    t = tokens()
    icon = QIcon.fromTheme("accessories-dictionary")
    if not icon.isNull():
        return icon
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(t["primary"]))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(4, 4, 56, 56, 14, 14)
    painter.setPen(QColor(t["on_primary"]))
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "02")
    painter.end()
    return QIcon(pixmap)


class _Bridge(QObject):
    finished = Signal(object)


@dataclass
class DesktopState:
    envelope: InputEnvelope | None = None
    suggestion: Suggestion | None = None
    selection: UserSelection | None = None


class DesktopApp(QObject):
    def __init__(self, settings: Settings, *, poll_interval: float = 1.0,
                 use_tray: bool = True, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.prefs = Preferences()
        self.state = DesktopState()
        self.generation = 0
        self._cancel: threading.Event | None = None
        self._seen: dict[str, float] = {}
        self._self_copy: set[str] = set()
        self._dup_ttl = float(settings.get("desktop_dup_ttl", 1))
        self.intercepts: list[dict] = []
        self.errors: list[str] = []
        self.stats = {"events": 0, "blocked": 0, "menus": 0,
                      "selections": 0, "requests": 0, "duplicates": 0}
        self._alive = True
        self._sigint = False

        self.bridge = _Bridge()
        self.bridge.finished.connect(self._on_worker_done)
        self.gateway = Gateway(GatewayConfig.from_settings(settings))
        self.cache = ResultCache()
        self.reader = ClipboardReader()

        self.menu_window: MenuWindow | None = None
        self.result_window: ResultWindow | None = None
        self.settings_window: SettingsWindow | None = None

        self.tray = None
        if use_tray and QSystemTrayIcon.isSystemTrayAvailable():
            self._build_tray()

        # 剪贴板轮询放在独立线程，UI 线程永不阻塞
        self.clip_thread = QThread(self)
        self.clip_worker = ClipboardWorker(
            interval_ms=int(poll_interval * 1000), reader=self.reader)
        self.clip_worker.moveToThread(self.clip_thread)
        self.clip_thread.started.connect(self.clip_worker.start_work)
        self.clip_worker.text_found.connect(self._on_clipboard_text)
        self.clip_thread.finished.connect(self.clip_worker.deleteLater)
        self._paused = False

    # -- 托盘 --------------------------------------------------------------
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip("clip-llm 0.2（前台进程）")
        menu = QMenu()
        self.act_pause = menu.addAction("暂停监听")
        self.act_pause.triggered.connect(self.toggle_pause)
        menu.addAction("查询当前剪贴板", self.query_current)
        menu.addAction("设置…", self.open_settings)
        menu.addSeparator()
        menu.addAction("退出", self.shutdown)
        self.tray.setContextMenu(menu)
        self.tray.show()

    def toggle_pause(self) -> None:
        self._paused = not self._paused
        if self.tray is not None:
            self.act_pause.setText("恢复监听" if self._paused else "暂停监听")

    def query_current(self) -> None:
        text = self.reader.read()
        if text:
            self._seen.pop(sha256_text(text), None)   # 手动重查绕过去重
            self.process_text(text, source="manual")

    # -- 剪贴板 ------------------------------------------------------------
    def start(self) -> None:
        self.clip_thread.start()

    def _on_clipboard_text(self, text: str) -> None:
        if not self._alive or self._paused:
            return
        self.process_text(text, source="clipboard")

    def _is_duplicate(self, digest: str) -> bool:
        now = time.time()
        if digest in self._self_copy:
            return True
        last = self._seen.get(digest)
        if last is not None and now - last < self._dup_ttl:
            return True
        self._seen[digest] = now
        if len(self._seen) > 512:                     # 防止长时间运行无限增长
            cutoff = now - self._dup_ttl
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        return False

    def process_text(self, text: str, *, source: str = "clipboard") -> None:
        """所有来源的唯一入口。任何异常都记录而不让 Qt 槽崩溃。"""
        if not self._alive:
            return
        try:
            self._process_text_inner(text, source=source)
        except Exception:
            self.errors.append(traceback.format_exc(limit=3))

    def _process_text_inner(self, text: str, *, source: str) -> None:
        digest = sha256_text(text)
        if self._is_duplicate(digest):
            self.stats["duplicates"] += 1
            return

        # 新输入：取消在途、清空旧状态，保证一定覆盖旧弹窗
        self.generation += 1
        if self._cancel is not None:
            self._cancel.set()
        self.state.selection = None
        self.stats["events"] += 1

        env = InputEnvelope.for_text(text, source=source)
        policy = preflight(env, self.settings)
        if not policy.allowed:
            self.stats["blocked"] += 1
            self.intercepts.append({
                "level": policy.level,
                "reasons": list(policy.block_reasons),
                "source": source,
                "text_len": len(text),
                "modality": env.modality,
            })
            self.state.envelope = None
            self.state.suggestion = None
            self._hide_windows()
            return

        suggestion = suggest(env, policy, self.settings)
        menu = build_menu(suggestion, self.settings, self.prefs)
        self.state.envelope = env
        self.state.suggestion = suggestion
        self.stats["menus"] += 1
        self._show_menu(env, policy, suggestion, menu)

    # -- 菜单 --------------------------------------------------------------
    def _show_menu(self, env, policy, suggestion, menu) -> None:
        if self.menu_window is None:
            self.menu_window = MenuWindow(self.settings, env, policy,
                                          suggestion, menu)
            self.menu_window.selection_made.connect(self.on_selection)
            self.menu_window.cancelled.connect(self._on_cancelled)
            self.menu_window.settings_requested.connect(self.open_settings)
            self.menu_window.search_requested.connect(self.on_search)
            self.menu_window.domain_changed.connect(self.on_domain_changed)
            self.menu_window.clipboard_written.connect(self._on_self_copy)
        else:
            self.menu_window.settings = self.settings
            self.menu_window.update_model(env, policy, suggestion, menu)
        if self.result_window is not None:
            self.result_window.hide()
        self.menu_window.show_without_focus()

    def _hide_windows(self) -> None:
        if self.menu_window is not None:
            self.menu_window.hide()
        if self.result_window is not None:
            self.result_window.hide()

    def _on_cancelled(self) -> None:
        self.generation += 1
        if self._cancel is not None:
            self._cancel.set()

    def on_domain_changed(self, domain_id: str) -> None:
        try:
            env = self.state.envelope
            if env is None or self.menu_window is None:
                return
            label = next((d["label"] for d in self.settings.domains
                          if d["id"] == domain_id), domain_id)
            candidate = DomainCandidate(id=domain_id, label=label, score=1.0,
                                        evidence=("用户手动选择领域",))
            new_suggestion = Suggestion(
                envelope_id=env.envelope_id, modality=env.modality,
                features=(self.state.suggestion.features if self.state.suggestion
                          else extract_features(env, self.settings.thresholds)),
                domain_candidates=(candidate,), level="HIGH",
                warnings=self.menu_window.policy.warnings,
            )
            menu = build_menu(new_suggestion, self.settings, self.prefs)
            self.state.suggestion = new_suggestion
            self.menu_window.update_model(env, self.menu_window.policy,
                                          new_suggestion, menu)
        except Exception:
            self.errors.append(traceback.format_exc(limit=3))

    # -- 选择 → 请求 -------------------------------------------------------
    def on_selection(self, data: dict) -> None:
        try:
            self._on_selection_inner(data)
        except Exception:
            self.errors.append(traceback.format_exc(limit=3))
            self._ensure_result_window()
            self.result_window.show_html("<p>本次请求构造失败，请重试。</p>",
                                         title="错误")

    def _on_selection_inner(self, data: dict) -> None:
        env = self.state.envelope
        if env is None:
            return
        self.generation += 1
        generation = self.generation
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
        self.state.selection = selection
        if selection.intent_id:
            self.prefs.record(env.text_hash, selection.intent_id,
                              selection.domain_id)
        self.stats["selections"] += 1
        if self.menu_window is not None:
            self.menu_window.hide()

        composed = build_request_prompt(selection, self.settings,
                                        content=env.text or "")
        self._ensure_result_window()
        self.result_window.show_loading(env.text or "", hint="正在组装请求…")
        key = self._cache_key(composed, env, selection)
        self.stats["requests"] += 1

        if not self.gateway.config.enabled:
            self.bridge.finished.emit(
                (generation, self.gateway.preview(composed, env.text or ""), None))
            return
        cached = self.cache.get(key)
        if cached:
            self.bridge.finished.emit(
                (generation, GatewayResult(html=cached, ok=True, raw=""), None))
            return

        cancel = threading.Event()
        self._cancel = cancel
        alive = self._alive

        def work():
            try:
                result = self.gateway.explain(composed.prompt, env.text or "",
                                              cancel=cancel)
            except Exception:
                result = GatewayResult(html="", ok=False, error="模型调用异常")
            if alive and self._alive:                 # 退出后不再 emit
                self.bridge.finished.emit((generation, result, key))

        threading.Thread(target=work, daemon=True).start()

    def _ensure_result_window(self) -> None:
        if self.result_window is None:
            providers = tuple(p for p in self.settings.providers if p.enabled)
            self.result_window = ResultWindow(providers)
            self.result_window.change_intent_requested.connect(self.on_change_intent)
            self.result_window.requery_requested.connect(self.on_requery)
            self.result_window.settings_requested.connect(self.open_settings)
            self.result_window.search_requested.connect(self.on_search)
            self.result_window.clipboard_written.connect(self._on_self_copy)
        else:
            self.result_window.providers = tuple(
                p for p in self.settings.providers if p.enabled)

    def _on_self_copy(self, text: str) -> None:
        if text:
            self._self_copy.add(sha256_text(text))
            if len(self._self_copy) > 128:
                self._self_copy.clear()

    def _cache_key(self, composed, env: InputEnvelope,
                   selection: UserSelection) -> str:
        from ..settings import structure_signature
        return composed.cache_key(
            model=self.gateway.config.model,
            route_id=composed.query_type,
            intent_id=selection.intent_id or "freeform",
            format_id=selection.format_id,
            schema_version="1.0.0",
            ruleset_version=structure_signature(self.settings)[:16],
            query_hash=env.query_hash,
            selection_salt=selection.cache_salt(),
        )

    def _on_worker_done(self, payload) -> None:
        try:
            generation, result, cache_key = payload
            if not self._alive or generation != self.generation:
                return
            self._ensure_result_window()
            if result.ok and cache_key and result.raw:
                self.cache.put(cache_key, result.html)
            if result.ok:
                self.result_window.show_html(result.html, title="解释结果")
                return
            # 降级：即使 state 已被新输入清空也不能崩
            selection = self.state.selection
            env = self.state.envelope
            if selection is None or env is None:
                self.result_window.show_html(
                    f"<p>{result.error or '请求失败'}</p>", title="错误")
                return
            from .gateway import build_preview_html
            composed = build_request_prompt(selection, self.settings,
                                            content=env.text or "")
            self.result_window.show_html(
                build_preview_html(composed, env.text or "",
                                   reason=result.error or "请求失败"),
                title="解释结果（降级）", error=result.error)
        except Exception:
            self.errors.append(traceback.format_exc(limit=3))

    def on_change_intent(self) -> None:
        env = self.state.envelope
        if env is None or self.state.suggestion is None:
            return
        policy = preflight(env, self.settings)
        menu = build_menu(self.state.suggestion, self.settings, self.prefs)
        self._show_menu(env, policy, self.state.suggestion, menu)

    def on_requery(self) -> None:
        selection = self.state.selection
        if selection is None:
            return
        self.on_selection({
            "domain_id": selection.domain_id,
            "intent_id": selection.intent_id,
            "format_id": selection.format_id,
            "custom_instruction": selection.custom_instruction,
            "free_input_mode": selection.free_input_mode,
            "style_ref": selection.style_ref,
            "allowed_route_ids": list(selection.allowed_route_ids),
        })

    # -- 搜索 / 设置 -------------------------------------------------------
    def on_search(self, provider_id: str, query: str) -> None:
        provider = self.settings.provider(provider_id)
        if provider is None:
            return
        url = provider.build_url(
            query, max_chars=int(self.settings.get("search_max_query_chars", 200)))
        QDesktopServices.openUrl(QUrl(url))

    def open_settings(self) -> None:
        if self.settings_window is None:
            self.settings_window = SettingsWindow(self.settings)
            self.settings_window.applied.connect(self.reload_settings)
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()   # 用户主动打开，允许获得焦点

    def reload_settings(self, data: dict) -> None:
        try:
            self.settings = load_settings(data)
            self.gateway = Gateway(GatewayConfig.from_settings(self.settings))
            if self.menu_window is not None:
                self.menu_window.settings = self.settings
            if self.settings_window is not None:
                self.settings_window.settings = self.settings
        except Exception:
            self.errors.append(traceback.format_exc(limit=3))

    # -- 生命周期 ----------------------------------------------------------
    def shutdown(self) -> None:
        self._alive = False
        if self._cancel is not None:
            self._cancel.set()
        # 只置标志，不跨线程调用 QTimer.stop()；worker 的下一次读取会自行跳过，
        # 迟到信号由 _on_clipboard_text 的 _alive 检查丢弃。
        self.clip_worker._active = False
        self.clip_thread.quit()
        self.clip_thread.wait(1500)
        try:
            self.clip_worker.text_found.disconnect()
            self.bridge.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        self._hide_windows()
        if self.tray is not None:
            self.tray.hide()
        QApplication.quit()
