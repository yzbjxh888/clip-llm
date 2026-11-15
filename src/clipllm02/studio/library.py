"""把每次查询结果落盘成 Markdown，供 Obsidian / Logseq 等构建知识图谱。

目录结构与提示词模板同名：

    <库根>/LaTeX 公式/2026-11-14_温度梯度与热流密度.md
    <库根>/化学物质/2026-11-14_氯化钠.md

文件名由「日期 + 原始查询摘要 + 查询哈希」决定，不再取模型给的标题，
所以一次会话（首次查询 + 若干追问）始终落在同一篇：首次写入覆盖，
追问以「## 追问 N」追加。每篇带 YAML frontmatter，笔记软件可以按
`template` / `intent` / `query` / `session` 建索引与双链。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .json_render import _label

_FILENAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_WS = re.compile(r"\s+")
_DATE_FMT = "%Y-%m-%d"
_STAMP_FMT = "%Y-%m-%d %H:%M"


def library_root(settings) -> Path:
    """库根目录：设置里填了就用它，否则放 XDG 数据目录。"""
    custom = str(settings.get("desktop_library_dir") or "").strip()
    if custom:
        return Path(custom).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "clip-llm02" / "library"


def safe_name(text: str, limit: int = 40) -> str:
    """文件名清洗：去掉路径分隔符与 Windows 非法字符，折叠空白。"""
    cleaned = _FILENAME_BAD.sub(" ", text or "")
    cleaned = _WS.sub(" ", cleaned).strip().strip(".")
    return cleaned[:limit].strip() or "未命名"


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def to_markdown(data: Any, level: int = 2) -> str:
    """JSON → Markdown：dict 用小节，list[dict] 用表格，标量数组用列表。"""
    if isinstance(data, dict):
        blocks: list[str] = []
        for key, value in data.items():
            # query_type 是元数据；顶层 title 已作为 H1，正文不再重复
            if key == "query_type" or (level == 2 and key == "title"):
                continue
            head = _label(str(key))
            if isinstance(value, (dict, list)):
                inner = to_markdown(value, level + 1)
                if inner:
                    blocks.append(f"{'#' * min(level, 6)} {head}\n\n{inner}")
            else:
                text = _scalar(value).strip()
                if text:
                    blocks.append(f"**{head}**：{text}")
        return "\n\n".join(blocks)

    if isinstance(data, list):
        rows = [x for x in data if isinstance(x, dict)]
        if rows and len(rows) == len(data):
            keys: list[str] = []
            for row in rows:
                for key in row:
                    if key not in keys:
                        keys.append(key)
            if keys and len(keys) <= 8:
                head = "| " + " | ".join(_label(k) for k in keys) + " |"
                sep = "|" + "---|" * len(keys)
                body = []
                for row in rows:
                    cells = [_scalar(row.get(k)).replace("|", "\\|")
                             .replace("\n", " ") for k in keys]
                    body.append("| " + " | ".join(cells) + " |")
                return "\n".join([head, sep] + body)
        lines: list[str] = []
        for item in data:
            if isinstance(item, (dict, list)):
                inner = to_markdown(item, level + 1)
                if inner:
                    lines.append(inner)
            else:
                text = _scalar(item).strip()
                if text:
                    lines.append(f"- {text}")
        return "\n".join(lines)

    return _scalar(data).strip()


def _frontmatter(fields: dict[str, str]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        text = str(value or "").replace("\n", " ").replace('"', "'")
        lines.append(f'{key}: "{text}"')
    lines.append("---")
    return "\n".join(lines)


def _parse_json(text: str) -> Any:
    """容忍模型在 JSON 前后加解释，取出第一个可解析对象。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except (ValueError, TypeError):
                continue
    return None


def _note_path(settings, *, query: str, route_label: str,
               query_type: str) -> Path:
    """同一天 + 同一段原始输入 → 同一篇笔记。

    文件名不再取模型给的 title：追问每轮标题都可能不同，会把一次会话拆
    成多篇，双链也会随模型改口而断。用查询摘要加短哈希，链接稳定。
    """
    folder = safe_name(route_label or query_type or "通用", 60)
    first = (query or "").strip().splitlines()
    stem = safe_name(first[0] if first else "", 24)
    digest = hashlib.sha256((query or "").encode("utf-8")).hexdigest()[:8]
    name = f"{datetime.now().strftime(_DATE_FMT)}_{stem}_{digest}.md"
    return library_root(settings) / folder / name


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """拆出已有笔记的 frontmatter 与正文，供追问时原地追加。"""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip('"')
    return meta, text[end + 4:].lstrip("\n")


def save_entry(settings, *, query: str, query_type: str = "",
               route_label: str = "", intent_label: str = "",
               followup: str = "", raw: str = "",
               is_followup: bool = False,
               session_id: str = "") -> Path | None:
    """一次会话写进一篇 Markdown：首次覆盖，追问追加，不另开文件。

    ``session_id`` 不同说明这是另一次查询，按首次写入处理，避免两次会话
    的追问串到同一篇里。未启用或写入失败返回 None。
    """
    if not bool(settings.get("desktop_library_enabled", True)):
        return None
    data = _parse_json(raw)

    title = ""
    if isinstance(data, dict):
        title = _scalar(data.get("title")).strip()
    if not title:
        first = (query or "").strip().splitlines()
        title = (first[0] if first else "")[:40].strip()

    path = _note_path(settings, query=query, route_label=route_label,
                      query_type=query_type)
    body = to_markdown(data) if data is not None else (raw or "").strip()
    now = datetime.now().strftime(_STAMP_FMT)
    created = now
    rounds = 1
    existing = ""
    same_session = True

    if is_followup and path.exists():
        try:
            old = path.read_text(encoding="utf-8")
        except OSError:
            old = ""
        meta, existing = _split_frontmatter(old)
        old_session = meta.get("session") or ""
        same_session = (not session_id or not old_session
                        or old_session == session_id)
        if same_session:
            created = meta.get("created") or now
            try:
                rounds = max(1, int(meta.get("rounds") or 1)) + 1
            except ValueError:
                rounds = 2

    def frontmatter() -> str:
        return _frontmatter({
            "query": (query or "").replace("\n", " ")[:300],
            "query_type": query_type,
            "template": route_label,
            "intent": intent_label,
            "session": session_id,
            "rounds": str(rounds),
            "created": created,
            "updated": now,
            "followup": (followup or "")[:300] if is_followup else "",
        })

    parts = [frontmatter()]
    if same_session and existing:
        parts.append(existing.rstrip())
        parts.append(f"## 追问 {rounds - 1}：{followup or '继续追问'}")
    else:
        parts.append(f"# {title}")
        if is_followup and followup:
            parts.append(f"> 追问：{followup}")
    if body:
        parts.append(body)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n\n".join(p for p in parts if p) + "\n",
                        encoding="utf-8")
    except OSError:
        return None
    return path
