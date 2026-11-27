"""独立 Policy Engine：preflight → BLOCK / ALLOW。

两级语义（弹窗词典的前提：任何额外提示都是一次打扰）：

- ``BLOCK``：静默，不弹窗、不发请求、只写脱敏计数；
- ``ALLOW``：正常弹窗，由分类器决定进哪个模板。

规则闭集只回答「这种内容该不该弹窗」，判据只有三种形态：整串形状匹配、
隐私子串匹配、元数据（长度/配额）。它**不读任何内容语义特征**，因此与
Classifier 的定义域天然互斥，两边不存在需要排序去调和的冲突。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .classify import available_categories, classify_text, content_kind_of
from .features import content_kind_allowlist, extract_features, evaluate_condition
from .modality import input_kind_for
from .models import (Classification, FlowDecision, InputEnvelope, MidRoute,
                     PolicyHit, PolicyResult)

# ADR-007 / schema.json: 入口固定顺序，不因内容形态改变。
FLOW_ORDER = ("local_regex_hard_block", "length_split",
              "route_or_classify_or_block")
SHORT_TIER_MAX = 80      # =80 归短段
MID_TIER_MAX = 2000      # =2000 归中段，>2000 拦截

LEVEL_ORDER = ("BLOCK", "ALLOW")


def length_tier_of(content_length: int) -> str:
    """上含下不含：0–80 短段、81–2000 中段、>2000 拦截。"""
    if content_length <= SHORT_TIER_MAX:
        return "short_0_80"
    if content_length <= MID_TIER_MAX:
        return "mid_80_2000"
    return "long_over_2000"


def compose_for_route(route_id: str, settings):
    """按 selected_route_id 找到启用路由，套用其提示词模板（FR-009）。"""
    from .prompts import compose_prompt
    pack = settings.routes.get(route_id)
    if not pack or not pack.get("enabled", True):
        raise ValueError(f"路由不存在或已禁用：{route_id}")
    intent = next((i for i in settings.intents
                   if i.primary_route == route_id and i.enabled), None)
    if intent is None:
        raise ValueError(f"路由没有可用查询方式：{route_id}")
    return compose_prompt(intent, settings)


def route_mid_text(envelope: InputEnvelope, settings) -> MidRoute:
    """中段三类放行走本地分类器；其余中段与超长硬拦截，诊断分类照常产出。

    判据严格复用 NC-001 结案（``content_kind_of``），不新增正则/阈值。
    中段永不把 state 发往决策模型；超长一律 BLOCK（boundary_prefer_block）。
    """
    tier = length_tier_of(len(envelope.text or ""))
    classified = classify_text(envelope.text or "", settings,
                               source=envelope.source)
    result = f"{classified.category}:{classified.reason}".strip()
    kind = content_kind_of(classified)
    allowlist = content_kind_allowlist()
    local_hard_block = preflight(envelope, settings).level == "BLOCK"
    allowed = (tier == "mid_80_2000" and kind in allowlist)
    hard_block = local_hard_block or not allowed
    return MidRoute(
        length_tier=tier,
        content_kind=kind if allowed else None,
        classification_result=result,
        hard_block=hard_block,
        local_classifier_enabled_for_mid=allowed,
        state_sent=False,
        block_suppresses_popup=hard_block,
        block_suppresses_request=hard_block,
    )


def route_short_text(envelope: InputEnvelope, settings, *, cancel=None) -> str | None:
    """短段且未拦截 → 把 state 发决策模型；仅接受命中 registry 的 id。"""
    from .desktop.gateway import request_route
    if length_tier_of(len(envelope.text or "")) != "short_0_80":
        return None
    if not preflight(envelope, settings).allowed:
        return None
    registry = settings.enabled_route_ids()
    reply = request_route(settings, state=envelope.text or "",
                          route_registry=registry, cancel=cancel)
    if not reply:
        return None
    try:
        data = json.loads(reply)
    except (ValueError, TypeError):
        return None
    route_id = str(data.get("selected_route_id") or "")
    return route_id if route_id in registry else None


def short_tier_classification(envelope: InputEnvelope, settings, *,
                              cancel=None) -> Classification | None:
    """≤80 短段初判的分类结果，来源由互斥开关决定（FR-021 / FR-022）。

    - ``local_classifier``：沿用 ``classify_text`` 的正则结果（零 token）；
    - ``decision_model``：取 SystemOne 返回的路由 id；失败/无有效 id 时返回
      ``None``，绝不隐式回退本地分类器（FR-020）。

    中段、超长、图片输入一律返回 ``None``，保持既有本地分类路径不变。
    """
    if envelope.assets or length_tier_of(len(envelope.text or "")) != "short_0_80":
        return None
    if not preflight(envelope, settings).allowed:
        return None
    if settings.short_classifier_source != "decision_model":
        return classify_text(envelope.text or "", settings, source=envelope.source)

    selected = route_short_text(envelope, settings, cancel=cancel)
    if not selected:
        return None
    pack = settings.routes.get(selected) or {}
    label = str(pack.get("label") or selected)
    return Classification(
        category=selected, label=label, reason="decision_model",
        candidates=tuple(available_categories(settings)))


def _classification_result(envelope: InputEnvelope, settings) -> str:
    """诊断用分类轨迹：序列化 category/reason，不做第二套正则（NC-001）。"""
    classified = classify_text(envelope.text or "", settings,
                               source=envelope.source)
    return f"{classified.category}:{classified.reason}".strip()


def run_flow(envelope: InputEnvelope, settings, *,
             runtime: dict[str, Any] | None = None,
             sensitive_patterns: tuple[str, ...] = ()) -> FlowDecision:
    """入口分流骨架：本地正则硬拦截 → 长度分流 → 决策/分类/拦截。

    分类结果先产出（被拦内容也要有诊断结果），再由 hard_block 决定弹窗与
    请求是否抑制。本函数不联网、不新建模型客户端（唯一联网点是
    desktop/gateway.py）；`decision_model_fallback_enabled` 恒为假。
    """
    runtime = runtime or {}
    is_image = bool(envelope.assets)
    input_kind = input_kind_for(envelope)

    # 分类先行：无论后续是否硬拦截，都留下可用于诊断的分类结果。
    classification_result = _classification_result(envelope, settings)

    # 本地正则硬拦截：图片跳过文本闸门（preflight 自身已按 assets 判定）。
    text_gate_result = preflight(
        envelope, settings, runtime=runtime,
        sensitive_patterns=sensitive_patterns)
    length_tier = length_tier_of(len(envelope.text or ""))
    hard_block = text_gate_result.level == "BLOCK" or text_gate_result.skipped

    return FlowDecision(
        input_kind=input_kind,
        local_regex_applied=not is_image,
        content_length=len(envelope.text or ""),
        length_tier=length_tier,
        classification_result=classification_result,
        hard_block=hard_block,
        flow_order=FLOW_ORDER,
        block_suppresses_popup=hard_block,
        block_suppresses_request=hard_block,
        decision_model_fallback_enabled=False,
    )


def _match_rule(rule_id: str, rule: dict[str, Any], envelope: InputEnvelope,
                fs, settings, runtime: dict[str, Any]) -> bool:
    detector = rule.get("detector")
    text = envelope.text or ""

    if detector == "text_is_blank":
        # 纯图片信封没有文本，但它不是「空输入」：有资产就不该被 empty 拦死。
        return not text.strip() and not envelope.assets

    if detector == "regex":
        pattern = rule.get("pattern") or ""
        if not pattern:
            return False
        try:
            return bool(re.search(pattern, text, re.I))
        except re.error:
            return False

    if detector == "feature":
        name = rule.get("feature")
        if not name or not fs.get(name):
            return False
        # 多行代码里出现 password= 通常是示例代码，不因此阻止解析。
        # 豁免只认结构性代码形态 code_form（多数行缩进 / 关键字 / 成对括号
        # 或高代码分），不看 code_like：后者只要两个 `=` 就成立。
        if rule.get("multiline_code_exempt") and fs.get("is_multiline") \
                and fs.get("code_form"):
            return False
        return True

    if detector == "threshold":
        when = rule.get("applies_when")
        if when and not evaluate_condition(when, fs):
            return False
        key = rule.get("threshold")
        limit = settings.thresholds.get(key) if key else None
        value = fs.get(rule.get("feature"))
        if limit is None or value is None:
            return False
        try:
            return float(value) > float(limit)
        except (TypeError, ValueError):
            return False

    if detector == "quota":
        used = int(runtime.get("requests_today", 0))
        limit = int(runtime.get("daily_limit", settings.get("daily_request_limit", 0)))
        return limit > 0 and used >= limit

    if detector == "dedup":
        return bool(runtime.get("is_duplicate"))

    return False


def preflight(envelope: InputEnvelope, settings, *,
              runtime: dict[str, Any] | None = None,
              sensitive_patterns: tuple[str, ...] = ()) -> PolicyResult:
    """对输入做分级检查，返回 PolicyResult。

    ``runtime`` 携带无法由纯函数推导的状态：
    ``requests_today`` / ``daily_limit`` / ``is_duplicate``。
    """
    runtime = runtime or {}
    fs = extract_features(envelope, settings.thresholds)
    rules = settings.policy.get("rules", {})
    hits: list[PolicyHit] = []
    skipped = False

    # 纯图片信封没有文本，文本类闸门（形状/长度/隐私）对它无意义：
    # 否则 no_letters 会把「只有一张图」直接判成 BLOCK。
    text_gates_apply = bool((envelope.text or "").strip()) or not envelope.assets
    TEXT_DETECTORS = {"regex", "feature", "threshold", "text_is_blank"}

    for rule_id, rule in rules.items():
        if rule.get("enabled") is False:
            continue                       # 规则被显式停用：此前该字段被忽略
        if not text_gates_apply and rule.get("detector") in TEXT_DETECTORS:
            continue
        level = str(rule.get("level", "ALLOW")).upper()
        if level not in LEVEL_ORDER:
            level = "ALLOW"
        if not _match_rule(rule_id, rule, envelope, fs, settings, runtime):
            continue
        if rule.get("detector") == "dedup":
            skipped = True
            continue
        hits.append(PolicyHit(reason=rule_id, level=level,
                              description=str(rule.get("description", rule_id))))

    # 用户自定义敏感正则跟随 secret 规则：开关、级别、多行代码豁免一致。
    secret_rule = rules.get("secret") or {}
    if (sensitive_patterns and text_gates_apply
            and secret_rule.get("enabled") is not False):
        secret_level = str(secret_rule.get("level", "BLOCK")).upper()
        if secret_level not in LEVEL_ORDER:
            secret_level = "BLOCK"
        exempt = (bool(secret_rule.get("multiline_code_exempt"))
                  and bool(fs.get("is_multiline")) and bool(fs.get("code_form")))
        text = envelope.text or ""
        if text and not exempt:
            for pattern in sensitive_patterns:
                try:
                    if pattern and re.search(pattern, text, re.I):
                        hits.append(PolicyHit(
                            reason="secret", level=secret_level,
                            description="命中自定义敏感规则，不外发"))
                        break
                except re.error:
                    continue

    levels_seen = {h.level for h in hits}
    final = next((lv for lv in LEVEL_ORDER if lv in levels_seen), "ALLOW")

    # ``levels`` 行为表是级别语义的唯一权威：配置为 ``request:false`` 的级别
    # 一律不得产生请求（例如把 ALLOW 也配成不发）。
    level_actions = settings.policy.get("levels") or {}
    if final != "BLOCK" and level_actions.get(final, {}).get("request") is False:
        hits.append(PolicyHit(
            reason=f"level_{final.lower()}_denied", level="BLOCK",
            description=f"策略级别 {final} 已配置为禁止请求"))
        final = "BLOCK"

    return PolicyResult(level=final, hits=tuple(hits), skipped=skipped)


def intercept_record(envelope: InputEnvelope, result: PolicyResult) -> dict[str, Any]:
    """脱敏拦截记录：只允许这些字段进入日志。"""
    return {
        "level": result.level,
        "reasons": list(result.block_reasons),
        "source": envelope.source,
        "text_len": len(envelope.text or ""),
        "modality": envelope.modality,
        "asset_count": len(envelope.assets),
    }
