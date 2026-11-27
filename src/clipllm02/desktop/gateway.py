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


def _decision_section(settings) -> dict:
    """Settings 与 dict 都能被 SystemOne 路径读取（测试与运行期同构）。"""
    if settings is None:
        return {}
    return dict(settings.get("decision_model_config") or {})


def decision_model_api_url(settings=None) -> str:
    return str(_decision_section(settings).get("decision_model_api_url") or "")


def decision_model_name(settings=None) -> str:
    return str(_decision_section(settings).get("decision_model_name") or "")


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
        main = dict(getattr(settings, "main_model_config", None) or {})
        return cls(
            api_url=str(main.get("main_model_api_url")
                        or settings.get("desktop_api_url", "")),
            api_key=str(main.get("main_model_key")
                        or settings.get("desktop_api_key", "")),
            model=str(main.get("main_model_name")
                      or settings.get("desktop_model", "")),
            timeout=int(settings.get("desktop_timeout", 60)),
            max_tokens=int(settings.get("desktop_max_tokens", 2048)),
        )


@dataclass(frozen=True)
class DecisionConfig:
    """决策模型配置：URL/model 可配置，Key 永远只来自 AGNES_API_KEY。"""
    api_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: int = 15
    max_tokens: int = 128
    temperature: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.api_url.strip() and self.api_key.strip()
                    and self.model.strip())

    @classmethod
    def from_settings(cls, settings) -> "DecisionConfig":
        cfg = dict(settings.decision_model_config or {})
        return cls(
            api_url=str(cfg.get("decision_model_api_url")
                        or cfg.get("main_model_api_url") or ""),
            api_key=str(cfg.get("main_model_key") or ""),
            model=str(cfg.get("decision_model_name")
                      or cfg.get("main_model_name") or ""),
            timeout=int(cfg.get("timeout") or 15),
            max_tokens=int(cfg.get("max_tokens") or 128),
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


# ADR-009 / FR-022：决策模型 = Agnes SystemOne，互斥单选（choice + argmax）。
SYSTEMONE_API_URL = "https://apihub.agnes-ai.com/v1/systemone"
SYSTEMONE_MODEL = "agnes-sys1-1.0"
SYSTEMONE_CATCH_ALL_ID = "通用解析"
SYSTEMONE_CATCH_ALL_DESCRIPTION = "其他无法归类的内容（兜底）"
SYSTEMONE_INSTRUCTIONS = "从候选类别中选择与输入最匹配的一个；无法确定时选 通用解析。"
IMAGE_ROUTE_ID = "图片识别"


def _env_config(name: str, default: str = "") -> str:
    """参考 agnes-sys1 MCP 的 _config()：环境变量优先，其次 ~/.env。"""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    env_path = Path.home() / ".env"
    if not env_path.is_file():
        return default
    pattern = re.compile(r"^\s*" + re.escape(name) + r"\s*=\s*(.*?)\s*$")
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return default
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value.strip() or default
    return default


def agnes_api_key() -> str:
    """Key 只从 AGNES_API_KEY 读取，绝不取工程配置、绝不写日志。"""
    return _env_config("AGNES_API_KEY")


def systemone_categories(decision_model_categories) -> dict[str, str]:
    """ADR-010：候选类别直接来自 B（{类别id: 判别描述}），不再从路由清单派生。

    保留 通用解析 catch-all；不修改传入的 B。这是 SystemOne 请求 criteria 的
    唯一来源，判别描述由用户在「拦截」页编辑。
    """
    categories: dict[str, str] = {}
    for key, value in (decision_model_categories or {}).items():
        rid = str(key)
        if not rid or rid == IMAGE_ROUTE_ID:
            continue
        categories[rid] = str(value) if value else rid
    categories.setdefault(SYSTEMONE_CATCH_ALL_ID, SYSTEMONE_CATCH_ALL_DESCRIPTION)
    return categories


def build_systemone_request(state: str, categories, *, instructions=None,
                            model=None, settings=None) -> dict:
    """SystemOne 的 choice 原语：一个 category 问题，criteria = 类别清单。

    instructions（判别指令）优先取显式参数，其次取用户配置
    ``decision_model_instructions``，均缺省时回退内置常量。criteria 只来自 B。
    """
    configured = ""
    if settings is not None:
        configured = str(settings.get("decision_model_instructions") or "").strip()
    return {
        "state": state,
        "questions": {"category": {
            "type": "choice",
            "instructions": instructions or configured or SYSTEMONE_INSTRUCTIONS,
            "criteria": dict(categories),
        }},
        "model": model or decision_model_name(settings) or SYSTEMONE_MODEL,
    }


def decode_systemone_classify(data) -> str | None:
    """choice + argmax：概率表存在时取概率最大者，否则回退 choice。"""
    if not isinstance(data, dict):
        return None
    answers = data.get("answers")
    if not isinstance(answers, dict):
        return None
    answer = answers.get("category")
    if not isinstance(answer, dict):
        return None
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, dict) and probabilities:
        try:
            return str(max(probabilities, key=lambda k: probabilities[k]))
        except (TypeError, ValueError):
            return None
    choice = answer.get("choice")
    return str(choice) if choice else None


def classify_with_systemone(state: str, categories, *, instructions=None,
                            api_url=None, model=None, timeout=15,
                            cancel=None, settings=None) -> str | None:
    """唯一的 SystemOne 联网实现；失败返回 None，不隐式回退本地分类器。"""
    key = agnes_api_key()
    if not key:
        return None
    if cancel is not None and cancel.is_set():
        return None
    payload = build_systemone_request(state, categories,
                                      instructions=instructions, model=model,
                                      settings=settings)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        api_url or decision_model_api_url(settings) or SYSTEMONE_API_URL,
        data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            OSError, ValueError, TypeError):
        return None
    return decode_systemone_classify(data)


def request_route(settings, *, state: str, route_registry, cancel=None) -> str:
    """短段 state → SystemOne classify → 命中 B 类别清单的 JSON 字符串回复。

    ADR-010 反转 ADR-009：categories/criteria 来自「拦截」页的 B，而非路由清单。
    ``route_registry`` 仅为兼容既有调用方保留，不再作为候选类别来源；返回的 id
    必须命中 B 才被接受。无 Key / 请求失败时返回空串，绝不隐式回退本地分类器。
    """
    b = settings.get("decision_model_categories") or {}
    config = DecisionConfig.from_settings(settings)
    categories = systemone_categories(b)
    selected = classify_with_systemone(
        state, categories,
        api_url=config.api_url or None, model=config.model or None,
        settings=settings, cancel=cancel)
    if not selected or selected not in categories:
        return ""
    return json.dumps({"selected_route_id": selected}, ensure_ascii=False)


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
