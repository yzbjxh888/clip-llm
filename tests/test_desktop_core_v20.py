"""票据 36 拆分保留：原 tests/test_desktop_fixes.py 中的非 classic 用例。

覆盖：剪贴板读取命令、按领域给不同意图、LOW 领域选项、配置迁移。
原文件里的 MenuWindow / ResultWindow / DesktopApp 用例随 classic 一并删除。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import (  # noqa: E402
    InputEnvelope, build_menu, load_defaults, preflight, suggest,
)
from clipllm02.studio.config import CONFIG_VERSION, load_config  # noqa: E402
from clipllm02.prompts import compose_prompt  # noqa: E402
from clipllm02.serve import ClipboardReader  # noqa: E402


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def make_context(text="NaCl"):
    settings = load_defaults()
    env = InputEnvelope.for_text(text, source="test")
    policy = preflight(env, settings)
    s = suggest(env, policy, settings)
    menu = build_menu(s, settings)
    return settings, env, policy, s, menu


class Issue2Performance(unittest.TestCase):
    def test_reader_tries_all_available_commands(self):
        commands = ClipboardReader().commands()
        self.assertTrue(commands)
        self.assertTrue(all(c[0] in {"wl-paste", "xclip"} for c in commands))


class Issue3DomainSpecificIntents(unittest.TestCase):
    def test_chem_menu_is_chem_specific(self):
        settings, _, _, _, menu = make_context("NaCl")
        ids = [o.intent.id for o in menu.recommended]
        self.assertTrue(ids)
        self.assertTrue(all(i.startswith("chem.") for i in ids), ids)
        self.assertNotIn("general.explain", ids)

    def test_phrase_menu_is_phrase_specific(self):
        _, _, _, _, menu = make_context("break the ice")
        labels = [o.intent.label for o in menu.recommended]
        self.assertTrue(labels)
        self.assertTrue(any("词组" in label or "语气" in label for label in labels))
        self.assertNotIn("通用解释", labels)

    def test_math_menu_has_interactive(self):
        _, _, _, _, menu = make_context("E=mc²")
        ids = [o.intent.id for o in menu.recommended]
        # 同一 latex 模板下也挂着物理领域的查询方式；设置里有多少条就显示多少条
        self.assertTrue(any(i.startswith("math.") for i in ids), ids)
        self.assertGreater(len(ids), 9)   # 不再被旧的 9 项上限截断
        self.assertTrue(any(o.intent.default_format == "table"
                            for o in menu.recommended))

    def test_each_intent_has_its_own_prompt(self):
        settings = load_defaults()
        a = compose_prompt(settings.intent("chem.naming"), settings).prompt
        b = compose_prompt(settings.intent("chem.compare"), settings).prompt
        self.assertNotEqual(a, b)
        self.assertIn("IUPAC", a)
        self.assertIn("区分", b)


class Issue5AutomaticWithOptions(unittest.TestCase):
    def test_low_level_auto_menu_keeps_domain_options(self):
        _, _, _, _, menu = make_context("12345")
        self.assertEqual(menu.level, "LOW")
        self.assertTrue(menu.recommended)
        self.assertGreater(len(menu.domain_chips), 1)
        self.assertTrue(menu.free_input["enabled"])


class ConfigUpgrade(unittest.TestCase):
    def test_stale_intents_refreshed_but_user_settings_kept(self):
        # 原用例经 desktop.settings_window.load_user_settings() 调用；
        # 该模块随 classic 删除，其实现就是 studio.config.load_config()，直接调用同源函数。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            data = load_defaults().as_dict()
            data["config_version"] = 1
            data["intents"] = [{
                "id": "stale", "label": "旧模板", "domains": ["*"],
                "modalities": ["text"], "primary_route": "general",
                "enabled": True,
            }]
            data["desktop_api_url"] = "https://example.com/v1/chat/completions"
            path.write_text(json.dumps(data), encoding="utf-8")
            with patch("clipllm02.studio.config.config_path", return_value=path):
                settings = load_config()
            self.assertEqual(settings.get("config_version"), CONFIG_VERSION)
            self.assertIsNotNone(settings.intent("chem.naming"))
            self.assertIsNone(settings.intent("stale"))
            self.assertEqual(settings.get("desktop_api_url"),
                             "https://example.com/v1/chat/completions")


if __name__ == "__main__":
    unittest.main()
