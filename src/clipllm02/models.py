"""clip-llm 0.2 核心数据对象。

设计原则（见 docs/05_ARCHITECTURE.md）：
- 跨层对象一律不可变 dataclass；
- 不照抄 0.1 的 Decision / ViewModel / RawResponse，它们仍由 0.1 负责；
- InputEnvelope 保留原始输入与图片资产，派生信息（OCR 等）单独放 derived；
- UserSelection 是用户意图的唯一权威来源，不含剪贴板原文。
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 输入
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AssetRef:
    asset_id: str
    mime: str
    sha256: str
    local_path: str
    width: int = 0
    height: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id, "mime": self.mime,
            "sha256": self.sha256, "local_path": self.local_path,
            "width": self.width, "height": self.height,
        }


@dataclass(frozen=True)
class InputEnvelope:
    envelope_id: str
    modality: str                       # text | image | mixed
    text: str | None
    assets: tuple[AssetRef, ...] = ()
    source: str = "manual"              # 枚举，绝不携带原文
    captured_at: float = field(default_factory=time.time)
    derived: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def for_text(cls, text: str, *, source: str = "manual") -> "InputEnvelope":
        return cls(envelope_id=new_id("env"), modality="text",
                   text=text or "", assets=(), source=source)

    @classmethod
    def for_image(cls, asset: AssetRef, *, source: str = "screenshot",
                  text: str | None = None) -> "InputEnvelope":
        return cls(envelope_id=new_id("env"),
                   modality="mixed" if text else "image",
                   text=text, assets=(asset,), source=source)

    @property
    def text_hash(self) -> str:
        return sha256_text(self.text or "")

    @property
    def query_hash(self) -> str:
        """缓存用的内容摘要：图片用 asset sha256，文本用正文 hash。"""
        if self.assets:
            return "|".join(a.sha256 for a in self.assets)
        return self.text_hash

    def as_dict(self) -> dict[str, Any]:
        return {
            "envelope_id": self.envelope_id, "modality": self.modality,
            "text_len": len(self.text or ""), "source": self.source,
            "assets": [a.as_dict() for a in self.assets],
            "text_hash": self.text_hash,
        }


# --------------------------------------------------------------------------
# 特征与领域
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FeatureSet:
    text: str
    norm: str
    values: dict[str, Any]
    thresholds: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.values)


@dataclass(frozen=True)
class DomainCandidate:
    id: str
    label: str
    score: float
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label,
                "score": round(self.score, 4), "evidence": list(self.evidence)}


@dataclass(frozen=True)
class Category:
    """一个提示词模板类别；清单由 settings.routes 动态提供。"""
    id: str
    label: str
    enabled: bool = True
    evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label,
                "enabled": self.enabled, "evidence": self.evidence}


@dataclass(frozen=True)
class Classification:
    """类别初判结果：最可能的类别 + 其他可选类别 + 可解释轨迹。"""
    category: str
    label: str
    reason: str = ""
    candidates: tuple[Category, ...] = ()
    trace: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"category": self.category, "label": self.label,
                "reason": self.reason,
                "candidates": [c.as_dict() for c in self.candidates]}


# --------------------------------------------------------------------------
# 策略
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PolicyHit:
    reason: str
    level: str
    description: str


@dataclass(frozen=True)
class PolicyResult:
    level: str                          # BLOCK | WARN | CONFIRM | ALLOW
    hits: tuple[PolicyHit, ...] = ()
    skipped: bool = False               # 重复内容：直接跳过，不弹窗不请求

    @property
    def allowed(self) -> bool:
        return self.level != "BLOCK" and not self.skipped

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(h.description for h in self.hits if h.level == "WARN")

    @property
    def confirmations(self) -> tuple[str, ...]:
        return tuple(h.description for h in self.hits if h.level == "CONFIRM")

    @property
    def block_reasons(self) -> tuple[str, ...]:
        return tuple(h.reason for h in self.hits if h.level == "BLOCK")

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level, "allowed": self.allowed,
            "hits": [{"reason": h.reason, "level": h.level,
                      "description": h.description} for h in self.hits],
        }


# --------------------------------------------------------------------------
# 建议与菜单
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Suggestion:
    envelope_id: str
    modality: str
    features: FeatureSet
    domain_candidates: tuple[DomainCandidate, ...]
    level: str                          # HIGH | MID | LOW | MIXED
    warnings: tuple[str, ...] = ()
    suggested_route: str | None = None
    suggested_intent: str | None = None   # 分类器自动判定，只作默认项不替用户提交
    classification: "Classification | None" = None

    @property
    def top_domain(self) -> str | None:
        return self.domain_candidates[0].id if self.domain_candidates else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "envelope_id": self.envelope_id,
            "modality": self.modality,
            "level": self.level,
            "domains": [d.as_dict() for d in self.domain_candidates],
            "warnings": list(self.warnings),
            "suggested_route": self.suggested_route,
            "suggested_intent": self.suggested_intent,
            "classification": (self.classification.as_dict()
                               if self.classification else None),
        }


@dataclass(frozen=True)
class IntentOption:
    id: str
    label: str
    hint: str = ""
    domains: tuple[str, ...] = ()
    modalities: tuple[str, ...] = ("text",)
    applicable: dict[str, Any] | None = None
    primary_route: str | None = None
    prompt_override: str = ""
    # 图片方式识别完之后怎么继续："" 直接显示识别结果；"auto" 按文本类别
    # 自动选模板；其他值是文本模板 id（例如 "latex"）
    followup_route: str = ""
    includes: tuple[dict[str, Any], ...] = ()
    default_format: str = "static"
    supported_formats: tuple[str, ...] = ("static",)
    order: int = 100
    enabled: bool = True
    free_input_only: bool = False
    conflict_policy: str = "primary_wins"
    token_budget: int = 1600
    prompt_version: str = "1.0.0"
    schema_version: str = "1.0.0"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IntentOption":
        return cls(
            id=str(data["id"]), label=str(data["label"]),
            hint=str(data.get("hint", "")),
            domains=tuple(data.get("domains") or ()),
            modalities=tuple(data.get("modalities") or ("text",)),
            applicable=data.get("applicable"),
            primary_route=data.get("primary_route"),
            prompt_override=str(data.get("prompt_override") or ""),
            followup_route=str(data.get("followup_route") or ""),
            includes=tuple(data.get("includes") or ()),
            default_format=str(data.get("default_format", "static")),
            supported_formats=tuple(data.get("supported_formats") or ("static",)),
            order=int(data.get("order", 100)),
            enabled=bool(data.get("enabled", True)),
            free_input_only=bool(data.get("free_input_only", False)),
            conflict_policy=str(data.get("conflict_policy", "primary_wins")),
            token_budget=int(data.get("token_budget", 1600)),
            prompt_version=str(data.get("prompt_version", "1.0.0")),
            schema_version=str(data.get("schema_version", "1.0.0")),
        )

    @classmethod
    def from_other_view(cls, view: dict[str, Any]) -> "IntentOption | None":
        """「其他视角」条目 → 可点击意图。

        与提示词模板无关：它是一条自带提示词的通用解读角度
        （历史背景、发展过程、其他解读方式等），可自由增删。
        """
        vid = str(view.get("id") or "").strip()
        if not vid:
            return None
        return cls(
            id=f"view.{vid}",
            label=str(view.get("label") or vid),
            hint=str(view.get("hint") or ""),
            domains=tuple(view.get("domains") or ("*",)),
            primary_route=str(view.get("primary_route") or "general"),
            prompt_override=str(view.get("prompt") or ""),
            order=int(view.get("order", 300)),
            enabled=bool(view.get("enabled", True)),
            default_format="static",
            supported_formats=("static",),
            token_budget=int(view.get("token_budget", 1200)),
        )

    def supports_modality(self, modality: str) -> bool:
        return "*" in self.modalities or modality in self.modalities

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "hint": self.hint,
            "domains": list(self.domains), "order": self.order,
            "default_format": self.default_format,
            "supported_formats": list(self.supported_formats),
            "primary_route": self.primary_route,
            "prompt_override": self.prompt_override,
            "followup_route": self.followup_route,
        }


def intent_prompt_source(item: dict[str, Any]) -> str:
    """查询方式的提示词来源：留空即复用主路由模板，填了就是专属提示词。

    与 ``prompts.compose_prompt`` / ``studio.vision.intent_vision_prompt``
    的优先级（override > 路由模板）严格一致。不看 ``use_legacy_template``：
    那个标记只说明提示词来自旧脚本的 9 套，不能拿来判断当前到底复用了模板
    还是用了专属提示词——否则界面上会写着「复用模板」，实际发的却是专属内容。
    """
    if item.get("free_input_only"):
        return "自由输入"
    if item.get("custom"):
        return "自定义"
    return ("专属提示词" if str(item.get("prompt_override") or "").strip()
            else "复用模板")


@dataclass(frozen=True)
class MenuOption:
    intent: IntentOption
    enabled: bool = True
    disabled_reason: str = ""
    section: str = "recommended"       # recommended | other | domain_picker

    def as_dict(self) -> dict[str, Any]:
        d = self.intent.as_dict()
        d.update({"enabled": self.enabled,
                  "disabled_reason": self.disabled_reason,
                  "section": self.section})
        return d


@dataclass(frozen=True)
class SearchProvider:
    id: str
    label: str
    url_template: str
    mode: str = "default_browser"
    executable: str = ""
    args: tuple[str, ...] = ()
    enabled: bool = True
    order: int = 100
    contexts: tuple[str, ...] = ("*",)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchProvider":
        return cls(
            id=str(data["id"]), label=str(data["label"]),
            url_template=str(data["url_template"]),
            mode=str(data.get("mode", "default_browser")),
            executable=str(data.get("executable", "")),
            args=tuple(data.get("args") or ()),
            enabled=bool(data.get("enabled", True)),
            order=int(data.get("order", 100)),
            contexts=tuple(data.get("contexts") or ("*",)),
        )

    def build_url(self, query: str, *, max_chars: int = 200) -> str:
        """把查询词填进网址；``{q}`` 与常见的 ``%s`` 两种写法都认。

        网址里写死的 %3A / %2F 等编码不受影响，只有占位符会被替换。
        """
        from urllib.parse import quote
        q = quote((query or "")[:max_chars], safe="")
        template = self.url_template
        if "{q}" in template:
            return template.replace("{q}", q)
        return template.replace("%s", q)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label,
                "url_template": self.url_template, "mode": self.mode,
                "enabled": self.enabled, "order": self.order,
                "contexts": list(self.contexts)}


@dataclass(frozen=True)
class MenuModel:
    envelope_id: str
    level: str
    domain_chips: tuple[DomainCandidate, ...]
    recommended: tuple[MenuOption, ...]
    others: tuple[MenuOption, ...]
    grayed: tuple[MenuOption, ...]
    default_intent: str | None
    formats: tuple[str, ...]
    free_input: dict[str, Any]
    tools: tuple[SearchProvider, ...]
    warnings: tuple[str, ...] = ()
    category: str | None = None              # 初判最可能的类别
    category_label: str | None = None
    category_chips: tuple[Category, ...] = ()   # 判定项 + 其他可选类别

    def as_dict(self) -> dict[str, Any]:
        return {
            "envelope_id": self.envelope_id,
            "level": self.level,
            "category": self.category,
            "category_label": self.category_label,
            "category_chips": [c.as_dict() for c in self.category_chips],
            "domain_chips": [d.as_dict() for d in self.domain_chips],
            "recommended": [o.as_dict() for o in self.recommended],
            "others": [o.as_dict() for o in self.others],
            "grayed": [o.as_dict() for o in self.grayed],
            "default_intent": self.default_intent,
            "formats": list(self.formats),
            "free_input": self.free_input,
            "tools": [p.as_dict() for p in self.tools],
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------
# 用户选择
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class UserSelection:
    request_id: str
    envelope_id: str
    domain_id: str
    intent_id: str | None                # None = 自由输入
    format_id: str
    custom_instruction: str | None = None
    free_input_mode: str = "instruction"
    style_ref: str | None = None
    allowed_route_ids: tuple[str, ...] = ()   # 多选预留，V1 只放一个

    @property
    def is_freeform(self) -> bool:
        return self.intent_id is None or bool(self.custom_instruction)

    def cache_salt(self) -> str:
        """只进缓存 hash，不进日志。"""
        parts = [self.domain_id, self.intent_id or "free",
                 self.format_id, self.custom_instruction or "",
                 self.free_input_mode, self.style_ref or "",
                 "|".join(self.allowed_route_ids)]
        return sha256_text("\0".join(parts))

    def as_dict(self) -> dict[str, Any]:
        """可复制的路由配置：不含原文、不含 Key、不含自定义指令全文。"""
        return {
            "request_id": self.request_id,
            "envelope_id": self.envelope_id,
            "domain_id": self.domain_id,
            "intent_id": self.intent_id,
            "format_id": self.format_id,
            "free_input_mode": self.free_input_mode,
            "has_custom_instruction": bool(self.custom_instruction),
            "style_ref": self.style_ref,
            "allowed_route_ids": list(self.allowed_route_ids),
        }


@dataclass(frozen=True)
class ValidationError:
    field: str
    message: str
    tab: str = "general"

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "message": self.message, "tab": self.tab}


class Preferences:
    """偏好只影响排序与默认高亮，绝不覆盖本次明确选择。"""

    def __init__(self, *, last: dict[str, str] | None = None,
                 freq: dict[tuple[str, str], int] | None = None):
        self.last = dict(last or {})
        self.freq = dict(freq or {})

    def last_intent(self, text_hash: str) -> str | None:
        return self.last.get(text_hash)

    def frequency(self, intent_id: str, domain: str | None) -> int:
        return self.freq.get((intent_id, domain or ""), 0)

    def record(self, text_hash: str, intent_id: str, domain: str) -> None:
        self.last[text_hash] = intent_id
        key = (intent_id, domain)
        self.freq[key] = self.freq.get(key, 0) + 1
