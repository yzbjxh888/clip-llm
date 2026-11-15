"""本地领域初判与菜单构建。

- ``DomainResolver`` 非短路地求值所有已启用领域，产出候选集 + 证据；
- 分数是启发式评分，**明确不是统计概率**，阈值全部可配置；
- ``build_menu`` 纯本地、零 token、零网络；偏好只影响排序与默认高亮。
"""
from __future__ import annotations

from .classify import available_categories, classify_text
from .features import evaluate_condition, extract_features
from .models import (
    Category, DomainCandidate, FeatureSet, InputEnvelope, IntentOption,
    MenuModel, MenuOption, PolicyResult, Preferences, Suggestion, sha256_text,
)


def disabled_domains(settings) -> set[str]:
    switches = settings.feature_switches
    return {str(d["id"]) for d in settings.domains
            if d.get("switch") and switches.get(d["switch"]) is False}


# 「其他接近领域」的亲和排序：语言 ↔ 语言、理科互相关
NEAR_DOMAINS = {
    "en": ("zh",), "zh": ("en",),
    "math": ("physics", "chem"), "physics": ("math", "chem"),
    "chem": ("math", "physics"), "code": ("math",),
}


# 类别判定统一由 classify.classify_text 完成（复用 gd_llm.py 的 L3 有序
# 规则表 + veto）。这里只把判定出的类别映射到该类别下的默认意图，
# 不再维护第二套判定逻辑。
def _pick_intent(settings, fs: FeatureSet, routes: tuple[str, ...]) -> str | None:
    """按 primary_route 找可用意图，不硬编码意图 id，保持可插拔。"""
    disabled = disabled_domains(settings)
    for rid in routes:
        pack = settings.routes.get(rid)
        if not pack or not pack.get("enabled", True):
            continue
        candidates = []
        for intent in settings.intents:
            if not intent.enabled or intent.primary_route != rid:
                continue
            if intent.domains and "*" not in intent.domains \
                    and all(d in disabled for d in intent.domains):
                continue
            if intent.applicable and not evaluate_condition(intent.applicable, fs):
                continue
            candidates.append(intent)
        if candidates:
            candidates.sort(key=lambda i: i.order)
            return candidates[0].id
    return None


def classify_intent(envelope: InputEnvelope, fs: FeatureSet,
                    settings) -> str | None:
    """按类别初判结果给出该类别下的默认意图；不替用户提交。"""
    result = classify_text(envelope.text or "", settings,
                           source=envelope.source)
    return _pick_intent(settings, fs, (result.category, "general"))


def resolve_domains(envelope: InputEnvelope, fs: FeatureSet,
                    settings) -> tuple[str, tuple[DomainCandidate, ...]]:
    disabled = disabled_domains(settings)
    det = settings.detection
    candidates: list[DomainCandidate] = []

    for domain in settings.domains:
        did = str(domain["id"])
        if did in disabled:
            continue
        modalities = domain.get("modalities") or ("text", "image", "mixed", "*")
        if "*" not in modalities and envelope.modality not in modalities:
            continue
        when = domain.get("when")
        if when is None:
            # 无判别条件的领域（如 image）只在确实存在图片资产时成立
            if not envelope.assets:
                continue
        elif not evaluate_condition(when, fs):
            continue
        candidates.append(DomainCandidate(
            id=did, label=str(domain.get("label", did)),
            score=float(domain.get("score", 0.0)),
            evidence=tuple(domain.get("evidence") or ()),
        ))

    candidates.sort(key=lambda c: (-c.score, c.id))
    max_candidates = int(det.get("max_candidates", 3))
    candidates = candidates[:max_candidates]

    high = float(det.get("high", 0.80))
    mid = float(det.get("mid", 0.50))
    gap = float(det.get("gap", 0.15))
    mixed_min = float(det.get("mixed_min_score", 0.80))

    if not candidates:
        return "LOW", ()
    top = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None
    if second and top.score >= mixed_min and second.score >= mixed_min:
        level = "MIXED"
    elif top.score >= high and (second is None or top.score - second.score >= gap):
        level = "HIGH"
    elif top.score >= mid:
        level = "MID"
    else:
        level = "LOW"
    return level, tuple(candidates)


def suggest(envelope: InputEnvelope, policy: PolicyResult,
            settings) -> Suggestion:
    fs = extract_features(envelope, settings.thresholds)
    level, candidates = resolve_domains(envelope, fs, settings)
    warnings = tuple(policy.warnings) + tuple(policy.confirmations)

    hint = None
    if candidates:
        for intent in sorted(settings.intents, key=lambda i: i.order):
            if intent.enabled and not intent.free_input_only \
                    and (candidates[0].id in intent.domains or "*" in intent.domains) \
                    and intent.primary_route \
                    and settings.routes.get(intent.primary_route, {}).get("enabled", True):
                hint = intent.primary_route
                break

    # 纯图片资产没有文本特征可判，类别初判交给图片专用领域
    classification = (classify_text(envelope.text or "", settings,
                                    source=envelope.source)
                      if (envelope.text or "").strip() else None)
    return Suggestion(
        envelope_id=envelope.envelope_id,
        modality=envelope.modality,
        features=fs,
        domain_candidates=candidates,
        level=level,
        warnings=warnings,
        suggested_route=hint,
        suggested_intent=(_pick_intent(settings, fs,
                                       (classification.category, "general"))
                          if classification else None),
        classification=classification,
    )


def _intent_state(intent, suggestion: Suggestion, settings):
    """返回 (是否可用, 不可用原因)。"""
    if not intent.enabled:
        return False, "该意图已在设置中关闭"
    if not intent.supports_modality(suggestion.modality):
        return False, f"不适用于 {suggestion.modality} 输入"
    disabled = disabled_domains(settings)
    if intent.domains and "*" not in intent.domains \
            and all(d in disabled for d in intent.domains):
        return False, "所属领域已关闭"
    if intent.primary_route:
        pack = settings.routes.get(intent.primary_route)
        if not pack:
            return False, "主路由不存在"
        if not pack.get("enabled", True):
            return False, f"主路由 {intent.primary_route} 已禁用"
    if intent.applicable and not evaluate_condition(intent.applicable, suggestion.features):
        return False, "对当前输入不适用"
    for inc in intent.includes:
        pack = settings.routes.get(inc.get("route_id"))
        if not pack or not pack.get("enabled", True):
            return False, f"引用路由 {inc.get('route_id')} 不可用"
    return True, ""


def build_menu(suggestion: Suggestion, settings,
               prefs: Preferences | None = None,
               *, category_id: str | None = None) -> MenuModel:
    """生成菜单。

    两条硬规则（对应用户反馈 3 / 5）：
    - **推荐区只放当前领域的专属查询类型**；``"*"`` 通用兜底意图只有在
      该领域没有可用专属意图时才出现，避免「是什么 / 怎么读」这类同一模板；
    - **LOW 档也自动出菜单**（通用兜底 + 各领域代表意图 + 领域切换 chip），
      不强制用户先选领域再出第二步，但选项始终保留。
    """
    prefs = prefs or Preferences()
    menu = settings.menu
    disabled = disabled_domains(settings)
    limit = int(menu.get("recommended_limit", 6))
    per_domain = int(menu.get("other_per_domain", 2))
    top_domain = suggestion.top_domain
    text_hash = sha256_text(suggestion.features.text)

    # 类别初判：当前类别（判定结果或用户切换）+ 全部可选类别（动态来自 routes）
    classification = suggestion.classification
    labels = {c.id: c.label for c in available_categories(settings)}
    category = category_id or (classification.category
                               if classification else None)
    category_label = labels.get(category, category) if category else None
    category_chips: tuple[Category, ...] = ()
    if classification:
        all_cats = [Category(id=classification.category,
                             label=classification.label,
                             evidence=(f"初判规则：{classification.reason}",)),
                    *classification.candidates]
        seen: set[str] = set()
        ordered: list[Category] = []
        for item in all_cats:
            if item.id not in seen:
                seen.add(item.id)
                ordered.append(item)
        category_chips = tuple(sorted(
            ordered, key=lambda c: 0 if c.id == category else 1))

    usable: list[MenuOption] = []
    grayed: list[MenuOption] = []
    for intent in settings.intents:
        if intent.free_input_only:
            continue                       # 自由输入单独成区
        ok, reason = _intent_state(intent, suggestion, settings)
        if ok:
            usable.append(MenuOption(intent=intent))
        else:
            grayed.append(MenuOption(intent=intent, enabled=False,
                                     disabled_reason=reason))

    specific = [o for o in usable if "*" not in o.intent.domains]
    fallback = [o for o in usable if "*" in o.intent.domains]

    def sort_key(opt: MenuOption):
        # 旧脚本 9 套系统提示词（*.legacy / 自定义模板）在其领域内排最前
        legacy_rank = -1 if (getattr(opt.intent, "use_legacy_template", False)
                             or opt.intent.id.endswith(".legacy")) else 0
        return (legacy_rank, opt.intent.order,
                -prefs.frequency(opt.intent.id, top_domain))

    def loose_pick(did: str) -> list[MenuOption]:
        """宽松选取某领域可用意图：忽略 applicable / modality（用户主动
        「换领域」时不该被默认适用性挡住），只要求启用且主路由可用。"""
        out: list[MenuOption] = []
        for intent in settings.intents:
            if intent.free_input_only or intent.id in rec_ids:
                continue
            if not intent.enabled:
                continue
            if did not in intent.domains:      # 只要该领域专属意图，通用兜底不进 others
                continue
            if intent.primary_route:
                pack = settings.routes.get(intent.primary_route)
                if not pack or not pack.get("enabled", True):
                    continue
            out.append(MenuOption(intent=intent))
        out.sort(key=sort_key)
        return out

    def category_pick(rid: str, *, loose: bool) -> list[MenuOption]:
        """该类别下的多面向解读；用户主动切换类别时忽略 applicable。"""
        out: list[MenuOption] = []
        for intent in settings.intents:
            if intent.free_input_only or not intent.enabled \
                    or intent.primary_route != rid:
                continue
            if intent.domains and "*" not in intent.domains \
                    and all(d in disabled for d in intent.domains):
                continue
            # 查询方式挂在模板下，不再按领域二次过滤：
            # 设置里该模板有几条，UI 就显示几条。
            if not loose:
                ok, _ = _intent_state(intent, suggestion, settings)
                if not ok:
                    continue
            out.append(MenuOption(intent=intent))
        out.sort(key=sort_key)
        return out

    def build_recommended() -> list[MenuOption]:
        """常用查询方式 = 该模板下的全部查询方式，数量与设置一一对应。"""
        if category:
            picked = category_pick(category, loose=bool(category_id))
        else:
            picked = sorted([o for o in specific
                             if top_domain in o.intent.domains], key=sort_key)
        if not picked:
            picked = sorted(fallback, key=sort_key)
        # 首位固定为「复用主路由模板」的那条（sort_key 已把它排在第一），
        # 其余严格按设置里的顺序；不再让推荐意图插队。
        return picked[:max(limit, len(picked))]

    rec_ids: set[str] = set()

    if suggestion.level == "LOW":
        chips = tuple(DomainCandidate(
            id=str(d["id"]), label=str(d.get("label", d["id"])), score=0.0,
            evidence=("领域不明确，可手动选择",))
            for d in settings.domains if str(d["id"]) not in disabled)
        recommended = build_recommended()
        rec_ids.update(o.intent.id for o in recommended)
        others: list[MenuOption] = []
        for chip in chips[:4]:
            for opt in loose_pick(chip.id)[:1]:
                others.append(MenuOption(intent=opt.intent, section="other"))
                rec_ids.add(opt.intent.id)
    else:
        chips = suggestion.domain_candidates
        recommended = build_recommended()
        rec_ids.update(o.intent.id for o in recommended)

        others = []
        other_views = settings.get("other_views") or {}
        max_others = int(other_views.get("max_items", 6))
        views = [
            view for view in (other_views.get("items") or ())
            if isinstance(view, dict) and view.get("enabled", True)
            and str(view.get("id") or "").strip()
            and (not view.get("domains") or "*" in view["domains"]
                 or (top_domain and top_domain in view["domains"]))
        ]
        if views:
            # 通用视角：与提示词模板无关，设置里改完立即出现在「其他角度」
            for view in views[:max_others]:
                intent = IntentOption.from_other_view(view)
                if intent is not None:
                    others.append(MenuOption(intent=intent, section="other"))
        else:
            # 未配置时回退到领域亲和候选（候选次之 → 亲和领域 → 其余领域）
            other_domains: list[str] = []
            seen = {top_domain} if top_domain else set()
            for cand in suggestion.domain_candidates:
                if cand.id not in seen:
                    other_domains.append(cand.id)
                    seen.add(cand.id)
            for near in NEAR_DOMAINS.get(top_domain, ()):
                if near not in seen and near not in disabled and near != "image":
                    other_domains.append(near)
                    seen.add(near)
            for d in settings.domains:
                did = str(d["id"])
                if did in seen or did in ("*", "image"):
                    continue
                other_domains.append(did)
                seen.add(did)
            for did in other_domains[:3]:
                for opt in loose_pick(did)[:per_domain]:
                    others.append(MenuOption(intent=opt.intent, section="other"))
                    rec_ids.add(opt.intent.id)
                if len(others) >= max_others:
                    break

    if menu.get("show_disabled_items") != "grayed":
        grayed = []

    default_intent = None
    policy_name = menu.get("default_intent_policy", "last_used")
    auto = (recommended[0].intent.id
            if category_id and recommended else suggestion.suggested_intent)
    usable_ids = {o.intent.id for o in recommended}
    if policy_name == "last_used":
        last = prefs.last_intent(text_hash)
        if last and last in usable_ids:
            default_intent = last      # 用户习惯优先于分类器
        elif auto and auto in usable_ids:
            default_intent = auto      # 首次查询：自动意图初判
    elif policy_name == "first" and recommended:
        default_intent = recommended[0].intent.id
    elif policy_name == "auto" and auto and auto in usable_ids:
        default_intent = auto

    all_options = tuple(recommended) + tuple(others)
    return MenuModel(
        envelope_id=suggestion.envelope_id,
        level=suggestion.level,
        domain_chips=chips,
        category=category,
        category_label=category_label,
        category_chips=category_chips,
        recommended=tuple(recommended),
        others=tuple(others),
        grayed=tuple(grayed),
        default_intent=default_intent,
        formats=_formats(settings, all_options),
        free_input=_free_input(settings),
        tools=_tools(suggestion, settings, all_options, chips),
        warnings=suggestion.warnings,
    )


def _formats(settings, options) -> tuple[str, ...]:
    enabled = tuple(settings.formats.get("enabled") or ("static",))
    supported = {f for o in options for f in o.intent.supported_formats}
    out = [f for f in enabled if f == "static" or f in supported]
    return tuple(out or ["static"])


def _free_input(settings) -> dict:
    fi = settings.free_input
    return {
        "enabled": bool(settings.menu.get("free_input_enabled", True)),
        "modes": ["instruction", "prompt"],
        "mode": str(fi.get("mode", "instruction")),
        "max_chars": int(fi.get("max_chars", 2000)),
        "max_prompt_chars": int(fi.get("max_prompt_chars", 4000)),
        "style_refs": list(settings.enabled_route_ids()),
    }


def _tools(suggestion, settings, options, chips) -> tuple:
    contexts = {suggestion.modality, "text", "image"}
    contexts.update(c.id for c in chips)
    for opt in options:
        if opt.intent.primary_route:
            contexts.add(opt.intent.primary_route)
        contexts.update(opt.intent.domains)
    return settings.enabled_search_providers(tuple(contexts))
