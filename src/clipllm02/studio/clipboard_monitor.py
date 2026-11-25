"""剪贴板监听：X11 dataChanged + 常驻 ``wl-paste --watch``（零周期 spawn）。

XWayland 下 ``QClipboard.dataChanged`` 收不到 Wayland 原生应用（Chrome / 终端）
的复制——合成器不向 X11 推 selection 事件。兜底不能每秒 spawn ``wl-paste``
（周期性 fork 会造成屏幕闪烁），改用**常驻** ``wl-paste --watch`` 进程：
它监听 Wayland 剪贴板变化并触发内层命令输出内容，我们通过 QProcess 读 stdout，
事件驱动、零轮询。
"""
from __future__ import annotations

import hashlib
import shutil
import time

from PySide6.QtCore import QBuffer, QObject, QProcess, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication


def image_to_png_bytes(image: QImage) -> bytes | None:
    if image is None or image.isNull():
        return None
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        return None
    return bytes(buffer.data())


class ClipboardMonitor(QObject):
    text_found = Signal(str)
    image_found = Signal()               # 剪贴板出现新图片（不自动识别）

    def __init__(self, parent=None, *, debounce_ms: int = 300):
        super().__init__(parent)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(debounce_ms)
        self._debounce.timeout.connect(self._emit_changes)
        self._watch_buffer = b""
        self._watch_flush = QTimer(self)
        self._watch_flush.setSingleShot(True)
        self._watch_flush.setInterval(150)
        self._watch_flush.timeout.connect(self._flush_watch)
        self._last_digest = ""
        self._last_image_digest = ""
        self._last_image_at = 0.0         # 图像事件时间，区分双通道重复与重新复制
        self._active = True               # 兼容旧测试的开关语义
        self._suppressed: set[str] = set()
        self.last_image: tuple[bytes, str] | None = None

        QApplication.clipboard().dataChanged.connect(self._on_change)

        # 常驻 wl-paste --watch：每次 Wayland 剪贴板变化时由它运行内层
        # wl-paste 输出新内容（零周期 spawn，消除轮询闪烁）
        self._watch: QProcess | None = None
        self._image_watch: QProcess | None = None
        self._image_buffer = b""
        self._start_watch()
        self._start_image_watch()

    def _start_watch(self) -> None:
        if not shutil.which("wl-paste"):
            return
        watch = QProcess(self)
        watch.setProgram("wl-paste")
        watch.setArguments(["--watch", "wl-paste", "--no-newline"])
        watch.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        watch.readyReadStandardOutput.connect(self._on_watch_output)
        watch.errorOccurred.connect(lambda *_: self._restart_watch())
        watch.finished.connect(lambda *_: self._restart_watch())
        self._watch = watch
        watch.start()

    def _start_image_watch(self) -> None:
        """监听 Wayland 剪贴板的图片变化（GNOME 截图 Win+Shift+S 会写入）。"""
        if not shutil.which("wl-paste"):
            return
        watch = QProcess(self)
        watch.setProgram("wl-paste")
        watch.setArguments(["--watch", "--type", "image/png",
                            "wl-paste", "--type", "image/png"])
        watch.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        watch.readyReadStandardOutput.connect(self._on_image_watch_output)
        self._image_watch = watch
        watch.start()

    def _on_image_watch_output(self) -> None:
        if self._image_watch is None:
            return
        self._image_buffer += bytes(self._image_watch.readAllStandardOutput())
        # PNG 以 IEND 块 + CRC 结尾（AE 42 60 82），据此判断一张完整 PNG
        if b"IEND\xaeB`\x82" in self._image_buffer:
            data, self._image_buffer = self._image_buffer, b""
            start = data.find(b"\x89PNG")
            if start >= 0:
                data = data[start:]
            if data.startswith(b"\x89PNG") and data.endswith(b"IEND\xaeB`\x82"):
                self._accept_image(data, "image/png")

    def _restart_watch(self) -> None:
        if not self._active or self._watch is None:
            return
        QTimer.singleShot(1000, self._start_watch)

    def _on_watch_output(self) -> None:
        """watch 输出可能分片到达——聚合成一次完整文本，避免反复弹窗。"""
        if self._watch is None:
            return
        self._watch_buffer += bytes(self._watch.readAllStandardOutput())
        self._watch_flush.start()

    def _flush_watch(self) -> None:
        data, self._watch_buffer = self._watch_buffer, b""
        text = data.decode("utf-8", errors="replace").strip()
        if text:
            self._emit_text(text)

    # -- 事件 --------------------------------------------------------------
    def _on_change(self) -> None:
        if self._active:
            self._debounce.start()        # 连续变化合并为一次处理

    def _digest(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _emit_text(self, text: str) -> None:
        # 图片事件刚发生（1 秒内）的文本是同一次复制的附带内容（文件名 /
        # URL / 图片说明）。放行会让图片初判被作废或覆盖，成「点了没反应」。
        # Wayland 的图片 / 文本是两条独立 watch，无法在同函数判断，这里兜底。
        if self._last_image_at and time.monotonic() - self._last_image_at < 1.0:
            return
        # 相同内容不去重：用户重复复制相同词也应再次弹窗（词典软件惯例）。
        # 抖动与双通道重复由 app 层的 desktop_dup_ttl 短窗口去重兜底。
        import os
        if os.environ.get("CLIPLLM_DIAG"):
            print(f"[diag] clip-text len={len(text)} head={text[:24]!r}",
                  flush=True)
        digest = self._digest(text.encode("utf-8"))
        if digest in self._suppressed:
            self._suppressed.discard(digest)   # 一次性抑制
            return
        self._last_digest = digest
        if self._active:
            self.text_found.emit(text)

    def _accept_image(self, data: bytes, mime: str) -> bool:
        """收一张剪贴板图，抑制「同一次复制」的双通道重复上报。

        watch 与 dataChanged 会在几百毫秒内对同一次复制各报一次；但用户
        隔一会儿重新复制**同一张**图，是明确的新动作，必须再次发出事件。
        用「内容摘要 + 1 秒窗口」区分二者：窗口内同摘要丢弃，窗口外放行。
        返回是否真的发出了图片事件，供调用方抑制同一次的文本事件。
        """
        digest = self._digest(data)
        now = time.monotonic()
        if (digest == self._last_image_digest
                and now - self._last_image_at < 1.0):
            return False
        self._last_image_digest = digest
        self._last_image_at = now
        self.last_image = (data, mime)
        self.image_found.emit()
        return True

    def _emit_changes(self) -> None:
        """X11 dataChanged 路径。

        一次「复制图片」事件里剪贴板往往**同时**带图片和文本（文件名 /
        URL / @image: 路径 / 图片说明）。若两个都上报，图片初判刚弹出，
        紧随的文本就会走 process_text —— 要么把当前标签标成 invalidated
        让图片按钮全部静默失效，要么把图片初判覆盖成文本初判。
        所以同一次事件里图片被接受就不再上报文本。
        """
        clipboard = QApplication.clipboard()
        image = clipboard.image()
        image_accepted = False
        if not image.isNull():
            data = image_to_png_bytes(image)
            if data:
                image_accepted = self._accept_image(data, "image/png")
        text = clipboard.text().strip()
        if text and not image_accepted:
            self._emit_text(text)

    # -- 兼容 / 控制 -------------------------------------------------------
    def suppress_text(self, text: str) -> None:
        """标记一次自我复制（如「复制结果」写回剪贴板），下次读到跳过。"""
        if text:
            self._suppressed.add(self._digest(text.encode("utf-8")))

    def take_image(self) -> tuple[bytes, str] | None:
        """取走最近一张剪贴板图片。

        取后清空 last_image，但**不清内容摘要**：那道摘要只用于抑制
        watch / dataChanged 双通道的同一次复制。若在这里清掉，用户
        「同一张图重新复制再查一次」会被误判成新事件反复弹窗；反过来
        也不能永久记住摘要，否则同一张图永远发不出第二次事件。
        这里保留摘要到下一次真正不同的内容到来为止即可。
        """
        image, self.last_image = self.last_image, None
        return image

    def stop(self) -> None:
        self._active = False
        for proc in (self._watch, self._image_watch):
            if proc is not None:
                proc.kill()
        self._watch = None
        self._image_watch = None
        try:
            QApplication.clipboard().dataChanged.disconnect(self._on_change)
        except (RuntimeError, TypeError):
            pass
