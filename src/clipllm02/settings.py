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

LEVELS = {"BLOCK", "ALLOW"}
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
    merged = migrate_policy(deep_merge(base, override))
    merged = migrate_intents(merged)
    merged = migrate_vision_routes(merged, base)
    merged = migrate_vision_prompts(merged, base)
    return Settings(merged)


# 0.2.7：图片侧只有一条视觉链路，7 条查询方式是过度拆分。公式解释 / 翻译 /
# 图表趋势并入「理解这张图」，这三条别名意图不再存在。
DEPRECATED_INTENT_IDS = {"image.formula", "image.translate", "image.chart"}

# 图片侧原先按输出契约拆成 4 条路由（提示词挂在路由上），导致「提示词模板」
# 里出现 4 个图片条目，「查询方式」按路由分组也碎成 4 组。现合并为一条
# `image`（图片识别），提示词下放到每条查询方式的 prompt_override：
# 一条查询方式 = 一条明确的输出契约，模板只留一条兜底。
OLD_VISION_ROUTES = {
    "image_ocr": "image.ocr",
    "image_table": "image.table",
    "image_explain": "image.explain",
    "image_annotate": "image.annotate",
}

# 旧出厂文案：用户没改过就升级成合并后的契约，改过的原样保留。
# 拆成 4 条路由时 order 只在各自组内有效，合并后必须重排一次才有序。
VISION_INTENT_ORDER = {"image.ocr": 10, "image.table": 20,
                       "image.explain": 30, "image.annotate": 40}

OLD_VISION_ROUTE_PROMPTS = {
    "image_ocr": "请识别图片中的文字和数学公式。公式用 LaTeX 表示，行内公式用 "
                 "$...$ 包裹，独立公式用 $$...$$ 包裹并单独占一行。保留原有"
                 "阅读顺序，只返回识别结果，不要添加额外解释。",
    "image_table": "请提取图片中的表格与数据，还原行列结构与数值。"
                   "严格按 JSON 输出，不要添加解释。",
    "image_explain": "请理解并解释这张图片的内容。若是机制图、统计图或结构图，"
                     "说明组成部分、相互关系与它想表达的结论。严格按 JSON 输出。",
    "image_annotate": "请找出图片中的关键内容并返回标注框。严格按 JSON 输出 "
                      "boxes 数组，每项含 x、y、w、h、label；"
                      "坐标使用 0 到 1 的归一化值。",
}


def migrate_intents(data: dict[str, Any]) -> dict[str, Any]:
    """去掉已合并的图片意图；幂等，可反复调用。"""
    intents = data.get("intents")
    if isinstance(intents, list):
        data["intents"] = [
            item for item in intents
            if not (isinstance(item, dict)
                    and item.get("id") in DEPRECATED_INTENT_IDS)]
    return data


# 旧版「理解这张图」契约：9 个字段、3 种结构，8B 视觉模型只会填前两个，
# 其余留空 → 渲染成一屏「—」。只按标记串替换，用户改过的不动。
VISION_PROMPT_OUTDATED = {
    "image.explain": ("只输出这些字段（没有内容的字段给空数组或空字符串）",
                      "只输出有内容的字段；没有内容就省略整个字段"),
}


def migrate_vision_prompts(data: dict[str, Any],
                           factory: dict[str, Any]) -> dict[str, Any]:
    intents = data.get("intents")
    if not isinstance(intents, list):
        return data
    defaults = {i.get("id"): i for i in (factory.get("intents") or [])
                if isinstance(i, dict)}
    for item in intents:
        if not isinstance(item, dict):
            continue
        markers = VISION_PROMPT_OUTDATED.get(item.get("id")) or ()
        if not markers:
            continue
        old = str(item.get("prompt_override") or "")
        new = str((defaults.get(item.get("id")) or {})
                  .get("prompt_override") or "")
        if new and any(m in old for m in markers):
            item["prompt_override"] = new
    return data


def migrate_vision_routes(data: dict[str, Any],
                          factory: dict[str, Any]) -> dict[str, Any]:
    """把 4 条图片路由 + 路由级提示词迁到「1 条路由 + 每条查询方式自带提示词」。"""
    routes = data.get("routes")
    intents = data.get("intents")
    if not isinstance(routes, dict) or not isinstance(intents, list):
        return data
    factory_intents = {i.get("id"): i for i in (factory.get("intents") or [])
                       if isinstance(i, dict)}
    for item in intents:
        old_route = item.get("primary_route")
        if old_route in OLD_VISION_ROUTES:
            pack = routes.get(old_route) or {}
            old_prompt = str(pack.get("prompt") or "").strip()
            item["primary_route"] = "image"
            if item.get("id") in VISION_INTENT_ORDER:
                item["order"] = VISION_INTENT_ORDER[item["id"]]
            if not str(item.get("prompt_override") or "").strip():
                unchanged = (old_prompt == str(
                    OLD_VISION_ROUTE_PROMPTS.get(old_route, "")).strip())
                factory_default = str(
                    (factory_intents.get(item.get("id")) or {})
                    .get("prompt_override") or "").strip()
                item["prompt_override"] = (factory_default
                                           if unchanged and factory_default
                                           else old_prompt)
        # 图片查询方式的提示词是各自的输出契约，不是旧脚本那 9 套。
        # 这一步不依赖路由改名：配置存过盘之后仍要能纠正（幂等）。
        if "image" in (item.get("domains") or ()):
            # 图片方式的提示词只有图片模板能提供；指到文本模板上会导致
            # 「点图片方式 → 不当成视觉请求」这种静默失效，这里直接修回来
            vision_route = next(
                (rid for rid, pack in routes.items()
                 if isinstance(pack, dict) and pack.get("vision")), "")
            current = str(item.get("primary_route") or "")
            if vision_route and not (routes.get(current) or {}).get("vision"):
                item["primary_route"] = vision_route
        if item.get("primary_route") == "image" \
                or "image" in (item.get("domains") or ()):
            item["use_legacy_template"] = False
            if not str(item.get("followup_route") or "").strip():
                # 「提取图片中的文字」必回流；没有输出契约的图片方式（提示词
                # 留空，靠模板兜底）产出的也是自由文本，同样回流到文本链路，
                # 否则只能把一段文字塞进 JSON 渲染器。
                if item.get("id") == "image.ocr" \
                        or not str(item.get("prompt_override") or "").strip():
                    item["followup_route"] = "auto"
    for old_route in OLD_VISION_ROUTES:
        routes.pop(old_route, None)
    route_prompts = data.get("route_prompts")
    if isinstance(route_prompts, dict):
        for old_route in OLD_VISION_ROUTES:
            route_prompts.pop(old_route, None)
    old_default = data.pop("desktop_vision_route", None)
    if old_default is not None:
        data["desktop_vision_intent"] = OLD_VISION_ROUTES.get(
            str(old_default), "image.ocr")
    return data


# 0.2.7 把四级收敛为两级：旧语义里的 WARN（发但提醒）与 CONFIRM（点两次才发）
# 在「弹窗词典」下都等于「不该弹」，统一映射为 BLOCK；用户仍可在设置页取消勾选。
# 已被合并/删除的规则整条丢弃，否则残留的旧级别会触发校验错误、挡住保存。
LEGACY_LEVELS = {"WARN": "BLOCK", "CONFIRM": "BLOCK"}
LEGACY_DROPPED_RULES = {
    "drive",                           # 已并入 path（路径整串匹配）
    "word_too_long", "phrase_too_long", "chinese_too_long",
    "chinese_sentence_too_long", "chinese_question_too_long",
    "chinese_paragraph_too_long",      # 软长度上限不再弹窗
    "english_sentence",                # 0.1 的硬拦截，0.2 起彻底移除
}


def migrate_policy(data: dict[str, Any]) -> dict[str, Any]:
    """把旧配置里的拦截规则迁移到两级语义；幂等，可反复调用。"""
    policy = data.get("policy")
    if not isinstance(policy, dict):
        return data
    rules = policy.get("rules")
    if isinstance(rules, dict):
        for rid in list(rules):
            if rid in LEGACY_DROPPED_RULES:
                rules.pop(rid, None)
                continue
            rule = rules[rid]
            if not isinstance(rule, dict):
                continue
            level = str(rule.get("level", "BLOCK")).upper()
            level = LEGACY_LEVELS.get(level, level)
            rule["level"] = level if level in LEVELS else "BLOCK"
    levels = policy.get("levels")
    if isinstance(levels, dict):
        for name in ("WARN", "CONFIRM"):
            levels.pop(name, None)
    return data


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
    if menu.get("show_disabled_items") not in {"grayed", "hidden", "hidden_in_fold"}:
        errors.append(ValidationError("menu.show_disabled_items", "未知显示方式", "general"))

    det = settings.detection
    for key in ("high", "mid", "gap", "mixed_min_score"):
        value = float(det.get(key, 0))
        if not 0 <= value <= 1:
            errors.append(ValidationError(f"detection.{key}", "阈值必须在 0–1 之间", "general"))

    if settings.get("image_text_conflict") not in {"image_only", "text_only", "mixed", "ask"}:
        errors.append(ValidationError("image_text_conflict", "未知冲突策略", "image"))
    # 图片默认查询方式必须存在且确实挂在图片模板下，否则识别会静默回退
    vision_intent = str(settings.get("desktop_vision_intent", "image.ocr")
                        or "image.ocr")
    picked = settings.intent(vision_intent)
    if picked is None:
        errors.append(ValidationError(
            "desktop_vision_intent", f"图片默认查询方式 {vision_intent!r} 不存在",
            "prompts"))
    elif "image" not in picked.domains:
        errors.append(ValidationError(
            "desktop_vision_intent", f"{vision_intent!r} 不是图片查询方式",
            "prompts"))

    # 图片方式的提示词来源必须是图片模板，否则点击它不会发起视觉请求
    intents = [i for i in (settings.data.get("intents") or ())
               if isinstance(i, dict)]
    for item in intents:
        if "image" not in (item.get("domains") or ()):
            continue
        rid = str(item.get("primary_route") or "")
        if not (settings.routes.get(rid) or {}).get("vision"):
            errors.append(ValidationError(
                f"intents.{item.get('id')}.primary_route",
                f"图片方式的提示词来源必须是图片模板（当前是 {rid!r}）",
                "prompts"))

    # 图片方式与它的模板都没有提示词：请求会落到全局兜底，属于静默降级
    for item in intents:
        if "image" not in (item.get("domains") or ()):
            continue
        if str(item.get("prompt_override") or "").strip():
            continue
        pack = settings.routes.get(str(item.get("primary_route") or "")) or {}
        if not str(pack.get("prompt") or "").strip():
            errors.append(ValidationError(
                f"intents.{item.get('id')}.prompt_override",
                "该图片方式没有提示词，所属模板也为空（会落到全局兜底）",
                "prompts"))

    # 图片方式指定了「识别后用哪个模板」时，目标必须真的能承接二次查询
    intents = [i for i in (settings.data.get("intents") or ())
               if isinstance(i, dict)]
    for item in intents:
        followup = str(item.get("followup_route") or "")
        if not followup or followup == "auto":
            continue
        field = f"intents.{item.get('id')}.followup_route"
        pack = settings.routes.get(followup)
        if pack is None:
            errors.append(ValidationError(
                field, f"二次查询模板 {followup!r} 不存在", "prompts"))
        elif pack.get("vision"):
            errors.append(ValidationError(
                field, "图片模板不能作为二次查询模板（会再次请求视觉模型）",
                "prompts"))
        elif not pack.get("enabled", True):
            errors.append(ValidationError(
                field, f"二次查询模板 {followup!r} 已禁用", "prompts"))
        elif not any(i.get("enabled", True)
                     and "image" not in (i.get("domains") or ())
                     and i.get("primary_route") == followup for i in intents):
            errors.append(ValidationError(
                field, f"模板 {followup!r} 下没有可用的查询方式", "prompts"))
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
        # recommended_limit 已按 ADR-7 废弃，不再影响输出，因此不进缓存签名。
        "menu": {k: v for k, v in settings.menu.items()
                 if k != "recommended_limit"},
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
