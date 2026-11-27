"""主进程 → 独立输入进程的桥。

主进程禁用 text-input；本桥在启动子进程时移除禁用变量，让那个进程
可以正常使用中文输入法。结果通过临时 JSON 文件回传，不用 socket、不写日志。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal


def bridge_input_kind() -> str:
    """输入子进程只回传文字；模态恒为 text，必经本地正则关卡（FR-001）。"""
    return "text"


class InputBridge(QObject):
    text_ready = Signal(str, str)          # (用途, 文本)
    settings_ready = Signal(dict)          # 用户保存的完整配置
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dir = Path(tempfile.mkdtemp(prefix="clipllm02-input-"))
        self._serial = 0
        self._pending: dict[int, tuple[str, str, Path]] = {}
        self._processes: dict[int, QProcess] = {}

    def _env(self) -> QProcessEnvironment:
        env = QProcessEnvironment.systemEnvironment()
        env.remove("QT_WAYLAND_TEXT_INPUT_PROTOCOL")
        env.remove("QT_WAYLAND_DISABLED_INTERFACES")
        return env

    def _launch(self, mode: str, purpose: str, args: list[str]) -> None:
        self._serial += 1
        serial = self._serial
        output = self._dir / f"out-{serial}.json"
        config = self._dir / f"cfg-{serial}.json"
        if mode == "settings":
            args = args + ["--config", str(config)]
        proc = QProcess(self)
        proc.setProcessEnvironment(self._env())
        proc.setProgram(sys.executable)
        proc.setArguments(["-m", "clipllm02.studio.input_host",
                           "--mode", mode, "--output", str(output)] + args)
        self._pending[serial] = (mode, purpose, output)
        self._processes[serial] = proc
        proc.finished.connect(lambda _code, _status, s=serial: self._finished(s))
        proc.start()

    def request_text(self, purpose: str, *, title: str = "输入",
                     placeholder: str = "", initial: str = "",
                     multiline: bool = False) -> None:
        mode = "prompt" if multiline else "line"
        self._launch(mode, purpose, [
            "--title", title, "--placeholder", placeholder,
            "--initial", initial,
        ])

    def request_settings(self, config: dict) -> None:
        self._serial += 1
        serial = self._serial
        cfg = self._dir / f"cfg-{serial}.json"
        cfg.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        output = self._dir / f"out-{serial}.json"
        proc = QProcess(self)
        proc.setProcessEnvironment(self._env())
        proc.setProgram(sys.executable)
        proc.setArguments(["-m", "clipllm02.studio.input_host",
                           "--mode", "settings", "--output", str(output),
                           "--config", str(cfg)])
        self._pending[serial] = ("settings", "settings", output)
        self._processes[serial] = proc
        proc.finished.connect(lambda _code, _status, s=serial: self._finished(s))
        proc.start()

    def _finished(self, serial: int) -> None:
        item = self._pending.pop(serial, None)
        proc = self._processes.pop(serial, None)
        if item is None:
            return
        mode, purpose, output = item
        try:
            data = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.failed.emit("输入进程未返回结果")
            return
        finally:
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass
            if proc is not None:
                proc.deleteLater()
        if not data.get("ok"):
            if data.get("error"):
                self.failed.emit(str(data["error"]))
            return
        if mode == "settings":
            self.settings_ready.emit(data.get("config") or {})
        else:
            self.text_ready.emit(purpose, str(data.get("text") or ""))

    def has_active(self) -> bool:
        """是否有仍在运行的独立输入进程；此时 Esc 应交给那个窗口处理。"""
        return any(proc.state() != QProcess.NotRunning
                   for proc in self._processes.values())

    def shutdown(self) -> None:
        for proc in self._processes.values():
            if proc.state() != QProcess.NotRunning:
                proc.kill()
        self._processes.clear()
        self._pending.clear()
