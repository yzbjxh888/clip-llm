"""大模型返回内容的排版：QTextDocument 兼容卡片 + 轻量 Markdown。

``QTextBrowser.setHtml()`` 走的是 Qt Rich Text（HTML4 + CSS2 子集），
**不支持** ``border-radius`` / ``display:inline-block`` / div 上的 padding /
``width:100%`` / 多数类选择器。旧渲染器按浏览器写 CSS，卡片、色条、标签云
在真实窗口里全部退化成无间距的纯文本堆叠——这是排版不美观的根因。

本版只用 Qt 支持的标签，并把关键样式内联到元素上：

- 分节色条：``p`` 上的 ``background-color`` + ``margin`` + ``padding``；
- 表格：``table/th/td``，边框与内边距写在单元格的内联 style；
- 标签：``span`` 上的 ``background-color``（不依赖 inline-block）；
- 卡片：``<div class='card'>`` 只作结构容器，视觉由内部 p 提供。

``<style>`` 保留供未来 Web 渲染器复用，但当前视觉效果不依赖它。
模型返回一律先转义，绝不执行 HTML/JS；JSON 与 Markdown 双通道。
"""
from __future__ import annotations

import html
import json
import re

from ..desktop.gateway import clean_output
from .latex_unicode import latex_to_unicode, looks_like_math
from .theme_tokens import html_theme

# 缓存键的一部分：渲染器结构升级时旧缓存自动失效，避免旧的 HTML 被当 raw 显示。
RENDER_SCHEMA = "studio-2.0"

FIELD_LABELS = {
    "title": "标题", "query_type": "类型", "phonetic": "读音", "meaning": "含义",
    "definitions": "义项", "senses": "义项", "etymology_roots": "词源",
    "phrases": "词组搭配", "examples": "例句", "example": "示例",
    "memory_tip": "记忆钩子", "literal_meaning": "字面义",
    "chinese_equivalent": "中文对应", "usage": "用法与语体",
    "similar": "近义表达", "opposite": "反义表达",
    "common_mistakes": "常见误用", "identification": "辨识",
    "definition": "定义", "usage_criteria": "判定条件",
    "failure_conditions": "失效条件", "neighbors": "邻近概念",
    "operations": "可执行操作", "anchors": "可核验锚点",
    "style": "领域与语体", "structure": "结构", "rhetoric": "修辞",
    "rewrite": "改写", "pitfalls": "易错点", "answer": "回答",
    "key_points": "要点", "keypoint": "要点", "evidence": "依据",
    "caveats": "注意", "thesis": "中心论点", "premises": "前提",
    "premise": "前提", "conclusion": "结论",
    "assumptions": "隐含假设", "assumption": "假设",
    "read_aloud": "读法", "read_as": "读法", "derivation": "推导",
    "behavior": "变化行为", "applications": "应用", "application": "应用",
    "purpose": "用途", "uses": "用途", "inputs": "输入", "outputs": "输出",
    "flow": "执行流程", "composition": "组成", "properties": "性质",
    "safety": "安全", "boxes": "标注框", "rows": "数据行",
    "headers": "表头", "summary": "简要解释", "notes": "备注",
    "note": "说明", "text": "识别文字", "latex": "LaTeX",
    "tables": "表格", "sections": "分节", "components": "组成部分",
    "relations": "关系", "type": "类型", "domain": "领域与定义域",
    "sense_taken": "采用义项", "is_ambiguous": "是否多义",
    "kind": "类别", "related": "关联与辨析", "conditions": "适用条件",
    "explanation": "解释", "part": "部分", "role": "所在位置",
    "symbol": "符号", "description": "描述", "analysis": "分析",
    "comment": "备注", "counterexample": "反例",
    # 查询方式专用字段（模板之外的侧面）
    "symbols": "符号", "reading": "读法", "readings": "念法", "step": "步骤",
    "expression": "表达式", "justification": "依据", "condition": "条件",
    "quantities": "量纲明细", "quantity": "物理量", "dimension": "量纲",
    "unit": "单位", "consistency": "自洽性", "failures": "失效情形",
    "transforms": "等价变形", "form": "形式", "identity": "恒等式",
    "parameters": "参数", "value": "取值", "reason": "理由",
    "calculation": "算式", "result": "结果", "idea": "主旨",
    "analogy": "类比", "implications": "推论", "limits": "边界",
    "general_form": "一般形式", "extra_conditions": "额外条件",
    "special_cases": "特例", "case": "特例", "relation": "关系",
    "item": "对象", "measurement": "测量", "criterion": "判据",
    "experiment": "实验", "method": "方法", "precision": "精度",
    "year": "年份", "status": "状态", "unverifiable": "待考",
    "contrasts": "对照", "focus": "侧重", "register": "语体",
    "collocation": "搭配", "choose_when": "选用条件", "use": "用法",
    "confusions": "易错", "wrong": "错误写法", "right": "正确写法",
    "why": "原因", "lookalikes": "易混词", "word": "词",
    "difference": "差异", "forms": "词形", "tone": "语气",
    "avoid_when": "忌用", "alternatives": "替换", "level": "正式度",
    "phrase": "说法", "context": "语境", "habits": "习惯",
    "scene": "场景", "risk": "风险", "literal_trap": "直译陷阱",
    "correct": "正确译法", "translation": "译法",
    "similar_traps": "同类陷阱", "rewritten": "改写后", "changes": "改动",
    "original": "原句", "revised": "改写", "kept": "保留",
    "annotated": "注释", "line": "代码行", "cases": "用例",
    "input": "输入", "expected": "期望", "framework": "框架",
    "risks": "风险", "impact": "影响", "fix": "修复",
    "severity": "等级", "location": "位置", "time": "时间复杂度",
    "space": "空间复杂度", "worst_case": "最坏情况", "bottlenecks": "瓶颈",
    "mapping": "对应", "source": "原文", "target": "译文",
    "breaks": "失效处", "claim": "说法", "problem": "问题",
    "needed_evidence": "所需证据", "survive": "成立部分",
    "iupac": "IUPAC 名称", "chinese": "中文名", "common": "俗名",
    "stress": "重音", "reactions": "反应", "equation": "方程式",
    "conservation": "守恒核对", "uncertain": "待定", "substance": "物质",
    "property": "性质", "outline": "思路", "pairs": "对照",
    "axes": "坐标轴", "axis": "坐标轴", "range": "范围",
    "trend": "趋势", "turning_points": "拐点", "where": "位置",
    "change": "变化", "anomalies": "异常", "explain": "解释",
    "syllables": "音节", "variants": "变体", "region": "地区",
    "ipa": "音标",
    "interpretation": "释义", "intuition": "直觉理解",
    "background": "背景", "history": "历史沿革", "formula": "公式",
    "result": "结果", "verify": "自查", "check": "核对",
    "steps": "步骤", "method": "方法", "source": "来源",
    "reference": "参考文献", "category": "分类", "value": "取值",
    "unit": "单位", "reason": "原因", "effect": "效果",
    "scope": "适用范围", "limitation": "局限", "context": "语境",
    "difference": "区别", "comparison": "对比", "transform": "变换",
    "inverse": "逆变换", "extension": "推广", "essence": "本质",
    "moment": "矩量", "constant": "常数", "variable": "变量",
    "operator": "算符", "range": "值域",
}

CODE_HINT = re.compile(
    r"^\s*(?:def |class |import |from |SELECT |function |public |private )",
    re.I)

_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_UL_RE = re.compile(r"^\s*[-*+]\s+(.+)$")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.+)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.+)$")
_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
# 模型常把公式写成 `$x$`，反引号会把它包进代码样式：公式优先，先剥壳
_QUOTED_MATH_RE = re.compile(r"`(\$[^`\n]+?)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def _label(key: str) -> str:
    key = str(key)
    if key in FIELD_LABELS:
        return FIELD_LABELS[key]
    if any("\u4e00" <= ch <= "\u9fff" for ch in key):
        return key                      # 中文键原样，别 .title() 折腾
    return key.replace("_", " ").replace("-", " ").strip().title() or "内容"


def _parse_markdown_table(lines: list[str]) -> tuple[list[str], list[list[str]]] | None:
    """识别 markdown 表格（表头 + 分隔行 + 数据行），返回 (表头, 行) 或 None。"""
    for i in range(len(lines) - 1):
        header = lines[i].strip()
        if not header.startswith("|") or not _TABLE_SEP_RE.match(lines[i + 1].strip()):
            continue
        headers = [c.strip() for c in header.strip("|").split("|")]
        rows: list[list[str]] = []
        for j in range(i + 2, len(lines)):
            row = lines[j].strip()
            if not row.startswith("|"):
                break
            rows.append([c.strip() for c in row.strip("|").split("|")])
        if headers and rows:
            return headers, rows
    return None


_MATH_RE = re.compile(
    r"(?<![\\$])\$\$([\s\S]+?)\$\$(?!\$)"      # 块级 $$...$$
    r"|(?<![\\$])\$([^$\n]+?)\$(?!\$)"          # 行内 $...$
    r"|\\\[([\s\S]+?)\\\]"                      # 块级 \[...\]（旧提示词）
    r"|\\\(([^\n]*?)\\\)")                      # 行内 \(...\)（旧提示词）
# 整行只有一个独立公式（Markdown 阶段就要单独成段）
_MATH_BLOCK_LINE_RE = re.compile(
    r"^(?:\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\])\s*$")
# 独占一整段的块级公式：Qt 只让块级元素居中，span 的 text-align 不生效。
# 两个分组都只捕获**定界符内部**的内容，避免整段被替换两次。
_BLOCK_PARA_RE = re.compile(
    r"<p>\s*(?:\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\])\s*</p>")

# 行内 `$` 也常出现在价格里（"价格 $5 与 $10"）。只有内容像公式时才渲染：
# 含反斜杠命令一律算公式；纯中文/日韩文不算；否则要求出现字母或数学符号。
_looks_like_math = looks_like_math      # 判定与预览共用，避免两套规则漂移


def _apply_math(html_text: str, theme: dict) -> str:
    """把四种公式定界符渲染成真实排版。

    支持 ``$$...$$`` / ``\\[...\\]``（独立公式）与 ``$...$`` / ``\\(...\\)``
    （行内公式）。QTextDocument 不能跑 MathJax，这里用「LaTeX→Unicode 符号化
    + 数学斜体」模拟：上下标、希腊字母、分式、根号、矩阵都会转成可读形式。

    独立公式必须落在**块级元素**上才会居中——Qt 会忽略 ``span`` 的
    ``text-align``，把 ``<p>$$...$$</p>`` 直接改成 ``<p align='center'>``。
    所有结果 HTML 在出口统一过这一道，所以 JSON 字段、Markdown、纯文本、
    图片识别转写都能生效。
    """
    font = f"font-family:{theme['font_math']};"
    block_style = (f"{font} font-style:italic; font-size:1.06em; "
                   f"color:{theme['fg']}; margin:8px 0;")
    inline_style = f"{font} font-style:italic; color:{theme['accent']};"

    def convert(body: str) -> str | None:
        """返回转义后的公式内容；None = 不像公式，调用方保留原文。"""
        if not _looks_like_math(body):
            return None                  # 价格等自然语言：原样保留美元符号
        # 输入是已转义的 HTML，先还原再符号化，最后重新转义，
        # 避免 &lt; 被二次转义成 &amp;lt;
        converted = latex_to_unicode(html.unescape(body).strip())
        # quote=False：文本节点不需要转引号，也避免 Qt 对 &#x27; 这类
        # 十六进制实体的支持差异导致显示成字面量
        return html.escape(converted, quote=False) if converted else ""

    def block_para(match: "re.Match[str]") -> str:
        body = next((g for g in match.groups() if g is not None), "")
        escaped = convert(body)
        if escaped is None:
            return match.group(0)
        if not escaped:
            return ""
        return (f"<p align='center' style='{block_style}'>{escaped}</p>")

    html_text = _BLOCK_PARA_RE.sub(block_para, html_text)

    def repl(match: "re.Match[str]") -> str:
        block = match.group(1) is not None or match.group(3) is not None
        body = next((g for g in match.groups() if g is not None), "")
        escaped = convert(body)
        if escaped is None:
            return match.group(0)
        if not escaped:
            return ""
        if block:                        # 混在段落中的独立公式：前后强制换行
            return (f"<br><span style='display:block; text-align:center; "
                    f"{block_style}'>{escaped}</span><br>")
        return f"<span style='{inline_style}'>{escaped}</span>"

    return _MATH_RE.sub(repl, html_text)


def _theme(dark: bool) -> dict:
    """统一取自 theme_tokens；保持下游使用的 bg/card/fg/... 键名不变。"""
    return html_theme(dark)


def _css(theme: dict, font_size: int) -> str:
    """QTextDocument 只支持 Qt Rich Text 子集：这里仅用标签选择器，
    颜色与字体全部来自 token；状态样式一律内联到生成 HTML 的元素上，
    不使用 class 选择器 / border-radius / display:inline-block。
    """
    bg, fg = theme["bg"], theme["fg"]
    accent, chip = theme["accent"], theme["chip"]
    return f"""
    <style>
    body {{ background:{bg}; color:{fg}; font-size:{font_size}px;
            font-family:{theme['font_body']}; }}
    p {{ margin:4px 0; }}
    h1, h2, h3 {{ color:{fg}; margin:8px 0 5px; }}
    table {{ border-collapse:collapse; }}
    th {{ background:{chip}; color:{accent}; text-align:left; }}
    td {{ vertical-align:top; }}
    pre {{ background:{chip}; color:{fg}; }}
    ul, ol {{ margin:4px 0 4px 18px; }}
    li {{ margin:2px 0; }}
    </style>
    """


def _field_head(label: str, theme: dict, font_size: int) -> str:
    """分节标题：只用颜色与粗细区分层级。

    原来用整行底色（``p`` 背景）做色条，在长结果里会连成一片灰块，
    显得拥挤；改成纯文字标题 + 上方留白，页面更干净。
    """
    return (f"<p style=\"color:{theme['accent']}; font-weight:bold; "
            f"font-size:{font_size + 1}px; margin:10px 0 2px;\">"
            f"{html.escape(label, quote=False)}</p>")


def _cell(text: str, theme: dict, *, head: bool = False) -> str:
    tag = "th" if head else "td"
    color = theme["accent"] if head else theme["fg"]
    bg = theme["chip"] if head else theme["card"]
    return (f"<{tag} style=\"background-color:{bg}; color:{color}; "
            f"border:1px solid {theme['line']}; padding:4px 7px; "
            f"vertical-align:top;\">{text}</{tag}>")


def _html_table(headers: list[str], rows: list[list[str]], theme: dict) -> str:
    head = "".join(_cell(html.escape(h, quote=False), theme, head=True) for h in headers)
    body = "".join(
        "<tr>" + "".join(_cell(html.escape(c, quote=False) or "—", theme) for c in row) + "</tr>"
        for row in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def _inline(line: str, theme: dict) -> str:
    """行内 Markdown；输入先转义，绝不把模型文本当 HTML 执行。"""
    text = html.escape(line, quote=False)
    text = _LINK_RE.sub(r"\1", text)            # 链接只保留文字，不生成可点击外链
    text = _QUOTED_MATH_RE.sub(r"\1", text)     # `$x$` → $x$，公式不该被当代码
    text = _INLINE_CODE_RE.sub(
        lambda m: (f"<span style='background-color:{theme['chip']}; "
                   f"font-family:monospace;'>&nbsp;{m.group(1)}&nbsp;</span>"), text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    return text


def _render_markdown(text: str, theme: dict, font_size: int) -> str:
    """轻量 Markdown → QTextDocument 兼容 HTML（标题/列表/表格/引用/代码/段落）。"""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    list_tag: str | None = None
    code: list[str] | None = None

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(_inline(x, theme) for x in para) + "</p>")
            para.clear()

    def flush_list() -> None:
        if list_tag and items:
            out.append(f"<{list_tag}>" + "".join(f"<li>{x}</li>" for x in items)
                       + f"</{list_tag}>")
        items.clear()

    def flush() -> None:
        flush_para()
        flush_list()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush()
            if code is None:
                code = []
            else:
                out.append("<pre>" + html.escape("\n".join(code), quote=False) + "</pre>")
                code = None
            i += 1
            continue
        if code is not None:
            code.append(line)
            i += 1
            continue

        if not stripped:
            flush()
            i += 1
            continue

        # 整行独立公式：必须单独成段，否则段落合并后无法居中
        if _MATH_BLOCK_LINE_RE.match(stripped):
            flush()
            out.append(f"<p>{stripped}</p>")
            i += 1
            continue

        if _TABLE_ROW_RE.match(line) and i + 1 < len(lines) \
                and _TABLE_SEP_RE.match(lines[i + 1].strip()):
            flush()
            block: list[str] = []
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
                block.append(lines[i])
                i += 1
            table = _parse_markdown_table(block)
            if table:
                out.append(_html_table(table[0], table[1], theme))
            else:
                out.append("<p>" + "<br>".join(
                    _inline(x, theme) for x in block) + "</p>")
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            level = min(3, len(heading.group(1)))
            out.append(f"<h{level}>" + _inline(heading.group(2), theme)
                       + f"</h{level}>")
            i += 1
            continue

        if _RULE_RE.match(line):
            flush()
            out.append("<hr>")
            i += 1
            continue

        bullet = _UL_RE.match(line) or _OL_RE.match(line)
        if bullet:
            tag = "ul" if _UL_RE.match(line) else "ol"
            flush_para()
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            items.append(_inline(bullet.group(1), theme))
            i += 1
            continue

        quote = _QUOTE_RE.match(line)
        if quote:
            flush()
            out.append(f"<p style='margin:3px 0 3px 14px; color:{theme['muted']};'>"
                       + _inline(quote.group(1), theme) + "</p>")
            i += 1
            continue

        flush_list()
        para.append(stripped)
        i += 1

    if code is not None:                # 围栏未闭合也不丢内容
        out.append("<pre>" + html.escape("\n".join(code), quote=False) + "</pre>")
    flush()
    return "".join(out)


def _render_text_block(text: str, theme: dict, font_size: int) -> str:
    """多行纯文本：优先 Markdown 结构化渲染。"""
    table = _parse_markdown_table(text.split("\n"))
    if table is not None:
        return _html_table(table[0], table[1], theme)
    return _render_markdown(text, theme, font_size)


def _looks_like_code(text: str) -> bool:
    if _MATH_BLOCK_LINE_RE.match(text.strip()):
        return False          # 独占一行的 \begin{pmatrix}... 是公式，不是代码
    # 公式里的 \begin{pmatrix} 不算代码：先剔除所有公式再判断
    if "\\begin{" in _MATH_RE.sub("", text):
        return True
    lines = text.splitlines()
    return len(lines) >= 2 and bool(CODE_HINT.match(lines[0]))


def _scalar(value, theme: dict, font_size: int) -> str:
    if value is None:
        return f"<span style='color:{theme['muted']};'>—</span>"
    if isinstance(value, bool):
        return "是" if value else "否"
    raw = str(value)
    if _looks_like_code(raw):
        return f"<pre style='background-color:{theme['chip']};'>{html.escape(raw, quote=False)}</pre>"
    text = latex_to_unicode(raw)
    if "\n" in text:
        return _render_text_block(text, theme, font_size)
    # 整个字段只有一条独立公式：包成段落，让 _apply_math 走居中分支
    if _MATH_BLOCK_LINE_RE.match(text.strip()):
        return f"<p>{text.strip()}</p>"
    return _inline(text, theme)


def _table(rows: list[dict], font_size: int, theme: dict) -> str:
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    if not keys or len(keys) > 8:
        return "".join(
            "<div class='card'>"
            + _render_value("", r, font_size, 1, theme) + "</div>" for r in rows)
    head = "".join(_cell(html.escape(_label(k), quote=False), theme, head=True) for k in keys)
    body = "".join(
        "<tr>" + "".join(
            _cell(_render_value(k, row.get(k), font_size, 2, theme), theme)
            for k in keys) + "</tr>" for row in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def _render_value(key: str, value, font_size: int, depth: int,
                  theme: dict) -> str:
    if isinstance(value, dict):
        parts = [
            f"<p style='margin:2px 0;'><b style='color:{theme['accent']}'>"
            f"{html.escape(_label(str(sub_key)), quote=False)}</b>："
            f"{_render_value(str(sub_key), sub_value, font_size, depth + 1, theme)}</p>"
            for sub_key, sub_value in value.items()]
        return "".join(parts) if depth > 0 else "".join(parts)
    if isinstance(value, list):
        if not value:
            return f"<span style='color:{theme['muted']};'>—</span>"
        if all(isinstance(item, dict) for item in value):
            return _table(value, font_size, theme)
        if all(not isinstance(item, (dict, list)) for item in value) \
                and all(len(str(item)) <= 24 for item in value):
            return " ".join(
                f"<span style='background-color:{theme['chip']}; "
                f"color:{theme['accent']}; padding:1px 6px;'>&nbsp;"
                f"{html.escape(latex_to_unicode(str(item)), quote=False)}&nbsp;</span>"
                for item in value[:24])
        items = "".join(
            f"<li>{_render_value('', item, font_size, depth + 1, theme)}</li>"
            for item in value[:24])
        return f"<ul>{items}</ul>"
    return _scalar(value, theme, font_size)


def _extract_json(text: str):
    """从任意返回里找 JSON：完整解析失败时扫描平衡括号，容忍前后解释文字。

    返回 ``(data, (prefix, suffix) | None)``；找不到返回 ``(None, None)``。
    """
    cleaned = clean_output(text)
    try:
        return json.loads(cleaned), None
    except (ValueError, TypeError):
        pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        attempts = 0
        while start >= 0 and attempts < 8:
            attempts += 1
            depth, in_str, escaped = 0, False, False
            for i in range(start, len(cleaned)):
                ch = cleaned[i]
                if in_str:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return (json.loads(cleaned[start:i + 1]),
                                    (cleaned[:start], cleaned[i + 1:]))
                        except (ValueError, TypeError):
                            break
            start = cleaned.find(opener, start + 1)
    return None, None


def _render_data(data, theme: dict, font_size: int, badge: str) -> list[str]:
    out: list[str] = []
    if isinstance(data, dict):
        tag = badge or str(data.get("query_type") or "")
        title = latex_to_unicode(str(data.get("title") or "")).strip()
        if title and len(title) <= 60:
            if _MATH_BLOCK_LINE_RE.match(title):
                # 标题本身就是一条独立公式：不套 <h3>，交给 _apply_math 居中
                out.append(f"<p>{html.escape(title, quote=False)}</p>")
            else:
                out.append(f"<h3>{html.escape(title, quote=False)}</h3>")
            skip = {"title", "query_type"}
        else:
            out.append("<h3>解释结果</h3>")
            skip = {"query_type"}       # 超长 title 按正文渲染，不再顶着「标题」标签
        if tag:
            out.append(
                f"<p style='margin:0 0 8px;'><span style='"
                f"background-color:{theme['chip']}; color:{theme['accent']}; "
                f"padding:2px 8px;'>{html.escape(tag, quote=False)}</span></p>")
        for key, value in data.items():
            if key in skip:
                continue
            body = _render_value(str(key), value, font_size, 0, theme)
            if key == "title":
                out.append("<div class='card'>" + body + "</div>")
                continue
            out.append(
                "<div class='card'>" + _field_head(_label(str(key)), theme, font_size)
                + f"<div style='margin:0 0 8px 8px;'>{body}</div></div>")
    else:
        out.append("<h3>解释结果</h3>")
        out.append("<div class='card'>"
                   + _render_value("", data, font_size, 0, theme) + "</div>")
    return out


def render_json_result(raw_text: str, *, font_size: int = 14,
                       dark: bool = False, badge: str = "") -> str:
    """把模型返回的任意内容渲染成完整卡片 HTML。"""
    theme = _theme(dark)
    css = _css(theme, font_size)
    cleaned = clean_output(raw_text or "")
    if not cleaned:
        return (css + f"<p style='background-color:{theme['warning_container']}; "
                f"color:{theme['warning_text']}; padding:6px 9px;'>"
                "模型未返回内容</p>")

    data, fringes = _extract_json(cleaned)
    out = [css]
    if data is not None:
        out.extend(_render_data(data, theme, font_size, badge))
    else:
        out.append("<h3>解释结果</h3>")
        out.append("<div class='card'>"
                   + _render_markdown(cleaned, theme, font_size) + "</div>")
    if fringes:
        # LaTeX 反斜杠常让 JSON 半截解析成功，剩下的碎片是原始 JSON 而不是
        # 解释文字；这类残留不贴给用户，避免结果里混进一整段源码。
        visible = [x.strip(" \n```") for x in fringes if x.strip(" \n```")]
        visible = [x for x in visible
                   if '"query_type"' not in x
                   and not x.lstrip().startswith(("{", "["))]
        note = " ".join(visible)
        if note:
            out.append(
                f"<p style='color:{theme['muted']}; "
                f"font-size:{max(9, font_size - 2)}px;'>"
                f"{html.escape(note[:300], quote=False)}</p>")
    return _apply_math("\n".join(out), theme)


def render_notice(title: str, body_html: str, *, font_size: int = 14,
                  dark: bool = False) -> str:
    theme = _theme(dark)
    return _apply_math(
        _css(theme, font_size) + f"<h3>{html.escape(title, quote=False)}</h3>"
        + f"<div class='card'>{body_html}</div>", theme)


def render_local_preview(composed, content: str, *, font_size: int = 14,
                         dark: bool = False, reason: str = "未配置模型") -> str:
    """无 API Key 时的第二层预览：展示提示词与字段，明确未发送网络请求。"""
    theme = _theme(dark)
    fields = "、".join(html.escape(str(f), quote=False) for f in composed.fields[:14])
    head = (f"<p style='background-color:{theme['chip']}; "
            f"color:{theme['accent']}; font-weight:bold; margin:6px 0 2px; "
            f"padding:3px 9px;'>")
    body = (
        f"<p style='background-color:{theme['warning_container']}; "
        f"color:{theme['warning_text']}; "
        f"padding:6px 9px;'><b>{html.escape(reason, quote=False)}</b>：本次只组装请求，"
        "未发送任何网络请求。</p>"
        f"{head}query_type</p><p>{html.escape(composed.query_type, quote=False)}</p>"
        f"{head}输出字段</p><p>{fields}</p>"
        f"{head}提示词（可在设置里修改）</p>"
        f"<pre style='background-color:{theme['chip']};'>"
        f"{html.escape(composed.prompt[:1400], quote=False)}</pre>"
        f"<p style='color:{theme['muted']};'>"
        f"输入：{html.escape((content or '')[:120], quote=False)}</p>"
    )
    return render_notice("查询预览", body, font_size=font_size, dark=dark)
