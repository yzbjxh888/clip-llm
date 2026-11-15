"""剪贴板轮询工作线程。

之前 ``subprocess.run(wl-paste)`` 直接在 UI 线程里同步执行（最长 1.5s），
会阻塞 Qt 事件循环，表现为点击菜单「巨大卡顿」。现在整个读取放在 QThread，
只有 ``text_found(str)`` 信号回主线程。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ..serve import ClipboardReader


class ClipboardWorker(QObject):
    text_found = Signal(str)
    status_changed = Signal(str)

    def __init__(self, *, interval_ms: int = 1000,
                 reader: ClipboardReader | None = None):
        super().__init__()
        self.interval_ms = max(200, interval_ms)
        self.reader = reader or ClipboardReader()
        self._timer: QTimer | None = None
        self._active = True
        self._busy = False

    @Slot()
    def start_work(self) -> None:
        self._timer = QTimer(self)
        self._timer.setInterval(self.interval_ms)
        self._timer.timeout.connect(self._read_once)
        self._timer.start()

    def stop_work(self) -> None:
        self._active = False
        if self._timer is not None:
            self._timer.stop()

    @Slot()
    def _read_once(self) -> None:
        if not self._active or self._busy:
            return
        self._busy = True
        try:
            text = self.reader.read()
            if text and self._active:
                self.text_found.emit(text)
            elif self.reader.last_error and self._active:
                self.status_changed.emit(self.reader.last_error)
        finally:
            self._busy = False
