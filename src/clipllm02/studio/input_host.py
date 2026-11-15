"""独立输入进程：唯一允许使用 Wayland text-input（中文输入法）的地方。

主进程默认禁用全部 text-input 接口以彻底消除与 fcitx5 / Chrome 的串扰；
需要打字时由 ``InputBridge`` 用 QProcess 拉起本模块，独立进程里清除禁用变量，
因此可以正常输入中文。结果通过 ``--output`` 指定的 JSON 文件回传。

模式：
- line      单行输入（查询栏 / 自由输入）
- prompt    多行输入（提示词编辑）
- settings  完整设置窗口（草稿事务由 SettingsDialog 处理）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _clear_isolation() -> None:
    # QProcess 已传干净环境，这里再兜底一次，确保 QApplication 创建前生效
    for key in ("QT_WAYLAND_TEXT_INPUT_PROTOCOL", "QT_WAYLAND_DISABLED_INTERFACES"):
        os.environ.pop(key, None)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _line_dialog(title: str, placeholder: str, initial: str, multiline: bool) -> str | None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QDialog, QDialogButtonBox, QLineEdit, QPlainTextEdit,
        QVBoxLayout, QLabel,
    )

    dialog = QDialog()
    dialog.setWindowTitle(title)
    dialog.setWindowFlags(dialog.windowFlags() | Qt.WindowStaysOnTopHint)
    dialog.resize(560, 220 if multiline else 130)
    dialog.setAttribute(Qt.WA_ShowWithoutActivating, False)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel(title))
    if multiline:
        edit = QPlainTextEdit()
        edit.setPlainText(initial)
        layout.addWidget(edit, 1)
        value = lambda: edit.toPlainText().strip()
    else:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setText(initial)
        layout.addWidget(edit)
        value = lambda: edit.text().strip()
        edit.returnPressed.connect(dialog.accept)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.button(QDialogButtonBox.Ok).setText("确定")
    buttons.button(QDialogButtonBox.Cancel).setText("取消")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    edit.setFocus()
    return value() if dialog.exec() == QDialog.Accepted else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="clipllm02-input")
    parser.add_argument("--mode", choices=["line", "prompt", "settings"], required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--config", default="")
    parser.add_argument("--title", default="输入")
    parser.add_argument("--placeholder", default="")
    parser.add_argument("--initial", default="")
    args = parser.parse_args(argv)

    _clear_isolation()
    from PySide6.QtCore import QEventLoop, Qt     # settings 分支的窗口 flags / 等待
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    output = Path(args.output)
    result: dict = {"ok": False}

    try:
        offscreen = os.environ.get("QT_QPA_PLATFORM") == "offscreen"
        if args.mode in ("line", "prompt"):
            # offscreen 下没有用户可交互，直接回传 initial，供自动化回归使用
            text = (args.initial or "offscreen") if offscreen else _line_dialog(
                args.title, args.placeholder, args.initial, args.mode == "prompt")
            if text is not None:
                result = {"ok": True, "text": text}
        else:
            from ..settings import load_settings
            from .settings_dialog import SettingsDialog
            data = json.loads(Path(args.config).read_text(encoding="utf-8")) \
                if args.config else {}
            if offscreen:
                result = {"ok": True, "config": data}
            else:
                dialog = SettingsDialog(load_settings(data))
                dialog.setWindowFlags(
                    dialog.windowFlags()
                    | Qt.WindowStaysOnTopHint
                    | Qt.WindowMinimizeButtonHint
                    | Qt.WindowMaximizeButtonHint)
                # exec() 会把窗口变成应用模态，Wayland 下模态窗口不给
                # 最大化 / 最小化按钮；改成非模态 + 本地事件循环等待结果。
                dialog.setModal(False)
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
                loop = QEventLoop()
                dialog.finished.connect(loop.quit)
                loop.exec()
                if dialog.result() == 1:    # QDialog.Accepted
                    result = {"ok": True, "config": dialog.data}
    except Exception as exc:            # 独立进程崩溃不能拖垮主进程
        result = {"ok": False, "error": str(exc)}
    _write(output, result)
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
