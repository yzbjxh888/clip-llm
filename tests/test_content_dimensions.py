"""特种内容判定回归：维度化之后「硬拦截 → 分类 → 模板」三层的行为基线。

覆盖四类真实剪贴板噪音：代码带中文注释、公式夹中文、路径/乱码、
中文长文本无句号。全部为纯本地判定，不产生任何网络请求。
"""
import unittest

from clipllm02 import InputEnvelope, extract_features, load_defaults, preflight
from clipllm02.classify import classify_text
from clipllm02.models import AssetRef


class TestChineseVsCode(unittest.TestCase):
    """字符集是最弱的证据：带中文的代码必须进代码模板。"""

    @classmethod
    def setUpClass(cls):
        cls.settings = load_defaults()

    def test_code_with_chinese_comment_is_code(self):
        cases = [
            'print("你好")',
            "return sum(nums)  # 求和",
            "if x > 0:  # 判断正数",
            "def f(x):\n    return x + 1",
            "for i in range(3):\n    print(i)",
        ]
        for text in cases:
            with self.subTest(text=text[:14]):
                self.assertEqual(
                    classify_text(text, self.settings).category, "代码")

    def test_cli_with_chinese_is_not_chinese_template(self):
        result = classify_text("git commit -m 'fix 中文'", self.settings)
        self.assertNotIn(result.category, ("中文词语", "中文句解"))


class TestCodeVsFormula(unittest.TestCase):
    """代码块、公式、通用解析三者的边界。"""

    @classmethod
    def setUpClass(cls):
        cls.settings = load_defaults()

    CPP = (
        "#include <functional>\n\n"
        "void add(int a, int b, int& result) {\n"
        "    result = a + b;\n}\n\n"
        "int main() {\n"
        "    int res;\n"
        "    auto func = std::bind(add, 10, 20, std::ref(res));\n"
        "    func(); // 执行后res=30\n"
        "    struct Foo { void bar(int x) {} };\n"
        "    Foo foo;\n"
        "    auto mem_func = std::mem_fn(&Foo::bar);\n"
        "    mem_func(foo, 42);\n}\n"
    )

    def test_cpp_with_underscores_is_code(self):
        """`= std::mem_fn` 里的下划线不是数学下标。"""
        fs = extract_features(self.CPP)
        self.assertFalse(fs.get("latex_signal"))
        self.assertTrue(fs.get("code_signal"))
        self.assertEqual(classify_text(self.CPP, self.settings).category, "代码")

    def test_latex_block_starting_with_begin_is_formula(self):
        text = ("\\begin{align}\n"
                "x &= a + b \\\\\n"
                "y &= c + d\n"
                "\\end{align}")
        self.assertEqual(classify_text(text, self.settings).category, "LaTeX 公式")

    def test_latex_string_inside_script_is_code(self):
        text = ('import math\n'
                'formula = r"$\\frac{a}{b}$"\n'
                'print(formula)')
        self.assertEqual(classify_text(text, self.settings).category, "代码")

    def test_code_with_subscript_identifiers_is_code(self):
        self.assertEqual(classify_text("int x_1 = 0;", self.settings).category,
                         "代码")

    def test_standalone_formula_is_not_code(self):
        for text in ("x_{n+1} = x_n (1 - x_n)", "a^2 + b^2 = c^2"):
            with self.subTest(text=text):
                self.assertEqual(classify_text(text, self.settings).category,
                                 "LaTeX 公式")


class TestFormulaAndChemistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_defaults()

    def test_formula_with_chinese_question_is_question(self):
        text = r"求导 $\frac{dy}{dx}$ 怎么算？"
        self.assertEqual(classify_text(text, self.settings).category,
                         "中文答疑")

    def test_formula_with_chinese_caption_is_formula(self):
        text = "a^2 + b^2 = c^2 这是勾股定理"
        self.assertEqual(classify_text(text, self.settings).category, "LaTeX 公式")

    def test_function_formula_is_not_code(self):
        self.assertEqual(classify_text("f(x) = x^2", self.settings).category,
                         "LaTeX 公式")

    def test_camel_words_are_not_chemicals(self):
        for word in ("LaTeX", "PhD"):
            with self.subTest(word=word):
                self.assertFalse(extract_features(word).get("chem_signal"))
                self.assertEqual(classify_text(word, self.settings).category,
                                 "英语单词")

    def test_real_formulas_are_chemicals(self):
        for word in ("NaCl", "H2O"):
            with self.subTest(word=word):
                self.assertEqual(classify_text(word, self.settings).category,
                                 "化学物质")


class TestPathAndNoise(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_defaults()

    def test_windows_path_is_not_latex(self):
        text = r"C:\Users\x\a.txt"
        self.assertFalse(extract_features(text).get("latex_signal"))
        self.assertEqual(classify_text(text, self.settings).category, "通用解析")
        result = preflight(InputEnvelope.for_text(text), self.settings)
        self.assertIn("path", [h.reason for h in result.hits])

    def test_command_with_path_still_explainable(self):
        self.assertEqual(classify_text("cd /usr/local/bin", self.settings).category,
                         "通用解析")

    def test_keyboard_mash_is_not_english_word(self):
        for text in ("asdfghjkl", "abcdefghijklmnop"):
            with self.subTest(text=text):
                self.assertFalse(extract_features(text).get("is_word"))
                self.assertEqual(classify_text(text, self.settings).category,
                                 "通用解析")

    def test_identifier_is_not_english_word(self):
        self.assertFalse(extract_features("getUserName").get("is_word"))
        self.assertEqual(classify_text("getUserName", self.settings).category,
                         "通用解析")

    def test_repeated_chars_not_english_word(self):
        self.assertEqual(classify_text("aaaaaaaaaaaa", self.settings).category,
                         "通用解析")

    def test_acronym_is_still_a_word(self):
        self.assertTrue(extract_features("NASA").get("is_word"))


class TestChineseShapes(unittest.TestCase):
    """中文四段派发只有一个实现：sentence_end / clause / line 三个维度。"""

    @classmethod
    def setUpClass(cls):
        cls.settings = load_defaults()

    def test_four_shapes(self):
        cases = [
            ("熵增原理", "中文词语"),
            ("这是一句话。", "中文句解"),
            ("第一句是定义。第二句给出了形式。第三句说明参数变化。",
             "中文段落"),
            ("为什么天空是蓝色的？", "中文答疑"),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_text(text, self.settings).category,
                                 expected)

    def test_half_selected_clause_is_sentence(self):
        text = "当参数逐渐增大时，迭代结果会从收敛变为周期振荡，再进入混沌"
        self.assertEqual(classify_text(text, self.settings).category,
                         "中文句解")

    def test_multiline_without_punctuation_is_paragraph(self):
        text = "第一行内容\n第二行内容\n第三行内容\n第四行内容\n第五行"
        self.assertTrue(extract_features(text).get("cn_paragraph_shape"))
        self.assertEqual(classify_text(text, self.settings).category,
                         "中文段落")

    def test_length_notices_do_not_block(self):
        """中文软长度上限只提示、不弹窗：一律 ALLOW，只受 1000 字符绝对上限约束。"""
        cases = [
            ("深" * 25, "中文词语"),
            ("深" * 130 + "。", "中文句解"),
            ("深" * 300 + "。" + "深" * 301 + "。", "中文段落"),
            ("深" * 130 + "？", "中文答疑"),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected):
                result = preflight(InputEnvelope.for_text(text), self.settings)
                self.assertEqual(result.level, "ALLOW")
                self.assertFalse(result.hits)
                self.assertEqual(classify_text(text, self.settings).category,
                                 expected)


class TestGateAndRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_defaults()

    def test_sentence_with_url_is_not_code(self):
        text = "https://arxiv.org/abs/2411.01234 这篇讲什么"
        self.assertEqual(classify_text(text, self.settings).category,
                         "中文句解")
        hits = [h.reason for h in
                preflight(InputEnvelope.for_text(text), self.settings).hits]
        self.assertNotIn("chinese_too_long", hits)

    def test_credential_lines_are_blocked(self):
        text = "password = hunter2\nnote = 2"
        result = preflight(InputEnvelope.for_text(text), self.settings)
        self.assertEqual(result.level, "BLOCK")
        self.assertIn("secret", [h.reason for h in result.hits])

    def test_gate_blocks_unexplainable_shapes(self):
        cases = {
            "12345": "no_letters",
            "2026-11-16": "no_letters",
            "$12.50": "no_letters",
            "。。。": "no_letters",
            "a": "single_latin",
            "https://example.com": "url",
            "config.py": "filename",
            "alice@example.com": "email",
            "3f2a9c1b8d4e5f6a7b8c9d0e1f2a3b4c": "token_blob",
            "-----BEGIN RSA PRIVATE KEY-----": "secret",  # ADR-007: pem_block 已并入 secret
        }
        for text, reason in cases.items():
            with self.subTest(reason=reason):
                result = preflight(InputEnvelope.for_text(text), self.settings)
                self.assertEqual(result.level, "BLOCK")
                self.assertTrue(
                    {reason, "no_letters"} & set(result.block_reasons), reason)

    def test_gate_keeps_explainable_shapes(self):
        for text in ("我", "熵", "NaCl", "打开 config.py 看看",
                     "cd /usr/local/bin", "https://arxiv.org/abs/2411 这篇讲什么"):
            with self.subTest(text=text[:12]):
                self.assertTrue(
                    preflight(InputEnvelope.for_text(text), self.settings).allowed)

    def test_multiline_code_secret_still_exempt(self):
        text = "def f():\n    password = get_secret()\n    return password"
        result = preflight(InputEnvelope.for_text(text), self.settings)
        self.assertNotIn("secret", [h.reason for h in result.hits])

    def test_absolute_cap_counts_non_letters(self):
        for text in ("1" * 5000, "A" * 2001):   # ADR-003: 绝对上限 2000
            with self.subTest(kind=text[0]):
                result = preflight(InputEnvelope.for_text(text), self.settings)
                self.assertEqual(result.level, "BLOCK")
                self.assertIn("too_long", result.block_reasons)

    def test_image_envelope_is_not_empty_block(self):
        asset = AssetRef("a" * 16, "image/png", "b" * 16, "memory://a")
        result = preflight(InputEnvelope.for_image(asset), self.settings)
        self.assertTrue(result.allowed)

    def test_letters_count_non_latin(self):
        self.assertEqual(extract_features("こんにちは").get("letters"), 5)
        self.assertEqual(extract_features("Привет").get("letters"), 6)

    def test_legacy_levels_are_migrated(self):
        """旧配置里的 WARN/CONFIRM 与已删除的规则必须能平滑升级。"""
        from clipllm02 import load_settings
        settings = load_settings({"policy": {"rules": {
            "url": {"level": "WARN"},
            "secret": {"level": "CONFIRM"},
            "chinese_too_long": {"level": "WARN"},
            "drive": {"level": "BLOCK"},
        }}})
        self.assertEqual(settings.policy["rules"]["url"]["level"], "BLOCK")
        self.assertEqual(settings.policy["rules"]["secret"]["level"], "BLOCK")
        self.assertNotIn("chinese_too_long", settings.policy["rules"])
        self.assertNotIn("drive", settings.policy["rules"])
        self.assertEqual(set(settings.policy["levels"]), {"BLOCK", "ALLOW"})



CN_PARA = ("第一段说明性文字，用来解释这个概念的定义与适用条件。\n"
           "第二段继续展开论证，给出可核验的要点与反例。\n"
           "第三段总结前提与结论，并指出隐含假设。") * 3
CODE = ("def compute_value(x):\n    if x < 0:\n        return -x\n    return x * 2\n") * 5
LATEX = ("\\begin{align}\nE &= mc^2 \\\\\n\\int_0^1 x^2 \\, dx &= \\frac{1}{3}\n\\end{align}\n") * 3
OTHER = ("break the ice and make a decision about the ordinary meaning "
         "of words in this sentence ") * 2

class TestMidTierRouting(unittest.TestCase):
    """T020/T023/T024/T039: 三类放行不发 state，其余硬拦截且留诊断。"""

    def setUp(self):
        from clipllm02 import InputEnvelope, load_defaults, policy as policy_mod
        self.settings = load_defaults()
        self.policy = policy_mod
        self.env = InputEnvelope.for_text

    def test_allowed_kinds_use_local_classifier_without_state(self):
        fn = getattr(self.policy, "route_mid_text", None)
        self.assertIsNotNone(fn)
        for text, kind in [(CN_PARA[:120], "cn_paragraph"),
                           (CODE[:150], "code_block"),
                           (LATEX[:150], "latex_formula")]:
            with self.subTest(kind=kind):
                r = fn(self.env(text), self.settings)
                self.assertEqual(r.content_kind, kind)
                self.assertIs(r.local_classifier_enabled_for_mid, True)
                self.assertIs(r.state_sent, False)
                self.assertIs(r.hard_block, False)
                self.assertTrue(r.classification_result)

    def test_other_mid_content_is_blocked_silently_with_diagnostic(self):
        fn = getattr(self.policy, "route_mid_text", None)
        self.assertIsNotNone(fn)
        r = fn(self.env(OTHER), self.settings)
        self.assertIs(r.hard_block, True)
        self.assertIs(r.state_sent, False)
        self.assertIs(r.block_suppresses_popup, True)
        self.assertIs(r.block_suppresses_request, True)
        self.assertTrue(r.classification_result)

    def test_over_2000_blocked_with_diagnostic(self):
        fn = getattr(self.policy, "route_mid_text", None)
        self.assertIsNotNone(fn)
        r = fn(self.env("A" * 2001), self.settings)
        self.assertEqual(r.length_tier, "long_over_2000")
        self.assertIs(r.hard_block, True)
        self.assertTrue(r.classification_result)


if __name__ == "__main__":
    unittest.main()
