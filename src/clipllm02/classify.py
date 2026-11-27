"""输入内容的类别初判：复用 ``gd_llm.py`` 的 L3 有序规则表语义。

与 ``policy.preflight`` 分工：

- policy 回答「能不能发请求」（BLOCK / ALLOW）；
- 本模块回答「根据内容特征，最可能属于哪个提示词模板类别」，不做拦截。

类别就是 ``settings.routes`` 的条目（设置页「提示词模板」页逐条编辑，
可新增/删除）。这里不硬编码类别清单，未来增减模板会自动成为可选类别。

0.2.7 起，所有谓词只有一处定义（``features.py``）：中文四段派发由
``cn_*_shape`` 提供，本模块只负责「按证据强度排序 + 声明 veto」，
不再自己实现 cn_dominant / cn_sent_route 之类的第二套判据。
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .features import extract_features
from .models import Category, Classification

# 出厂类别的中文显示名；仅作为 label 回退，类别本身仍由 routes 决定。
DEFAULT_CATEGORY_LABELS = {
    "英语单词": "英语单词", "英语短语": "英语短语", "中文词语": "中文词语",
    "中文句解": "中文句解", "中文答疑": "中文答疑",
    "中文段落": "中文段落", "LaTeX 公式": "LaTeX 公式",
    "代码": "代码块", "通用解析": "通用解析",
    "化学物质": "化学物质",
    # 图片侧只有一条模板（图片识别），4 条查询方式靠 intent 区分
    "图片识别": "图片识别",
}

# 同类别的其他角度优先排在一起；只是排序提示，不参与类别存在性判定。
CATEGORY_AFFINITY = {
    "中文词语": ("中文句解", "中文答疑", "中文段落"),
    "中文句解": ("中文词语", "中文段落", "中文答疑"),
    "中文答疑": ("中文句解", "中文词语", "中文段落"),
    "中文段落": ("中文句解", "中文词语", "中文答疑"),
    "英语单词": ("英语短语", "通用解析"), "英语短语": ("英语单词", "通用解析"),
    "LaTeX 公式": ("化学物质", "代码", "通用解析"),
    "化学物质": ("LaTeX 公式", "通用解析"),
    "图片识别": ("LaTeX 公式", "通用解析"),
    "代码": ("通用解析", "LaTeX 公式"), "通用解析": ("代码", "中文词语"),
}

# 中文结构 → 提示词模板类别（唯一映射，policy 用同一组特征做长度提示）。
CN_SHAPE_CATEGORY = {
    "question": "中文答疑",
    "paragraph": "中文段落",
    "sentence": "中文句解",
    "term": "中文词语",
}

CITATION_REF_RE = re.compile(
    r"(?:\[|［)\s*(?:\d+(?:\s*[-,–~]\s*\d+)*|[图表注式附]\s*\d+"
    r"(?:\s*[-–]\s*\d+)?)\s*(?:\]|］)")
CODE_SIGNAL_STRICT_RE = re.compile(r"[=;{}<>|\\`~^_]")
# 注意不要用「行首 #」：`## 结论` 这类 Markdown 标题会被误判成代码块
MULTILINE_CODE_SYM_RE = re.compile(r"=|\{|\}|\[|\]|->|=>|::|//", re.M)


def _context(fs, thresholds: dict[str, Any]) -> dict[str, Any]:
    """把 FeatureSet 摊平成规则表可直接读的字典；不新增任何判据。

    这里出现的每个键都必须来自 ``features.py``（单一事实源），只有
    ``cn_prose`` / ``cn_sentence`` 是本模块的**组合**谓词（用于 veto），
    组合规则本身也一目了然：代码形态成立时，中文不让位。
    """
    text = fs.text
    thresholds = thresholds or {}
    cn_dominant = bool(fs.get("cn_dominant"))

    shape = None
    if fs.get("cn_question_shape"):
        shape = "question"
    elif fs.get("cn_paragraph_shape"):
        shape = "paragraph"
    elif fs.get("cn_sentence_shape"):
        shape = "sentence"
    elif fs.get("cn_term_shape"):
        shape = "term"

    # 「中文是否为自然句」只看**结构性**代码证据（多行块 / 高代码分）：
    # 句子里夹一个 `f(x)` 会让 code_like 成立，但那不改变它是个中文句子。
    code_structural = bool(fs.get("code_form"))
    code_shaped = code_structural or bool(fs.get("code_like"))
    cn_prose = shape in ("sentence", "paragraph", "question") and not code_structural
    cn_sentence = shape in ("sentence", "paragraph") and not code_structural

    stripped = CITATION_REF_RE.sub(" ", text) if cn_dominant else text
    return {
        "text": text,
        "shape": shape,
        "letters": int(fs.get("letters") or 0),
        "cn_count": int(fs.get("cn_count") or 0),
        "en_count": int(fs.get("en_count") or 0),
        "has_cn": bool(fs.get("has_cn")),
        "has_en": bool(fs.get("has_en")),
        "word_count": int(fs.get("word_count") or 0),
        "is_multiline": bool(fs.get("is_multiline")),
        "cn_question": bool(fs.get("cn_question")),
        "cn_dominant": cn_dominant,
        "cn_prose": cn_prose,
        "cn_sentence": cn_sentence,
        "strong_sym_nocite": bool(CODE_SIGNAL_STRICT_RE.search(stripped)),
        "multiline_code_sym_nocite": bool(MULTILINE_CODE_SYM_RE.search(stripped)),
        "code_block": bool(fs.get("code_block")),
        "code_shaped": code_shaped,
        "code_form": bool(fs.get("code_form")),
        "code_like": bool(fs.get("code_like")),
        "latex_signal": bool(fs.get("latex_signal")),
        "latex_leading": bool(fs.get("latex_leading")),
        "latex_strong": bool(fs.get("latex_strong")),
        "chem_signal": bool(fs.get("chem_signal")),
        "filename": bool(fs.get("filename")),
        "path_like": bool(fs.get("path_like")),
        "is_word": bool(fs.get("is_word")),
        "is_phrase": bool(fs.get("is_phrase")),
        "fake_word": bool(fs.get("fake_word")),
        "en_sentence": bool(fs.get("en_sentence")),
        "is_cli": bool(fs.get("is_cli")),
        "code_max": int(thresholds.get("code_max", 80)),
    }


# (规则名, requires, ((否决名, veto 谓词), ...), 类别 id 或 None=动态派发)
Rule = tuple[str, Callable[[dict], bool],
            tuple[tuple[str, Callable[[dict], bool]], ...],
            str | None]

# 排序 = 证据强度：结构符号 > 行结构 > 命令行 > 词形 > 字符集。
# 「字符集」是最弱的证据，所以 chinese 排在所有结构类规则之后，
# 并显式声明三个 veto，避免任何带中文注释的代码被吸进中文模板。
CATEGORY_RULES: tuple[Rule, ...] = (
    ("latex", lambda f: f["latex_signal"],
     (("cn_prose", lambda f: f["cn_prose"]),
      # 代码块里的 `=`/`_`/`{}` 不是公式；只有「以公式开头」的粘贴才让
      # 代码形态让位（`\begin{align}`、`$$…` 或 `\frac…` 起头）。
      ("code_form", lambda f: f["code_form"] and not f["latex_leading"])),
     "LaTeX 公式"),
    ("chem", lambda f: f["chem_signal"] and not f["has_cn"],
     (), "化学物质"),
    # 结构性证据先于长度证据：多行代码块直接判代码，长度只用于兜住
    # 「没有结构但很长、且带代码符号」的粘贴。
    ("code_block", lambda f: f["code_block"],
     (("cn_sentence", lambda f: f["cn_sentence"]),), "代码"),
    ("multiline_code_symbol",
     lambda f: f["is_multiline"] and f["multiline_code_sym_nocite"],
     (("cn_sentence", lambda f: f["cn_sentence"]),), "代码"),
    ("long_text_code", lambda f: f["letters"] > f["code_max"],
     (("no_code_signal", lambda f: not (f["code_block"]
                                        or f["multiline_code_sym_nocite"]
                                        or f["strong_sym_nocite"])),
      ("cn_sentence", lambda f: f["cn_sentence"])), "代码"),
    ("cli_command", lambda f: f["is_cli"],
     (("english_sentence", lambda f: f["en_sentence"]),
      # 「git 怎么用？」是中文提问，不是命令块
      ("cn_prose", lambda f: f["cn_prose"])), "通用解析"),
    ("code_like", lambda f: f["code_like"],
     (("path", lambda f: f["path_like"]),
      # 中文自然句里夹一段代码/调用，仍然是句解或答疑
      ("cn_prose", lambda f: f["cn_prose"])), "代码"),
    ("english_word", lambda f: f["is_word"],
     (("fake_word", lambda f: f["fake_word"]),), "英语单词"),
    # 0.2 不再拦截英文整句（ADR-4），归到通用解析而不是 BLOCK
    ("english_sentence", lambda f: f["en_sentence"], (), "通用解析"),
    ("english_phrase", lambda f: f["is_phrase"], (), "英语短语"),
    ("chinese", lambda f: f["has_cn"],
     # 代码/公式只在「中文不是自然句」时抢走中文：句子里夹一个
     # `f(x) = x^2` 或 `arr.map(x => x*2)` 仍然是中文句解/答疑。
     (("code", lambda f: (f["code_like"] or f["code_block"] or f["is_cli"])
       and not f["cn_prose"]),
      ("formula", lambda f: f["latex_signal"] and not f["cn_prose"])), None),
    ("fallback", lambda f: True, (), "通用解析"),
)


def content_kind_of(classification) -> str | None:
    """NC-001 结案判据：复用 CATEGORY_RULES 胜出的 category/reason。

    cn_paragraph=category=="中文段落"；code_block=category=="代码"
    且 reason∈{code_block,multiline_code_symbol}；latex_formula=category=="LaTeX 公式"
    且 reason=="LaTeX 公式"。其余（含 long_text_code/code_like 弱证据）不放行。
    """
    category = classification.category
    reason = classification.reason
    if category == "中文段落":
        return "cn_paragraph"
    if category == "代码" and reason in {"code_block", "multiline_code_symbol"}:
        return "code_block"
    if category == "LaTeX 公式" and reason == "latex":
        return "latex_formula"
    return None


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
        # 空输入也走同一套「类别必须存在」降级；无可用类别时返回空 id，
        # 绝不伪造一个已删除/停用的 general。
        primary = "通用解析" if "通用解析" in known else next(iter(known), "")
        return Classification(
            category=primary,
            label=_category_label(primary, categories) if primary else "无可用模板",
            reason="empty", candidates=(), trace=())

    fs = extract_features(cleaned, settings.thresholds)
    f = _context(fs, settings.thresholds)
    trace: list[str] = []

    # 图片文字（OCR）回流的识别结果以公式为主（用户：截图大多用于识别公式），
    # 但必须真有公式信号：`known` 只是「已启用的类别」，拿它当判据会把
    # 截图里的表格文字、菜单文字一起塞进公式模板。代码/中文段落仍不强行变。
    vision_formula = (source == "vision" and f["latex_signal"]
                      and "LaTeX 公式" in known
                      and not f["code_form"] and not f["cn_prose"])
    if vision_formula:
        if explain:
            trace.append("L3 [vision_source]：图片输入优先公式类别")
        primary, reason = "LaTeX 公式", "vision_source"
    else:
        primary, reason = "通用解析", "fallback"
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
            primary = CN_SHAPE_CATEGORY.get(f["shape"], "中文词语") \
                if category is None else category
            reason = name
            if explain:
                trace.append(f"L3 [{name}]：胜出 → {primary}")
            break

    # 类别可能被删除/停用：降级到仍存在的类别，绝不返回不存在的选项
    if primary not in known:
        if explain:
            trace.append(f"类别 {primary} 未启用，降级到可用类别")
        primary = "通用解析" if "通用解析" in known else next(iter(known), "")

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
    features = _context(fs, settings.thresholds)
    lines = [f"输入：{(text or '')[:60]!r}",
             "L2 特征：" + ", ".join(
                 f"{k}={features[k]!r}" for k in
                 ("letters", "cn_count", "en_count", "shape",
                  "cn_dominant", "cn_sentence", "latex_signal", "chem_signal",
                  "code_block", "code_shaped", "is_word", "is_phrase",
                  "is_cli", "path_like")),
             *result.trace,
             f"判定类别：{result.category}（{result.label}），规则 {result.reason}",
             "其他可选类别：" + "、".join(c.label for c in result.candidates[:8])]
    return "\n".join(lines)
