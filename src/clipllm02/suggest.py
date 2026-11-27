"""本地领域初判与菜单构建。

- ``DomainResolver`` 非短路地求值所有已启用领域，产出候选集 + 证据；
- 分数是启发式评分，**明确不是统计概率**，阈值全部可配置；
- ``build_menu`` 纯本地、零 token、零网络；偏好只影响排序与默认高亮。
"""
from __future__ import annotations

from dataclasses import replace

from .classify import available_categories, classify_text
from .features import evaluate_condition, extract_features
from .policy import length_tier_of, short_tier_classification
from .models import (
    Category, Classification, DomainCandidate, FeatureSet, InputEnvelope,
    IntentOption, MenuModel, MenuOption, PolicyResult, Preferences, Suggestion,
    sha256_text,
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


def modality_ok(intent, modality: str) -> bool:
    """该方式能否处理这种输入；双模态时文本方式仍可用（处理文字部分）。"""
    if intent.supports_modality(modality):
        return True
    return modality == "mixed" and intent.supports_modality("text")


def vision_category(settings) -> str:
    """图片类别的 id：图片侧只有一条模板，类别即模板 id（图片识别）。"""
    for route_id, pack in (getattr(settings, "routes", None) or {}).items():
        if isinstance(pack, dict) and pack.get("vision"):
            return str(route_id)
    return "图片识别"


def auto_followup_intent(menu, settings) -> str | None:
    """图片识别结果回流后自动续跑用的查询方式。

    取初判的默认项；若默认项仍是图片方式（例如双模态没判出文本类别），
    返回 None，让调用方退回手动选择，避免又发一次视觉请求。
    """
    if menu is None or not menu.default_intent:
        return None
    intent = settings.intent(menu.default_intent)
    if intent is None or "image" in intent.domains:
        return None
    return intent.id


def vision_default_intent(settings) -> str | None:
    """设置里选定的「图片默认查询方式」；配置失效时退到第一条可用图片方式。"""
    want = str(settings.get("desktop_vision_intent", "image.ocr")
               or "image.ocr")
    picked = settings.intent(want)
    if picked is not None and picked.enabled and "image" in picked.domains:
        return picked.id
    for intent in sorted(settings.intents, key=lambda i: i.order):
        if intent.enabled and "image" in intent.domains:
            return intent.id
    return None


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

    if classification is not None and not envelope.assets:
        # 图片类别只在有图时成立；否则「其他 ▾」里会多出一个点进去全是
        # 图片方式的类别，选中后必然发不出请求。
        classification = replace(classification, candidates=tuple(
            c for c in classification.candidates
            if c.id != vision_category(settings)))

    if envelope.assets:
        # 截图/图片输入：类别就是「图片识别」本身（模板 = 图片路由）。
        # 文本框里的内容只是附加要求，判定出的文本类别降为可切换的候选，
        # 否则会出现「截了图，标题却写中文词语」。
        category_id = vision_category(settings)
        pick = settings.routes.get(category_id) or {}
        chips: tuple[Category, ...] = ()
        if classification is not None:
            chips = (
                Category(id=classification.category, label=classification.label,
                         evidence=(f"初判规则：{classification.reason}",)),
                *tuple(c for c in classification.candidates
                       if c.id != classification.category))
        classification = Classification(
            category=category_id,
            label=str(pick.get("label") or category_id),
            reason="image_asset", candidates=chips)

    suggested = None
    if classification:
        suggested = _pick_intent(settings, fs,
                                 (classification.category, "通用解析"))
    if envelope.assets:
        # 默认高亮「设置 → 图片默认查询方式」，用户仍可在类别里换其他方式。
        suggested = vision_default_intent(settings) or suggested
    return Suggestion(
        envelope_id=envelope.envelope_id,
        modality=envelope.modality,
        features=fs,
        domain_candidates=candidates,
        level=level,
        warnings=warnings,
        suggested_route=hint,
        suggested_intent=suggested,
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


DECISION_NO_RESULT_NOTICE = (
    "决策模型初判无结果：本次不显示类别高亮，请手动选择查询方式，"
    "或在设置页切换分类来源。")


def suggest_with_short_source(envelope: InputEnvelope, policy: PolicyResult,
                              settings, *, cancel=None) -> Suggestion:
    """按 ``short_classifier_source`` 决定 ≤80 短段初判的类别来源。

    决策模型失败时显式给出「无结果」提示，不回退本地正则（FR-020）；
    中段、超长与图片输入保持原有本地初判路径。
    """
    base = suggest(envelope, policy, settings)
    if (envelope.assets or not policy.allowed
            or length_tier_of(len(envelope.text or "")) != "short_0_80"):
        return base
    if settings.short_classifier_source != "decision_model":
        # 本地分类器沿用 suggest() 既有结果（含候选类别过滤），不重复判定。
        return base
    classified = short_tier_classification(envelope, settings, cancel=cancel)
    if classified is not None:
        return replace(base, classification=classified,
                       suggested_route=classified.category)
    return replace(base, classification=None, suggested_route=None,
                   warnings=base.warnings + (DECISION_NO_RESULT_NOTICE,))


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
            if not modality_ok(intent, suggestion.modality):
                continue        # 文本输入里不列图片方式（反之亦然）
            # 查询方式挂在模板下，不再按领域二次过滤：
            # 设置里该模板有几条，UI 就显示几条。
            if not loose:
                ok, _ = _intent_state(intent, suggestion, settings)
                if not ok:
                    continue
            out.append(MenuOption(intent=intent))
        out.sort(key=sort_key)
        if rid == vision_category(settings):
            # 设置里的「图片默认排最前」仍然生效
            default_intent = vision_default_intent(settings)
            out.sort(key=lambda o: (0 if o.intent.id == default_intent else 1,
                                    sort_key(o)))
        return out

    def build_recommended() -> list[MenuOption]:
        """常用查询方式 = 该模板下的全部查询方式，数量与设置一一对应。"""
        if category:
            picked = category_pick(category, loose=bool(category_id))
            # 类别由分类器决定时，该类别下的查询方式不应再被 applicable
            # 二次否决：否则标题写「中文句解」，推荐区却退回通用解析。
            if not picked and not category_id:
                picked = category_pick(category, loose=True)
        else:
            picked = sorted([o for o in specific
                             if top_domain in o.intent.domains], key=sort_key)
        if not picked:
            picked = sorted(fallback, key=sort_key)
        # 首位固定为「复用主路由模板」的那条（sort_key 已把它排在第一），
        # 其余严格按设置里的顺序；不再让推荐意图插队。
        return picked

    rec_ids: set[str] = set()

    # v12（票据 28）：换领域下拉不再只列判定候选，而是列全部可用领域
    # （排除被禁用与图片专用领域），保证 HIGH 单候选时也能手动改选。
    # 判定项对象优先复用（保留其分数/证据），其余按 settings.domains 顺序新建。
    judged = {c.id: c for c in suggestion.domain_candidates}
    chip_list: list[DomainCandidate] = []
    for d in settings.domains:
        did = str(d["id"])
        if did in disabled or did == "image":
            continue
        chip = judged.get(did)
        if chip is None:
            chip = DomainCandidate(
                id=did, label=str(d.get("label", did)), score=0.0,
                evidence=("领域不明确，可手动选择",))
        chip_list.append(chip)
    top_id = suggestion.top_domain
    if top_id:
        # 稳定排序：判定项置顶，其余保持 settings.domains 的顺序。
        chip_list.sort(key=lambda c: 0 if c.id == top_id else 1)
    chips = tuple(chip_list)

    recommended = build_recommended()
    rec_ids.update(o.intent.id for o in recommended)

    # 其他视角是各查询路由都能用的公共提示词：无论初判置信度高低，
    # 都优先显示 other_views.items（用户配置的中文通用视角）。
    # 领域亲和仅在未配置时回退；B（决策模型类别）与 other_views 相互独立。
    others: list[MenuOption] = []
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
    elif suggestion.level == "LOW":
        # 未配置时回退：领域不明确时用现场构造的 chips
        for chip in chips[:4]:
            for opt in loose_pick(chip.id)[:1]:
                others.append(MenuOption(intent=opt.intent, section="other"))
                rec_ids.add(opt.intent.id)
    else:
        # 未配置时回退到领域亲和候选（候选次之 → 亲和领域 → 其余领域）
        other_domains: list[str] = []
        seen = {top_domain} if top_domain else set()
        for cand in suggestion.domain_candidates:
            if cand.id not in seen:
                other_domains.append(cand.id)
                seen.add(cand.id)
        for near in NEAR_DOMAINS.get(top_domain or "", ()):
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
    # 兜底：推荐区有可选项就必须有默认项（回车 / 数字键才有落点）。
    # 类别由分类器判出、但 applicable 与类别形状不一致时，auto 会是 None。
    if default_intent is None and recommended:
        default_intent = recommended[0].intent.id

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
