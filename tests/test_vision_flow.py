"""图片作为一等模态：开关、菜单、双模态、提示词组装。

不涉及任何网络请求与 Qt：只验证「截图之后发生什么」的纯逻辑。
"""
import unittest
from dataclasses import replace

from clipllm02 import (
    InputEnvelope, build_menu, load_defaults, preflight, suggest,
)
from clipllm02.modality import MemoryAssetStore
from clipllm02.suggest import auto_followup_intent
from clipllm02.studio.vision import (
    compose_vision_prompt, intent_vision_prompt, vision_enabled,
    vision_route_prompt,
)

PNG = b"\x89PNG" + b"0" * 64
IMAGE_INTENTS = ("image.ocr", "image.table", "image.explain", "image.annotate")


def _asset():
    return MemoryAssetStore().put(PNG, "image/png")


class TestVisionToggle(unittest.TestCase):
    def test_factory_default_is_on(self):
        self.assertTrue(vision_enabled(load_defaults()))

    def test_switch_off(self):
        settings = load_defaults()
        settings.data.setdefault("image", {})["vision_enabled"] = False
        self.assertFalse(vision_enabled(settings))


class TestImageMenu(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_defaults()
        cls.asset = _asset()

    def _menu(self, text=None, settings=None):
        settings = settings or self.settings
        env = InputEnvelope.for_image(self.asset, source="screenshot", text=text)
        policy = preflight(env, settings)
        suggestion = suggest(env, policy, settings)
        return env, suggestion, build_menu(suggestion, settings)

    def test_image_envelope_is_not_blocked(self):
        env, _, _ = self._menu()
        self.assertTrue(preflight(env, self.settings).allowed)

    def test_image_input_is_its_own_category(self):
        """纯图片：类别就是「图片识别」，推荐区是它下面的 4 条查询方式。"""
        env, suggestion, menu = self._menu()
        self.assertEqual(env.modality, "image")
        self.assertEqual(menu.category, "图片识别")
        self.assertEqual(menu.category_label, "图片识别")
        ids = [o.intent.id for o in menu.recommended]
        self.assertEqual(ids, list(IMAGE_INTENTS))
        self.assertEqual(menu.default_intent, "image.ocr")
        self.assertTrue(all(o.section != "other" for o in menu.recommended))

    def test_default_query_method_goes_first(self):
        settings = load_defaults()
        settings.data["desktop_vision_intent"] = "image.table"
        _, _, menu = self._menu(settings=settings)
        self.assertEqual(menu.recommended[0].intent.id, "image.table")
        self.assertEqual(menu.default_intent, "image.table")

    def test_mixed_input_stays_image_category(self):
        """双模态：文字只是附加要求，类别仍是「图片识别」，文本类别进抽屉。"""
        env, suggestion, menu = self._menu("这个公式怎么推导？")
        self.assertEqual(env.modality, "mixed")
        self.assertEqual(menu.category, "图片识别")
        self.assertEqual([o.intent.id for o in menu.recommended],
                         list(IMAGE_INTENTS))
        self.assertEqual(menu.default_intent, "image.ocr")
        chip_ids = [c.id for c in menu.category_chips]
        self.assertEqual(chip_ids[0], "图片识别")
        self.assertIn("中文答疑", chip_ids)

    def test_switching_category_drops_image_methods(self):
        """切到别的类别后，不能还看到图片识别里的查询方式。"""
        _, suggestion, _ = self._menu("这个公式怎么推导？")
        menu = build_menu(suggestion, self.settings, category_id="中文词语")
        ids = [o.intent.id for o in menu.recommended]
        self.assertTrue(ids)
        self.assertFalse(any(i.startswith("image.") for i in ids), ids)

    def test_image_envelope_level_is_high(self):
        _, suggestion, _ = self._menu()
        self.assertEqual(suggestion.level, "HIGH")
        self.assertEqual(suggestion.top_domain, "image")


class TestImageIntentShape(unittest.TestCase):
    """图片侧只有一条模板：4 条查询方式挂在同一个「图片识别」下。"""

    def test_four_intents_share_one_route(self):
        settings = load_defaults()
        intents = [i for i in settings.intents
                   if "image" in i.domains and i.enabled]
        self.assertEqual({i.id for i in intents}, set(IMAGE_INTENTS))
        self.assertEqual([i.id for i in sorted(intents, key=lambda i: i.order)],
                         list(IMAGE_INTENTS))
        self.assertEqual({i.primary_route for i in intents}, {"图片识别"})
        self.assertTrue(settings.routes["图片识别"].get("vision"))
        # 公式 / 翻译 / 图表不再各占一条查询方式
        for gone in ("image.formula", "image.translate", "image.chart"):
            self.assertIsNone(settings.intent(gone))


class TestModalityIsolation(unittest.TestCase):
    """图片类别/图片方式只在真的有图时出现；反之亦然。"""

    def setUp(self):
        self.settings = load_defaults()

    def _menu(self, env, **kw):
        return build_menu(suggest(env, preflight(env, self.settings),
                                  self.settings), self.settings, **kw)

    def test_text_input_has_no_image_category(self):
        menu = self._menu(InputEnvelope.for_text("熵"))
        self.assertNotIn("图片识别", [c.id for c in menu.category_chips])

    def test_text_input_cannot_reach_image_methods(self):
        menu = self._menu(InputEnvelope.for_text("熵"), category_id="图片识别")
        ids = [o.intent.id for o in menu.recommended]
        self.assertFalse(any(i.startswith("image.") for i in ids), ids)

    def test_mixed_input_still_offers_text_methods(self):
        env = InputEnvelope.for_image(_asset(), source="screenshot",
                                      text="这个公式怎么推导？")
        menu = self._menu(env, category_id="中文词语")
        self.assertTrue([o for o in menu.recommended
                         if not o.intent.id.startswith("image.")])


class TestFollowupDrivenAutoSubmit(unittest.TestCase):
    """v18：续跑由每条图片方式的 followup_route 决定，全局开关已移除。"""

    def test_legacy_switch_field_is_data_only(self):
        # 出厂字段保留仅为数据兼容；生产代码不再读取它（v18 gate 文件锁定）。
        self.assertFalse(load_defaults().get("image", {}).get("auto_continue"))

    def test_ocr_text_gets_text_default_intent(self):
        settings = load_defaults()
        cases = ((r"\(E = mc^2\)", "LaTeX 公式"),
                 ("这是一段从截图里识别出来的中文说明。", "中文句解"),
                 ("def f(x):\n    return x + 1", "代码"))
        for text, category in cases:
            with self.subTest(text=text[:12]):
                env = InputEnvelope.for_text(text, source="vision")
                menu = build_menu(suggest(env, preflight(env, settings), settings),
                                  settings)
                self.assertEqual(menu.category, category, text)
                self.assertIsNotNone(menu.default_intent, text)
                picked = auto_followup_intent(menu, settings)
                self.assertIsNotNone(picked, text)
                self.assertNotIn("image", settings.intent(picked).domains)

    def test_pure_image_menu_does_not_auto_followup(self):
        """图片菜单的默认项仍是图片方式，不能拿它自动再发一次视觉请求。"""
        settings = load_defaults()
        env = InputEnvelope.for_image(_asset(), source="screenshot")
        menu = build_menu(suggest(env, preflight(env, settings), settings), settings)
        self.assertEqual(menu.default_intent, "image.ocr")
        self.assertIsNone(auto_followup_intent(menu, settings))


class TestVisionPromptComposition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_defaults()

    def test_template_prompt_is_the_fallback(self):
        prompt = vision_route_prompt(self.settings, "图片识别")
        self.assertIn("LaTeX", prompt)

    def test_empty_route_prompt_falls_back(self):
        settings = load_defaults()
        settings.routes["图片识别"]["prompt"] = ""
        settings.data["desktop_vision_prompt"] = "兜底"
        self.assertEqual(vision_route_prompt(settings, "图片识别"), "兜底")

    def test_user_text_leads_the_prompt(self):
        """要求必须排在方式提示词之前，否则小模型会照着方式跑。"""
        composed = compose_vision_prompt("识别这张图", "重点看右下角")
        self.assertTrue(composed.startswith("用户的要求：重点看右下角"))
        self.assertIn("参考格式", composed)
        self.assertLess(composed.index("用户的要求"),
                        composed.index("识别这张图"))

    def test_empty_user_text_keeps_prompt_intact(self):
        self.assertEqual(compose_vision_prompt("识别这张图", "   "), "识别这张图")

    def test_user_text_overrides_output_format(self):
        """有用户要求时，方式的字段/格式契约不能压住它。"""
        composed = compose_vision_prompt("只返回识别结果", "这张图有几个符号")
        self.assertIn("用户的要求：这张图有几个符号", composed)
        self.assertIn("不要只做文字/公式识别", composed)

    def test_user_text_is_prompt_when_template_empty(self):
        self.assertEqual(compose_vision_prompt("", "解释这个公式"), "解释这个公式")

    def test_intent_override_beats_route_prompt(self):
        """查询方式自带 prompt_override 时，不能被所属模板的提示词盖掉。"""
        settings = load_defaults()
        settings.routes["LaTeX 公式"]["prompt"] = "模板提示词"
        intent = next(i for i in settings.intents if i.id == "math.derive")
        prompt = intent_vision_prompt(settings, "LaTeX 公式", intent, "看看这个")
        self.assertNotIn("模板提示词", prompt)
        self.assertIn("用户的要求：看看这个", prompt)

    def test_intent_without_override_uses_route_prompt(self):
        settings = load_defaults()
        settings.routes["图片识别"]["prompt"] = "模板提示词"
        intent = replace(settings.intent("image.ocr"), prompt_override="")
        self.assertEqual(
            intent_vision_prompt(settings, "图片识别", intent), "模板提示词")


if __name__ == "__main__":
    unittest.main()
