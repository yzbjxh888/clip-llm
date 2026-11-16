"""多语言代码 / 公式语料回归：验证「代码块、LaTeX、通用解析」三者边界。

这批样本是按真实剪贴板内容挑的（C++/Python/JS/Rust/Go/Java/SQL/Shell/
HTML/CSS + 行内/独立/环境/多行公式），既检查分类结果，也检查闸门不会把
可解释内容拦掉、veto 不会把判定打成兜底空洞。
"""
import unittest

from clipllm02 import InputEnvelope, load_defaults, preflight
from clipllm02.classify import classify_text

CPP_BLOCK = """#include <functional>

void add(int a, int b, int& result) {
    result = a + b;
}

int main() {
    int res;
    auto func = std::bind(add, 10, 20, std::ref(res));
    func(); // 执行后res=30
    struct Foo { void bar(int x) {} };
    Foo foo;
    auto mem_func = std::mem_fn(&Foo::bar);
    mem_func(foo, 42);
}
"""

CODE_SAMPLES = {
    "cpp_block": CPP_BLOCK,
    "cpp_chrono_auto":
        "    auto t1 = std::chrono::high_resolution_clock::now();",
    "cpp_template": "template <typename T> T add(T a, T b) { return a + b; }",
    "cpp_class": "class Foo {\npublic:\n    void bar(int x);\nprivate:\n"
                 "    int value_;\n};\n",
    "py_func": "def f(x):\n    return x_1 + 2",
    "py_import": "import numpy as np\narr = np.zeros((3, 3))",
    "py_print": 'print("hello")',
    "py_loop": "for i in range(10):\n    total += i",
    "js_query": "const el = document.querySelector('#app');",
    "js_arrow": "arr.filter(x => x > 0).map(x => x * 2)",
    "rust_vec": "let mut v: Vec<i32> = Vec::new();",
    "go_main": 'func main() {\n\tfmt.Println("hi")\n}',
    "java_class": "public class Main {\n    public static void main("
                  "String[] args) {}\n}",
    "sql_select": "SELECT id, name FROM users WHERE age > 18;",
    "sh_script": "#!/bin/bash\nset -e\necho hi",
    "html_div": '<div class="a">hello</div>',
    "css_body": "body { margin: 0; }",
    "xor_operator": "int a = b ^ c;",
}

FORMULA_SAMPLES = {
    "inline": ("$E = mc^2$", "latex"),
    "frac": (r"\frac{a}{b}", "latex"),
    "display": (r"$$\int_0^1 x^2 dx$$", "latex"),
    "pmatrix": ("\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}", "latex"),
    "align_block": ("\\begin{align}\nx &= a + b \\\\\ny &= c + d\n\\end{align}",
                    "latex"),
    "quadratic": (r"x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}", "latex"),
    "chemistry": (r"2H_2 + O_2 \rightarrow 2H_2O", "latex"),
    "conditional_prob": (r"P(A|B) = \frac{P(B|A)P(A)}{P(B)}", "latex"),
    "cn_inline": (r"速度为 $v = \frac{s}{t}$", "latex"),
    "cn_question": (r"求导 $\frac{dy}{dx}$ 怎么算？", "chinese_question"),
    "power": ("y = x^2 + 1", "latex"),
    "func_formula": ("f(x) = x^2", "latex"),
    "braced_sub": ("x_{n+1} = x_n (1 - x_n)", "latex"),
    "unicode_power": ("E=mc²", "latex"),
}

# 中文句子里夹代码/公式：仍然是中文模板，而不是代码或兜底
CN_WITH_CODE = {
    "当 x 满足 f(x) = x^2 时，函数单调递增。": "chinese_sentence",
    "把 arr.map(x => x * 2) 改成循环。": "chinese_sentence",
    "这段代码 print(1) 是什么意思？": "chinese_question",
    "写一个 class 很难吗？": "chinese_question",
    "git 怎么用？": "chinese_question",
    "## 结论\n\n- 第一点\n- 第二点": "chinese_paragraph",
}


class TestCodeCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_defaults()

    def test_code_is_code_and_never_blocked(self):
        for name, text in CODE_SAMPLES.items():
            with self.subTest(name=name):
                env = InputEnvelope.for_text(text)
                self.assertTrue(preflight(env, self.settings).allowed)
                self.assertEqual(classify_text(text, self.settings).category,
                                 "code")

    def test_no_sample_falls_into_fallback(self):
        """每条样本都必须由某条规则明确接住，而不是「没认出来」。"""
        for name, text in {**CODE_SAMPLES,
                           **{k: v[0] for k, v in FORMULA_SAMPLES.items()}}.items():
            with self.subTest(name=name):
                self.assertNotEqual(classify_text(text, self.settings).reason,
                                    "fallback")


class TestFormulaCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_defaults()

    def test_formulas_match_expected(self):
        for name, (text, expected) in FORMULA_SAMPLES.items():
            with self.subTest(name=name):
                env = InputEnvelope.for_text(text)
                self.assertTrue(preflight(env, self.settings).allowed)
                self.assertEqual(classify_text(text, self.settings).category,
                                 expected)

    def test_code_block_with_latex_string_is_still_code(self):
        text = ('import math\n'
                'formula = r"$\\frac{a}{b}$"\n'
                'print(formula)')
        self.assertEqual(classify_text(text, self.settings).category, "code")


class TestChineseWithCode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_defaults()

    def test_chinese_prose_keeps_chinese_template(self):
        for text, expected in CN_WITH_CODE.items():
            with self.subTest(text=text[:14]):
                self.assertEqual(classify_text(text, self.settings).category,
                                 expected)


if __name__ == "__main__":
    unittest.main()
