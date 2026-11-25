"""统一桌面应用入口：``python -m clipllm02.studio`` / ``clipllm02-desktop``。

前台进程，Ctrl+C 退出；不注册 systemd。

平台策略（默认 XWayland）：
- 在 Wayland 会话里，客户端不能自行定位窗口，且 Unity/Mutter 合成器拒绝
  ``startSystemResize``，导致无边框窗口拖左/上边缘时方向反向、拖右/下也异常；
  同时参与 ``zwp_text_input_manager`` 会与 fcitx5 / Chrome 争用 text-input。
- 因此默认走 ``xcb``（XWayland）：窗口缩放交给 X11 窗口管理器（方向正确），
  且完全不参与 Wayland text-input 协议，从根上消除与 Chrome / 输入法的串扰。
- 只有显式 ``--wayland-native`` 才回到原生 Wayland（输入法走 text-input-v3，
  但无边框缩放受限、可能串扰，仅供调试）。
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

GS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
GS_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/clipllm02-vision/"

BUILD = "2026-11-23-unified-input"  # 每轮修复递增；启动横幅与托盘 tooltip 均显示


def check_single_instance() -> None:
    """前台进程单实例：旧实例残留会让托盘/快捷键指向旧代码。

    精确匹配入口脚本名（desktop / studio / classic）或 `python -m` 的模块名
    正好是 clipllm02.studio / clipllm02.desktop；不会把
    `clipllm02.studio.input_host` 这类子进程误判为主实例。
    """
    import subprocess
    script_names = {"clipllm02-desktop", "clipllm02-studio", "clipllm02-classic"}
    modules = {"clipllm02.studio", "clipllm02.desktop"}
    try:
        out = subprocess.run(["pgrep", "-f", "clipllm02"],
                             capture_output=True, timeout=3, check=False)
        candidates = [int(x) for x in out.stdout.split()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return
    pids: list[int] = []
    for pid in candidates:
        if pid == os.getpid():
            continue
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            argv = [a.decode("utf-8", "replace")
                    for a in raw.split(b"\0") if a]
        except OSError:
            continue
        if not argv:
            continue
        exe = argv[0].rsplit("/", 1)[-1]
        matched = exe in script_names
        if not matched and exe.startswith("python"):
            matched = any(
                arg == "-m" and i + 1 < len(argv) and argv[i + 1] in modules
                for i, arg in enumerate(argv))
        if matched:
            pids.append(pid)
    if pids:
        print(f"⚠ 已有实例在运行（PID {pids}），旧实例占用着托盘与快捷键。", flush=True)
        print("  请先：pkill -9 -f 'clipllm02-(desktop|studio|classic)'  再重新启动。",
              flush=True)
        raise SystemExit(1)


def _select_platform(offscreen: bool, wayland_native: bool) -> str:
    if offscreen:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        return "offscreen"
    if os.environ.get("QT_QPA_PLATFORM"):
        return os.environ["QT_QPA_PLATFORM"]        # 用户显式指定，尊重
    if wayland_native:
        return "wayland"
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"        # Wayland 会话 → XWayland
        return "xcb"
    return ""                                       # X11 会话自然用 xcb


def register_global_shortcuts() -> int:
    """可选：注册 GNOME 全局快捷键（不是 systemd，用户显式执行才改系统设置）。"""
    exe = str(Path(sys.executable).parent / "clipllm02-desktop")
    commands = [
        ["gsettings", "set", GS_SCHEMA, "custom-keybindings", f"['{GS_PATH}']"],
        ["gsettings", "set", f"{GS_SCHEMA}.custom-keybinding:{GS_PATH}",
         "name", "clip-llm 图片识别"],
        ["gsettings", "set", f"{GS_SCHEMA}.custom-keybinding:{GS_PATH}",
         "command", f"{exe} --vision-now"],
        ["gsettings", "set", f"{GS_SCHEMA}.custom-keybinding:{GS_PATH}",
         "binding", "<Control><Alt>s"],
    ]
    for command in commands:
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"注册失败：{exc}", file=sys.stderr)
            return 1
    print("已注册全局快捷键 Ctrl+Alt+S（图片识别）。"
          "撤销：gsettings reset-recursively "
          f"{GS_SCHEMA}.custom-keybinding:{GS_PATH}")
    return 0


def send_vision_now() -> int:
    """全局快捷键触发：写 IPC 请求文件，由已运行的主进程执行。"""
    root = Path(os.environ.get("XDG_CACHE_HOME",
                               str(Path.home() / ".cache"))) / "clip-llm02" / "ipc"
    root.mkdir(parents=True, exist_ok=True)
    (root / "vision.cmd").write_text("vision", encoding="utf-8")
    return 0


def run_doctor() -> int:
    """环境诊断：定位输入法串扰 / 截图 / 剪贴板问题的现场信息。"""
    import shutil

    print("== clip-llm 环境诊断 ==")
    session = os.environ.get("XDG_SESSION_TYPE", "?")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "?")
    print(f"会话: {session} · 桌面: {desktop} · "
          f"QT_QPA_PLATFORM={os.environ.get('QT_QPA_PLATFORM', '(默认)')}")
    chosen = _select_platform(False, False)
    print(f"平台策略选择: {chosen or '(系统默认)'}")
    for var in ("QT_IM_MODULE", "GTK_IM_MODULE", "XMODIFIERS"):
        print(f"{var}={os.environ.get(var, '(未设置)')}")
    print("-- 剪贴板命令 --")
    for tool in ("wl-paste", "xclip"):
        print(f"  {tool}: {shutil.which(tool) or '无'}")
    print("-- 截图工具链 --")
    for tool in ("grim", "slurp", "gnome-screenshot", "scrot", "import"):
        print(f"  {tool}: {shutil.which(tool) or '无'}")
    print("-- xdg-desktop-portal Screenshot --")
    try:
        from PySide6.QtDBus import QDBusConnection, QDBusMessage
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            print("  session bus 不可用")
        else:
            message = QDBusMessage.createMethodCall(
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.Screenshot", "Screenshot")
            message.setArguments(["", {"handle_token": "doctor",
                                       "interactive": True}])
            reply = bus.call(message)
            ok = reply.type() == reply.MessageType.ReplyMessage
            print(f"  接口调用: {'可用' if ok else '失败'}"
                  + ("（portal 会弹出桌面截图界面）" if ok else ""))
    except Exception as exc:            # noqa: BLE001
        print(f"  异常: {exc}")
    print("-- 建议 --")
    print("  · Chrome 打字被干扰的具体表现是什么（打字卡 / 输入法失效 / 焦点跳走）？")
    print("  · 输入法是 fcitx5 还是 ibus？在 Chrome 原生 Wayland 还是 XWayland 运行？")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="clipllm02-desktop",
        description="clip-llm Studio：词典式双层内容解释（前台进程，不加入 systemd）")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="剪贴板轮询间隔（秒）")
    parser.add_argument("--offscreen", action="store_true",
                        help="Qt offscreen 平台（无界面自测）")
    parser.add_argument("--wayland-native", action="store_true",
                        help="使用原生 Wayland（输入法走 text-input-v3，无边框缩放受限）")
    parser.add_argument("--register-shortcuts", action="store_true",
                        help="注册 GNOME 全局快捷键 Ctrl+Alt+S 后退出（不注册 systemd）")
    parser.add_argument("--doctor", action="store_true",
                        help="环境诊断：平台 / 输入法模块 / 截图与剪贴板可用性")
    parser.add_argument("--vision-now", action="store_true",
                        help="供全局快捷键调用：通知已运行实例执行图片识别")
    parser.add_argument("--smoke", metavar="TEXT",
                        help="只处理一次输入后退出，不进入事件循环")
    args = parser.parse_args(argv)

    if args.register_shortcuts:
        return register_global_shortcuts()
    if args.vision_now:
        return send_vision_now()
    if args.doctor:
        return run_doctor()

    # 自测模式不进入事件循环，也不接管托盘/快捷键，无需单实例保护
    if args.smoke is None:
        check_single_instance()

    platform = _select_platform(args.offscreen, args.wayland_native)
    os.environ.setdefault("QT_LOGGING_RULES",
                          "qt.qpa.wayland.textinput.warning=false")

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from .app import StudioApp
    from .config import load_config
    from .main_window import app_icon

    app = QApplication.instance() or QApplication([])
    app.setWindowIcon(app_icon())      # 任务栏 / Alt+Tab / 通知都显示它
    settings = load_config()
    studio = StudioApp(settings, poll_interval=args.interval)

    print("clip-llm Studio | 第一层初判 + 第二层大模型请求 | 前台进程 | "
          "未注册 systemd", flush=True)
    print(f"build {BUILD} · 平台 {platform or app.platformName()} · "
          f"剪贴板事件驱动（CopyQ 式）· "
          f"字号 {settings.get('desktop_font_size', 14)}pt", flush=True)
    print("快捷键：Esc 关闭初判 · Ctrl+T 新标签 · Ctrl+Alt+S 图片识别 · "
          "F3 设置（均可在设置里改）", flush=True)

    if args.smoke is not None:
        studio.start()
        studio.process_text(args.smoke, source="smoke")
        app.processEvents()
        studio.shutdown()
        return 0

    studio.start()
    signal.signal(signal.SIGINT, lambda *_args: setattr(studio, "_sigint", True))
    heartbeat = QTimer()

    def check_signal():
        if getattr(studio, "_sigint", False):
            studio.shutdown()

    heartbeat.timeout.connect(check_signal)
    heartbeat.start(200)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
