"""0.2 前台常驻进程。

把「复制 → 本地初判 → 意图菜单 → 用户选择 → 组装请求」这条链路跑在
**一个普通前台进程**里，方便手动试用：

- 不注册 / 不启动 systemd 服务，不写 ~/.config/systemd；
- 不开任何网络请求，不调用大模型；
- 不依赖 Qt / PySide6，纯标准库；
- 剪贴板输入适配器只在本模块调用本地 ``wl-paste`` / ``xclip``；
  核心决策链路（features / policy / suggest / prompts）仍是零 IO 纯函数；
- Ctrl+C 或 SIGTERM 优雅退出并打印统计。

输入来源：
- ``clipboard``：轮询系统剪贴板，内容变化才出菜单；
- ``stdin``：每行当作一次「复制」，并支持 ``1-9`` 选意图、
  ``f <描述>`` 自由输入、``q`` 退出。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .models import (
    InputEnvelope, MenuModel, Preferences, Suggestion, UserSelection,
    new_id, sha256_text,
)
from .policy import preflight
from .prompts import build_request_prompt
from .settings import Settings, structure_signature
from .suggest import build_menu, suggest

BANNER = (
    "clip-llm 0.2 前台进程已启动 | 未注册 systemd | 无网络请求 | 无 Qt\n"
    "输入一段文本 = 模拟复制；1-9 = 选择意图；f <描述> = 自由输入；q = 退出"
)


@dataclass
class ServeStats:
    events: int = 0
    blocked: int = 0
    menus: int = 0
    selections: int = 0
    duplicates: int = 0


@dataclass
class _State:
    settings: Settings
    prefs: Preferences = field(default_factory=Preferences)
    pending_env: InputEnvelope | None = None
    pending_menu: MenuModel | None = None
    last_hash: str = ""
    last_at: float = 0.0


class ClipboardReader:
    """只读系统剪贴板文本；Wayland 下优先 wl-paste，XWayland 用 xclip。"""

    def __init__(self, *, timeout: float = 1.5):
        self.timeout = timeout
        self.last_error: str | None = None

    def commands(self) -> list[list[str]]:
        """所有可用的读取命令；运行时逐个尝试，任何一个成功即可。

        XWayland（xcb）下优先 xclip 读 X11 剪贴板，避免与 Wayland 剪贴板
        出现时序偏差；原生 Wayland 下优先 wl-paste。
        """
        xclip = ["xclip", "-selection", "clipboard", "-o"] if shutil.which("xclip") else None
        wlpaste = ["wl-paste", "--no-newline"] if shutil.which("wl-paste") else None
        prefer_x11 = (os.environ.get("QT_QPA_PLATFORM") == "xcb"
                      or os.environ.get("XDG_SESSION_TYPE") != "wayland")
        ordered = ([xclip, wlpaste] if prefer_x11 else [wlpaste, xclip])
        return [c for c in ordered if c]

    def command(self) -> list[str] | None:
        commands = self.commands()
        return commands[0] if commands else None

    @property
    def available(self) -> bool:
        return bool(self.commands())

    def read(self) -> str | None:
        commands = self.commands()
        if not commands:
            self.last_error = "未找到 wl-paste / xclip，请改用 --mode stdin"
            return None
        errors: list[str] = []
        for command in commands:
            try:
                proc = subprocess.run(command, capture_output=True,
                                      timeout=self.timeout, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(f"{command[0]}: {exc}")
                continue
            if proc.returncode != 0:
                errors.append(f"{command[0]}: 剪贴板无文本")
                continue
            text = proc.stdout.decode("utf-8", errors="replace").strip()
            if text:
                self.last_error = None
                return text
            errors.append(f"{command[0]}: 内容为空")
        self.last_error = "；".join(errors)
        return None


def iter_clipboard(interval: float = 1.0,
                   reader: ClipboardReader | None = None) -> Iterable[str]:
    reader = reader or ClipboardReader()
    while True:
        text = reader.read()
        if text:
            yield text
        time.sleep(max(0.05, interval))


def iter_stdin() -> Iterable[str]:
    for line in sys.stdin:
        yield line.rstrip("\r\n")


def render_menu(env: InputEnvelope, policy, suggestion: Suggestion,
                menu: MenuModel, *, show_preview: bool = True) -> str:
    """人类可读的终端菜单（对应 docs/01_UI_SPEC.md 的五区）。"""
    lines: list[str] = ["", "─" * 62]
    preview = (env.text or "")[:80].replace("\n", " ") if show_preview else "（已隐藏）"
    lines.append(f"[输入] {env.modality} · {len(env.text or '')} 字符 · 预览: {preview}")
    lines.append(f"[策略] {policy.level}"
                 + (f"  ⚠ {' / '.join(policy.warnings)}" if policy.warnings else ""))
    chips = " / ".join(c.label for c in menu.domain_chips) or "无"
    lines.append(f"[初判] {menu.level}  {chips}")
    if suggestion.domain_candidates and suggestion.domain_candidates[0].evidence:
        lines.append("[证据] " + "；".join(suggestion.domain_candidates[0].evidence))
    lines.append("[推荐]")
    if menu.recommended:
        for idx, opt in enumerate(menu.recommended, 1):
            default = "  ⏎默认" if menu.default_intent == opt.intent.id else ""
            lines.append(f"   {idx}. {opt.intent.label} "
                         f"({opt.intent.default_format}){default}")
    else:
        lines.append("   （先选领域）" if menu.level == "LOW" else "   （无可用项）")
    if menu.others:
        lines.append("[其他角度] " + " / ".join(o.intent.label for o in menu.others))
    if menu.grayed:
        lines.append(f"[置灰] {len(menu.grayed)} 项（设置中关闭或不适用）")
    lines.append(f"[呈现] {' / '.join(menu.formats)}")
    if menu.free_input.get("enabled"):
        lines.append("[自由输入] f <描述>；参考风格：" +
                     ", ".join(menu.free_input["style_refs"][:5]) + " …")
    lines.append("[搜索] " + ", ".join(p.label for p in menu.tools))
    lines.append("输入 1-9 选择 / f <描述> / 新文本 / q 退出")
    lines.append("─" * 62)
    return "\n".join(lines)


def render_selection(selection: UserSelection, composed, settings: Settings,
                     env: InputEnvelope) -> str:
    """只展示「组装好的请求」，绝不发送。"""
    key = composed.cache_key(
        model=settings.get("model_name", "demo-model"),
        route_id=composed.query_type,
        intent_id=selection.intent_id or "freeform",
        format_id=selection.format_id,
        schema_version="1.0.0",
        ruleset_version=structure_signature(settings)[:16],
        query_hash=env.query_hash,
        selection_salt=selection.cache_salt(),
    )
    return "\n".join([
        "", "─" * 62,
        "[已选择] " + (selection.intent_id or "自由输入"),
        f"[请求] query_type={composed.query_type} · 字段 {len(composed.fields)} 个 · "
        f"prompt {len(composed.prompt)} 字符",
        f"[缓存键] {key[:32]}…",
        "[注意] 仅本地组装，未发送任何网络请求（0.2 骨架不含 LLM Gateway）",
        "─" * 62,
    ])


def _choose(state: _State, index: int, *, free_text: str | None = None) -> str | None:
    env, menu = state.pending_env, state.pending_menu
    if env is None or menu is None:
        return None
    if free_text is not None:
        intent = None
        fmt = state.settings.formats.get("default", "static")
        custom: str | None = free_text
    else:
        options = menu.recommended
        if not 1 <= index <= len(options):
            return f"[提示] 没有第 {index} 项，当前推荐 {len(options)} 项"
        intent = options[index - 1].intent
        fmt = intent.default_format
        custom = None

    selection = UserSelection(
        request_id=new_id("req"), envelope_id=env.envelope_id,
        domain_id=menu.domain_chips[0].id if menu.domain_chips else "*",
        intent_id=intent.id if intent else None,
        format_id=fmt, custom_instruction=custom,
        allowed_route_ids=(intent.primary_route,) if intent and intent.primary_route else (),
    )
    composed = build_request_prompt(selection, state.settings,
                                    content=env.text or "")
    if intent:
        state.prefs.record(env.text_hash, intent.id, selection.domain_id)
    return render_selection(selection, composed, state.settings, env)


def _handle_text(state: _State, text: str, *, show_preview: bool,
                 as_json: bool, stats: ServeStats) -> str | None:
    digest = sha256_text(text)
    now = time.time()
    if digest == state.last_hash and now - state.last_at < 300:
        stats.duplicates += 1
        return None
    state.last_hash, state.last_at = digest, now
    stats.events += 1

    env = InputEnvelope.for_text(text, source="serve")
    policy = preflight(env, state.settings)
    if not policy.allowed:
        stats.blocked += 1
        return f"[拦截] {policy.level} · 原因 {', '.join(policy.block_reasons) or '重复内容'} · 长度 {len(text)}（不弹窗、不发请求）"

    suggestion = suggest(env, policy, state.settings)
    menu = build_menu(suggestion, state.settings, state.prefs)
    state.pending_env, state.pending_menu = env, menu
    stats.menus += 1
    if as_json:
        import json
        return json.dumps(menu.as_dict(), ensure_ascii=False)
    return render_menu(env, policy, suggestion, menu, show_preview=show_preview)


def serve(settings: Settings, lines: Iterable[str], *,
          printer: Callable[[str], None] = print,
          once: bool = False, as_json: bool = False,
          show_preview: bool = True, banner: bool = True) -> ServeStats:
    """前台主循环。``lines`` 由调用方提供（剪贴板轮询或 stdin）。"""
    state = _State(settings=settings)
    stats = ServeStats()
    if banner and not as_json:
        printer(BANNER)

    try:
        for raw in lines:
            line = (raw or "").strip()
            if not line:
                continue
            if line.lower() in {"q", "quit", "exit"}:
                break
            if not as_json and state.pending_menu is not None:
                if line.isdigit():
                    message = _choose(state, int(line))
                    if message:
                        stats.selections += 1
                        printer(message)
                    continue
                if line.startswith("f ") or line.startswith("f\t"):
                    custom = line[2:].strip()
                    if custom:
                        message = _choose(state, 0, free_text=custom)
                        if message:
                            stats.selections += 1
                            printer(message)
                        continue
            message = _handle_text(state, line, show_preview=show_preview,
                                   as_json=as_json, stats=stats)
            if message:
                printer(message)
            if once:
                break
    except KeyboardInterrupt:
        pass
    finally:
        if banner and not as_json:
            printer(f"[退出] 事件 {stats.events} · 菜单 {stats.menus} · "
                    f"选择 {stats.selections} · 拦截 {stats.blocked} · "
                    f"重复跳过 {stats.duplicates}；未注册 systemd、未发送网络请求")
    return stats
