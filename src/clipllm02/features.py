"""具名特征注册表。

对应 0.1 的 ``core/features.py``，但把拦截判别、领域信号、预筛选统一到一处，
并导出 ``feature_catalog()`` 供设置页「输入与拦截」只读展示。

所有特征都是**本地纯函数**，不调用任何模型、不产生网络请求。

0.2.7 的四个判定维度（顺序即证据强度）：
1. 符号体制（latex / 代码 / 化学）——结构性证据，最强；
2. 行结构 ``code_score``——多行缩进、注释、行尾分号、括号配平；
3. 标点体制 ``cn_shape``——中文四段派发的唯一权威；
4. 词形合理性 ``word_plausible``——元音比、辅音簇、标识符形态。
长度（``char_count`` / ``letters``）只是过滤器，不参与「是什么」的判定。
"""
from __future__ import annotations

import mimetypes
import re
import shutil
from dataclasses import dataclass
from typing import Any, Callable

from .models import FeatureSet, InputEnvelope

# --------------------------------------------------------------------------
# 判别式（与 0.1 保持一致的命名与语义）
# --------------------------------------------------------------------------
CN_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
EN_CHAR_RE = re.compile(r"[A-Za-z]")
WORD_RE = re.compile(r"^[A-Za-z]+(?:['-][A-Za-z]+)*$")
PHRASE_RE = re.compile(r"^[A-Za-z]+(?: [A-Za-z]+)+$")
EN_SENTENCE_RE = re.compile(r"^[A-Za-z]+(?: [A-Za-z]+)+[.?!]$")
# 只认 ASCII：旧分类器把它放在「纯英文单词」分支里，0.2 曾提升为全局
# detector，导致任何 ≥13 字的中文（不含 aeiouy）都被判成伪词而硬拦。
FAKE_WORD_RE = re.compile(
    r"^([A-Za-z0-9])\1{3,}$|^[b-df-hj-np-tv-xzB-DF-HJ-NP-TV-XZ]{13,}$")
CAMELCASE_RE = re.compile(r"^[a-z]+(?:[A-Z][a-z0-9]*){1,}$")
SNAKE_CASE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+$")
CONST_CASE_RE = re.compile(r"^[A-Z][A-Z0-9]*_[A-Z0-9_]+$")

URL_RE = re.compile(r"^(?:https?|ftp|file)://\S+$", re.I)
URL_IN_TEXT_RE = re.compile(r"(?:https?|ftp|file)://\S+", re.I)
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
PATH_RE = re.compile(r"^(?:/|~/|\.\.?/)\S*$")
# 盘符路径必须「整串」成立且不含汉字：此前用前缀 `^[A-Za-z]:[\\/]`，
# 任何以 C:\ 开头的中文句子都会命中，和分类器的 latex 判定直接打架。
DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/][^\u4e00-\u9fff\n]*$")
BACKSLASH_PATH_RE = re.compile(r"^\\{1,2}[A-Za-z0-9._-]+(?:\\[A-Za-z0-9._-]+)+$")

TOKEN_BLOB_RE = re.compile(r"^[A-Za-z0-9+/=_\-\.]+$")

SECRET_HINT_RE = re.compile(
    r"(?i)(password|passphrase|secret[_-]?key|private[_-]?key|api[_-]?key|"
    r"BEGIN [A-Z ]*PRIVATE KEY|otpauth://|\b\d{6}\b.*(?:otp|token|验证码)|"
    r"\b(?:sk|pk|ghp|gho|xox[bap])[-_][A-Za-z0-9_-]{8,}|\bAKIA[0-9A-Z]{12,})"
)

PRICE_RE = re.compile(r"^[$¥€£]\s?\d[\d,.]*$|^\d[\d,.]*\s?[$¥€£]$")
LATEX_DOLLAR_RE = re.compile(r"\$([^$\n]+)\$")
LATEX_PAREN_RE = re.compile(r"\\[\(\[\]\)]")
LATEX_BEGIN_RE = re.compile(r"\\begin\s*\{[A-Za-z*]+\}")
LATEX_COMMAND_RE = re.compile(r"(?<!\\)\\(?:[A-Za-z]{2,})(?![a-z\\])")
BARE_FORMULA_RE = re.compile(r"[A-Za-z]\w*\s*=.{0,80}[\^_²³¹⁰]|\\[A-Za-z]{2,}")
# `f(x) = x^2` 这类函数式：此前既不像公式也命中 code_like，被判成代码。
FUNC_FORMULA_RE = re.compile(r"[A-Za-z_]\w*\s*\([^)\n]*\)\s*=(?!=)")
# `a^2 + b^2 = c^2`：BARE_FORMULA 的左半边不允许 `^`，左侧带幂次就漏判。
# 只在「有等号」时成立，避免把 `a = b ^ c` 之外的空格写法误伤。
POWER_FORMULA_RE = re.compile(r"[A-Za-z0-9)\]]\^[\dA-Za-z({]")
# `x_{n+1}`：花括号下标是 LaTeX 写法，代码里几乎不会出现
BRACED_SUB_RE = re.compile(r"[A-Za-z0-9)\]]_\{[^}\n]+\}")

# 至少两个元素符号，且含小写或数字（排除 ABC 这类纯大写缩写）。
# 光靠字符形态会把 LaTeX / PhD 判成化学式，所以命中后还要过元素表。
CHEM_RE = re.compile(
    r"(?<![A-Za-z])(?:[A-Z][a-z]?\d*){2,}(?:\([A-Za-z0-9]+\)\d*)?(?:[+-]\d*)?(?![A-Za-z])"
)
ELEMENTS = frozenset("""
H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu
Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs
Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl
Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh
Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og
""".split())

PHYSICS_RE = re.compile(
    r"(?i)(e\s*=\s*mc|f\s*=\s*ma|p\s*=\s*mv|量纲|"
    r"\b(?:joule|newton|tesla|hertz|watt)\b)"
)
PHYSICS_TERM_RE = re.compile(
    r"(熵|焓|动量|量纲|引力|热力学|电磁|波函数|量子|相对论|惯性|压强)"
)

STRONG_CODE_SYMBOLS_RE = re.compile(r"[=;{}[\]<>|\\`~^_]")
CODE_KEYWORDS_RE = re.compile(
    r"\b(def|class|import|from|function|var|const|let|return|if|elif|else|for|"
    r"while|try|except|public|private|static|lambda|print|include|define|"
    r"SELECT|FROM|WHERE|INSERT|UPDATE|DELETE)\b"
)
CN_SENT_END_RE = re.compile(r"[。；！？…]\s*$")
CN_PUNCT_RE = re.compile(r"[。！？…]")
CN_SENT_END_ANY_RE = re.compile(r"[。！？…]")
CN_CLAUSE_RE = re.compile(r"[，、；：]")
FULLWIDTH_PUNCT_RE = re.compile(r"[。！？…，、；：（）【】《》“”‘’—]")
HALFWIDTH_PUNCT_RE = re.compile(r"[,.;:!?()\[\]{}\"'<>|/\\@#$%^&*_+=~`-]")
PREPROCESSOR_RE = re.compile(
    r"^\s*#\s*(?:include|define|undef|if|ifdef|ifndef|elif|else|endif|"
    r"pragma|error|warning|line)\b", re.M)
INDENT_RE = re.compile(r"^(?:\t| {2,})\S")
COMMENT_START_RE = re.compile(r"^\s*(?:#|//|/\*|\*|--|;)")
COMMENT_TAIL_RE = re.compile(r"\s(?:#|//)\s")
TAIL_SYMBOL_RE = re.compile(r"[;{}),]\s*$")
VOWELS = frozenset("aeiouyAEIOUY")
FILE_EXTENSIONS = {
    "py", "js", "ts", "json", "md", "txt", "sh", "yaml", "yml", "toml",
    "ini", "cfg", "conf", "html", "css", "csv", "sql", "go", "rs", "java",
    "c", "h", "cpp", "rb", "php", "xml", "log", "tex", "ipynb", "png", "jpg",
}
CLI_COMMANDS = {"git", "pip", "npm", "docker", "cargo", "uv", "systemctl", "ls", "cat"}

FEATURES: dict[str, Callable[["TextContext"], Any]] = {}
_DESCRIPTIONS: dict[str, str] = {}


@dataclass(frozen=True)
class TextContext:
    text: str
    norm: str
    thresholds: dict[str, Any]

    @property
    def cn_count(self) -> int:
        return len(CN_CHAR_RE.findall(self.text))

    @property
    def en_count(self) -> int:
        return len(EN_CHAR_RE.findall(self.text))

    @property
    def letters(self) -> int:
        """全部文字的字母数（Unicode 级）。

        0.2.6 之前是 ``cn_count + en_count``，只认汉字与 ASCII 字母，于是
        假名 / 西里尔 / 希腊文一律算 0 字母，被空输入闸门静默拦掉。
        """
        return sum(1 for ch in self.text if ch.isalpha())

    @property
    def words(self) -> list[str]:
        return self.text.split()

    @property
    def is_multiline(self) -> bool:
        return "\n" in self.norm

    @property
    def lines(self) -> list[str]:
        return self.norm.splitlines() or [self.norm]

    @property
    def content_lines(self) -> list[str]:
        return [ln for ln in self.lines if ln.strip()]

    def threshold(self, key: str, default: Any) -> Any:
        return self.thresholds.get(key, default)


def feature(name: str, description: str = ""):
    def deco(fn: Callable[[TextContext], Any]):
        if name in FEATURES:
            raise ValueError(f"特征重复注册: {name}")
        FEATURES[name] = fn
        _DESCRIPTIONS[name] = description or (fn.__doc__ or "").strip()
        return fn
    return deco


# -- 长度 / 语言 -----------------------------------------------------------
@feature("char_count", "字符总数（Unicode 码位，含标点、数字、空白）")
def _char_count(c: TextContext) -> int:
    return len(c.text)


@feature("letters", "全部文字的字母数（Unicode 级，含非中英文字）")
def _letters(c: TextContext) -> int:
    return c.letters


@feature("cn_count", "中文字符数")
def _cn_count(c: TextContext) -> int:
    return c.cn_count


@feature("en_count", "英文字母数（仅 ASCII）")
def _en_count(c: TextContext) -> int:
    return c.en_count


@feature("word_count", "空白切分的词数")
def _word_count(c: TextContext) -> int:
    return len(c.words)


@feature("line_count", "非空行数")
def _line_count(c: TextContext) -> int:
    return len(c.content_lines)


@feature("avg_line_len", "非空行平均长度")
def _avg_line_len(c: TextContext) -> float:
    lines = c.content_lines
    return round(sum(len(ln.strip()) for ln in lines) / len(lines), 2) if lines else 0.0


@feature("is_multiline", "是否多行")
def _is_multiline(c: TextContext) -> bool:
    return c.is_multiline


@feature("has_cn", "含中文字符")
def _has_cn(c: TextContext) -> bool:
    return c.cn_count > 0


@feature("has_en", "含英文字母")
def _has_en(c: TextContext) -> bool:
    return c.en_count > 0


@feature("cn_dominant", "中文占主导（cn_dominant_min 且 cn>=en）")
def _cn_dominant(c: TextContext) -> bool:
    minimum = int(c.threshold("cn_dominant_min", 6))
    return c.cn_count >= minimum and c.cn_count >= c.en_count


# -- 标点体制 ---------------------------------------------------------------
@feature("sentence_end_count", "中文句末标点数量（。！？…）")
def _sentence_end_count(c: TextContext) -> int:
    return len(CN_SENT_END_ANY_RE.findall(c.text))


@feature("clause_count", "中文分句标点数量（，、；：）")
def _clause_count(c: TextContext) -> int:
    return len(CN_CLAUSE_RE.findall(c.text))


@feature("fullwidth_punct_ratio", "全角标点占全部标点的比例")
def _fullwidth_punct_ratio(c: TextContext) -> float:
    full = len(FULLWIDTH_PUNCT_RE.findall(c.text))
    half = len(HALFWIDTH_PUNCT_RE.findall(c.text))
    return round(full / (full + half), 3) if (full + half) else 0.0


# -- 行结构 -----------------------------------------------------------------
@feature("indent_ratio", "带缩进的行占比（制表符或 ≥2 空格开头）")
def _indent_ratio(c: TextContext) -> float:
    lines = c.content_lines
    if not lines:
        return 0.0
    return round(sum(1 for ln in lines if INDENT_RE.match(ln)) / len(lines), 3)


@feature("comment_ratio", "注释行占比（行首 # // -- 或行尾 # //）")
def _comment_ratio(c: TextContext) -> float:
    lines = c.content_lines
    if not lines:
        return 0.0
    hit = sum(1 for ln in lines
              if COMMENT_START_RE.match(ln) or COMMENT_TAIL_RE.search(ln))
    return round(hit / len(lines), 3)


@feature("semicolon_tail", "以 ; { } , 收尾的行占比")
def _semicolon_tail(c: TextContext) -> float:
    lines = c.content_lines
    if not lines:
        return 0.0
    return round(sum(1 for ln in lines if TAIL_SYMBOL_RE.search(ln)) / len(lines), 3)


@feature("bracket_balanced", "括号是否配平且出现过")
def _bracket_balanced(c: TextContext) -> bool:
    text = c.norm
    pairs = (("(", ")"), ("[", "]"), ("{", "}"))
    seen = False
    for left, right in pairs:
        if left in text or right in text:
            seen = True
        if text.count(left) != text.count(right):
            return False
    return seen


@feature("code_score", "代码形态加权分（缩进/注释/行尾符号/括号配平）")
def _code_score(c: TextContext) -> float:
    score = (0.35 * _indent_ratio(c) + 0.30 * _comment_ratio(c)
             + 0.25 * _semicolon_tail(c)
             + (0.10 if _bracket_balanced(c) else 0.0))
    return round(score, 3)


@feature("code_shaped", "code_score 达到阈值，判为代码形态")
def _code_shaped(c: TextContext) -> bool:
    return _code_score(c) >= float(c.threshold("code_score_min", 0.5))


# -- 词形 -------------------------------------------------------------------
@feature("vowel_ratio", "ASCII 字母中的元音占比")
def _vowel_ratio(c: TextContext) -> float:
    letters = [ch for ch in c.text if ch.isascii() and ch.isalpha()]
    if not letters:
        return 0.0
    return round(sum(1 for ch in letters if ch in VOWELS) / len(letters), 3)


@feature("max_consonant_run", "最长连续辅音串长度")
def _max_consonant_run(c: TextContext) -> int:
    best = run = 0
    for ch in c.text:
        if ch.isascii() and ch.isalpha() and ch not in VOWELS:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


@feature("is_identifier", "代码标识符形态（驼峰/下划线/常量）")
def _is_identifier(c: TextContext) -> bool:
    text = c.text
    return bool(CAMELCASE_RE.match(text) or SNAKE_CASE_RE.match(text)
                or CONST_CASE_RE.match(text))


@feature("word_plausible", "词形合理（元音比达标且无超长辅音簇）")
def _word_plausible(c: TextContext) -> bool:
    min_ratio = float(c.threshold("vowel_ratio_min", 0.15))
    max_run = int(c.threshold("consonant_run_max", 4))
    if _max_consonant_run(c) > max_run:
        return False
    return _vowel_ratio(c) >= min_ratio or c.letters <= 5


@feature("is_word", "纯英文单词（词形合理且非标识符）")
def _is_word(c: TextContext) -> bool:
    if not WORD_RE.match(c.text):
        return False
    if _is_identifier(c) or not _word_plausible(c):
        return False
    return True


@feature("phrase_form", "多词英文短语形态（不含长度上限）")
def _phrase_form(c: TextContext) -> bool:
    return bool(PHRASE_RE.match(c.text))


@feature("is_phrase", "2 个及以上英文词组成的短语（词数不超过上限）")
def _is_phrase(c: TextContext) -> bool:
    if not PHRASE_RE.match(c.text):
        return False
    return len(c.words) <= int(c.threshold("phrase_max_words", 5))


@feature("en_sentence", "以 .?! 收尾的英文整句")
def _en_sentence(c: TextContext) -> bool:
    return bool(EN_SENTENCE_RE.match(c.text))


@feature("fake_word", "重复字符或无元音超长串")
def _fake_word(c: TextContext) -> bool:
    return bool(FAKE_WORD_RE.match(c.text))


@feature("camel", "驼峰标识符")
def _camel(c: TextContext) -> bool:
    return bool(CAMELCASE_RE.match(c.text))


# -- 中文结构 ---------------------------------------------------------------
@feature("cn_question", "以全角问号收尾")
def _cn_question(c: TextContext) -> bool:
    return c.text.rstrip().endswith("？")


@feature("cn_sentence_like", "带句末标点的中文语句")
def _cn_sentence_like(c: TextContext) -> bool:
    return c.cn_count > 0 and bool(CN_SENT_END_RE.search(c.text))


@feature("para_like", "含两个及以上句末标点的中文段落")
def _para_like(c: TextContext) -> bool:
    return c.cn_count > 0 and len(CN_PUNCT_RE.findall(c.text)) >= 2


def _cn_shape(c: TextContext) -> str | None:
    """中文四段派发的**唯一**实现：疑问 → 段落 → 句解 → 词语。

    classify 与 policy 都只消费下面四个互斥布尔特征，不再各写一套谓词
    （0.2.6 之前 classify 自己另算一遍 cn_dominant / cn_sent_route，两边
    判据不同，UI 上出现「领域是数学、推荐是中文答疑」）。
    """
    if c.cn_count == 0:
        return None
    if _cn_question(c):
        return "question"
    sentences = _sentence_end_count(c)
    clauses = _clause_count(c)
    letters = c.letters
    term_max = int(c.threshold("cn_pure_max", 20))
    sent_max = int(c.threshold("cn_sentence_max", 120))
    min_lines = int(c.threshold("line_para_min_lines", 3))
    min_line_len = float(c.threshold("line_para_min_len", 2))
    if sentences >= 2 and letters > term_max:
        return "paragraph"
    if sentences == 1:
        return "sentence"
    # 无任何句末标点但分行成列表：粘贴的多行中文说明，按段落解析
    if _line_count(c) >= min_lines and _avg_line_len(c) >= min_line_len:
        return "paragraph"
    if letters > sent_max:
        return "paragraph"
    if clauses >= 1 and letters > term_max:
        return "sentence"
    # 中英混排且超长（`https://… 这篇讲什么`）：不是术语，是半截句子。
    if c.en_count > 0 and letters > term_max:
        return "sentence"
    return "term"


@feature("cn_question_shape", "中文结构=疑问句")
def _cn_question_shape(c: TextContext) -> bool:
    return _cn_shape(c) == "question"


@feature("cn_paragraph_shape", "中文结构=段落")
def _cn_paragraph_shape(c: TextContext) -> bool:
    return _cn_shape(c) == "paragraph"


@feature("cn_sentence_shape", "中文结构=句解")
def _cn_sentence_shape(c: TextContext) -> bool:
    return _cn_shape(c) == "sentence"


@feature("cn_term_shape", "中文结构=词语/术语（无句末标点且不构成句段）")
def _cn_term_shape(c: TextContext) -> bool:
    return _cn_shape(c) == "term"


# 兼容旧配置：同一实现的别名，避免两套谓词各自演化。
@feature("cn_sentence_only", "中文陈述句（cn_sentence_shape 的旧名）")
def _cn_sentence_only(c: TextContext) -> bool:
    return _cn_shape(c) == "sentence"


@feature("cn_paragraph_only", "中文段落（cn_paragraph_shape 的旧名）")
def _cn_paragraph_only(c: TextContext) -> bool:
    return _cn_shape(c) == "paragraph"


@feature("cn_term_like", "中文术语（cn_term_shape 的旧名）")
def _cn_term_like(c: TextContext) -> bool:
    return _cn_shape(c) == "term"


# -- 符号体制信号 -----------------------------------------------------------
@feature("latex_strong", "明确的 LaTeX 定界符或命令（不含裸公式猜测）")
def _latex_strong(c: TextContext) -> bool:
    text = c.norm
    if PRICE_RE.match(text) or text.startswith("$PATH"):
        return False
    if _path_like(c):
        return False
    if LATEX_BEGIN_RE.search(text) or LATEX_PAREN_RE.search(text):
        return True
    for match in LATEX_DOLLAR_RE.finditer(text):
        if re.search(r"[=^_\\{}+-]", match.group(1)):
            return True
    return bool(LATEX_COMMAND_RE.search(text))


@feature("latex_leading", "文本以公式/LaTeX 块开头（用于与代码块区分）")
def _latex_leading(c: TextContext) -> bool:
    return bool(re.match(r"^\s*(?:\$|\\begin|\\[A-Za-z]{2,})", c.norm))


@feature("code_form", "结构性代码形态（多行代码块或高代码分）")
def _code_form(c: TextContext) -> bool:
    return _code_block(c) or _code_shaped(c)


@feature("latex_signal", "LaTeX 定界符、命令或裸公式（含上下标）")
def _latex_signal(c: TextContext) -> bool:
    text = c.norm
    if PRICE_RE.match(text) or text.startswith("$PATH"):
        return False
    # 路径不是公式：`C:\Users` 会被 `\command` 规则当成 LaTeX 命令
    if _path_like(c):
        return False
    if _latex_strong(c):
        return True
    # 明确的公式式样优先于代码痕迹：`f(x) = x^2` 里的调用括号不代表代码
    if FUNC_FORMULA_RE.search(text):
        return True
    if POWER_FORMULA_RE.search(text) and "=" in text:
        return True
    if BRACED_SUB_RE.search(text):
        return True
    # 代码形态下「裸公式」证据无效：`std::mem_fn` 的下划线不是数学下标，
    # `std::chrono::…now()` 的调用括号同样不是公式。
    if _code_form(c) or _code_marks(c):
        return False
    return bool(BARE_FORMULA_RE.search(text))


@feature("physics_signal", "物理量纲、物理公式或单位")
def _physics_signal(c: TextContext) -> bool:
    return bool(PHYSICS_RE.search(c.norm))


@feature("physics_term", "中文物理术语")
def _physics_term(c: TextContext) -> bool:
    return bool(PHYSICS_TERM_RE.search(c.text))


@feature("chem_signal", "元素表校验通过的化学式（两个及以上元素符号）")
def _chem_signal(c: TextContext) -> bool:
    for match in CHEM_RE.finditer(c.text):
        token = match.group(0)
        if not re.search(r"[a-z0-9]", token):
            continue
        groups = re.findall(r"[A-Z][a-z]?", token)
        if len(groups) >= 2 and all(g in ELEMENTS for g in groups):
            return True
    return False


# -- 代码 -------------------------------------------------------------------
@feature("strong_sym", "含强代码符号")
def _strong_sym(c: TextContext) -> bool:
    return bool(STRONG_CODE_SYMBOLS_RE.search(c.text))


def _code_marks(c: TextContext) -> bool:
    """结构性代码痕迹（不依赖「强符号计数」这种弱证据）。

    单独抽出来给 ``_latex_signal`` 用：`std::chrono::…now()` 里的 `::` 与
    调用括号是代码证据，足以否掉「`=` 后面有下划线就是公式」的猜测，
    而 `x_i = a_i + b` 这种纯数学下划线不会命中任何一条。
    """
    if _path_like(c):
        return False
    scan = URL_IN_TEXT_RE.sub(" ", c.text)
    if re.search(r"[A-Za-z_]\w*\([^)]*\)", scan):
        return True
    if any(token in scan for token in ("//", "::", "->", "=>", "&&", "||")):
        return True
    if re.search(r"(?:^|\s)--?\w", scan):
        return True
    if CODE_KEYWORDS_RE.search(scan):
        return True
    # 行尾注释 + 代码符号：`if x > 0:  # 判断正数` 单行也应按代码处理
    if COMMENT_TAIL_RE.search(scan) and re.search(r"[=:><{}()\[\];]", scan):
        return True
    return scan.rstrip().endswith((";", "{", "}"))


@feature("code_marks", "结构性代码痕迹（调用/::/关键字/行尾分号）")
def _code_marks_feature(c: TextContext) -> bool:
    return _code_marks(c)


@feature("code_like", "单行代码/命令特征")
def _code_like(c: TextContext) -> bool:
    # 只有「明确的公式定界符」才压过代码痕迹；裸公式猜测不能反向否决代码
    if _latex_strong(c):
        return False
    if _code_marks(c):
        return True
    scan = URL_IN_TEXT_RE.sub(" ", c.text)
    return len(STRONG_CODE_SYMBOLS_RE.findall(scan)) >= 2


@feature("code_block", "多行代码块特征")
def _code_block(c: TextContext) -> bool:
    text = c.norm
    if "\n" not in text:
        return False
    lines = text.splitlines()
    if len(lines) < 2:
        return False
    if _code_shaped(c):
        return True
    # 只有「多数非空行缩进」才算代码块；邮件引用 / Markdown 列表里偶尔
    # 一行缩进不能让整段文本获得敏感规则的多行代码豁免。
    non_empty = [ln for ln in lines if ln.strip()]
    if non_empty and sum(1 for ln in non_empty
                         if re.match(r"^\s+\S", ln)) / len(non_empty) >= 0.6:
        return True
    if CODE_KEYWORDS_RE.search(text):
        return True
    if "{" in text and "}" in text:
        return True
    # 只认 C 风格预处理指令：`## 结论` 这类 Markdown 标题不是代码
    return bool(PREPROCESSOR_RE.search(text) or "//" in text)


@feature("is_cli", "首词是系统 PATH 中真实存在的命令")
def _is_cli(c: TextContext) -> bool:
    words = c.words
    if len(words) < 2:
        return False
    first = words[0].lower()
    if first in {"the", "a", "an", "of", "to", "for", "with"}:
        return False
    return bool(shutil.which(first)) or first in CLI_COMMANDS


@feature("code_signal", "代码/命令综合信号（用于 code 领域判定）")
def _code_signal(c: TextContext) -> bool:
    # 结构性代码优先：多行代码块里出现公式符号仍然算代码
    if FEATURES["code_block"](c) or FEATURES["code_shaped"](c):
        return True
    # 裸公式的 \\ { } ^ 不是代码：否则一行公式会同时点亮 math 与 code 两个领域
    if _latex_signal(c):
        return False
    if FEATURES["code_like"](c) or FEATURES["is_cli"](c):
        return True
    if CODE_KEYWORDS_RE.search(c.text):
        return True
    if re.search(r"[A-Za-z_]\w*\([^)]*\)", c.text):
        return True
    if any(token in c.text for token in ("//", "::", "->", "=>", "&&", "||")):
        return True
    return len(STRONG_CODE_SYMBOLS_RE.findall(c.text)) >= 3


# -- 隐私 / 拦截 ------------------------------------------------------------
@feature("url", "URL 链接")
def _url(c: TextContext) -> bool:
    return bool(URL_RE.match(c.text))


@feature("email", "邮箱地址")
def _email(c: TextContext) -> bool:
    return bool(EMAIL_RE.match(c.text))


@feature("drive", "Windows 盘符路径（整串）")
def _drive(c: TextContext) -> bool:
    return bool(DRIVE_RE.match(c.text))


@feature("path_like", "POSIX / 反斜杠 / 盘符路径（整串）")
def _path_like(c: TextContext) -> bool:
    text = c.text
    return bool(PATH_RE.match(text) or BACKSLASH_PATH_RE.match(text)
                or DRIVE_RE.match(text))


@feature("filename", "带已知后缀且无空格的文件名")
def _filename(c: TextContext) -> bool:
    text = c.text
    if " " in text or re.search(r"[\"'()]", text):
        return False
    if STRONG_CODE_SYMBOLS_RE.search(text):
        return False
    suffix = text.rsplit(".", 1)[-1].lower() if "." in text else ""
    if not suffix:
        return False
    if suffix in FILE_EXTENSIONS:
        return True
    return mimetypes.guess_type(text)[0] is not None


@feature("secret_hint", "疑似密钥/口令/私钥/验证码")
def _secret_hint(c: TextContext) -> bool:
    return bool(SECRET_HINT_RE.search(c.text))


@feature("pem_block", "PEM 私钥块")
def _pem_block(c: TextContext) -> bool:
    return bool(re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", c.text))


# -- 闸门（形状 / 不可解释对象） --------------------------------------------
@feature("no_letters", "没有任何文字（纯数字/标点/符号/emoji）")
def _no_letters(c: TextContext) -> bool:
    return c.letters == 0


@feature("single_latin", "只有一个 ASCII 字母")
def _single_latin(c: TextContext) -> bool:
    text = c.text
    return len(text) == 1 and text.isascii() and text.isalpha()


@feature("token_blob", "像密钥/哈希的长串（单 token、≥32 且含数字）")
def _token_blob(c: TextContext) -> bool:
    text = c.text
    if len(text) < 32 or len(text.split()) != 1:
        return False
    if not TOKEN_BLOB_RE.match(text):
        return False
    return any(ch.isdigit() for ch in text)


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------
def build_context(text: str, thresholds: dict[str, Any] | None = None) -> TextContext:
    """L0 归一化：引号包裹的文本还原字面 \\n \\t，便于识别复制的代码。

    注意 ``text`` 保留原文，``norm`` 只用于特征识别，绝不替换发送给模型的内容。
    """
    cleaned = (text or "").strip()
    norm = cleaned
    if '"' in cleaned or "'" in cleaned:
        norm = (cleaned.replace("\\n", "\n").replace("\\t", "\t")
                .replace("\\r", "\r"))
    return TextContext(text=cleaned, norm=norm, thresholds=thresholds or {})


def extract_features(source: str | InputEnvelope,
                     thresholds: dict[str, Any] | None = None) -> FeatureSet:
    text = source if isinstance(source, str) else (source.text or "")
    ctx = build_context(text, thresholds)
    values: dict[str, Any] = {}
    for name, fn in FEATURES.items():
        try:
            values[name] = fn(ctx)
        except Exception as exc:      # 单个特征出错不拖垮整条链路
            values[name] = None
            values[f"_{name}_error"] = str(exc)
    return FeatureSet(text=ctx.text, norm=ctx.norm,
                      values=values, thresholds=dict(ctx.thresholds))


_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "in": lambda a, b: a in b,
    "contains": lambda a, b: b in a,
    "regex": lambda a, b: bool(re.search(b, str(a))),
}


def evaluate_condition(cond: dict[str, Any] | None, fs: FeatureSet) -> bool:
    """求值条件树；未知特征/非法运算符一律判假，不让规则静默改判。"""
    if not cond:
        return True
    if "all" in cond:
        return all(evaluate_condition(c, fs) for c in cond["all"])
    if "any" in cond:
        return any(evaluate_condition(c, fs) for c in cond["any"])
    if "not" in cond:
        return not evaluate_condition(cond["not"], fs)

    name = cond.get("feature")
    if name not in FEATURES:
        return False
    actual = fs.get(name)
    op = cond.get("op", "eq")
    expected = cond.get("value", True)
    if isinstance(expected, dict) and "setting" in expected:
        expected = fs.thresholds.get(expected["setting"], expected.get("default", 0))
    if op == "truthy":
        return bool(actual)
    opfn = _OPS.get(op)
    if opfn is None or actual is None:
        return False
    try:
        return bool(opfn(actual, expected))
    except (TypeError, ValueError, re.error):
        return False


def feature_catalog() -> dict[str, str]:
    """设置页「输入与拦截」的只读说明。"""
    return {name: _DESCRIPTIONS.get(name, "") for name in sorted(FEATURES)}
