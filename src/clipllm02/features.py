"""具名特征注册表。

对应 0.1 的 ``core/features.py``，但把拦截判别、领域信号、预筛选统一到一处，
并导出 ``feature_catalog()`` 供设置页「输入与拦截」只读展示。

所有特征都是**本地纯函数**，不调用任何模型、不产生网络请求。
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

URL_RE = re.compile(r"^(?:https?|ftp|file)://\S+$", re.I)
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
PATH_RE = re.compile(r"^(?:/|~/|\.\.?/)\S*$")
DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
BACKSLASH_PATH_RE = re.compile(r"^\\{1,2}[A-Za-z0-9._-]+(?:\\[A-Za-z0-9._-]+)+$")

SECRET_HINT_RE = re.compile(
    r"(?i)(password|passphrase|secret[_-]?key|private[_-]?key|api[_-]?key|"
    r"BEGIN [A-Z ]*PRIVATE KEY|otpauth://|\b\d{6}\b.*(?:otp|token|验证码))"
)

PRICE_RE = re.compile(r"^[$¥€£]\s?\d[\d,.]*$|^\d[\d,.]*\s?[$¥€£]$")
LATEX_DOLLAR_RE = re.compile(r"\$([^$\n]+)\$")
LATEX_PAREN_RE = re.compile(r"\\[\(\[\]\)]")
LATEX_BEGIN_RE = re.compile(r"\\begin\s*\{[A-Za-z*]+\}")
LATEX_COMMAND_RE = re.compile(r"(?<!\\)\\(?:[A-Za-z]{2,})(?![a-z\\])")
BARE_FORMULA_RE = re.compile(r"[A-Za-z]\w*\s*=.{0,80}[\^_²³¹⁰]|\\[A-Za-z]{2,}")

# 至少两个元素符号，且含小写或数字（排除 ABC 这类纯大写缩写）
CHEM_RE = re.compile(
    r"(?<![A-Za-z])(?:[A-Z][a-z]?\d*){2,}(?:\([A-Za-z0-9]+\)\d*)?(?:[+-]\d*)?(?![A-Za-z])"
)
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
        return self.cn_count + self.en_count

    @property
    def words(self) -> list[str]:
        return self.text.split()

    @property
    def is_multiline(self) -> bool:
        return "\n" in self.norm


def feature(name: str, description: str = ""):
    def deco(fn: Callable[[TextContext], Any]):
        if name in FEATURES:
            raise ValueError(f"特征重复注册: {name}")
        FEATURES[name] = fn
        _DESCRIPTIONS[name] = description or (fn.__doc__ or "").strip()
        return fn
    return deco


# -- 长度 / 语言 -----------------------------------------------------------
@feature("letters", "中文与英文字母总数")
def _letters(c: TextContext) -> int:
    return c.letters


@feature("cn_count", "中文字符数")
def _cn_count(c: TextContext) -> int:
    return c.cn_count


@feature("en_count", "英文字母数")
def _en_count(c: TextContext) -> int:
    return c.en_count


@feature("word_count", "空白切分的词数")
def _word_count(c: TextContext) -> int:
    return len(c.words)


@feature("is_multiline", "是否多行")
def _is_multiline(c: TextContext) -> bool:
    return c.is_multiline


@feature("has_cn", "含中文字符")
def _has_cn(c: TextContext) -> bool:
    return c.cn_count > 0


@feature("has_en", "含英文字母")
def _has_en(c: TextContext) -> bool:
    return c.en_count > 0


@feature("cn_dominant", "中文占主导（受 cn_dominant_min 阈值约束）")
def _cn_dominant(c: TextContext) -> bool:
    minimum = int(c.thresholds.get("cn_dominant_min", 6))
    return c.cn_count >= minimum and c.cn_count >= c.en_count


# -- 词形 -------------------------------------------------------------------
@feature("is_word", "纯英文单词（允许撇号与连字符）")
def _is_word(c: TextContext) -> bool:
    return bool(WORD_RE.match(c.text))


@feature("is_phrase", "2 个及以上英文词组成的短语")
def _is_phrase(c: TextContext) -> bool:
    return bool(PHRASE_RE.match(c.text))


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


# 旧分类器 L3 的四段派发（疑问 → 段落 → 句解 → 术语）需要互斥的判别特征，
# 否则句子、段落、疑问会共用同一条 20 字闸门而互相冲突。
@feature("cn_sentence_only", "中文陈述句：句末标点收尾，非疑问、非段落")
def _cn_sentence_only(c: TextContext) -> bool:
    return bool(FEATURES["cn_sentence_like"](c)
                and not FEATURES["cn_question"](c)
                and not FEATURES["para_like"](c))


@feature("cn_paragraph_only", "中文段落：多句末标点且非疑问句")
def _cn_paragraph_only(c: TextContext) -> bool:
    return bool(FEATURES["para_like"](c) and not FEATURES["cn_question"](c))


@feature("cn_term_like", "中文术语/词语：无句末标点")
def _cn_term_like(c: TextContext) -> bool:
    return c.cn_count > 0 and not (
        FEATURES["cn_sentence_like"](c)
        or FEATURES["cn_question"](c)
        or FEATURES["para_like"](c))


# -- 符号体制信号 -----------------------------------------------------------
@feature("latex_signal", "LaTeX 定界符、命令或裸公式（含上下标）")
def _latex_signal(c: TextContext) -> bool:
    text = c.norm
    if PRICE_RE.match(text) or text.startswith("$PATH"):
        return False
    if LATEX_BEGIN_RE.search(text) or LATEX_PAREN_RE.search(text):
        return True
    for match in LATEX_DOLLAR_RE.finditer(text):
        if re.search(r"[=^_\\{}+-]", match.group(1)):
            return True
    if LATEX_COMMAND_RE.search(text):
        return True
    return bool(BARE_FORMULA_RE.search(text))


@feature("physics_signal", "物理量纲、物理公式或单位")
def _physics_signal(c: TextContext) -> bool:
    return bool(PHYSICS_RE.search(c.norm))


@feature("physics_term", "中文物理术语")
def _physics_term(c: TextContext) -> bool:
    return bool(PHYSICS_TERM_RE.search(c.text))


@feature("chem_signal", "两个及以上元素符号组合（含小写或数字）")
def _chem_signal(c: TextContext) -> bool:
    for m in CHEM_RE.finditer(c.text):
        if re.search(r"[a-z0-9]", m.group(0)):
            return True
    return False


# -- 代码 -------------------------------------------------------------------
@feature("strong_sym", "含强代码符号")
def _strong_sym(c: TextContext) -> bool:
    return bool(STRONG_CODE_SYMBOLS_RE.search(c.text))


@feature("code_like", "单行代码/命令特征")
def _code_like(c: TextContext) -> bool:
    text = c.text
    if re.search(r"[A-Za-z_]\w*\([^)]*\)", text):
        return True
    if any(token in text for token in ("//", "::", "->", "=>", "&&", "||")):
        return True
    if re.search(r"(?:^|\s)--?\w", text):
        return True
    return len(STRONG_CODE_SYMBOLS_RE.findall(text)) >= 2


@feature("code_block", "多行代码块特征")
def _code_block(c: TextContext) -> bool:
    text = c.norm
    if "\n" not in text:
        return False
    lines = text.splitlines()
    if len(lines) < 2:
        return False
    if any(re.match(r"^\s+\S", ln) for ln in lines):
        return True
    if CODE_KEYWORDS_RE.search(text):
        return True
    if "{" in text and "}" in text:
        return True
    return bool(re.search(r"^\s*#", text, re.M) or "//" in text)


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
    if FEATURES["code_block"](c) or FEATURES["is_cli"](c):
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


@feature("drive", "Windows 盘符路径")
def _drive(c: TextContext) -> bool:
    return bool(DRIVE_RE.match(c.text))


@feature("path_like", "POSIX / 反斜杠 / 盘符路径")
def _path_like(c: TextContext) -> bool:
    return bool(PATH_RE.match(c.text) or BACKSLASH_PATH_RE.match(c.text)
                or DRIVE_RE.match(c.text))


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
