"""0.2 配置加载、合并、校验与结构签名。

- 出厂数据来自 ``spec/*.json``；用户配置按深合并覆盖；
- 校验错误带 ``tab``，设置页保存失败时自动跳到对应页签；
- ``structure_signature()`` 覆盖所有会影响菜单/缓存的配置段。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .features import FEATURES
from .models import IntentOption, SearchProvider, ValidationError

DEFAULT_SPEC_DIR = Path(__file__).resolve().parents[2] / "spec"

LEVELS = {"BLOCK", "WARN", "CONFIRM", "ALLOW"}
FORMAT_IDS = {"static", "table", "interactive", "annotate"}
TRIGGER_POLICIES = {"auto", "confirm", "hotkey", "menu"}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Settings:
    def __init__(self, data: dict[str, Any]):
        self.data = dict(data)
        self.intents = tuple(IntentOption.from_dict(i)
                             for i in self.data.get("intents") or ())
        self.policy: dict[str, Any] = self.data.get("policy") or {"rules": {}}
        self.providers = tuple(SearchProvider.from_dict(p)
                               for p in self.data.get("search_providers") or ())
        self.domains: tuple[dict[str, Any], ...] = tuple(self.data.get("domains") or ())
        self.routes: dict[str, Any] = dict(self.data.get("routes") or {})
        self.thresholds: dict[str, Any] = dict(self.data.get("thresholds") or {})

    # -- 访问 --------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    @property
    def trigger_policy(self) -> str:
        return str(self.data.get("trigger_policy", "auto"))

    @property
    def disabled_route_action(self) -> str:
        return str(self.data.get("disabled_route_action", "block"))

    @property
    def menu(self) -> dict[str, Any]:
        return dict(self.data.get("menu") or {})

    @property
    def free_input(self) -> dict[str, Any]:
        return dict(self.data.get("free_input") or {})

    @property
    def feature_switches(self) -> dict[str, Any]:
        return dict(self.data.get("feature_switches") or {})

    @property
    def detection(self) -> dict[str, Any]:
        return dict(self.data.get("detection") or {})

    @property
    def formats(self) -> dict[str, Any]:
        return dict(self.data.get("formats") or {})

    @property
    def image(self) -> dict[str, Any]:
        return dict(self.data.get("image") or {})

    def intent(self, intent_id: str) -> IntentOption | None:
        found = next((i for i in self.intents if i.id == intent_id), None)
        if found is not None:
            return found
        # 「其他视角」独立于提示词模板：按 id 现场构造，保证菜单里点得动
        if intent_id.startswith("view."):
            vid = intent_id[len("view."):]
            for view in (self.get("other_views") or {}).get("items") or ():
                if isinstance(view, dict) and str(view.get("id") or "") == vid:
                    return IntentOption.from_other_view(view)
        return None

    def provider(self, provider_id: str) -> SearchProvider | None:
        return next((p for p in self.providers if p.id == provider_id), None)

    def enabled_route_ids(self) -> tuple[str, ...]:
        return tuple(sorted(rid for rid, pack in self.routes.items()
                            if pack.get("enabled", True)))

    def enabled_search_providers(self, contexts: tuple[str, ...] | list[str]) -> tuple[SearchProvider, ...]:
        wanted = set(contexts)
        out = [p for p in self.providers
               if p.enabled and ("*" in p.contexts or wanted.intersection(p.contexts))]
        return tuple(sorted(out, key=lambda p: p.order))

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)


def load_defaults(spec_dir: str | Path | None = None) -> Settings:
    """加载出厂配置（4 个 JSON 文件合成一个 Settings）。"""
    root = Path(spec_dir) if spec_dir else Path(
        os.environ.get("CLIPLLM02_SPEC_DIR", str(DEFAULT_SPEC_DIR)))
    data = _read_json(root / "builtin_settings.json")
    intents = _read_json(root / "builtin_intents.json")
    providers = _read_json(root / "builtin_search_providers.json")
    policy = _read_json(root / "builtin_policy.json")

    data["intents"] = intents.get("intents", [])
    data["search_providers"] = providers.get("providers", [])
    data["search_default_provider"] = providers.get(
        "default_provider", data.get("search_default_provider", "google"))
    data["search_show_inline_buttons"] = providers.get(
        "show_inline_buttons", data.get("search_show_inline_buttons", True))
    data["search_max_query_chars"] = providers.get(
        "max_query_chars", data.get("search_max_query_chars", 200))
    data["policy"] = policy
    # 提示词模板统一复用 gd_llm.py 抽出的 9 套完整 SYSTEM_PROMPT_*：
    # 出厂 routes 只存结构与字段，不再维护短版占位，避免核心层 / CLI 与
    # Studio 拿到不同版本的模板。用户对 route_prompts 的覆盖在
    # load_settings / studio.config 里后写入，优先级更高。
    try:
        legacy = _read_json(root / "legacy_assets.json")
    except (OSError, ValueError):
        legacy = {}
    for rid, pack in (legacy.get("packs") or {}).items():
        route = data.get("routes", {}).get(rid)
        if not route:
            continue
        if pack.get("prompt"):
            route["prompt"] = pack["prompt"]
        if pack.get("label"):
            route["label"] = pack["label"]
    return Settings(data)


def load_settings(override: dict[str, Any] | str | Path | None = None,
                  *, spec_dir: str | Path | None = None) -> Settings:
    base = load_defaults(spec_dir).as_dict()
    if override is None:
        return Settings(base)
    if isinstance(override, (str, Path)):
        override = _read_json(Path(override))
    return Settings(deep_merge(base, override))


def _condition_features(cond) -> list[str]:
    """收集条件树里引用的全部特征名，支持 all / any / not 嵌套。"""
    if not isinstance(cond, dict):
        return []
    out: list[str] = []
    for key in ("all", "any"):
        for sub in cond.get(key) or ():
            out.extend(_condition_features(sub))
    if isinstance(cond.get("not"), dict):
        out.extend(_condition_features(cond["not"]))
    name = cond.get("feature")
    if isinstance(name, str):
        out.append(name)
    return out


def _validate_policy(settings: Settings, defaults: Settings) -> list[ValidationError]:
    errors: list[ValidationError] = []
    rules = settings.policy.get("rules", {})
    locked = {rid: r.get("level") for rid, r in
              defaults.policy.get("rules", {}).items() if r.get("locked")}
    for rid, rule in rules.items():
        level = str(rule.get("level", "")).upper()
        if level not in LEVELS:
            errors.append(ValidationError(f"policy.{rid}.level",
                                          f"未知级别 {level!r}", "policy"))
        if rid in locked and level != locked[rid]:
            # BLOCK 不允许被下调
            if locked[rid] == "BLOCK" and level != "BLOCK":
                errors.append(ValidationError(f"policy.{rid}.level",
                                              "该项为硬性上限，不允许下调", "policy"))
        for key in ("feature",):
            name = rule.get(key)
            if name and name not in FEATURES and rule.get("detector") in ("feature", "threshold"):
                errors.append(ValidationError(f"policy.{rid}.{key}",
                                              f"未知特征 {name!r}", "policy"))
        for name in _condition_features(rule.get("applies_when")):
            if name not in FEATURES:
                errors.append(ValidationError(f"policy.{rid}.applies_when",
                                              f"未知特征 {name!r}", "policy"))
    return errors


def _validate_intents(settings: Settings) -> list[ValidationError]:
    errors: list[ValidationError] = []
    seen: set[str] = set()
    known_domains = {str(d["id"]) for d in settings.domains}
    max_tokens = int(settings.get("max_tokens", 8192))
    for intent in settings.intents:
        if intent.id in seen:
            errors.append(ValidationError(intent.id, "意图 ID 重复", "intents"))
        seen.add(intent.id)
        for dom in intent.domains:
            if dom != "*" and dom not in known_domains:
                errors.append(ValidationError(
                    intent.id, f"意图引用了未定义的领域 {dom!r}（会导致该意图永远进不了推荐区）",
                    "intents"))
        if intent.token_budget > max_tokens:
            errors.append(ValidationError(intent.id,
                                          "token_budget 超过模型 max_tokens", "intents"))
        # 路由「不存在」是真配置错误；路由「被禁用」是正常操作
        # （例如"关闭词组查询"），运行时由 disabled_route_action 置灰或回退，
        # 不应阻止用户在设置页保存配置。
        primary = intent.primary_route
        if primary and primary not in settings.routes:
            errors.append(ValidationError(intent.id,
                                          f"主路由 {primary} 不存在", "intents"))
        for inc in intent.includes:
            rid = inc.get("route_id")
            pack = settings.routes.get(rid)
            if not pack:
                errors.append(ValidationError(intent.id,
                                              f"引用路由 {rid} 不存在", "intents"))
                continue
            if rid == primary:
                errors.append(ValidationError(intent.id,
                                              "不能把主路由同时作为片段引用", "intents"))
            sections = pack.get("sections") or {}
            for section in inc.get("sections") or ():
                if section not in sections and section not in (pack.get("fields") or []):
                    errors.append(ValidationError(intent.id,
                                                  f"路由 {rid} 没有片段 {section}", "intents"))
        if intent.applicable:
            stack = [intent.applicable]
            while stack:
                cond = stack.pop()
                name = cond.get("feature")
                if name and name not in FEATURES:
                    errors.append(ValidationError(intent.id,
                                                  f"未知特征 {name!r}", "intents"))
                for key in ("all", "any"):
                    stack.extend(cond.get(key) or [])
                if cond.get("not"):
                    stack.append(cond["not"])
    return errors


def validate(settings: Settings, *, defaults: Settings | None = None) -> list[ValidationError]:
    defaults = defaults or load_defaults()
    errors: list[ValidationError] = []

    if settings.trigger_policy not in TRIGGER_POLICIES:
        errors.append(ValidationError("trigger_policy", "未知触发策略", "general"))
    if settings.disabled_route_action not in {"block", "fallback", "hide_from_menu"}:
        errors.append(ValidationError("disabled_route_action", "未知禁用行为", "general"))

    menu = settings.menu
    limit = int(menu.get("recommended_limit", 9))
    if not 1 <= limit <= 12:
        errors.append(ValidationError("menu.recommended_limit",
                                      "推荐项必须在 1–12 之间（前 9 项带数字快捷键）",
                                      "general"))
    if menu.get("show_disabled_items") not in {"grayed", "hidden", "hidden_in_fold"}:
        errors.append(ValidationError("menu.show_disabled_items", "未知显示方式", "general"))

    det = settings.detection
    for key in ("high", "mid", "gap", "mixed_min_score"):
        value = float(det.get(key, 0))
        if not 0 <= value <= 1:
            errors.append(ValidationError(f"detection.{key}", "阈值必须在 0–1 之间", "general"))

    if settings.get("image_text_conflict") not in {"image_only", "text_only", "mixed", "ask"}:
        errors.append(ValidationError("image_text_conflict", "未知冲突策略", "image"))
    if settings.trigger_policy == "menu" and settings.image.get("auto_recognize_in_menu"):
        errors.append(ValidationError("image.auto_recognize_in_menu",
                                      "菜单模式下禁止自动识别剪贴板图片", "image"))

    for p in settings.providers:
        if not p.enabled:
            continue                      # 未启用的搜索源不要求本机存在其浏览器
        template = p.url_template
        has_placeholder = "{q}" in template or "%s" in template
        if not has_placeholder or not template.startswith(("http://", "https://")):
            errors.append(ValidationError(
                f"search_providers.{p.id}",
                "URL 必须是 http(s) 且含 {q} 或 %s 占位符", "search"))
        if p.mode == "explicit_browser":
            if not p.executable or not shutil.which(p.executable):
                errors.append(ValidationError(f"search_providers.{p.id}",
                                              f"找不到浏览器 {p.executable!r}", "search"))
        if not set(p.contexts).issubset(set("*") | {d["id"] for d in settings.domains}
                                        | set(settings.routes) | {"text", "image", "mixed"}):
            errors.append(ValidationError(f"search_providers.{p.id}",
                                          "contexts 含未知上下文", "search"))
    if not settings.provider(str(settings.get("search_default_provider", ""))):
        errors.append(ValidationError("search_default_provider",
                                      "默认搜索源不存在", "search"))

    for fmt in settings.formats.get("enabled") or []:
        if fmt not in FORMAT_IDS:
            errors.append(ValidationError("formats.enabled",
                                          f"未知呈现形态 {fmt!r}", "general"))

    errors.extend(_validate_policy(settings, defaults))
    errors.extend(_validate_intents(settings))
    return errors


def structure_signature(settings: Settings) -> str:
    """任何影响菜单、拦截、提示词组合或缓存的配置变化都必须改变这个签名。"""
    relevant = {
        "trigger_policy": settings.trigger_policy,
        "disabled_route_action": settings.disabled_route_action,
        "menu": settings.menu,
        "free_input": settings.free_input,
        "feature_switches": settings.feature_switches,
        "detection": settings.detection,
        "policy": settings.policy,
        "formats": settings.formats,
        "thresholds": settings.thresholds,
        "domains": list(settings.domains),
        "routes": {k: {"enabled": v.get("enabled", True),
                       "query_type": v.get("query_type"),
                       "prompt": v.get("prompt")}
                   for k, v in settings.routes.items()},
        "intents": [i.as_dict() | {
            "enabled": i.enabled, "includes": [dict(x) for x in i.includes],
            "applicable": i.applicable, "modalities": list(i.modalities),
            "supported_formats": list(i.supported_formats),
            "token_budget": i.token_budget, "prompt_version": i.prompt_version,
        } for i in settings.intents],
        "search_providers": [p.as_dict() for p in settings.providers],
    }
    blob = json.dumps(relevant, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
