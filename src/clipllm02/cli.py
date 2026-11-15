"""0.2 骨架命令行：无需 Qt、无需网络即可验证全部本地决策链路。

    PYTHONPATH=src python3 -m clipllm02.cli selftest
    PYTHONPATH=src python3 -m clipllm02.cli describe "NaCl"
    PYTHONPATH=src python3 -m clipllm02.cli menu "E=mc²"
    PYTHONPATH=src python3 -m clipllm02.cli compose en.phrase_deep
    PYTHONPATH=src python3 -m clipllm02.cli validate path/to/settings.json
    PYTHONPATH=src python3 -m clipllm02.cli signature
    PYTHONPATH=src python3 -m clipllm02.cli serve --mode clipboard
"""
from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path

from .models import InputEnvelope, Preferences, UserSelection, new_id
from .policy import intercept_record, preflight
from .prompts import build_request_prompt, compose_prompt
from .serve import (
    ClipboardReader, iter_clipboard, iter_stdin, serve as run_serve,
)
from .settings import load_defaults, load_settings, structure_signature, validate
from .suggest import build_menu, suggest


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _describe(text: str, settings) -> dict:
    env = InputEnvelope.for_text(text, source="cli")
    policy = preflight(env, settings)
    s = suggest(env, policy, settings)
    menu = build_menu(s, settings, Preferences())
    return {
        "input": env.as_dict(),
        "features": {k: v for k, v in s.features.as_dict().items()
                     if not k.startswith("_")},
        "policy": policy.as_dict(),
        "intercept_record": intercept_record(env, policy),
        "suggestion": s.as_dict(),
        "menu": menu.as_dict(),
    }


def cmd_describe(args) -> int:
    settings = load_settings(args.config) if args.config else load_defaults()
    print(_dump(_describe(args.text, settings)))
    return 0


def cmd_menu(args) -> int:
    settings = load_settings(args.config) if args.config else load_defaults()
    data = _describe(args.text, settings)
    print(_dump(data["menu"]))
    return 0


def cmd_compose(args) -> int:
    settings = load_settings(args.config) if args.config else load_defaults()
    intent = settings.intent(args.intent_id)
    if intent is None:
        print(f"未知意图：{args.intent_id}", file=sys.stderr)
        return 2
    try:
        result = compose_prompt(intent, settings, content=args.content)
    except ValueError as exc:
        print(f"组合失败：{exc}", file=sys.stderr)
        return 2
    print(_dump(result.as_dict()))
    if args.show_prompt:
        print("\n----- composed prompt -----\n")
        print(result.prompt)
    return 0


def cmd_request(args) -> int:
    """演示 UserSelection → RequestEnvelope 的完整构造与缓存键。"""
    settings = load_settings(args.config) if args.config else load_defaults()
    selection = UserSelection(
        request_id=new_id("req"), envelope_id=new_id("env"),
        domain_id=args.domain or "*", intent_id=args.intent or None,
        format_id=args.format or "static",
        custom_instruction=args.custom, free_input_mode=args.mode,
        style_ref=args.style_ref,
    )
    try:
        composed = build_request_prompt(selection, settings, content=args.text)
    except ValueError as exc:
        print(f"请求构造失败：{exc}", file=sys.stderr)
        return 2
    key = composed.cache_key(
        model=settings.get("model_name", "demo-model"),
        route_id=composed.query_type,
        intent_id=selection.intent_id or "freeform",
        format_id=selection.format_id,
        schema_version="1.0.0",
        ruleset_version=structure_signature(settings)[:16],
        query_hash=InputEnvelope.for_text(args.text).query_hash,
        selection_salt=selection.cache_salt(),
    )
    print(_dump({
        "selection": selection.as_dict(),
        "request": composed.as_dict(),
        "cache_key": key,
        "note": "UserSelection 不含原文；custom_instruction 只进 hash，不进日志",
    }))
    return 0


def cmd_validate(args) -> int:
    settings = load_settings(args.config) if args.config else load_defaults()
    errors = validate(settings)
    if not errors:
        print("OK：配置校验通过")
        return 0
    print(_dump([e.as_dict() for e in errors]))
    return 1


def cmd_signature(args) -> int:
    settings = load_settings(args.config) if args.config else load_defaults()
    print(structure_signature(settings))
    return 0


def cmd_selftest(args) -> int:
    root = Path(__file__).resolve().parents[2]
    suite = unittest.defaultTestLoader.discover(str(root / "tests"),
                                                pattern="test_*.py",
                                                top_level_dir=str(root))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def cmd_serve(args) -> int:
    """前台常驻进程；不注册 systemd，不启动任何后台服务。"""
    import signal
    settings = load_settings(args.config) if args.config else load_defaults()
    reader = ClipboardReader()
    mode = args.mode
    if mode == "auto":
        mode = "clipboard" if sys.stdin.isatty() and reader.available else "stdin"
    if mode == "clipboard" and not reader.available:
        print("未找到 wl-paste / xclip，已回退到 stdin 模式", file=sys.stderr)
        mode = "stdin"
    lines = (iter_clipboard(args.interval, reader) if mode == "clipboard"
             else iter_stdin())

    def on_term(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_term)
    print(f"[模式] {mode} | 轮询 {args.interval}s | 单次 {args.once} | "
          "Ctrl+C / SIGTERM 退出", flush=True)
    run_serve(settings, lines, once=args.once, as_json=args.json,
              show_preview=not args.hide_preview)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="clipllm02", description="clip-llm 0.2 核心骨架")
    parser.add_argument("--config", help="用户配置 JSON；缺省使用 spec/ 出厂配置")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("describe", help="本地特征 / 拦截 / 领域 / 菜单全链路")
    p.add_argument("text")
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("menu", help="只输出 MenuModel")
    p.add_argument("text")
    p.set_defaults(func=cmd_menu)

    p = sub.add_parser("compose", help="组合某个意图的提示词")
    p.add_argument("intent_id")
    p.add_argument("--content", default="")
    p.add_argument("--show-prompt", action="store_true")
    p.set_defaults(func=cmd_compose)

    p = sub.add_parser("request", help="演示 UserSelection → 缓存键")
    p.add_argument("text")
    p.add_argument("--intent")
    p.add_argument("--domain", default="*")
    p.add_argument("--format", default="static")
    p.add_argument("--custom")
    p.add_argument("--mode", default="instruction", choices=["instruction", "prompt"])
    p.add_argument("--style-ref")
    p.set_defaults(func=cmd_request)

    p = sub.add_parser("validate", help="校验配置")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("signature", help="输出结构签名")
    p.set_defaults(func=cmd_signature)

    p = sub.add_parser("selftest", help="运行 tests/ 下的单元测试")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("serve", help="前台常驻进程（不注册 systemd / 无网络 / 无 Qt）")
    p.add_argument("--mode", choices=["auto", "clipboard", "stdin"], default="auto",
                   help="auto: 终端且剪贴板工具可用时用 clipboard，否则 stdin")
    p.add_argument("--interval", type=float, default=1.0, help="剪贴板轮询间隔（秒）")
    p.add_argument("--once", action="store_true", help="只处理一次输入就退出")
    p.add_argument("--json", action="store_true", help="每行输出一个 MenuModel JSON")
    p.add_argument("--hide-preview", action="store_true", help="不显示输入预览")
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
