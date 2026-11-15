"""输入模态仲裁与图片资产存储。

把 0.1 中「文本 Provider」与「图片 Provider」各自独立触发的关系显式化：
同一次复制可能同时提供文本与图片，必须在进入 Policy Engine 之前合并。
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import AssetRef, InputEnvelope

IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp")
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def image_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_image(data: bytes, mime: str, max_bytes: int) -> str | None:
    """返回错误原因，合法则返回 None。"""
    if mime not in IMAGE_MIMES:
        return f"不支持的图片类型 {mime}"
    if not data or len(data) > max_bytes:
        return "图片为空或超过大小上限"
    if mime == "image/png" and not data.startswith(b"\x89PNG"):
        return "PNG 文件头不合法"
    if mime == "image/jpeg" and not data.startswith(b"\xff\xd8"):
        return "JPEG 文件头不合法"
    if mime == "image/webp" and not (data.startswith(b"RIFF") and data[8:12] == b"WEBP"):
        return "WebP 文件头不合法"
    return None


class AssetStore:
    """把原图落到 XDG 缓存目录；数据对象只保留路径与摘要。"""

    def __init__(self, base_dir: str | Path | None = None, *, max_bytes: int = 8 * 1024 * 1024):
        if base_dir is None:
            root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
            base_dir = root / "clip-llm" / "assets"
        self.base_dir = Path(base_dir)
        self.max_bytes = max_bytes

    def put(self, data: bytes, mime: str, *, width: int = 0, height: int = 0) -> AssetRef:
        error = validate_image(data, mime, self.max_bytes)
        if error:
            raise ValueError(error)
        digest = image_digest(data)
        sub = self.base_dir / digest[:2]
        sub.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(sub, 0o700)
        except OSError:
            pass
        target = sub / f"{digest}{_EXT[mime]}"
        if not target.exists():
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        return AssetRef(asset_id=digest, mime=mime, sha256=digest,
                        local_path=str(target), width=width, height=height)


class MemoryAssetStore(AssetStore):
    """测试/CLI 用：不落盘，仍产出完整 AssetRef。"""

    def __init__(self, *, max_bytes: int = 8 * 1024 * 1024):
        super().__init__(base_dir="<memory>", max_bytes=max_bytes)
        self.saved: dict[str, bytes] = {}

    def put(self, data: bytes, mime: str, *, width: int = 0, height: int = 0) -> AssetRef:
        error = validate_image(data, mime, self.max_bytes)
        if error:
            raise ValueError(error)
        digest = image_digest(data)
        self.saved[digest] = data
        return AssetRef(asset_id=digest, mime=mime, sha256=digest,
                        local_path=f"memory://{digest}", width=width, height=height)


@dataclass
class _Pending:
    text: str | None = None
    text_source: str = "manual"
    image: bytes | None = None
    image_mime: str | None = None
    image_source: str = "screenshot"
    first_at: float = 0.0


class ModalityArbiter:
    """300ms 合并窗口内把文本事件与图片事件合成一个 InputEnvelope。

    ``resolve()`` 在窗口未满时返回 None；调用方（守护进程）用定时器驱动。
    测试或 CLI 可传 ``force=True`` 立即结算。
    """

    def __init__(self, settings, *, store: AssetStore | None = None,
                 clock=time.time):
        self.settings = settings
        self.store = store or MemoryAssetStore(
            max_bytes=int(settings.image.get("max_bytes", 8 * 1024 * 1024)))
        self.clock = clock
        self._pending = _Pending()

    def _touch(self, now: float) -> None:
        if not self._pending.first_at:
            self._pending.first_at = now

    def push_text(self, text: str, *, source: str = "poll") -> None:
        self._touch(self.clock())
        self._pending.text = text
        self._pending.text_source = source

    def push_image(self, data: bytes, mime: str, *, source: str = "screenshot") -> None:
        self._touch(self.clock())
        self._pending.image = data
        self._pending.image_mime = mime
        self._pending.image_source = source

    @property
    def has_pending(self) -> bool:
        return self._pending.text is not None or self._pending.image is not None

    def resolve(self, *, now: float | None = None, force: bool = False) -> InputEnvelope | None:
        if not self.has_pending:
            return None
        now = now if now is not None else self.clock()
        window = int(self.settings.get("modality_window_ms", 300)) / 1000.0
        if not force and now - self._pending.first_at < window:
            return None

        p = self._pending
        self._pending = _Pending()
        conflict = str(self.settings.get("image_text_conflict", "image_only"))
        asset: AssetRef | None = None
        if p.image is not None and p.image_mime:
            asset = self.store.put(p.image, p.image_mime)

        if asset and p.text and conflict in ("mixed", "ask"):
            env = InputEnvelope.for_image(asset, source=p.image_source, text=p.text)
            if conflict == "ask":
                env = InputEnvelope(
                    envelope_id=env.envelope_id, modality="mixed", text=p.text,
                    assets=(asset,), source=p.image_source,
                    derived={"needs_modality_choice": True})
            return env
        if asset and (conflict in ("image_only", "mixed", "ask") or not p.text):
            return InputEnvelope.for_image(asset, source=p.image_source)
        if p.text and conflict == "text_only":
            return InputEnvelope.for_text(p.text, source=p.text_source)
        if asset:
            return InputEnvelope.for_image(asset, source=p.image_source)
        return InputEnvelope.for_text(p.text or "", source=p.text_source)
