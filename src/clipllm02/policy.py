"""独立 Policy Engine：preflight → BLOCK / ALLOW。

两级语义（弹窗词典的前提：任何额外提示都是一次打扰）：

- ``BLOCK``：静默，不弹窗、不发请求、只写脱敏计数；
- ``ALLOW``：正常弹窗，由分类器决定进哪个模板。

规则闭集只回答「这种内容该不该弹窗」，判据只有三种形态：整串形状匹配、
隐私子串匹配、元数据（长度/配额）。它**不读任何内容语义特征**，因此与
Classifier 的定义域天然互斥，两边不存在需要排序去调和的冲突。
"""
from __future__ import annotations

import re
from typing import Any

from .features import extract_features, evaluate_condition
from .models import InputEnvelope, PolicyHit, PolicyResult

LEVEL_ORDER = ("BLOCK", "ALLOW")


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
