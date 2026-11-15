"""独立 Policy Engine：preflight → BLOCK / WARN / CONFIRM / ALLOW。

严格独立于 Classifier 与 DomainResolver：改一条拦截级别不触碰分类规则，
也不会改变 0.1 的 135 条分类回归用例。

拦截记录只写 ``(ts, level, reason, source, text_len, modality)``，
不含原文、URL 全文、API Key、内容 hash。
"""
from __future__ import annotations

import re
from typing import Any

from .features import extract_features, evaluate_condition
from .models import InputEnvelope, PolicyHit, PolicyResult

LEVEL_ORDER = ("BLOCK", "CONFIRM", "WARN", "ALLOW")


def _match_rule(rule_id: str, rule: dict[str, Any], envelope: InputEnvelope,
                fs, settings, runtime: dict[str, Any]) -> bool:
    detector = rule.get("detector")
    text = envelope.text or ""

    if detector == "text_is_blank":
        return not text.strip()

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
        # 多行代码里出现 password= 通常是示例代码，不因此阻止解析
        if rule.get("multiline_code_exempt") and fs.get("is_multiline") \
                and (fs.get("code_block") or fs.get("code_like")):
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

    for rule_id, rule in rules.items():
        if rule.get("enabled") is False:
            continue                       # 规则被显式停用：此前该字段被忽略
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

    # 用户自定义敏感正则一律按 secret 的当前级别处理
    if sensitive_patterns:
        text = envelope.text or ""
        for pattern in sensitive_patterns:
            try:
                if pattern and re.search(pattern, text):
                    hits.append(PolicyHit(
                        reason="secret", level="CONFIRM",
                        description="命中自定义敏感规则，需显式授权"))
                    break
            except re.error:
                continue

    levels_seen = {h.level for h in hits}
    if "BLOCK" in levels_seen:
        final = "BLOCK"
    elif "CONFIRM" in levels_seen:
        final = "CONFIRM"
    elif "WARN" in levels_seen:
        final = "WARN"
    else:
        final = "ALLOW"

    # ``levels`` 行为表是级别语义的唯一权威：配置为 ``request:false`` 的级别
    # 一律不得产生请求。此前该表从未被读取，导致 CONFIRM/WARN/ALLOW 行为失控。
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
