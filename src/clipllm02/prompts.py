"""提示词组合与自由输入请求构造。

ADR-2a：主路由 prompt + 其他路由的 section 片段。
ADR-2b：自由输入支持 instruction / prompt 两种模式。
系统输出契约永远 append-only 追加在最后，用户不可覆盖。
追问时用 followup_system() 把契约换成自由作答规则，其余设定原样保留。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .models import IntentOption, UserSelection

CONTRACT_TEMPLATE = r"""

【输出契约（系统自动附加，不可修改）】
1. 只输出一个严格合法的 JSON 对象，禁止代码围栏与任何前后解释。
2. 对象必须包含字段 "query_type"，其值固定为 "{query_type}"。
3. 字段范围：{fields}。不要输出范围之外的字段。
4. 字符串里的反斜杠必须写成 JSON 合法的 \\，例如 \psi 写成 \\psi。
   绝对禁止 \ψ、\Σ、\λ、\( 这类非法转义——它们会让整条结果解析失败，
   用户只能看到原始文本。
5. 数组元素的键只用清单里写明的那些，不要把解释文字塞进额外的键。
"""

FREEFORM_TEMPLATE = """你是内容解释助手，服务于桌面弹窗。
用户想要：{custom_instruction}
输入内容：{content}
参考风格（可选）：{style_hint}
请按用户描述生成解析；严格输出 JSON，至少含 title 与 answer，其余字段自定。
禁止编造依据，不确定处明确标注；不要输出代码围栏或前后解释。
"""

CONTRACT_MARKERS = ("【输出契约", "query_type", "JSON 转义铁律")

FOLLOWUP_MARKER = "【追问模式 · 自由作答】"

GENERIC_TEMPLATE: dict[str, Any] = {
    "prompt": (
        "你是通用解析专家，服务于桌面词典。用户输入一段内容（可能是词、句、段落、公式或截图文字），"
        "读者希望能跨学科、多角度理解它。严格按 JSON 输出，禁止代码围栏。用清晰的中文，"
        "分块给出：它是什么、核心意思、结构或组成、常见用法或例子、必要的背景与来源、"
        "不同视角下的理解与争议。"
    ),
    "fields": [
        "title", "what_is_it", "core_meaning", "structure", "examples",
        "background", "perspectives", "caveats",
    ],
    "sections": {
        "what_is_it": "【它是什么】一句话说清这是什么，它属于哪个领域。",
        "core_meaning": "【核心意思】用小白能懂的话讲清它到底表达什么，避免术语堆砌。",
        "structure": "【结构或组成】如果有内部结构或组成部分，分点列出并说明相互关系。",
        "examples": "【例子】至少两个具体例子，覆盖不同使用场景。",
        "background": "【背景与来源】必要的历史、起源或发展脉络，帮助理解它为什么是这样。",
        "perspectives": "【不同视角】从不同学科、范式或立场给出不同的理解方式。",
        "caveats": "【争议与注意】常见的误解、反对观点或需要注意的边界。",
    },
}

# v8：不再提供空泛占位默认；新增路由一律用完整通用结构化模板。
NEW_TEMPLATE_DEFAULT_PROMPT = GENERIC_TEMPLATE["prompt"]


def new_template_prompt() -> str:
    """新增提示词模板时自动注入的默认提示词（schema.json）。

    v8：返回通用完整模板，调用方可从 GENERIC_TEMPLATE 同时取 fields/sections。
    """
    return str(GENERIC_TEMPLATE["prompt"])


FOLLOWUP_TEMPLATE = f"""{FOLLOWUP_MARKER}（本段优先级最高，覆盖上文任何输出格式要求）
- 你正在回答用户针对上一条解释的追问。对话历史里已有上一轮的输入与完整回答，
  请针对本次追问直接作答，不要重开话题，也不要重复上一轮已说过的内容。
- 用自然的中文段落或简短列表回答，需要时可用 Markdown 标题、列表、表格。
- 不要输出 JSON、字段名清单、代码围栏；忽略上文关于「严格 JSON」「只输出这些字段」
  「输出契约」「字段范围」「转义规则」的全部要求。
- 不确定处明确标注，禁止编造依据。
"""


def followup_system(system: str) -> str:
    """把初次查询的 system 换成追问用的自由作答规则。

    保留上半段（领域、角度、角色、字段语义），只剥掉 compose_prompt /
    freeform_prompt 追加在末尾的输出契约与转义铁律。多轮上下文不在 system
    里，仍由 gateway 的 history 传递，所以这里不会丢上下文。
    多轮追问时 chat_system 已是追问版本，命中标记即原样返回，避免重复追加。
    """
    text = (system or "").strip()
    if FOLLOWUP_MARKER in text:
        return text
    pos = text.find("【输出契约")
    if pos >= 0:
        text = text[:pos].rstrip()
    return (text + "\n\n" + FOLLOWUP_TEMPLATE.strip()).strip()


@dataclass(frozen=True)
class ComposedPrompt:
    prompt: str
    composition_hash: str
    query_type: str
    fields: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def prompt_hash(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()

    def cache_key(self, *, model: str, route_id: str, intent_id: str,
                  format_id: str, schema_version: str, ruleset_version: str,
                  query_hash: str, selection_salt: str = "") -> str:
        parts = [
            self.prompt_hash, self.composition_hash, model, route_id, intent_id,
            format_id, schema_version, selection_salt, ruleset_version, query_hash,
        ]
        return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        return {
            "query_type": self.query_type,
            "fields": list(self.fields),
            "composition_hash": self.composition_hash,
            "prompt_hash": self.prompt_hash,
            "warnings": list(self.warnings),
            "prompt_chars": len(self.prompt),
        }


def composition_hash(includes) -> str:
    blob = json.dumps([dict(i) for i in includes], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _include_graph(settings, intent: IntentOption) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = {}
    for rid, pack in settings.routes.items():
        graph[rid] = [str(inc.get("route_id"))
                      for inc in (pack.get("includes") or []) if inc.get("route_id")]
    if intent.primary_route:
        graph.setdefault(intent.primary_route, [])
        graph["__intent__"] = [intent.primary_route] + [
            str(inc.get("route_id")) for inc in intent.includes if inc.get("route_id")
        ]
    return graph


def detect_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """返回一条循环路径，无环返回 None。"""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GREY
        stack.append(node)
        for nxt in graph.get(node, []):
            if nxt not in color:
                color[nxt] = WHITE
            if color[nxt] == GREY:
                idx = stack.index(nxt) if nxt in stack else 0
                return stack[idx:] + [nxt]
            if color[nxt] == WHITE:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for node in list(graph):
        if color.get(node, WHITE) == WHITE:
            found = visit(node)
            if found:
                return found
    return None


def extract_section(pack: dict, section: str) -> tuple[str, bool]:
    """优先读 sections 字典；否则按 【标题】 从 prompt 正文切分。"""
    sections = pack.get("sections") or {}
    if section in sections:
        return str(sections[section]), True
    prompt = str(pack.get("prompt") or "")
    marker = re.search(r"【[^】]*" + re.escape(section) + r"[^】]*】", prompt, re.I)
    if not marker:
        # 也允许直接用字段名作为标题
        marker = re.search(r"【" + re.escape(section) + r"】", prompt)
    if marker:
        rest = prompt[marker.end():]
        nxt = re.search(r"【[^】]+】", rest)
        return (rest[:nxt.start()] if nxt else rest).strip(), True
    return "", False


def compose_prompt(intent: IntentOption, settings, *, content: str = "") -> ComposedPrompt:
    """把一个 Intent 组装成最终 system prompt。"""
    graph = _include_graph(settings, intent)
    cycle = detect_cycle(graph)
    if cycle:
        raise ValueError(f"提示词组合存在循环引用：{' -> '.join(cycle)}")

    if not intent.primary_route:
        # free_input_only 意图：给出通用兜底，避免调用方拿到异常
        return freeform_prompt("请对输入内容给出通用解释",
                               mode="instruction", settings=settings,
                               content=content)

    primary = settings.routes.get(intent.primary_route)
    if not primary:
        raise ValueError(f"主路由不存在：{intent.primary_route}")
    if not primary.get("enabled", True):
        raise ValueError(f"主路由已禁用：{intent.primary_route}")

    warnings: list[str] = []
    # 意图自带专属提示词 > 用户编辑的模板文案 > 路由出厂提示词。
    # 编辑只写入 route_prompts（下游），绝不回写 routes / B（单向约束）。
    edited_prompt = str(
        (getattr(settings, "route_prompts", {}) or {}).get(intent.primary_route)
        or "")
    main_prompt = (intent.prompt_override.strip()
                   or edited_prompt
                   or str(primary.get("prompt") or ""))
    fragments: list[str] = []
    extra_fields: list[str] = []

    for inc in intent.includes:
        rid = str(inc.get("route_id"))
        pack = settings.routes.get(rid)
        if not pack or not pack.get("enabled", True):
            warnings.append(f"跳过不可用路由 {rid}")
            continue
        label = str(inc.get("as") or rid)
        for section in inc.get("sections") or ():
            text, found = extract_section(pack, str(section))
            if not found:
                warnings.append(f"路由 {rid} 缺少片段 {section}，已跳过")
                continue
            fragments.append(f"【{label} · {section}】\n{text}")
            if section not in extra_fields:
                extra_fields.append(str(section))

    # 本条有专属字段清单时，契约不再重复列模板字段，避免两套清单打架
    if intent.prompt_override.strip():
        fields_text = "以上方列出的字段清单为准"
    else:
        fields_text = ("、".join((primary.get("fields") or []) + extra_fields)
                       or "title、answer")
    contract = CONTRACT_TEMPLATE.format(
        query_type=primary.get("query_type", intent.primary_route),
        fields=fields_text,
    )
    escape_rules = str(settings.get("json_escape_rules", ""))

    # Token 预算：先截片段，绝不截主 prompt 与系统契约
    budget_chars = max(1, int(intent.token_budget)) * 4
    fixed = len(main_prompt) + len(contract) + len(escape_rules)
    while fragments and fixed + sum(len(f) for f in fragments) > budget_chars:
        dropped = fragments.pop()
        warnings.append(f"超出 token 预算，已丢弃片段：{dropped.splitlines()[0]}")
        if dropped:
            extra_fields = [f for f in extra_fields if f not in dropped]

    parts = [main_prompt, *fragments, contract]
    if escape_rules:
        parts.append(escape_rules)
    prompt = "\n".join(p for p in parts if p).strip()

    return ComposedPrompt(
        prompt=prompt,
        composition_hash=composition_hash(intent.includes),
        query_type=str(primary.get("query_type", intent.primary_route)),
        fields=tuple((primary.get("fields") or []) + extra_fields),
        warnings=tuple(warnings),
    )


def freeform_prompt(custom: str, *, mode: str = "instruction",
                    style_ref: str | None = None, settings,
                    content: str = "") -> ComposedPrompt:
    """自由输入：instruction 描述需求，prompt 直接写提示词。"""
    fi = settings.free_input
    limit = int(fi.get("max_prompt_chars", 4000) if mode == "prompt"
                else fi.get("max_chars", 2000))
    custom = (custom or "").strip()
    if len(custom) > limit:
        raise ValueError(f"自由输入超过长度上限 {limit} 字")
    if not custom:
        raise ValueError("自由输入不能为空")

    warnings: list[str] = []
    if mode == "prompt":
        body = custom
        # 用户不得覆盖系统输出契约
        if any(marker in body for marker in CONTRACT_MARKERS):
            body = "\n".join(line for line in body.splitlines()
                             if not any(m in line for m in CONTRACT_MARKERS))
            warnings.append("已剥离用户输入中的输出契约段落")
    else:
        hint = ""
        if style_ref:
            pack = settings.routes.get(style_ref)
            if pack and pack.get("enabled", True):
                hint = str(pack.get("prompt") or "")[:200]
            else:
                warnings.append(f"参考风格路由 {style_ref} 不可用，已忽略")
        body = FREEFORM_TEMPLATE.format(
            custom_instruction=custom,
            content=content or "（见随请求附带的输入内容）",
            style_hint=hint or "无",
        )

    contract = CONTRACT_TEMPLATE.format(query_type="freeform",
                                        fields="title、answer、sections")
    escape_rules = str(settings.get("json_escape_rules", ""))
    parts = [body, contract] + ([escape_rules] if escape_rules else [])
    return ComposedPrompt(
        prompt="\n".join(p for p in parts if p).strip(),
        composition_hash=hashlib.sha256(custom.encode("utf-8")).hexdigest(),
        query_type="freeform",
        fields=("title", "answer", "sections"),
        warnings=tuple(warnings),
    )


def build_request_prompt(selection: UserSelection, settings, *, content: str = ""):
    """Orchestrator 入口：按 UserSelection 选择组合或自由输入路径。"""
    if selection.is_freeform or not selection.intent_id:
        return freeform_prompt(
            selection.custom_instruction or "",
            mode=selection.free_input_mode,
            style_ref=selection.style_ref,
            settings=settings, content=content,
        )
    intent = settings.intent(selection.intent_id)
    if intent is None:
        raise ValueError(f"未知意图：{selection.intent_id}")
    return compose_prompt(intent, settings, content=content)
