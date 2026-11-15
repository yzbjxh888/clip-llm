"""轻量 LaTeX → Unicode 符号化渲染。

不引入 KaTeX/MathJax（QTextBrowser 禁止 JS），把常见 LaTeX 转成 Unicode：
- 命令：``\\hbar``→ℏ、``\\approx``→≈、``\\mathcal{F}``→ℱ；
- 上下标：``10^{-34}``→10⁻³⁴；无法用 Unicode 表示时回退 ``^(…)``/``_(…)``；
- 环境：``\\begin{vmatrix}…``→|…|、``cases``→{…}；
- 复杂内容递归转换，未识别的命令去反斜杠保留，绝不丢内容。
"""
from __future__ import annotations

import re

SYMBOLS = {
    "hbar": "ℏ", "hslash": "ℏ", "ell": "ℓ", "Re": "ℜ", "Im": "ℑ",
    "approx": "≈", "times": "×", "div": "÷", "pm": "±", "mp": "∓",
    "cdot": "·", "ldots": "…", "cdots": "⋯", "vdots": "⋮", "dots": "…",
    "ddots": "⋱", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "ne": "≠", "equiv": "≡", "sim": "∼", "simeq": "≃",
    "propto": "∝", "infty": "∞", "partial": "∂", "nabla": "∇",
    "sum": "Σ", "prod": "Π", "coprod": "∐", "int": "∫", "oint": "∮",
    "rightarrow": "→", "to": "→", "leftarrow": "←", "gets": "←",
    "Rightarrow": "⇒", "mapsto": "↦", "leftrightarrow": "↔",
    "Leftrightarrow": "⇔", "uparrow": "↑", "downarrow": "↓",
    "degree": "°", "circ": "∘", "bullet": "•", "prime": "′",
    "angle": "∠", "perp": "⊥", "parallel": "∥", "mid": "∣",
    "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆",
    "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩",
    "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃",
    "nexists": "∄", "neg": "¬", "land": "∧", "lor": "∨",
    "langle": "⟨", "rangle": "⟩", "lceil": "⌈", "rceil": "⌉",
    "lfloor": "⌊", "rfloor": "⌋",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π",
    "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ",
    "phi": "φ", "varphi": "ϕ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "ast": "∗", "star": "⋆", "oplus": "⊕", "ominus": "⊖", "otimes": "⊗",
    "vdash": "⊢", "models": "⊨",
    "sin": "sin", "cos": "cos", "tan": "tan", "cot": "cot", "sec": "sec",
    "csc": "csc", "arcsin": "arcsin", "arccos": "arccos", "arctan": "arctan",
    "log": "log", "ln": "ln", "exp": "exp", "lim": "lim",
    "max": "max", "min": "min", "quad": "  ", "qquad": "    ",
    "displaystyle": "", "limits": "", "nolimits": "",
}

# \mathcal / \mathbb 的单字母参数 → 花体/空心字母
SCRIPT_LETTERS = {"F": "ℱ", "L": "ℒ", "R": "ℝ", "Z": "ℤ", "N": "ℕ",
                  "Q": "ℚ", "C": "ℂ", "P": "ℙ", "H": "ℍ", "E": "ℰ",
                  "S": "𝒮", "B": "ℬ", "M": "ℳ", "T": "𝒯", "W": "𝒲"}

SUPERSCRIPTS = {"0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
                "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
                "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
                "n": "ⁿ", "i": "ⁱ", "a": "ᵃ", "b": "ᵇ", "c": "ᶜ",
                "k": "ᵏ", "m": "ᵐ", "t": "ᵗ", "x": "ˣ"}
SUBSCRIPTS = {"0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄",
              "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
              "+": "₊", "-": "₋", "−": "₋", "=": "₌", "(": "₍", ")": "₎",
              "a": "ₐ", "e": "ₑ", "k": "ₖ", "m": "ₘ", "n": "ₙ",
              "x": "ₓ", "i": "ᵢ", "j": "ⱼ"}

ENV_DELIMITERS = {
    "vmatrix": ("|", "|"), "Vmatrix": ("‖", "‖"),
    "pmatrix": ("(", ")"), "bmatrix": ("[", "]"), "Bmatrix": ("{", "}"),
    "matrix": ("[", "]"), "smallmatrix": ("(", ")"),
    "cases": ("{", ""), "array": ("", ""), "aligned": ("", ""),
    "align": ("", ""), "align*": ("", ""), "split": ("", ""),
    "equation": ("", ""), "gather": ("", ""),
}

_ENV_RE = re.compile(r"\\begin\{(\w+\*?)\}([\s\S]*?)\\end\{\1\}", re.S)
_MATHFONT_RE = re.compile(
    r"\\(mathcal|mathbb|mathbf|mathrm|text|operatorname|displaystyle)"
    r"\s*\{([^{}]*)\}")
_SCRIPT_RE = re.compile(r"\^\{([^{}]*)\}|\^(\S)")


# 四种定界符都认：$$...$$ / $...$ / \[...\] / \(...\)
# 后两种是旧提示词（\(...\)）和模型「自作主张」时的写法，必须兼容。
_MATH_DELIM = re.compile(
    r"\$\$([\s\S]+?)\$\$"
    r"|\$([^$\n]+?)\$"
    r"|\\\[([\s\S]+?)\\\]"
    r"|\\\(([^\n]*?)\\\)")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
_MATH_HINT_RE = re.compile(r"[A-Za-z0-9\\^_={}+\-*/<>|]")
_NUMERIC_ONLY_RE = re.compile(r"^[\d\s.,%]+$")
_MATH_OP_RE = re.compile(r"[=^_+*/<>]")


def looks_like_math(body: str) -> bool:
    """判断 ``$...$`` 里的内容是否真的是公式。

    文本里也常出现价格（"价格 $5 与 $10"），不能一律当成公式吞掉美元符号：
    含 LaTeX 命令或数学运算符才算；纯中文片段、纯数值都不算。
    """
    text = body.strip()
    if not text:
        return False
    if _NUMERIC_ONLY_RE.match(text):      # $10$、$3.14$
        return False
    if "\\" in text:                      # \alpha、\frac、\text{中文}
        return True
    if _CJK_RE.search(text):              # 自然语言片段：只有运算符才认
        return bool(_MATH_OP_RE.search(text))
    return bool(_MATH_HINT_RE.search(text))


def strip_math_delimiters(text: str) -> str:
    """去掉**公式**的 ``$`` 定界符并保留内容（纯文本预览用）。

    价格等非公式内容原样保留，避免预览里 ``$5`` 变成 ``5``。
    """

    def repl(match: "re.Match[str]") -> str:
        body = next((g for g in match.groups() if g is not None), "")
        return body if looks_like_math(body) else match.group(0)

    return _MATH_DELIM.sub(repl, text)


def _convert_environments(text: str) -> str:
    """矩阵/行列式等环境 → 定界符包住的单行内容（& 转空格、\\ 转分号）。"""

    def convert(m: "re.Match[str]") -> str:
        env, body = m.group(1), m.group(2).strip()
        left, right = ENV_DELIMITERS.get(env, ("", ""))
        body = body.replace("&", "  ").replace("\\\\", "；")
        body = re.sub(r"\s+", " ", body).strip()
        return f"{left}{body}{right}"

    previous = None
    while previous != text:                      # 嵌套环境逐层转换
        previous = text
        text = _ENV_RE.sub(convert, text)
    return text


def latex_to_unicode(text: str) -> str:
    """把 LaTeX 片段转成 Unicode 数学符号；无 LaTeX 特征时原样返回。"""
    if not text or ("\\" not in text and "^" not in text and "_" not in text):
        return text
    result = text
    # 1. 环境 → 定界符
    result = _convert_environments(result)
    # 2. 间距、\left/\right；\(...\) \[...\] 定界符保留给上层 _apply_math 识别
    result = re.sub(r"\\(?:left|right)\b\s*", "", result)
    result = re.sub(r"\\[,;!\s]", " ", result)
    # 3. \frac{a}{b} / \sqrt{x}——参数允许一层嵌套花括号，内容递归符号化
    prev = None
    while prev != result:
        prev = result
        result = re.sub(
            r"\\frac\s*\{((?:[^{}]|\{[^{}]*\})*)\}\s*"
            r"\{((?:[^{}]|\{[^{}]*\})*)\}",
            lambda m: f"({latex_to_unicode(m.group(1))})/"
                      f"({latex_to_unicode(m.group(2))})",
            result, flags=re.S)
        result = re.sub(r"\\sqrt\s*\{((?:[^{}]|\{[^{}]*\})*)\}",
                        lambda m: "√(" + latex_to_unicode(m.group(1)) + ")",
                        result, flags=re.S)
    # 4. 数学字体剥壳；mathcal/mathbb 单字母 → 花体
    def strip_font(m: "re.Match[str]") -> str:
        cmd, arg = m.group(1), m.group(2).strip()
        if cmd in ("mathcal", "mathbb") and arg in SCRIPT_LETTERS:
            return SCRIPT_LETTERS[arg]
        return arg
    result = _MATHFONT_RE.sub(strip_font, result)
    # 5. \overline / \hat / \vec 等 → 保留内容
    result = re.sub(r"\\(?:overline|bar|hat|vec|tilde)\s*\{([^{}]*)\}",
                    r"\1", result)
    # 6. 上下标：内容先递归符号化；全可映射用上/下标字符，否则 ^(…)/_(…)
    def script(m: "re.Match[str]", table, wrap: str) -> str:
        content = m.group(1) if m.group(1) is not None else m.group(2)
        converted = latex_to_unicode(content)
        if converted and all(c in table for c in converted):
            return "".join(table[c] for c in converted)
        return wrap.format(converted)
    result = _SCRIPT_RE.sub(
        lambda m: script(m, SUPERSCRIPTS, "^({})", ), result)
    result = re.sub(r"_\{([^{}]*)\}|_(\S)",
                    lambda m: script(m, SUBSCRIPTS, "_({})", ), result)
    # 7. \command → Unicode
    result = re.sub(r"\\([A-Za-z]+)",
                    lambda m: SYMBOLS.get(m.group(1), m.group(0)), result)
    # 8. 兜底清理
    result = re.sub(r"\\(?:begin|end)\{[^{}]*\}", "", result)
    result = re.sub(r"\\([{}])", r"\1", result)
    result = result.replace("{", "").replace("}", "")
    return result
