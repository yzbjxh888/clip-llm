"""可选的真实模型调用与结果渲染。

设计要点：
- **没有配置 API Key 时绝不发请求**，只把本地组装好的 prompt 渲染成预览卡片；
- 有 Key 时走 OpenAI 兼容 /chat/completions，支持超时与取消；
- 模型返回任意内容都能渲染成完整卡片（纯文本 / JSON / 代码围栏 / 空响应）；
- 不写日志、不打印原文与 Key。
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_FENCE_RE = re.compile(r"^```(?:json|latex|tex|markdown)?\s*|\s*```$", re.I)


@dataclass(frozen=True)
class GatewayConfig:
    api_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: int = 60
    max_tokens: int = 2048
    temperature: float = 0.2

    @property
    def enabled(self) -> bool:
        return bool(self.api_url.strip() and self.api_key.strip()
                    and self.model.strip())

    @classmethod
    def from_settings(cls, settings) -> "GatewayConfig":
        return cls(
            api_url=str(settings.get("desktop_api_url", "")),
            api_key=str(settings.get("desktop_api_key", "")),
            model=str(settings.get("desktop_model", "")),
            timeout=int(settings.get("desktop_timeout", 60)),
            max_tokens=int(settings.get("desktop_max_tokens", 2048)),
        )


@dataclass(frozen=True)
class GatewayResult:
    html: str
    ok: bool
    error: str = ""
    raw: str = ""
    preview_only: bool = False


def clean_output(text: str) -> str:
    trimmed = (text or "").strip()
    match = re.match(r"^```(?:json|latex|tex|markdown)?[ \t]*\r?\n([\s\S]*?)\r?\n?```$",
                     trimmed, re.I)
    return (match.group(1) if match else trimmed).strip()


def render_response(text: str) -> str:
    """把模型返回的任意内容渲染成 QTextDocument 兼容 HTML。"""
    cleaned = clean_output(text)
    if not cleaned:
        return "<p><i>（模型未返回内容）</i></p>"
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return f"<pre style='white-space:pre-wrap'>{html.escape(cleaned)}</pre>"
    return render_json(data)


def render_json(data) -> str:
    if not isinstance(data, dict):
        return f"<pre style='white-space:pre-wrap'>{html.escape(json.dumps(data, ensure_ascii=False, indent=2))}</pre>"
    out: list[str] = []
    title = data.get("title") or data.get("query_type") or "解释结果"
    out.append(f"<h3>{html.escape(str(title))}</h3>")
    for key, value in data.items():
        if key in {"title", "query_type"}:
            continue
        out.append(f"<p><b>{html.escape(str(key))}</b></p>")
        out.append(_render_value(value))
    return "\n".join(out)


def _render_value(value) -> str:
    if isinstance(value, str):
        return f"<p>{html.escape(value)}</p>"
    if isinstance(value, list):
        items = []
        for item in value:
            items.append(f"<li>{html.escape(json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item))}</li>")
        return "<ul>" + "".join(items) + "</ul>" if items else "<p>—</p>"
    if isinstance(value, dict):
        rows = [f"<li><b>{html.escape(str(k))}</b>：{html.escape(str(v))}</li>"
                for k, v in value.items()]
        return "<ul>" + "".join(rows) + "</ul>"
    return f"<p>{html.escape(str(value))}</p>"


def build_preview_html(composed, content: str, *, reason: str = "未配置 API Key") -> str:
    """无 Key 时的本地预览：展示组装结果，明确说明没有发送网络请求。"""
    fields = "、".join(composed.fields[:12]) or "title、answer"
    return "\n".join([
        "<h3>本地请求预览</h3>",
        f"<p><b>状态</b>：{html.escape(reason)}，<b>未发送任何网络请求</b>。</p>",
        f"<p><b>query_type</b>：{html.escape(composed.query_type)}</p>",
        f"<p><b>输入</b>：{html.escape((content or '')[:120])}</p>",
        f"<p><b>输出字段</b>：{html.escape(fields)}</p>",
        f"<p><b>提示词长度</b>：{len(composed.prompt)} 字符 · "
        f"hash {html.escape(composed.prompt_hash[:16])}…</p>",
        "<p><b>提示词（前 600 字）</b></p>",
        f"<pre style='white-space:pre-wrap'>{html.escape(composed.prompt[:600])}</pre>",
        "<p><i>在「设置 → 模型」填入 API URL / Key / 模型名后，"
        "选择意图即可获得真实解释卡片。</i></p>",
    ])


class Gateway:
    def __init__(self, config: GatewayConfig):
        self.config = config

    def preview(self, composed, content: str) -> GatewayResult:
        return GatewayResult(
            html=build_preview_html(composed, content),
            ok=True, preview_only=True,
        )

    def explain(self, prompt: str, content: str, *,
                history=None,
                image: tuple[bytes, str] | None = None,
                cancel: threading.Event | None = None) -> GatewayResult:
        """``history`` 是追问时的多轮上下文：``[{role, content}, ...]``。

        追问沿用上一轮的 system（输出契约已换成自由作答规则），并把
        「原问题 / 上一轮回答」作为历史消息发出去，否则就退化成一次全新的
        单轮查询。上下文靠 history，不靠 system。
        """
        if not self.config.enabled:
            return GatewayResult(html="", ok=False, error="未配置模型")
        messages: list[dict[str, str]] = [{"role": "system", "content": prompt}]
        for item in history or ():
            role = str(item.get("role") or "")
            text = item.get("content")
            if role in ("user", "assistant") and isinstance(text, str) and text:
                messages.append({"role": role, "content": text})
        user_content: object = content
        if image is not None:
            # 追问带原图：文本模型若是多模态（如 gemini-flash-lite），
            # 它能重新看图，不必依赖上一轮那段可能不准的答案。
            data, mime = image
            user_content = [
                {"type": "text", "text": content},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,"
                                      f"{base64.b64encode(data).decode('ascii')}"}},
            ]
        messages.append({"role": "user", "content": user_content})
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }
        if cancel is not None and cancel.is_set():
            return GatewayResult(html="", ok=False, error="已取消")
        try:
            request = urllib.request.Request(self.config.api_url, data=body,
                                             headers=headers)
            with urllib.request.urlopen(request, timeout=self.config.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            text = ""
            try:
                text = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                text = data.get("text") or data.get("content") or ""
            if isinstance(text, list):
                text = "".join(part.get("text", "") for part in text
                               if isinstance(part, dict))
            cleaned = clean_output(str(text))
            if not cleaned:
                return GatewayResult(html=render_response(""), ok=False,
                                     error="模型返回内容为空")
            return GatewayResult(html=render_response(cleaned), ok=True, raw=cleaned)
        except urllib.error.HTTPError as exc:
            if image is not None and exc.code in (400, 404, 415, 422):
                # 文本模型不支持图片输入：去掉图重发一次，别让追问直接失败
                return self.explain(prompt, content, history=history,
                                    cancel=cancel)
            return GatewayResult(html="", ok=False, error=f"HTTP {exc.code} {exc.reason}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return GatewayResult(html="", ok=False, error=f"网络请求失败：{exc}")
        except (ValueError, TypeError) as exc:
            return GatewayResult(html="", ok=False, error=f"响应解析失败：{exc}")


class ResultCache:
    """轻量磁盘结果缓存；不存 API Key。

    调用方决定存渲染后的 HTML 还是模型原始结构化文本。升级渲染器时应通过
    缓存键里的 ``schema_version`` 让旧条目自动失效，避免把旧格式当新格式读。
    键由 ``ComposedPrompt.cache_key()`` 提供，涵盖 prompt / 意图 / 呈现 /
    自定义指令 / 规则集版本 / 输入摘要，改任何一项都会自动失效。
    """

    def __init__(self, path: str | Path | None = None, *, max_entries: int = 500):
        if path is None:
            root = Path(os.environ.get("XDG_CACHE_HOME",
                                       str(Path.home() / ".cache")))
            path = root / "clip-llm02" / "results.json"
        self.path = Path(path)
        self.max_entries = max_entries
        self._data: dict[str, str] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {str(k): str(v) for k, v in raw.items()}
        except (OSError, ValueError):
            pass

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def put(self, key: str, html_text: str) -> None:
        if not html_text:
            return
        self._data[key] = html_text
        if len(self._data) > self.max_entries:
            for old_key in list(self._data)[:len(self._data) - self.max_entries]:
                self._data.pop(old_key, None)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass
