"""桌面版入口：``python -m clipllm02.desktop``。

前台进程，Ctrl+C 退出；不注册 systemd、不启动后台服务。
"""
from __future__ import annotations

import argparse
import os
import signal
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="clipllm02-desktop",
        description="clip-llm 0.2 桌面版（前台进程，不加入 systemd）")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="剪贴板轮询间隔（秒），默认 1.0")
    parser.add_argument("--no-tray", action="store_true",
                        help="不显示系统托盘（无托盘扩展时自动跳过）")
    parser.add_argument("--offscreen", action="store_true",
                        help="Qt 使用 offscreen 平台（无界面自测）")
    parser.add_argument("--isolate-text-input", action="store_true",
                        help="按 0.1 方式彻底禁用 Wayland text-input（最流畅，但内联输入框无法用中文输入法）")
    parser.add_argument("--smoke", metavar="TEXT",
                        help="只处理一次输入后退出，不进入事件循环")
    args = parser.parse_args(argv)

    if args.offscreen:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if args.isolate_text_input:
        os.environ["QT_WAYLAND_TEXT_INPUT_PROTOCOL"] = ""
        os.environ["QT_WAYLAND_DISABLED_INTERFACES"] = (
            "zwp_text_input_manager_v1,zwp_text_input_manager_v2,"
            "zwp_text_input_manager_v3,zwp_text_input_v1,zwp_text_input_v2,"
            "zwp_text_input_v3,ext_text_input_manager_v1,ext_text_input_v1")
    else:
        # Qt Wayland 在 WA_ShowWithoutActivating 窗口上会打印无害的
        # textinput disableSurface 警告；默认保留中文输入法，仅屏蔽该条日志。
        os.environ.setdefault(
            "QT_LOGGING_RULES", "qt.qpa.wayland.textinput.warning=false")

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from .app import DesktopApp
    from .settings_window import load_user_settings

    app = QApplication.instance() or QApplication([])
    settings = load_user_settings()
    desktop = DesktopApp(settings, poll_interval=args.interval,
                         use_tray=not args.no_tray)

    print("clip-llm 0.2 桌面版 | 前台进程 | 未注册 systemd | "
          "无 Key 时不发送网络请求", flush=True)
    print(f"配置: {settings.trigger_policy} · 剪贴板轮询 {args.interval}s · "
          f"托盘 {'开启' if desktop.tray is not None else '不可用'} · "
          "Ctrl+C 退出", flush=True)

    if args.smoke is not None:
        desktop.process_text(args.smoke, source="smoke")
        app.processEvents()
        desktop.shutdown()
        return 0

    desktop.start()

    # Python 信号处理器里只置标志；真正的 Qt 退出在事件循环线程里做，
    # 避免跨线程直接调用 Qt API 造成崩溃。
    signal.signal(signal.SIGINT, lambda *_args: setattr(desktop, "_sigint", True))
    heartbeat = QTimer()

    def check_signal():
        if getattr(desktop, "_sigint", False):
            desktop.shutdown()

    heartbeat.timeout.connect(check_signal)
    heartbeat.start(200)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
