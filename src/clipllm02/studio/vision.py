"""图像识别：融合 Chrome Math OCR 扩展的调用方式。

- 剪贴板图片 / 本地图片文件读取，MIME 与文件头校验；
- OpenAI 兼容 vision 请求，``image_url`` + ``data:image/png;base64,...``；
- 提示词完全可在设置里编辑（默认与 Chrome 扩展一致）；
- 识别出的 LaTeX / 文字作为新的查询内容进入第一层初判，
  不另开一套 UI。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..modality import IMAGE_MIMES, validate_image

COMMANDS = {
    "image/png": (["wl-paste", "--type", "image/png"],
                  ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]),
    "image/jpeg": (["wl-paste", "--type", "image/jpeg"],
                   ["xclip", "-selection", "clipboard", "-t", "image/jpeg", "-o"]),
    "image/webp": (["wl-paste", "--type", "image/webp"],
                   ["xclip", "-selection", "clipboard", "-t", "image/webp", "-o"]),
}


@dataclass(frozen=True)
class VisionResult:
    text: str = ""
    error: str = ""
    mime: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


def _prefer_x11() -> bool:
    return (os.environ.get("QT_QPA_PLATFORM") == "xcb"
            or os.environ.get("XDG_SESSION_TYPE") != "wayland")


def _list_clipboard_image_types() -> set[str] | None:
    """探测剪贴板当前可用的图片 MIME；失败返回 None（表示不限制）。"""
    if _prefer_x11():
        command = ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]
    else:
        command = ["wl-paste", "--list-types"]
    if not shutil.which(command[0]):
        return None
    try:
        proc = subprocess.run(command, capture_output=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lines = proc.stdout.decode("utf-8", errors="replace").splitlines()
    return {line.strip() for line in lines if line.strip()}


def read_clipboard_image(max_bytes: int = 8 * 1024 * 1024) -> tuple[bytes, str] | None:
    available = _list_clipboard_image_types()
    for mime in IMAGE_MIMES:
        if available is not None and mime not in available:
            continue                       # 剪贴板明确没有该类型，跳过
        commands = COMMANDS.get(mime, ())
        if _prefer_x11():
            commands = tuple(reversed(commands))   # xcb 下 xclip 优先
        for command in commands:
            if not shutil.which(command[0]):
                continue
            try:
                proc = subprocess.run(command, capture_output=True,
                                      timeout=3, check=False)
            except (OSError, subprocess.SubprocessError):
                continue
            if proc.returncode != 0 or not proc.stdout:
                continue
            data = proc.stdout
            if validate_image(data, mime, max_bytes) is None:
                return data, mime
    return None


def read_image_file(path: str, max_bytes: int = 8 * 1024 * 1024) -> tuple[bytes, str] | None:
    from pathlib import Path
    file = Path(path)
    suffix = file.suffix.lower()
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp"}.get(suffix.lstrip("."))
    if not mime:
        return None
    data = file.read_bytes()
    return (data, mime) if validate_image(data, mime, max_bytes) is None else None


def capture_screenshot(max_bytes: int = 8 * 1024 * 1024) -> tuple[bytes, str] | None:
    """区域截图：工具链 → xdg-desktop-portal → Qt 全屏，逐级尝试。

    - grim + slurp（Wayland 原生交互选区）
    - gnome-screenshot -a（GNOME/Unity 交互选区）
    - scrot -s / import（X11 交互选区）
    - xdg-desktop-portal Screenshot（桌面弹出截图 UI，用户选区后回传文件）
    - QScreen.grabWindow(0) 全屏兜底
    """
    from pathlib import Path

    data = _capture_with_tools(max_bytes)
    if data is None:
        data = _capture_with_portal(max_bytes)
    if data is None:
        data = _capture_with_qt(max_bytes)
    return data


def _capture_with_tools(max_bytes: int) -> tuple[bytes, str] | None:
    import tempfile
    from pathlib import Path

    handle, target = tempfile.mkstemp(prefix="clipllm02-shot-", suffix=".png")
    os.close(handle)
    target_path = Path(target)
    try:
        candidates: list[list[str]] = []
        if shutil.which("grim") and shutil.which("slurp"):
            try:
                geo = subprocess.run(["slurp"], capture_output=True,
                                     timeout=60, check=False)
                region = geo.stdout.decode("utf-8", errors="replace").strip()
            except (OSError, subprocess.SubprocessError):
                region = ""
            if region:
                candidates.append(["grim", "-g", region, target])
        if shutil.which("gnome-screenshot"):
            candidates.append(["gnome-screenshot", "-a", "-f", target])
        if shutil.which("scrot"):
            candidates.append(["scrot", "-s", "-o", target])
        if shutil.which("import"):
            candidates.append(["import", target])

        for command in candidates:
            try:
                proc = subprocess.run(command, capture_output=True,
                                      timeout=90, check=False)
            except (OSError, subprocess.SubprocessError):
                continue
            if proc.returncode != 0 or not target_path.exists():
                continue
            data = target_path.read_bytes()
            if data and validate_image(data, "image/png", max_bytes) is None:
                return data, "image/png"
        return None
    finally:
        try:
            target_path.unlink(missing_ok=True)
        except OSError:
            pass


def _capture_with_portal(max_bytes: int) -> tuple[bytes, str] | None:
    """xdg-desktop-portal Screenshot：桌面弹出截图界面（可交互选区），
    完成后通过 Response 信号回传图片 URI。"""
    try:
        from PySide6.QtCore import QEventLoop, QTimer, QUrl
        from PySide6.QtDBus import QDBusConnection, QDBusMessage
    except ImportError:
        return None
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return None
    message = QDBusMessage.createMethodCall(
        "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.Screenshot", "Screenshot")
    message.setArguments(["", {"handle_token": "clipllm02",
                               "interactive": True}])
    reply = bus.call(message)
    if reply.type() != reply.MessageType.ReplyMessage or not reply.arguments():
        return None
    request = reply.arguments()[0]
    request_path = request.path() if hasattr(request, "path") else str(request)
    if not request_path.startswith("/org/freedesktop/portal/"):
        return None

    loop = QEventLoop()
    captured: dict = {}
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(60_000)                 # 用户最多 1 分钟完成截图

    def _unwrap(value):
        for _ in range(3):              # 解包 QDBusVariant / QDBusArgument
            if hasattr(value, "variant"):
                value = value.variant()
            elif hasattr(value, "value") and not isinstance(value, (str, int, float)):
                value = value.value()
            else:
                break
        return value

    def on_response(*args) -> None:
        # Response(uint response_code, dict results)；results 值多为 a{sv} 变体
        captured["code"] = args[0] if args else 1
        results = args[1] if len(args) >= 2 else {}
        try:
            for key, value in dict(results).items():
                if key.lower().endswith("uri"):
                    captured["uri"] = str(_unwrap(value))
        except (AttributeError, TypeError, ValueError):
            pass
        loop.quit()

    connected = False
    try:
        connected = bus.connect("", request_path, "org.freedesktop.portal.Request",
                                "Response", on_response)
    except (TypeError, RuntimeError):
        connected = False
    if not connected:
        return None
    loop.exec()
    uri = captured.get("uri", "")
    if not uri.startswith("file://"):
        return None
    from urllib.parse import unquote, urlparse
    uri_path = urlparse(uri).path
    path = unquote(uri_path)
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if data and validate_image(data, "image/png", max_bytes) is None:
        try:
            Path(path).unlink(missing_ok=True)   # portal 临时文件，读后即删
        except OSError:
            pass
        return data, "image/png"
    return None


def _capture_with_qt(max_bytes: int) -> tuple[bytes, str] | None:
    """Qt 全屏兜底：XWayland 下抓 X11 root（Wayland 原生窗口抓不到）。"""
    try:
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return None
        pixmap = screen.grabWindow(0)
        if pixmap.isNull():
            return None
        from PySide6.QtCore import QBuffer
        buffer = QBuffer()
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        pixmap.save(buffer, "PNG")
        data = bytes(buffer.data())
        if data and validate_image(data, "image/png", max_bytes) is None:
            return data, "image/png"
    except (ImportError, RuntimeError, ValueError):
        return None
    return None


def vision_profile(settings) -> dict:
    return {
        "api_url": (settings.get("desktop_vision_api_url")
                    or settings.get("desktop_api_url", "")).strip(),
        "api_key": (settings.get("desktop_vision_api_key")
                    or settings.get("desktop_api_key", "")).strip(),
        "model": (settings.get("desktop_vision_model")
                  or settings.get("desktop_model", "")).strip(),
        "timeout": int(settings.get("desktop_timeout", 45)),
        "max_tokens": int(settings.get("desktop_max_tokens", 1500)),
    }


def recognize(image_bytes: bytes, mime: str, prompt: str,
              settings, *, cancel=None) -> VisionResult:
    profile = vision_profile(settings)
    if not all(profile[k] for k in ("api_url", "api_key", "model")):
        return VisionResult(error="未配置视觉模型（设置 → 模型）", mime=mime)
    payload = {
        "model": profile["model"],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,"
                                     f"{base64.b64encode(image_bytes).decode('ascii')}"}},
            ],
        }],
        "max_tokens": profile["max_tokens"],
    }
    request = urllib.request.Request(
        profile["api_url"],
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {profile['api_key']}"})
    if cancel is not None and cancel.is_set():
        return VisionResult(error="已取消", mime=mime)
    try:
        with urllib.request.urlopen(request, timeout=profile["timeout"]) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content
                              if isinstance(part, dict))
        text = str(content).strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.lower().startswith(("latex", "tex")):
                text = text.split("\n", 1)[-1].strip()
        return VisionResult(text=text, mime=mime) if text else VisionResult(
            error="视觉模型未返回内容", mime=mime)
    except urllib.error.HTTPError as exc:
        return VisionResult(error=f"HTTP {exc.code} {exc.reason}", mime=mime)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return VisionResult(error=f"图片识别失败：{exc}", mime=mime)
