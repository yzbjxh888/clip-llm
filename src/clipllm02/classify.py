"""输入内容的类别初判：复用 ``gd_llm.py`` 的 L3 有序规则表语义。

与 ``policy.preflight`` 分工：

- policy 回答「能不能发请求」（BLOCK / WARN / CONFIRM / ALLOW）；
- 本模块回答「根据内容特征，最可能属于哪个提示词模板类别」，不做拦截。

类别就是 ``settings.routes`` 的条目（设置页「提示词模板」页逐条编辑，
可新增/删除）。这里不硬编码类别清单，未来增减模板会自动成为可选类别。

判定结构照搬旧分类器的三层：L0 归一化 → L2 特征 → L3 有序规则表
（自上而下取第一条 requires 成立且未被任何 veto 否决的规则）。
新增判定只需往表里插一条并声明 veto，不会静默改判既有分支。
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .features import extract_features
from .models import Category, Classification

# 出厂类别的中文显示名；仅作为 label 回退，类别本身仍由 routes 决定。
DEFAULT_CATEGORY_LABELS = {
    "en_word": "英语单词", "en_phrase": "英语短语", "chinese": "中文词语",
    "chinese_sentence": "中文句解", "chinese_question": "中文答疑",
    "chinese_paragraph": "中文段落", "latex": "LaTeX 公式",
    "code": "代码块", "general": "通用解析",
    "chem_substance": "化学物质", "image_ocr": "图片文字",
    "image_table": "图片表格", "image_explain": "图片解释",
    "image_annotate": "图片标注",
}

# 同类别的其他角度优先排在一起；只是排序提示，不参与类别存在性判定。
CATEGORY_AFFINITY = {
    "chinese": ("chinese_sentence", "chinese_question", "chinese_paragraph"),
    "chinese_sentence": ("chinese", "chinese_paragraph", "chinese_question"),
    "chinese_question": ("chinese_sentence", "chinese", "chinese_paragraph"),
    "chinese_paragraph": ("chinese_sentence", "chinese", "chinese_question"),
    "en_word": ("en_phrase", "general"), "en_phrase": ("en_word", "general"),
    "latex": ("chem_substance", "code", "general"),
    "chem_substance": ("latex", "general"),
    "image_ocr": ("latex", "image_table", "image_explain", "image_annotate"),
    "image_table": ("image_ocr", "image_explain", "image_annotate"),
    "image_explain": ("image_ocr", "image_table", "image_annotate"),
    "image_annotate": ("image_ocr", "image_explain", "image_table"),
    "code": ("general", "latex"), "general": ("code", "chinese"),
}

CN_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
EN_CHAR_RE = re.compile(r"[A-Za-z]")
CN_QUESTION_END_RE = re.compile(r"？\s*$")
CN_SENT_PUNCT_RE = re.compile(r"[。！…]")
CN_SENT_END_PUNCT_RE = re.compile(r"[。；！…]\s*$")
CN_END_FULL_STOP_RE = re.compile(r"。\s*$")
CN_SENT_PUNCT_COUNT_RE = re.compile(r"[。！？…]")
CN_CLAUSE_PUNCT_RE = re.compile(r"[。？！，；：]")
CN_DOMINANT_PUNCT_RE = re.compile(r"[。？！，；]")
CITATION_REF_RE = re.compile(
    r"(?:\[|［)\s*(?:\d+(?:\s*[-,–~]\s*\d+)*|[图表注式附]\s*\d+"
    r"(?:\s*[-–]\s*\d+)?)\s*(?:\]|］)")
CODE_SIGNAL_STRICT_RE = re.compile(r"[=;{}<>|\\`~^_]")
MULTILINE_CODE_SYM_RE = re.compile(r"=|\{|\}|\[|\]|->|=>|::|//|^\s*#", re.M)


def _context(text: str, fs, thresholds: dict[str, Any]) -> dict[str, Any]:
    """L2 扩展特征：把旧分类器特有、0.2 FeatureSet 没有的谓词补齐。"""
    cn = int(fs.get("cn_count") or 0)
    en = int(fs.get("en_count") or 0)
    letters = cn + en
    thresholds = thresholds or {}
    sent_max = int(thresholds.get("cn_sentence_max", 120))
    term_max = int(thresholds.get("cn_pure_max", 20))
    code_max = int(thresholds.get("code_max", 80))
    min_len = int(thresholds.get("cn_sentence_min_len", 4))
    dominant_min = int(thresholds.get("cn_dominant_min", 6))

    cn_question = bool(CN_QUESTION_END_RE.search(text))
    ends_cn = bool(CN_SENT_END_PUNCT_RE.search(text))
    ends_full_stop = bool(CN_END_FULL_STOP_RE.search(text))
    has_cn_sent_punct = bool(CN_SENT_PUNCT_RE.search(text))
    # 旧分类器 CN_PUNCT_END_ONLY=1：只认以句末标点收尾的输入。
    cn_sent_route = ends_full_stop or (ends_cn and letters >= min_len)
    punct_count = len(CN_SENT_PUNCT_COUNT_RE.findall(text))
    cn_para_route = ends_cn and letters > sent_max and punct_count >= 1
    cn_dominant = cn > dominant_min or bool(CN_DOMINANT_PUNCT_RE.search(text))
    cn_nl_route = (cn_question or cn_sent_route or cn_para_route
                   or (cn_dominant and letters > sent_max))

    stripped = CITATION_REF_RE.sub(" ", text) if cn_dominant else text
    code_block = bool(fs.get("code_block"))
    strong_sym_nocite = bool(CODE_SIGNAL_STRICT_RE.search(stripped))
    code_like = bool(fs.get("code_like"))

    # 中文自然句让位谓词（旧脚本 _is_cn_sentence）：含汉字 + 自然语言信号，
    # 且没有真实代码痕迹，才能让代码类规则让位。
    cn_sentence = False
    if cn > 0 and cn_nl_route and not code_block:
        if strong_sym_nocite:
            cn_sentence = cn >= en
        elif re.search(r"[A-Za-z_]\w*\s*\(", stripped):
            cn_sentence = False
        elif any(token in stripped for token in ("::", "->", "=>", "//")):
            cn_sentence = False
        else:
            cn_sentence = True

    return {
        "text": text,
        "letters": letters, "cn_count": cn, "en_count": en,
        "has_cn": cn > 0, "has_en": en > 0,
        "word_count": int(fs.get("word_count") or 0),
        "is_multiline": bool(fs.get("is_multiline")),
        "cn_question": cn_question,
        "cn_sent_route": cn_sent_route,
        "cn_para_route": cn_para_route,
        "cn_punct_count": punct_count,
        "cn_dominant": cn_dominant,
        "cn_sentence": cn_sentence,
        "strong_sym_nocite": strong_sym_nocite,
        "multiline_code_sym_nocite": bool(MULTILINE_CODE_SYM_RE.search(stripped)),
        "code_block": code_block,
        "code_like": code_like,
        "code_signal": bool(fs.get("code_signal")),
        "latex_signal": bool(fs.get("latex_signal")),
        "chem_signal": bool(fs.get("chem_signal")),
        "filename": bool(fs.get("filename")),
        "is_word": bool(fs.get("is_word")),
        "is_phrase": bool(fs.get("is_phrase")),
        "en_sentence": bool(fs.get("en_sentence")),
        "is_cli": bool(fs.get("is_cli")),
        "has_cn_clause_punct": bool(CN_CLAUSE_PUNCT_RE.search(text)),
        "term_max": term_max, "sent_max": sent_max, "code_max": code_max,
    }


def _decide_chinese(f: dict[str, Any]) -> str:
    """中文四段派发：疑问 → 段落 → 句解 → 词语（对齐 _decide_chinese）。"""
    if f["cn_question"]:
        return "chinese_question"
    if f["cn_para_route"]:
        return "chinese_paragraph"
    # 相对旧脚本的放宽 1：旧脚本只在 >120 字时才升格段落（为省 token），
    # 两句短文本仍算句解。0.2 不再静默限长，按「多句 = 段落」这一最可能
    # 类别判定，更贴合用户所说的「根据内容特征判定最可能的类别」。
    if f["cn_punct_count"] >= 2 and f["letters"] > f["term_max"]:
        return "chinese_paragraph"
    if f["cn_sent_route"]:
        return "chinese_sentence"
    # 放宽 2：无句末标点的半截划选，只要含中文分句标点且长于术语上限，
    # 判句解而不是「中文词语」。
    if f["has_cn_clause_punct"] and f["letters"] > f["term_max"]:
        return "chinese_sentence"
    return "chinese"


# (规则名, requires, ((否决名, veto 谓词), ...), 类别 id 或 None=动态派发)
Rule = tuple[str, Callable[[dict], bool],
            tuple[tuple[str, Callable[[dict], bool]], ...],
            str | None]

CATEGORY_RULES: tuple[Rule, ...] = (
    ("latex", lambda f: f["latex_signal"],
     (("chinese_dominant", lambda f: f["cn_dominant"]),), "latex"),
    ("chem", lambda f: f["chem_signal"] and not f["has_cn"],
     (), "chem_substance"),
    ("long_text_code", lambda f: f["letters"] > f["code_max"],
     (("no_code_signal", lambda f: not (f["code_block"]
                                        or f["multiline_code_sym_nocite"]
                                        or f["strong_sym_nocite"])),
      ("cn_sentence", lambda f: f["cn_sentence"])), "code"),
    ("code_block", lambda f: f["code_block"], (), "code"),
    ("multiline_code_symbol",
     lambda f: f["is_multiline"] and f["multiline_code_sym_nocite"],
     (("cn_sentence", lambda f: f["cn_sentence"]),), "code"),
    ("filename", lambda f: f["filename"],
     (("cn_sentence", lambda f: f["cn_sentence"]),), "general"),
    ("english_word", lambda f: f["is_word"], (), "en_word"),
    ("chinese", lambda f: f["has_cn"], (), None),
    ("code_like", lambda f: f["code_like"], (), "code"),
    ("cli_command", lambda f: f["is_cli"],
     (("english_sentence", lambda f: f["en_sentence"]),), "general"),
    # 0.2 不再拦截英文整句（ADR-4），归到通用解析而不是 BLOCK
    ("english_sentence", lambda f: f["en_sentence"], (), "general"),
    ("english_phrase", lambda f: f["is_phrase"], (), "en_phrase"),
    ("fallback", lambda f: True, (), "general"),
)


def available_categories(settings) -> tuple[Category, ...]:
    """可选类别 = 当前启用的提示词模板，动态读取，支持未来增删。"""
    out: list[Category] = []
    for rid, pack in (settings.routes or {}).items():
        label = str(pack.get("label") or DEFAULT_CATEGORY_LABELS.get(rid) or rid)
        out.append(Category(id=str(rid), label=label,
                            enabled=bool(pack.get("enabled", True))))
    return tuple(out)


def _category_label(category: str, categories: tuple[Category, ...]) -> str:
    for item in categories:
        if item.id == category:
            return item.label
    return DEFAULT_CATEGORY_LABELS.get(category, category)


def _rank_candidates(categories: tuple[Category, ...],
                     primary: str) -> tuple[Category, ...]:
    """其他类别：同族优先，其余按模板定义顺序；已判定的类别排在首位。"""
    near = CATEGORY_AFFINITY.get(primary, ())
    enabled = [c for c in categories if c.enabled and c.id != primary]
    enabled.sort(key=lambda c: (0 if c.id in near else 1,
                                near.index(c.id) if c.id in near else 0))
    return tuple(enabled)


def classify_text(text: str, settings, *, source: str = "manual",
                  explain: bool = False) -> Classification:
    """按内容特征判定最可能的类别；返回判定 + 其他可选类别。"""
    cleaned = (text or "").strip()
    categories = available_categories(settings)
    known = {c.id for c in categories if c.enabled}

    if not cleaned:
        return Classification(category="general", label="通用解析",
                              reason="empty", candidates=(), trace=())

    fs = extract_features(cleaned, settings.thresholds)
    f = _context(fs.text, fs, settings.thresholds)
    trace: list[str] = []

    # 图片/OCR 来源以公式为主（用户：图片大多用于识别公式）
    if source == "vision" and "latex" in known:
        if explain:
            trace.append("L3 [vision_source]：图片输入优先公式类别")
        primary, reason = "latex", "vision_source"
    else:
        primary, reason = "general", "fallback"
        for name, requires, vetoes, category in CATEGORY_RULES:
            if not requires(f):
                if explain:
                    trace.append(f"L3 [{name}]：requires 不成立，跳过")
                continue
            veto = next((vname for vname, vfn in vetoes if vfn(f)), None)
            if veto is not None:
                if explain:
                    trace.append(f"L3 [{name}]：命中但被 {veto} 否决")
                continue
            primary = _decide_chinese(f) if category is None else category
            reason = name
            if explain:
                trace.append(f"L3 [{name}]：胜出 → {primary}")
            break

    # 类别可能被删除/停用：降级到仍存在的类别，绝不返回不存在的选项
    if primary not in known:
        if explain:
            trace.append(f"类别 {primary} 未启用，降级到 general")
        primary = "general" if "general" in known else next(iter(known), "general")

    return Classification(
        category=primary,
        label=_category_label(primary, categories),
        reason=reason,
        candidates=_rank_candidates(categories, primary),
        trace=tuple(trace),
    )


def explain_text(text: str, settings, *, source: str = "manual") -> str:
    """逐条输出规则命中/否决轨迹，用于诊断「初判不准」。"""
    result = classify_text(text, settings, source=source, explain=True)
    fs = extract_features(text, settings.thresholds)
    features = _context(fs.text, fs, settings.thresholds)
    lines = [f"输入：{(text or '')[:60]!r}",
             "L2 特征：" + ", ".join(
                 f"{k}={features[k]!r}" for k in
                 ("letters", "cn_count", "en_count", "cn_question",
                  "cn_sent_route", "cn_para_route", "cn_dominant",
                  "cn_sentence", "latex_signal", "chem_signal", "code_block",
                  "is_word", "is_phrase", "is_cli")),
             *result.trace,
             f"判定类别：{result.category}（{result.label}），规则 {result.reason}",
             "其他可选类别：" + "、".join(c.label for c in result.candidates[:8])]
    return "\n".join(lines)
