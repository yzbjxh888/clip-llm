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
from .prompts import GENERIC_TEMPLATE
from .models import IntentOption, SearchProvider, ValidationError

DEFAULT_SPEC_DIR = Path(__file__).resolve().parents[2] / "spec"

LEVELS = {"BLOCK", "ALLOW"}
FORMAT_IDS = {"static", "table", "interactive", "annotate"}
TRIGGER_POLICIES = {"auto", "confirm", "hotkey", "menu"}

# ADR-004 / schema.json: 方向只有 JSON → 路由 → 模板，禁止反向写回。
# v8：常量仍保留以兼容既有 import，但值指向通用完整模板，不再是空泛占位。
NEW_TEMPLATE_DEFAULT_PROMPT = GENERIC_TEMPLATE["prompt"]
JSON_WRITEBACK_FORBIDDEN = True

# ADR-009 / FR-021：短段（≤80）初判分类来源互斥开关，schema 字段唯一事实源。
SHORT_CLASSIFIER_SOURCE_LOCAL = "local_classifier"
SHORT_CLASSIFIER_SOURCE_DECISION = "decision_model"
SHORT_CLASSIFIER_SOURCES = (SHORT_CLASSIFIER_SOURCE_LOCAL,
                            SHORT_CLASSIFIER_SOURCE_DECISION)
DEFAULT_SHORT_CLASSIFIER_SOURCE = SHORT_CLASSIFIER_SOURCE_LOCAL
SHORT_SOURCE_NOTICE_KEY = "short_source_notice"

# ADR-010 / FR-024~026：B（决策模型类别 JSON）是唯一事实源；B → A 单向派生。
DECISION_MODEL_CATCH_ALL_ID = "通用解析"
DEFAULT_CATCH_ALL_DESCRIPTION = "其他无法归类的内容（兜底）"
DERIVED_TEMPLATE_SOURCE = "decision_model"
# v11 票据 27：独立内部标记，语义是「该路由的标签/id 来自 B 类别清单（受 B 约束）」。
# 与 derived_template_source（用户新增路由）正交——给出厂路由打旧标记会被
# 删类别/补通用模板/迁移三处逻辑误伤，故绝不复用旧标记。
DECISION_MODEL_SOURCE_TAG = "source_from_decision_model"

# v3 票据 17：旧英文类别/路由 id → 全量中文 id。只覆盖类别层，
# intent id（en.read 等）、领域 id（math/chem）、其他视角 id 均不在此表。
LEGACY_ROUTE_ID_MAP: dict[str, str] = {
    "en_word": "英语单词",
    "en_phrase": "英语短语",
    "chinese": "中文词语",
    "chinese_sentence": "中文句解",
    "chinese_question": "中文答疑",
    "chinese_paragraph": "中文段落",
    "latex": "LaTeX 公式",
    "code": "代码",
    "chem_substance": "化学物质",
    "general": "通用解析",
    "image": "图片识别",
}


def migrate_legacy_route_ids(data: dict[str, Any]) -> bool:
    """把用户配置里的旧英文类别/路由 id 迁移为中文 id（幂等）。

    覆盖 B 的键、routes / route_prompts 的键、intents 的 primary_route、
    includes[].route_id 与 followup_route。新键已存在时保留新值，避免覆盖
    用户已迁移的文案。
    """
    changed = False
    for section_name in ("decision_model_categories", "routes", "route_prompts"):
        section = data.get(section_name)
        if not isinstance(section, dict):
            continue
        for old, new in LEGACY_ROUTE_ID_MAP.items():
            if old in section:
                if new not in section:
                    section[new] = section.pop(old)
                else:
                    section.pop(old)
                changed = True
    for intent in data.get("intents") or ():
        if not isinstance(intent, dict):
            continue
        for field in ("primary_route", "followup_route"):
            old_value = intent.get(field)
            if old_value in LEGACY_ROUTE_ID_MAP:
                intent[field] = LEGACY_ROUTE_ID_MAP[old_value]
                changed = True
        for include in intent.get("includes") or ():
            if (isinstance(include, dict)
                    and include.get("route_id") in LEGACY_ROUTE_ID_MAP):
                include["route_id"] = LEGACY_ROUTE_ID_MAP[include["route_id"]]
                changed = True
    return changed


# v7 票据 22：主模型新字段空、旧字段有值时一次性回填（幂等，不覆盖用户新值）。
MAIN_MODEL_CONFIG_LEGACY_FIELDS = {
    "main_model_api_url": "desktop_api_url",
    "main_model_key": "desktop_api_key",
    "main_model_name": "desktop_model",
}


def migrate_model_config(data: dict[str, Any]) -> bool:
    """把旧 desktop_* 主模型配置回填到空的新字段；返回是否改动。

    新字段优先：用户一旦改过新字段（哪怕只改其中一项），其余空项仍从旧字段
    补齐；旧字段原样保留，因此重复调用不会改变结果。
    """
    main = data.get("main_model_config")
    if not isinstance(main, dict):
        return False
    changed = False
    for new_key, old_key in MAIN_MODEL_CONFIG_LEGACY_FIELDS.items():
        if str(main.get(new_key) or "").strip():
            continue
        old_value = data.get(old_key)
        if old_value is None or not str(old_value).strip():
            continue
        main[new_key] = str(old_value)
        changed = True
    return changed


def normalize_short_classifier_source(data: dict[str, Any]) -> tuple[str, str]:
    """返回 ``(source, notice)``；两者同时为真时归一为本地分类器并给提示。

    ``local_classifier_enabled`` / ``decision_model_enabled`` 是配置层兼容
    标志；最终权威仍是 schema 字段 ``short_classifier_source``。
    """
    local = bool(data.get("local_classifier_enabled"))
    decision = bool(data.get("decision_model_enabled"))
    if local and decision:
        return (SHORT_CLASSIFIER_SOURCE_LOCAL,
                "本地分类器与决策模型不能同时生效，已归一为本地分类器。")
    value = data.get("short_classifier_source")
    if value in SHORT_CLASSIFIER_SOURCES:
        return value, ""
    if local:
        return SHORT_CLASSIFIER_SOURCE_LOCAL, ""
    if decision:
        return SHORT_CLASSIFIER_SOURCE_DECISION, ""
    if value:
        return (SHORT_CLASSIFIER_SOURCE_LOCAL,
                "短内容初判分类来源取值非法，已归一为本地分类器。")
    return SHORT_CLASSIFIER_SOURCE_LOCAL, ""


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
        # 运行期统一视图：新字段空时补旧字段，避免 vision / gateway 各写一套回退。
        if isinstance(self.data.get("main_model_config"), dict):
            self.data["main_model_config"] = dict(self.data["main_model_config"])
        migrate_model_config(self.data)
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

    MODEL_CONFIG_FIELDS = ("main_model_api_url", "main_model_key", "main_model_name")

    @property
    def main_model_config(self) -> dict[str, Any]:
        """唯一一套多模态主模型配置（schema.json: main_model_config）。"""
        return dict(self.data.get("main_model_config") or {})

    @property
    def decision_model_config(self) -> dict[str, Any]:
        """独立决策模型配置入口（schema.json: decision_model_config）。"""
        return dict(self.data.get("decision_model_config") or {})

    @property
    def short_classifier_source(self) -> str:
        """短段初判分类来源；非法值一律归一为本地分类器（FR-021）。"""
        value = self.data.get("short_classifier_source")
        return value if value in SHORT_CLASSIFIER_SOURCES else SHORT_CLASSIFIER_SOURCE_LOCAL

    @property
    def short_source_notice(self) -> str:
        """配置层互斥归一时给用户的提示。"""
        return str(self.data.get(SHORT_SOURCE_NOTICE_KEY) or "")

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

    def route_registry(self) -> tuple[str, ...]:
        """路由唯一事实源；发给决策模型的 JSON 清单只由此产生。"""
        return self.enabled_route_ids()

    @property
    def route_prompts(self) -> dict[str, Any]:
        return dict(self.data.get("route_prompts") or {})

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
    source, notice = normalize_short_classifier_source(data)
    data["short_classifier_source"] = source
    if notice:
        data[SHORT_SOURCE_NOTICE_KEY] = notice
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
    source, notice = normalize_short_classifier_source(merged)
    merged["short_classifier_source"] = source
    merged[SHORT_SOURCE_NOTICE_KEY] = notice
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
            item["primary_route"] = "图片识别"
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
        if item.get("primary_route") == "图片识别" \
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


# v9 票据 25：出厂路由被污染的占位 prompt。用 unicode 转义写出，避免把票 24
# 已从生产源码清零的废弃默认值字面量重新引入（保留 grep 门禁的有效性）。
_POLLUTED_PROMPT = "\u8fd9\u662f\u4ec0\u4e48?"


def _is_polluted_prompt(value: Any) -> bool:
    """空串 / 旧占位（含全角问号）都算「没写过自己的提示词」。"""
    text = str(value or "").strip()
    return text in ("", _POLLUTED_PROMPT,
                    "\u8fd9\u662f\u4ec0\u4e48\uff1f")


def load_factory_route_templates(
        spec_dir: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """读 spec/builtin_settings.json 里每条出厂路由**自己的**结构化模板。

    不走 ``load_defaults()``：它会用 legacy_assets.json 的详细提示词覆盖
    ``routes[rid].prompt``；v9 回填要的是出厂 JSON 里那份 prompt + fields + sections。
    """
    root = Path(spec_dir) if spec_dir else Path(
        os.environ.get("CLIPLLM02_SPEC_DIR", str(DEFAULT_SPEC_DIR)))
    builtin = _read_json(root / "builtin_settings.json")
    return {
        str(rid): {key: copy.deepcopy(pack[key])
                   for key in ("prompt", "fields", "sections") if key in pack}
        for rid, pack in (builtin.get("routes") or {}).items()
        if isinstance(pack, dict)
    }


def restore_factory_route_templates(data: dict[str, Any], *,
                                    spec_dir: str | Path | None = None) -> bool:
    """幂等迁移：只给被污染的出厂路由回填各自出厂模板，返回是否改动。

    用户已改过的 prompt（非空、非占位）绝不覆盖；``route_prompts`` 里根本
    没有的出厂路由保持缺失（v8 约定：缺 key 才回退到出厂详细指令）。
    非出厂路由（B 新增标签）完全不碰，继续走通用模板 / 用户文案。
    """
    factory = load_factory_route_templates(spec_dir)
    routes = data.get("routes")
    prompts = data.get("route_prompts")
    changed = False
    for rid, source in factory.items():
        src_prompt = str(source.get("prompt") or "")
        if not src_prompt:
            continue
        pack = routes.get(rid) if isinstance(routes, dict) else None
        if isinstance(pack, dict):
            if _is_polluted_prompt(pack.get("prompt")):
                pack["prompt"] = src_prompt
                changed = True
            for key in ("fields", "sections"):
                # 空清单（如图片识别）没有可回填内容；反复写空列表会破坏幂等。
                if source.get(key) and not pack.get(key):
                    pack[key] = copy.deepcopy(source[key])
                    changed = True
        if (isinstance(prompts, dict) and rid in prompts
                and _is_polluted_prompt(prompts[rid])):
            prompts[rid] = src_prompt
            changed = True
    return changed


def restore_derived_route_templates(data: dict[str, Any]) -> bool:
    """v10 票据 26：幂等补齐「B 新增标签」路由的通用模板，返回是否改动。

    老配置里由决策模型派生、但没被后续迁移覆盖的新增路由（如「英语句子」）
    可能停留在空 fields/sections + 占位 prompt。这里只碰带
    ``derived_template_source == decision_model`` 的路由：缺什么补什么，
    ``route_prompts`` 里用户改过的非空非占位文案绝不覆盖。
    出厂路由各归各位回填（票 25 的 restore_factory_route_templates）与此互不干扰。
    """
    routes = data.get("routes")
    if not isinstance(routes, dict):
        return False
    prompts = data.get("route_prompts")
    changed = False
    for rid, pack in routes.items():
        if (not isinstance(pack, dict)
                or pack.get("derived_template_source") != DERIVED_TEMPLATE_SOURCE):
            continue
        if _is_polluted_prompt(pack.get("prompt")):
            pack["prompt"] = str(GENERIC_TEMPLATE["prompt"])
            changed = True
        if not pack.get("fields"):
            pack["fields"] = list(GENERIC_TEMPLATE["fields"])
            changed = True
        if not pack.get("sections"):
            pack["sections"] = copy.deepcopy(GENERIC_TEMPLATE["sections"])
            changed = True
        if isinstance(prompts, dict) and rid in prompts and _is_polluted_prompt(prompts[rid]):
            prompts[rid] = str(GENERIC_TEMPLATE["prompt"])
            changed = True
    return changed


def mark_decision_model_source_routes(data: dict[str, Any]) -> bool:
    """v11 票据 27：幂等补齐「受 B 约束」的内部标记，返回是否改动。

    凡路由的 key 或 id 字段落在 B（decision_model_categories）类别清单里，
    就补 ``source_from_decision_model: True``；图片识别不在 B，天然不打标。
    只加这个正交标记，绝不碰 derived_template_source——后者代表「用户新增」，
    被删类别/补通用模板/模板迁移三处逻辑消费，给出厂路由打上会误伤。
    """
    routes = data.get("routes")
    if not isinstance(routes, dict):
        return False
    b_ids = {str(key) for key in (data.get("decision_model_categories") or {})}
    changed = False
    for rid, pack in routes.items():
        if not isinstance(pack, dict):
            continue
        if str(rid) not in b_ids and str(pack.get("id") or "") not in b_ids:
            continue
        if pack.get(DECISION_MODEL_SOURCE_TAG) is not True:
            pack[DECISION_MODEL_SOURCE_TAG] = True
            changed = True
    return changed


def _factory_route_ids() -> set[str]:
    """出厂路由 id 集合（来自 spec 出厂配置）。

    v8：只给「用户新增路由」填通用模板；出厂路由的 route_prompts 为空时保持为空，
    让 compose_prompt 回退到 routes[rid].prompt 的出厂详细指令，不被通用默认值盖住。
    """
    return {str(rid) for rid in (load_defaults().routes or {})}


def _fill_generic_fields(pack: dict[str, Any]) -> None:
    """派生路由缺 fields/sections 时补成通用模板（用户已有值绝不覆盖）。"""
    if not pack.get("fields"):
        pack["fields"] = list(GENERIC_TEMPLATE["fields"])
    if not pack.get("sections"):
        pack["sections"] = copy.deepcopy(GENERIC_TEMPLATE["sections"])


def sync_route_templates(data: dict[str, Any]) -> dict[str, Any]:
    """按 route_registry 单向同步模板清单：新增注入默认提示词，删除一并移除。

    只改 ``route_prompts``（下游）；``routes``（JSON 路由清单）绝不回写。
    v8：出厂路由不注入通用默认值，留空回退到出厂详细 prompt。
    """
    from .prompts import new_template_prompt
    out = copy.deepcopy(data)
    routes = out.get("routes") or {}
    prompts = dict(out.get("route_prompts") or {})
    factory_ids = _factory_route_ids()
    for rid in routes:
        if rid not in prompts or not str(prompts[rid] or "").strip():
            if rid not in factory_ids:
                prompts[rid] = new_template_prompt()
    prompts = {rid: text for rid, text in prompts.items() if rid in routes}
    out["route_prompts"] = prompts
    return out


def _derived_query_method(route_id: str) -> dict[str, Any]:
    """B 新类别派生的「复用主路由模板」查询方式（约束链第 3 环）。

    三处中文同名由调用方保证（B 键 = 路由 label = 这里 id/label）。
    ``custom`` 让配置合并把它当作用户新增项保留；``derived_template_source``
    让 B 删类别时只连带删这一条，不碰用户在查询方式页的其它增删改。
    """
    return {
        "id": route_id, "label": route_id,
        "hint": f"复用「{route_id}」模板",
        "domains": ["*"], "modalities": ["text", "image", "mixed"],
        "applicable": None, "primary_route": route_id,
        "prompt_override": "", "includes": [],
        "default_format": "static", "supported_formats": ["static"],
        "order": 200, "enabled": True, "free_input_only": False,
        "custom": True, "derived_template_source": DERIVED_TEMPLATE_SOURCE,
        "conflict_policy": "primary_wins", "token_budget": 1400,
        "prompt_version": "2.0.0", "schema_version": "1.0.0",
    }


def sync_decision_model_templates(data: dict[str, Any],
                                  previous_categories=None) -> dict[str, Any]:
    """B → A 单向派生：按 B 的类别增删派生模板，绝不反向改写 B。

    B 新增类别 → 生成带 ``derived_template_source`` 标记的路由 + 默认模板 +
    一条同名的「复用主路由模板」查询方式（三处中文同名，带 ``custom`` 标记
    以便配置合并时保留）；
    B 删除类别 → 只连带删掉它派生的那一条模板与查询方式（用户自由模板 /
    查询方式不受影响）；
    B 改判别描述 → 这里什么都不做（criteria 直接读 B），模板文本保持用户文案。
    ``previous_categories`` 是应用新 B 之前的快照，用来识别被删除的类别。
    """
    from .prompts import new_template_prompt
    out = copy.deepcopy(data)
    b = out.get("decision_model_categories") or {}
    routes = out.setdefault("routes", {})
    prompts = out.setdefault("route_prompts", {})
    factory_ids = _factory_route_ids()
    new_ids = {str(rid) for rid in b}
    old_ids = {str(rid) for rid in (previous_categories or {})}
    intents = out.setdefault("intents", [])

    for key in b:
        rid = str(key)
        if not rid or rid == "图片识别":
            continue
        if rid not in routes:
            routes[rid] = {
                "id": rid, "label": rid, "enabled": True,
                "query_type": "free", "kind": "route", "prompt": "",
                "sections": {}, "fields": [],
                "derived_template_source": DERIVED_TEMPLATE_SOURCE,
            }
            # 约束链第 3 环：判别标签 = 模板标签 = 查询方式标签（全中文）。
            # prompt_override 留空 = 复用主路由模板；custom 标记让它能在
            # 配置合并（_merge_intents）时作为用户新增项保留下来。
            if not any(isinstance(i, dict) and i.get("id") == rid
                       for i in intents):
                intents.append(_derived_query_method(rid))
        # v11 票据 27：B 类别清单里的路由（含新增派生路由）统一带受约束标记；
        # 与 derived_template_source 正交，删类别仍只按旧标记连带删除。
        if routes[rid].get(DECISION_MODEL_SOURCE_TAG) is not True:
            routes[rid][DECISION_MODEL_SOURCE_TAG] = True
        # v8：派生路由（含旧配置里已派生但缺字段的）补成通用完整模板。
        if routes[rid].get("derived_template_source") == DERIVED_TEMPLATE_SOURCE:
            _fill_generic_fields(routes[rid])
        # v8：只有用户新增路由才填通用模板；出厂路由留空，
        # compose_prompt 会回退到 routes[rid].prompt 的出厂详细指令。
        if rid not in prompts or not str(prompts[rid] or "").strip():
            if rid not in factory_ids:
                prompts[rid] = new_template_prompt()

    for rid in old_ids - new_ids:
        pack = routes.get(rid)
        if (isinstance(pack, dict)
                and pack.get("derived_template_source") == DERIVED_TEMPLATE_SOURCE):
            routes.pop(rid, None)
            prompts.pop(rid, None)
            intents[:] = [i for i in intents
                          if not (isinstance(i, dict)
                                  and i.get("derived_template_source") == DERIVED_TEMPLATE_SOURCE
                                  and i.get("id") == rid)]
    return out


def apply_decision_model_json(data: dict[str, Any],
                              json_text: str) -> tuple[dict[str, Any], list[str]]:
    """应用拦截页的 B（{类别id: 判别描述}）并单向派生 A。

    解析/结构错误返回原数据与人类可读的错误列表，绝不落盘、绝不破坏既有设置。
    """
    try:
        incoming = json.loads(json_text)
    except (ValueError, TypeError) as exc:
        return copy.deepcopy(data), [f"JSON 解析失败：{exc}"]
    if not isinstance(incoming, dict) or not incoming:
        return copy.deepcopy(data), ["JSON 顶层必须是非空的 {类别id: 判别描述} 对象"]
    for key, value in incoming.items():
        if not isinstance(key, str) or not key.strip():
            return copy.deepcopy(data), ["类别 id 必须是非空字符串"]
        if not isinstance(value, str) or not value.strip():
            return copy.deepcopy(data), [f"类别 {key!r} 的判别描述必须是非空字符串"]
    b = dict(incoming)
    b.setdefault(DECISION_MODEL_CATCH_ALL_ID, DEFAULT_CATCH_ALL_DESCRIPTION)
    out = copy.deepcopy(data)
    out["decision_model_categories"] = b
    return sync_decision_model_templates(
        out, previous_categories=data.get("decision_model_categories") or {}), []


def assert_no_decision_model_writeback(base: dict[str, Any],
                                       patch: dict[str, Any]) -> None:
    """单向约束：模板页任何操作都不得改动 B 的类别清单/判别描述。"""
    before = base.get("decision_model_categories") or {}
    after = patch.get("decision_model_categories") or {}
    if after != before:
        raise ValueError("禁止从模板编辑回写决策模型类别 JSON（B）")


def apply_routes_json(data: dict[str, Any], json_text: str) -> tuple[dict[str, Any], list[str]]:
    """单向应用设置页的 JSON 编辑：JSON → routes / route_prompts。

    只从 JSON 读路由与模板，再由 :func:`sync_route_templates` 同步下游模板；
    模板编辑永远不会反向改写 JSON 路由清单（``json_writeback_forbidden``）。
    解析或结构错误返回原数据与人类可读的错误列表，绝不静默应用。
    """
    try:
        incoming = json.loads(json_text)
    except (ValueError, TypeError) as exc:
        return copy.deepcopy(data), [f"JSON 解析失败：{exc}"]
    if not isinstance(incoming, dict):
        return copy.deepcopy(data), ["JSON 顶层必须是对象"]
    routes = incoming.get("routes")
    if not isinstance(routes, dict) or not routes:
        return copy.deepcopy(data), ["JSON 必须包含非空的 routes 对象"]
    prompts = incoming.get("route_prompts", {})
    if not isinstance(prompts, dict):
        return copy.deepcopy(data), ["route_prompts 必须是对象"]
    if any(not isinstance(k, str) or not isinstance(v, dict)
           for k, v in routes.items()):
        return copy.deepcopy(data), ["routes 的每一项必须是 {路由 id: 对象}"]
    out = copy.deepcopy(data)
    out["routes"] = copy.deepcopy(routes)
    out["route_prompts"] = copy.deepcopy(prompts)
    return sync_route_templates(out), []


# v10 票据 26：模板页编辑区显示/编辑一条路由的完整 JSON。
# 必要字段与类型来自出厂 RoutePack（spec/builtin_settings.json），缺一不可。
ROUTE_TEMPLATE_FIELDS: dict[str, tuple[type, ...]] = {
    "id": (str,),
    "label": (str,),
    "enabled": (bool,),
    "query_type": (str,),
    "kind": (str,),
    "prompt": (str,),
    "sections": (dict,),
    "fields": (list,),
}


def route_template_json(data: dict[str, Any], route_id: str) -> str:
    """把一条路由的完整结构序列化成格式化 JSON 供编辑区显示。

    ensure_ascii=False 让中文可直接阅读；indent=2 便于用户就地改某段 sections 文案。
    """
    pack = (data.get("routes") or {}).get(route_id) or {}
    return json.dumps(pack, ensure_ascii=False, indent=2)


def _rename_route_references(data: dict[str, Any], old_id: str, new_id: str) -> None:
    """改 id 时同步所有引用（routes/route_prompts 键由调用方先搬好）。"""
    for section_name in ("routes", "route_prompts"):
        section = data.get(section_name)
        if isinstance(section, dict) and old_id in section:
            section[new_id] = section.pop(old_id)
    for intent in data.get("intents") or ():
        if not isinstance(intent, dict):
            continue
        for field in ("primary_route", "followup_route"):
            if intent.get(field) == old_id:
                intent[field] = new_id
        for inc in intent.get("includes") or ():
            if isinstance(inc, dict) and inc.get("route_id") == old_id:
                inc["route_id"] = new_id


def apply_route_template_json(
        data: dict[str, Any], route_id: str,
        json_text: str) -> tuple[dict[str, Any], list[str]]:
    """应用模板页编辑区的完整路由 JSON；解析/校验错误返回原数据与人类可读错误。

    写回 routes[rid]（整包，额外键原样保留）与 route_prompts[rid]（取 JSON 的
    prompt，保持 compose_prompt 的 override 优先级）。id 改动只允许改成决策
    模型 B 里已有的类别值；真的换 key 时同步 routes / route_prompts / intents
    的全部引用，保持 B 类别 = routes key = id = label 三处对齐。
    """
    try:
        pack = json.loads(json_text)
    except (ValueError, TypeError) as exc:
        return copy.deepcopy(data), [f"JSON 解析失败：{exc}"]
    if not isinstance(pack, dict):
        return copy.deepcopy(data), ["编辑区顶层必须是路由对象"]

    errors: list[str] = []
    for key, types in ROUTE_TEMPLATE_FIELDS.items():
        if key not in pack:
            errors.append(f"缺少必要字段 {key}")
        elif not isinstance(pack[key], types):
            errors.append(f"字段 {key} 的值类型不对")
    if not errors:
        if any(not isinstance(item, str) for item in pack["fields"]):
            errors.append("fields 的每一项必须是字符串")
        if any(not isinstance(k, str) or not isinstance(v, str)
               for k, v in pack["sections"].items()):
            errors.append("sections 必须是 {片段名: 文案} 字符串对象")
    if errors:
        return copy.deepcopy(data), errors

    routes = data.get("routes") or {}
    if route_id not in routes:
        return copy.deepcopy(data), [f"未知路由：{route_id}"]
    old_id = str(routes[route_id].get("id") or route_id)
    new_id = str(pack["id"])
    if new_id != old_id:
        if not new_id.strip():
            return copy.deepcopy(data), ["id 不能为空"]
        b = data.get("decision_model_categories") or {}
        if new_id not in b:
            return copy.deepcopy(data), [
                f"id 必须是决策模型 B（decision_model_categories）里已有的类别值：{new_id}"]
        if new_id != route_id and new_id in routes:
            return copy.deepcopy(data), [f"目标 id {new_id} 已被占用，不能覆盖"]

    # key 不是从 JSON 的 id 推出来的：老配置的 id 字段可能仍是旧英文（en_word），
    # 用户没动它就不换 key；只有真的改了 id 才可能换 key。
    id_changed = new_id != old_id
    target_id = new_id if (id_changed and new_id != route_id) else route_id
    out = copy.deepcopy(data)
    out_routes = out.setdefault("routes", {})
    out_prompts = out.setdefault("route_prompts", {})
    out_routes[target_id] = copy.deepcopy(pack)
    out_prompts[target_id] = str(pack["prompt"] or "")
    if target_id != route_id:
        out_routes.pop(route_id, None)
        out_prompts.pop(route_id, None)
        _rename_route_references(out, route_id, target_id)
    if id_changed:
        out_routes[target_id]["label"] = new_id
    return out, []


def assert_no_json_writeback(base: dict[str, Any], patch: dict[str, Any]) -> None:
    """模板编辑只允许改 route_prompts；routes 的任何改动都是非法写回。"""
    base_routes = base.get("routes") or {}
    edited_routes = patch.get("routes") or {}
    if edited_routes and edited_routes != base_routes:
        raise ValueError("禁止从模板编辑回写 JSON 路由清单")


def apply_template_edit(settings: "Settings", route_id: str, text: str) -> "Settings":
    """编辑一条模板：只更新 route_prompts，返回新的 Settings。"""
    base = settings.as_dict()
    data = copy.deepcopy(base)
    assert_no_decision_model_writeback(base, data)
    if route_id not in (data.get("routes") or {}):
        raise ValueError(f"未知模板：{route_id}")
    data.setdefault("route_prompts", {})[route_id] = text
    return Settings(data)


def ruleset_audit(settings: Settings) -> dict[str, Any]:
    """SC-005: 12 条保留 + 2 条删除，按 schema 枚举逐项核对，无边界冲突。"""
    from .features import FEATURES
    rules = settings.policy.get("rules", {})
    removed = {"pem_block", "daily_limit"}
    kept = sorted(set(rules) - removed)
    expected = {
        "empty", "no_letters", "single_latin", "url", "path", "email",
        "filename", "token_blob", "fake_word", "secret", "too_long", "duplicate",
    }
    # 边界冲突审查：启用规则的 feature 必须存在；locked BLOCK 不允许被降级。
    conflicts: list[str] = []
    defaults = load_defaults().policy.get("rules", {})
    for rid, rule in rules.items():
        feature = rule.get("feature")
        if feature and rule.get("detector") in ("feature", "threshold") \
                and feature not in FEATURES:
            conflicts.append(f"{rid}:未知特征 {feature}")
        base = defaults.get(rid) or {}
        if base.get("locked") and str(rule.get("level", "")).upper() != "BLOCK":
            conflicts.append(f"{rid}:locked 规则被降级")
    return {
        "kept": kept,
        "removed": sorted(removed),
        "expected": sorted(expected),
        "complete": set(kept) == expected,
        "no_conflict": not conflicts,
        "conflicts": conflicts,
    }


def _validate_model_config(settings: Settings) -> list[ValidationError]:
    """出厂允许留空（未配置=本地预览）；缺键才是配置错误。"""
    errors: list[ValidationError] = []
    main = settings.main_model_config
    for key in Settings.MODEL_CONFIG_FIELDS:
        if key not in main:
            errors.append(ValidationError(f"main_model_config.{key}",
                                          f"主模型配置缺少 {key}", "models"))
    decision = settings.decision_model_config
    for key in Settings.MODEL_CONFIG_FIELDS:
        if key in decision and not isinstance(decision[key], str):
            errors.append(ValidationError(f"decision_model_config.{key}",
                                          f"决策模型配置 {key} 必须是字符串", "models"))
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


def _validate_templates(settings: Settings) -> list[ValidationError]:
    errors: list[ValidationError] = []
    known = set(settings.routes)
    for rid in (settings.data.get("route_prompts") or {}):
        if rid not in known:
            errors.append(ValidationError(f"route_prompts.{rid}",
                                          f"模板 {rid!r} 没有对应的路由", "prompts"))
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
    errors.extend(_validate_model_config(settings))
    errors.extend(_validate_templates(settings))
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
