"""图片识别：一条模板（图片识别）+ 每条查询方式自带一份提示词。

图片侧没有文本那样的分类器，输出契约只有 4 种，所以提示词挂在查询方式上，
模板只留一份兜底；下面锁住这条结构，避免又裂成多条路由。
"""
import unittest
from dataclasses import replace

from clipllm02 import InputEnvelope, load_defaults, load_settings, preflight
from clipllm02.classify import classify_text
from clipllm02.settings import validate
from clipllm02.studio.vision import (
    intent_vision_prompt, vision_prompt, vision_route,
)

IMAGE_INTENTS = ("image.ocr", "image.table", "image.explain", "image.annotate")


class TestVisionPromptResolution(unittest.TestCase):
    def test_only_one_image_route(self):
        settings = load_defaults()
        routes = [rid for rid, pack in settings.routes.items()
                  if pack.get("vision")]
        self.assertEqual(routes, ["image"])
        self.assertEqual(vision_route(settings), "image")

    def test_every_image_intent_carries_a_prompt(self):
        settings = load_defaults()
        for intent_id in IMAGE_INTENTS:
            intent = settings.intent(intent_id)
            self.assertIsNotNone(intent, intent_id)
            self.assertEqual(intent.primary_route, "image", intent_id)
            self.assertTrue(intent.prompt_override.strip(), intent_id)

    def test_image_intents_never_claim_to_reuse_template(self):
        """有专属提示词就不能标「复用模板」——否则与模板内容对不上。"""
        from clipllm02.models import intent_prompt_source
        settings = load_defaults()
        for intent_id in IMAGE_INTENTS:
            raw = next(i for i in settings.as_dict()["intents"]
                       if i.get("id") == intent_id)
            self.assertFalse(raw.get("use_legacy_template"), intent_id)
            self.assertEqual(intent_prompt_source(raw), "专属提示词", intent_id)

    def test_prompt_source_follows_actual_priority(self):
        from clipllm02.models import intent_prompt_source
        self.assertEqual(intent_prompt_source({"prompt_override": "  "}),
                         "复用模板")
        self.assertEqual(intent_prompt_source({"prompt_override": "写点东西"}),
                         "专属提示词")
        self.assertEqual(intent_prompt_source(
            {"prompt_override": "写点东西", "custom": True}), "自定义")

    def test_intent_prompt_wins_over_template(self):
        """查询方式的提示词才是识别行为的唯一来源。"""
        settings = load_defaults()
        settings.routes["image"]["prompt"] = "模板兜底提示词"
        prompt = intent_vision_prompt(settings, "image",
                                      settings.intent("image.table"))
        self.assertIn("表格", prompt)
        self.assertNotIn("模板兜底提示词", prompt)

    def test_intent_without_override_uses_template(self):
        settings = load_defaults()
        settings.routes["image"]["prompt"] = "模板兜底提示词"
        intent = replace(settings.intent("image.table"), prompt_override="")
        self.assertEqual(
            intent_vision_prompt(settings, "image", intent), "模板兜底提示词")

    def test_empty_template_prompt_falls_back_globally(self):
        settings = load_defaults()
        settings.routes["image"]["prompt"] = ""
        settings.data["desktop_vision_prompt"] = "兜底提示词"
        self.assertEqual(vision_prompt(settings)[1], "兜底提示词")

    def test_validate_rejects_unknown_or_text_intent(self):
        for bad in ("no_such_intent", "math.derive"):
            with self.subTest(bad=bad):
                errors = validate(load_settings(
                    {"desktop_vision_intent": bad}))
                self.assertTrue(any(e.field == "desktop_vision_intent"
                                    for e in errors))


class TestFreeformVisionPrompt(unittest.TestCase):
    """图片侧的「自定义」：用户要求是主指令，不是某段通用提示词的附注。"""

    def test_instruction_becomes_primary_instruction(self):
        settings = load_defaults()
        prompt = intent_vision_prompt(settings, "image", None, "解释图里的公式")
        self.assertTrue(prompt.startswith("按下面的要求"))
        self.assertIn("要求：解释图里的公式", prompt)
        self.assertNotIn("请识别图片中的文字和数学公式", prompt)

    def test_prompt_mode_is_used_verbatim(self):
        settings = load_defaults()
        self.assertEqual(
            intent_vision_prompt(settings, "image", None, "只输出图中的数值",
                                 mode="prompt"),
            "只输出图中的数值")

    def test_chosen_intent_still_wins(self):
        settings = load_defaults()
        prompt = intent_vision_prompt(settings, "image",
                                      settings.intent("image.table"),
                                      "重点看第二行")
        self.assertIn("表格", prompt)
        self.assertIn("用户的要求：重点看第二行", prompt)


class TestVisionRouteMigration(unittest.TestCase):
    """老配置（4 条图片路由）升级后：1 条路由、提示词不丢、键改名。"""

    def _legacy_data(self):
        base = load_defaults().as_dict()
        data = {"routes": dict(base["routes"]),
                "intents": [dict(i) for i in base["intents"]],
                "desktop_vision_route": "image_table"}
        data["routes"].pop("image", None)
        data["routes"]["image_ocr"] = {
            "id": "image_ocr", "vision": True, "label": "图片文字",
            "prompt": "我自己改过的 OCR 提示词"}
        data["routes"]["image_table"] = {
            "id": "image_table", "vision": True, "label": "图片表格",
            "prompt": "请提取图片中的表格与数据，还原行列结构与数值。"
                      "严格按 JSON 输出，不要添加解释。"}
        for item in data["intents"]:
            if item.get("id") == "image.ocr":
                item["primary_route"] = "image_ocr"
                item["prompt_override"] = ""
            if item.get("id") == "image.table":
                item["primary_route"] = "image_table"
                item["prompt_override"] = ""
        return data

    def test_old_routes_collapse_and_prompts_survive(self):
        settings = load_settings(self._legacy_data())
        self.assertNotIn("image_ocr", settings.routes)
        self.assertNotIn("image_table", settings.routes)
        self.assertTrue(settings.routes["image"].get("vision"))
        ocr = settings.intent("image.ocr")
        self.assertEqual(ocr.primary_route, "image")
        self.assertIn("我自己改过的", ocr.prompt_override)   # 自定义不能被覆盖
        self.assertIn("表格", settings.intent("image.table").prompt_override)
        self.assertNotIn("desktop_vision_route", settings.data)
        self.assertEqual(settings.data["desktop_vision_intent"], "image.table")


class TestExplainContractMigration(unittest.TestCase):
    """旧版「理解这张图」9 字段契约升级成精简版；用户改过的不动。"""

    def _with_prompt(self, text):
        base = load_defaults().as_dict()
        for item in base["intents"]:
            if item["id"] == "image.explain":
                item["prompt_override"] = text
        return load_settings(base)

    def test_outdated_contract_is_upgraded(self):
        settings = self._with_prompt(
            "请理解并解释这张图片，只输出这些字段（没有内容的字段给空数组或空字符串）：\n"
            "- title：x")
        text = settings.intent("image.explain").prompt_override
        self.assertIn("没有内容就省略整个字段", text)
        self.assertNotIn("没有内容的字段给空数组", text)

    def test_second_generation_contract_is_upgraded_too(self):
        settings = self._with_prompt(
            "请理解并解释这张图片。只输出有内容的字段；没有内容就省略整个字段，"
            "不要输出空值或占位符。\n- type：x")
        self.assertIn("只输出一个 JSON 对象",
                      settings.intent("image.explain").prompt_override)

    def test_custom_contract_is_kept(self):
        settings = self._with_prompt("我自己写的解释契约，只输出这些字段：- a")
        self.assertEqual(settings.intent("image.explain").prompt_override,
                         "我自己写的解释契约，只输出这些字段：- a")


class TestVisionTemplateIsGeneric(unittest.TestCase):
    """模板是类别级兜底：只放跨方式通用的规则，格式要求留在查询方式里。"""

    def test_factory_template_has_no_format_rules(self):
        prompt = load_defaults().routes["image"]["prompt"]
        self.assertIn("LaTeX", prompt)          # 公式表达方式（通用）
        self.assertNotIn("$$", prompt)          # 包裹符是方式级的
        self.assertNotIn("JSON", prompt)        # 输出格式是方式级的

    def test_validate_rejects_empty_prompt_on_both_layers(self):
        from clipllm02.settings import Settings

        data = load_defaults().as_dict()
        data["routes"]["image"]["prompt"] = ""
        for item in data["intents"]:
            if item["id"] == "image.ocr":
                item["prompt_override"] = ""
        errors = validate(Settings(data))
        self.assertTrue(any("没有提示词" in e.message for e in errors))


class TestImageRouteIntegrity(unittest.TestCase):
    """图片方式只能挂在图片模板下；指错地方必须是可修复 + 可报错的。"""

    def _data(self, intent_id: str, **patch):
        base = load_defaults().as_dict()
        for item in base["intents"]:
            if item["id"] == intent_id:
                item.update(patch)
        return base

    def test_wrong_route_is_repaired_on_load(self):
        settings = load_settings(self._data("image.ocr", primary_route="latex"))
        self.assertEqual(settings.intent("image.ocr").primary_route, "image")

    def test_validate_reports_wrong_route(self):
        from clipllm02.settings import Settings

        errors = validate(Settings(self._data("image.ocr",
                                              primary_route="latex")))
        self.assertTrue(any("primary_route" in e.field for e in errors))

    def test_validate_rejects_vision_or_missing_followup(self):
        from clipllm02.settings import Settings

        for bad in ("image", "no_such_route"):
            with self.subTest(bad=bad):
                errors = validate(Settings(self._data(
                    "image.ocr", followup_route=bad)))
                self.assertTrue(any("followup_route" in e.field
                                    for e in errors), bad)


class TestFollowupRouting(unittest.TestCase):
    """图片方式识别完之后怎么继续：直出结果 / 自动判类 / 指定文本模板。"""

    def test_ocr_follows_up_automatically(self):
        settings = load_defaults()
        self.assertEqual(settings.intent("image.ocr").followup_route, "auto")
        for intent_id in ("image.table", "image.explain", "image.annotate"):
            self.assertEqual(settings.intent(intent_id).followup_route, "",
                             intent_id)

    def test_legacy_custom_image_intent_gets_auto_followup(self):
        """没有输出契约的自定义图片方式，识别结果是自由文本，必须回流。"""
        base = load_defaults().as_dict()
        extra = dict(next(i for i in base["intents"] if i["id"] == "image.ocr"))
        extra.update({"id": "custom.1", "label": "公式识别", "custom": True,
                      "prompt_override": "", "followup_route": ""})
        base["intents"] = list(base["intents"]) + [extra]
        settings = load_settings(base)
        self.assertEqual(settings.intent("custom.1").followup_route, "auto")


class TestVisionClassification(unittest.TestCase):
    def _classify(self, text):
        return classify_text(text, load_defaults(), source="vision")

    def test_ocr_formula_prefers_formula(self):
        self.assertEqual(self._classify(r"\(E = mc^2\)").category, "latex")

    def test_ocr_chinese_paragraph_uses_chinese_template(self):
        self.assertEqual(
            self._classify("这是一段从截图里识别出来的中文说明。").category,
            "chinese_sentence")

    def test_non_formula_text_is_not_forced_into_formula(self):
        self.assertNotEqual(self._classify("表头：A B C\n第一行：1 2 3").category,
                            "latex")

    def test_ocr_code_screenshot_is_code(self):
        text = "def f(x):\n    return x + 1"
        self.assertEqual(self._classify(text).category, "code")


class TestImageEnvelopeStillAllowed(unittest.TestCase):
    def test_pure_image_not_blocked(self):
        from clipllm02.models import AssetRef
        settings = load_defaults()
        asset = AssetRef("a" * 16, "image/png", "b" * 16, "memory://a")
        self.assertTrue(preflight(InputEnvelope.for_image(asset),
                                  settings).allowed)


if __name__ == "__main__":
    unittest.main()
