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
        timer.stop()
        loop.quit()

    connected = False
    try:
        connected = bus.connect("", request_path, "org.freedesktop.portal.Request",
                                "Response", on_response)
    except (TypeError, RuntimeError):
        connected = False
    if not connected:
        return None
    try:
        # 若响应已在 connect 与 exec 之间落地，就不要依赖 quit 对未运行循环生效。
        if captured:
            timer.stop()
        else:
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
    finally:
        timer.stop()
        try:
            bus.disconnect(request_path, "org.freedesktop.portal.Request",
                           "Response", on_response)
        except (TypeError, RuntimeError):
            pass


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


def vision_enabled(settings) -> bool:
    """图像识别总开关（设置 → 模型）。关闭后截图不弹菜单、不发视觉请求。"""
    image = getattr(settings, "image", None) or {}
    return bool(image.get("vision_enabled", True))


def vision_route_prompt(settings, route_id: str) -> str:
    """取某套图片模板的提示词；为空时回退全局兜底提示词。"""
    pack = (getattr(settings, "routes", None) or {}).get(route_id) or {}
    prompt = str(pack.get("prompt") or "").strip()
    if prompt:
        return prompt
    return str(settings.get("desktop_vision_prompt", "") or "").strip()


# 图片结果的追问 system：中性说明，不带上一轮那次的具体提问。
# 上一轮的问答由 history 承载；把「用户的要求：…」塞进 system 会让模型把
# 旧问题当成常驻指令，每次都重复答上一题。
VISION_FOLLOWUP_SYSTEM = (
    "你正在就一张图片回答后续问题。图片会随消息附带；"
    "回答必须基于图中实际内容与上面的对话，不要编造图中不存在的符号或文字。"
    "用自然语言回答，不要输出 JSON。"
)


def compose_vision_prompt(prompt: str, user_text: str) -> str:
    """双模态：用户文本作为「附加要求」拼在图片模板之后，不改模板本身。

    明确写出优先级：方式的输出格式（字段清单 / 只返回识别结果）不该压住
    用户这次真正想问的问题，否则模型会照着格式跑、无视要求。
    """
    text = (user_text or "").strip()
    if not text:
        return prompt
    if not str(prompt).strip():
        return text
    # 要求放最前：小模型对「开头」和「结尾」最敏感，放末尾会被方式里的
    # 「只返回识别结果」压住（实测 8B 视觉模型会直接转写图片、无视问题）。
    # 同时把方式提示词降级为「参考格式」，避免它继续当主指令。
    return (f"用户的要求：{text}\n\n"
            "请直接回答上面这个要求，不要只做文字/公式识别，"
            "也不要只输出字段清单。\n\n"
            "【参考格式（仅在与上面要求相容时使用）】\n"
            f"{prompt}")


def intent_vision_prompt(settings, route_id: str, intent=None,
                         user_text: str = "", mode: str = "instruction") -> str:
    """某条图片查询方式最终发给视觉模型的提示词。

    有三条路径：
    1. 选了查询方式：``prompt_override`` > 所属模板 prompt > 全局兜底，
       末尾再拼「用户附加要求」；
    2. 自由输入（``intent is None``）：用户要求就是主指令，不该被降级成
       附加要求挂在某段通用提示词后面（否则模型只会照着那段落跑）；
       ``mode="prompt"`` 时直接把它当提示词用。
    """
    text = (user_text or "").strip()
    if intent is None and text:
        if mode == "prompt":
            return text
        return ("按下面的要求处理这张图片，只输出结果本身，不要描述分析过程。\n"
                f"要求：{text}\n"
                "图中出现公式时用 LaTeX 表示（反斜杠双写）；"
                "辨认不清的内容直接说明，不要编造。")
    override = str(getattr(intent, "prompt_override", "") or "").strip()
    return compose_vision_prompt(
        override or vision_route_prompt(settings, route_id), user_text)


def vision_route(settings) -> str:
    """图片模板 id：图片侧只有一条路由（图片识别）。"""
    for route_id, pack in (getattr(settings, "routes", None) or {}).items():
        if isinstance(pack, dict) and pack.get("vision"):
            return route_id
    return "图片识别"


def vision_prompt(settings) -> tuple[str, str]:
    """返回 (模板 id, 发给视觉模型的提示词)。

    提示词的唯一权威是**图片路由自己的 prompt**（设置页「提示词模板 → 图片模板」）；
    只有该模板为空时才回退到 ``desktop_vision_prompt``。0.2.7 之前这里只读
    全局那一条，导致「图片文字」模板改了也不生效。
    """
    route_id = vision_route(settings)
    return route_id, vision_route_prompt(settings, route_id)


def vision_profile(settings) -> dict:
    """视觉链路与主模型共用同一套配置（FR-003）：main_model_config 唯一来源。

    旧 ``desktop_vision_*`` 优先逻辑已移除；保留键只为兼容旧配置读取，
    不再作为覆盖主模型的第二套来源。
    """
    main = dict(getattr(settings, "main_model_config", None) or {})
    return {
        "api_url": str(main.get("main_model_api_url")
                       or settings.get("desktop_api_url", "")).strip(),
        "api_key": str(main.get("main_model_key")
                       or settings.get("desktop_api_key", "")).strip(),
        "model": str(main.get("main_model_name")
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
