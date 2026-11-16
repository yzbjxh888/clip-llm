import unittest

from clipllm02 import (
    InputEnvelope, evaluate_condition, extract_features, feature_catalog,
    intercept_record, load_defaults, preflight,
)


class TestFeatures(unittest.TestCase):
    def test_nacl_is_chem_and_latin_word(self):
        fs = extract_features("NaCl")
        self.assertTrue(fs.get("chem_signal"))
        self.assertTrue(fs.get("is_word"))
        self.assertFalse(fs.get("latex_signal"))
        self.assertFalse(fs.get("code_signal"))

    def test_emc2_is_math_and_physics(self):
        fs = extract_features("E=mc²")
        self.assertTrue(fs.get("latex_signal"))
        self.assertTrue(fs.get("physics_signal"))
        self.assertFalse(fs.get("chem_signal"))
        self.assertFalse(fs.get("code_signal"))

    def test_english_phrase_and_sentence(self):
        self.assertTrue(extract_features("break the ice").get("is_phrase"))
        self.assertFalse(extract_features("break the ice").get("is_word"))
        self.assertTrue(extract_features("break the ice.").get("en_sentence"))
        self.assertFalse(extract_features("break the ice.").get("is_phrase"))

    def test_code_and_filename(self):
        self.assertTrue(extract_features('print("hello")').get("code_signal"))
        self.assertTrue(extract_features("config.py").get("filename"))
        self.assertTrue(extract_features("/usr/bin/python").get("path_like"))

    def test_chinese_structures(self):
        self.assertTrue(extract_features("熵增原理").get("physics_term"))
        self.assertTrue(extract_features("这是一句话。").get("cn_sentence_like"))
        self.assertTrue(extract_features("第一句。第二句。").get("para_like"))

    def test_condition_tree(self):
        fs = extract_features("NaCl")
        self.assertTrue(evaluate_condition(
            {"all": [{"feature": "chem_signal", "op": "eq", "value": True},
                     {"feature": "is_word", "op": "eq", "value": True}]}, fs))
        self.assertTrue(evaluate_condition(
            {"any": [{"feature": "has_cn", "op": "eq", "value": True},
                     {"feature": "chem_signal", "op": "eq", "value": True}]}, fs))
        self.assertTrue(evaluate_condition(
            {"not": {"feature": "has_cn", "op": "eq", "value": True}}, fs))
        self.assertFalse(evaluate_condition(
            {"feature": "no_such_feature", "op": "eq", "value": True}, fs))

    def test_threshold_indirection(self):
        fs = extract_features("tetrafluoroethylene", {"word_max_len": 5})
        self.assertTrue(evaluate_condition(
            {"feature": "letters", "op": "gt", "value": {"setting": "word_max_len"}}, fs))

    def test_catalog_is_complete(self):
        catalog = feature_catalog()
        self.assertIn("latex_signal", catalog)
        self.assertIn("secret_hint", catalog)
        self.assertTrue(all(catalog.values()))


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.settings = load_defaults()

    def _check(self, text, **runtime):
        return preflight(InputEnvelope.for_text(text), self.settings, runtime=runtime)

    def test_empty_is_block(self):
        result = self._check("")
        self.assertEqual(result.level, "BLOCK")
        self.assertFalse(result.allowed)
        self.assertIn("empty", result.block_reasons)

    def test_url_is_blocked(self):
        result = self._check("https://example.com")
        self.assertEqual(result.level, "BLOCK")
        self.assertFalse(result.allowed)
        self.assertIn("url", result.block_reasons)

    def test_filename_is_blocked(self):
        self.assertEqual(self._check("config.py").level, "BLOCK")

    def test_secret_is_blocked(self):
        result = self._check("api_key=abcdef")
        self.assertEqual(result.level, "BLOCK")
        self.assertIn("secret", result.block_reasons)

    def test_english_sentence_not_blocked(self):
        result = self._check("break the ice.")
        self.assertNotIn("english_sentence", result.block_reasons)
        self.assertEqual(result.level, "ALLOW")

    def test_absolute_length_still_blocks(self):
        result = self._check("A" * 1200)
        self.assertEqual(result.level, "BLOCK")
        self.assertIn("too_long", result.block_reasons)

    def test_long_word_is_not_a_gate(self):
        """长度提醒不再弹窗：长单词照常进入初判，只受绝对上限约束。"""
        result = self._check("tetrafluoroethylene")
        self.assertEqual(result.level, "ALLOW")
        self.assertFalse(result.hits)

    def test_quota_blocks(self):
        result = self._check("NaCl", requests_today=300)
        self.assertEqual(result.level, "BLOCK")

    def test_duplicate_is_skipped(self):
        result = self._check("NaCl", is_duplicate=True)
        self.assertTrue(result.skipped)
        self.assertFalse(result.allowed)

    def test_multiline_code_secret_exempt(self):
        text = "def f():\n    password = get_secret()\n    return password"
        result = self._check(text)
        self.assertNotIn("secret", [h.reason for h in result.hits])

    def test_intercept_record_has_no_raw_content(self):
        env = InputEnvelope.for_text("https://secret.example.com/path")
        record = intercept_record(env, self._check(env.text))
        self.assertNotIn("text", record)
        self.assertNotIn("url", record)
        self.assertEqual(record["text_len"], len(env.text))
        self.assertNotIn("secret.example.com", str(record))


if __name__ == "__main__":
    unittest.main()
