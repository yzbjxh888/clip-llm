"""Ticket 25 gates (v9): per-route factory template restore + disk migration.

RED first: the polluted user config keeps "这是什么?" in every factory route's
prompt / route_prompts, so the model receives an empty system instruction.
All expected literals are hand-derived from CHANGE-generic-template-v9.md
section 5 and spec/builtin_settings.json (the authority), not from the
implementation under test. No real network calls; Qt runs offscreen.
"""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel,  # noqa: E402
                               QPlainTextEdit, QPushButton, QRadioButton)

from clipllm02 import load_defaults  # noqa: E402
from clipllm02 import settings as settings_mod  # noqa: E402
from clipllm02.desktop import gateway as gateway_mod  # noqa: E402
from clipllm02.prompts import compose_prompt  # noqa: E402
from clipllm02.studio import config as config_mod  # noqa: E402
from clipllm02.studio.settings_dialog import SettingsDialog  # noqa: E402
from tests._json_editor_v10 import set_editor_prompt  # noqa: E402

# unicode-escaped so this file (and production) never carries the retired
# default literal verbatim; ticket 24's grep gate scans production sources.
POLLUTED = "\u8fd9\u662f\u4ec0\u4e48?"
USER_WORD = "用户自己改过的中文词语提示XYZ"

FACTORY_IDS = [
    "英语单词", "英语短语", "中文词语", "中文句解", "中文答疑", "中文段落",
    "LaTeX 公式", "代码", "通用解析", "化学物质", "图片识别",
]

# v9 section 5 / spec/builtin_settings.json, each route keeps ITS OWN content.
EN_PROMPT = (
    "你是英语词汇专家，服务于桌面词典弹窗。用户输入一个英语单词，读者是中文母语学习者。"
    "严格按 JSON 输出，禁止代码围栏。按字段给出音标、词性释义、词源、搭配、例句与记忆钩子。"
)
EN_FIELDS = ["title", "phonetic", "definitions", "etymology_roots",
             "phrases", "examples", "memory_tip"]
EN_SECTION_KEYS = ["phonetic", "definitions", "etymology_roots", "phrases",
                   "examples", "memory_tip"]
CN_SENTENCE_PROMPT = (
    "你是跨学科句子解读专家。严格按 JSON 输出。先定位领域，再讲清科学意思，"
    "把专业概念当场落地，看清论断与依据，最后点出易错点。严禁语法成分拆解。"
)
CN_SENTENCE_FIELDS = ["title", "style", "meaning", "structure", "rhetoric",
                      "rewrite", "pitfalls"]
CN_SENTENCE_SECTION_KEYS = ["meaning", "structure"]
IMAGE_PROMPT = (
    "只输出结果本身，不要描述分析过程。图中出现公式时一律用 LaTeX 表示（反斜杠双写）；"
    "辨认不清的内容直接说明，不要编造。"
)

# v9 section 4: the generic template applies to B-new labels only.
GENERIC_PROMPT = (
    "你是通用解析专家，服务于桌面词典。用户输入一段内容（可能是词、句、段落、公式或截图文字），"
    "读者希望能跨学科、多角度理解它。严格按 JSON 输出，禁止代码围栏。用清晰的中文，"
    "分块给出：它是什么、核心意思、结构或组成、常见用法或例子、必要的背景与来源、"
    "不同视角下的理解与争议。"
)
GENERIC_FIELDS = ["title", "what_is_it", "core_meaning", "structure", "examples",
                  "background", "perspectives", "caveats"]
GENERIC_SECTION_KEYS = ["what_is_it", "core_meaning", "structure", "examples",
                        "background", "perspectives", "caveats"]
NEW_CATEGORY = "票25新增英语句子"
NEW_CRITERIA = "票25：英文完整句子，含主谓宾结构与时态"
USER_TEMPLATE = "【票25用户改过的通用模板】按句子结构分层解释，并给出两种译法。"


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _restore():
    # getattr keeps collection green when the migration is missing: RED is a
    # named assertion failure, not an ImportError.
    return getattr(settings_mod, "restore_factory_route_templates", None)


def _polluted_config():
    """Hand-built polluted user config: factory prompts all clobbered, two user
    edits and one non-factory route that must survive untouched."""
    data = load_defaults().as_dict()
    for rid in FACTORY_IDS:
        data["routes"][rid]["prompt"] = POLLUTED
    data["route_prompts"] = {rid: POLLUTED for rid in FACTORY_IDS}
    # User-authored values (not placeholders): never overwrite these.
    data["routes"]["中文词语"]["prompt"] = USER_WORD
    data["route_prompts"]["中文词语"] = USER_WORD
    # Non-factory route: v9 generic template applies only to B-new labels, and
    # its user text is authoritative here.
    data["routes"]["英语句子"] = {
        "id": "英语句子", "label": "英语句子", "enabled": True,
        "query_type": "free", "kind": "route", "prompt": POLLUTED,
        "sections": {}, "fields": [], "derived_template_source": "decision_model",
    }
    data["route_prompts"]["英语句子"] = "翻译成中文。"
    data["config_version"] = 9
    return data


def _composed(settings, route_id):
    intent = next(i for i in settings.intents if i.primary_route == route_id)
    return compose_prompt(intent, settings)


class TestFactoryTemplateRestore(unittest.TestCase):
    def setUp(self):
        self.data = _polluted_config()
        self.restore = _restore()

    def test_restore_function_exists(self):
        self.assertIsNotNone(self.restore,
                             "缺少 restore_factory_route_templates 迁移函数")

    def test_each_factory_route_gets_its_own_prompt_fields_sections(self):
        self.assertIsNotNone(self.restore)
        changed = self.restore(self.data)
        self.assertTrue(changed, "被污染配置必须报告已迁移")

        en = self.data["routes"]["英语单词"]
        self.assertEqual(en["prompt"], EN_PROMPT)
        self.assertEqual(list(en["fields"]), EN_FIELDS)
        self.assertEqual(list(en["sections"]), EN_SECTION_KEYS)
        self.assertIn("音标", en["sections"]["phonetic"])

        sentence = self.data["routes"]["中文句解"]
        self.assertEqual(sentence["prompt"], CN_SENTENCE_PROMPT)
        self.assertEqual(list(sentence["fields"]), CN_SENTENCE_FIELDS)
        self.assertEqual(list(sentence["sections"]), CN_SENTENCE_SECTION_KEYS)
        self.assertIn("依据", sentence["sections"]["structure"])

        self.assertEqual(self.data["route_prompts"]["英语单词"], EN_PROMPT)
        self.assertEqual(self.data["route_prompts"]["中文句解"], CN_SENTENCE_PROMPT)
        self.assertEqual(self.data["route_prompts"]["图片识别"], IMAGE_PROMPT)
        self.assertEqual(list(self.data["routes"]["图片识别"]["fields"]), [])
        self.assertEqual(dict(self.data["routes"]["图片识别"]["sections"]), {})

        # 全部 11 个出厂路由都按 v9 第五节各归各位（期望值来自出厂 spec，
        # 不是从被测实现读回）；绝不出现「所有标签刷成同一套」。
        spec = (Path(settings_mod.__file__).resolve().parents[2]
                / "spec" / "builtin_settings.json")
        builtin = json.loads(spec.read_text(encoding="utf-8"))["routes"]
        self.assertEqual(len(builtin), len(FACTORY_IDS))
        for rid in FACTORY_IDS:
            source = builtin[rid]
            route = self.data["routes"][rid]
            if rid == "中文词语":   # 用户改过的模板：只回填结构、prompt 原样保留
                self.assertEqual(route["prompt"], USER_WORD)
                self.assertEqual(self.data["route_prompts"][rid], USER_WORD)
            else:
                self.assertEqual(route["prompt"], source["prompt"], rid)
                self.assertEqual(self.data["route_prompts"][rid],
                                 source["prompt"], rid)
            self.assertEqual(list(route.get("fields") or []),
                             list(source.get("fields") or []), rid)
            self.assertEqual(dict(route.get("sections") or {}),
                             dict(source.get("sections") or {}), rid)
        restored = [self.data["route_prompts"][rid] for rid in FACTORY_IDS
                    if rid != "中文词语"]
        self.assertEqual(len(set(restored)), len(restored),
                         "出厂路由不能刷成同一套通用模板")
        # 各归各位：两条不同路由绝不能被刷成同一套模板
        self.assertNotEqual(en["prompt"], sentence["prompt"])

    def test_user_edited_prompts_and_non_factory_route_are_preserved(self):
        self.assertIsNotNone(self.restore)
        self.restore(self.data)
        self.assertEqual(self.data["routes"]["中文词语"]["prompt"], USER_WORD)
        self.assertEqual(self.data["route_prompts"]["中文词语"], USER_WORD)
        self.assertEqual(self.data["routes"]["英语单词"]["prompt"], EN_PROMPT)
        self.assertEqual(self.data["route_prompts"]["英语单词"], EN_PROMPT)
        self.assertEqual(self.data["routes"]["英语句子"]["prompt"], POLLUTED)
        self.assertEqual(self.data["route_prompts"]["英语句子"], "翻译成中文。")
        self.assertEqual(self.data["routes"]["英语句子"]["sections"], {})

    def test_restore_is_idempotent(self):
        self.assertIsNotNone(self.restore)
        self.assertTrue(self.restore(self.data))
        once = copy.deepcopy(self.data)
        self.assertFalse(self.restore(self.data), "重复迁移不能再改动配置")
        self.assertEqual(self.data, once)


class _TempConfigHome(unittest.TestCase):
    def setUp(self):
        self._old_xdg = os.environ.get("XDG_CONFIG_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        self.real_config = Path.home() / ".config" / "clip-llm02" / "settings.json"
        self.real_mtime = (self.real_config.stat().st_mtime_ns
                           if self.real_config.exists() else None)

    def tearDown(self):
        self._tmp.cleanup()
        if self._old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._old_xdg

    def _disk(self):
        path = config_mod.config_path()
        self.assertTrue(path.exists(), f"load_config 未写盘：{path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _assert_real_config_untouched(self):
        mtime = (self.real_config.stat().st_mtime_ns
                 if self.real_config.exists() else None)
        self.assertEqual(mtime, self.real_mtime, "真实用户配置被测试改动了")


class TestPollutedConfigMigrationEndToEnd(_TempConfigHome):
    def test_polluted_config_restores_each_route_persists_and_survives_restart(self):
        path = config_mod.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_polluted_config(), ensure_ascii=False,
                                   indent=2), encoding="utf-8")

        with patch.object(gateway_mod.urllib.request, "urlopen",
                          side_effect=AssertionError("票据25不得发起真实网络调用")):
            settings = config_mod.load_config()

            disk = self._disk()
            self.assertEqual(disk["routes"]["英语单词"]["prompt"], EN_PROMPT)
            self.assertEqual(list(disk["routes"]["英语单词"]["sections"]),
                             EN_SECTION_KEYS)
            self.assertIn("音标", disk["routes"]["英语单词"]["sections"]["phonetic"])
            self.assertEqual(disk["route_prompts"]["英语单词"], EN_PROMPT)

            self.assertEqual(disk["routes"]["中文句解"]["prompt"], CN_SENTENCE_PROMPT)
            self.assertEqual(list(disk["routes"]["中文句解"]["sections"]),
                             CN_SENTENCE_SECTION_KEYS)
            self.assertEqual(disk["route_prompts"]["中文句解"], CN_SENTENCE_PROMPT)
            self.assertNotEqual(disk["route_prompts"]["英语单词"],
                                disk["route_prompts"]["中文句解"])

            self.assertEqual(disk["route_prompts"]["图片识别"], IMAGE_PROMPT)
            self.assertEqual(list(disk["routes"]["图片识别"]["fields"]), [])
            self.assertEqual(dict(disk["routes"]["图片识别"]["sections"]), {})

            # 用户改过的 prompt / 非出厂路由原样保留
            self.assertEqual(disk["route_prompts"]["中文词语"], USER_WORD)
            self.assertEqual(disk["routes"]["中文词语"]["prompt"], USER_WORD)
            self.assertEqual(disk["route_prompts"]["英语单词"], EN_PROMPT)
            self.assertEqual(disk["route_prompts"]["英语句子"], "翻译成中文。")

            # 运行期与磁盘一致，compose_prompt 拿到出厂详细指令而非空泛占位
            self.assertEqual(settings.route_prompts["英语单词"], EN_PROMPT)
            self.assertEqual(settings.routes["英语单词"]["sections"],
                             disk["routes"]["英语单词"]["sections"])
            composed = _composed(settings, "英语单词").prompt
            self.assertTrue(composed.startswith(EN_PROMPT),
                            f"system 没拿到英语单词出厂指令：{composed[:80]}")
            self.assertNotIn(POLLUTED, composed)

            # 重启仍在：第二次 load_config 不再产生变化
            restarted = config_mod.load_config()
            self.assertEqual(restarted.route_prompts["英语单词"], EN_PROMPT)
            self.assertEqual(restarted.route_prompts["中文句解"], CN_SENTENCE_PROMPT)
            self.assertEqual(self._disk(), disk, "重启后磁盘配置发生漂移")
            self.assertEqual(restarted.route_prompts["中文词语"], USER_WORD)
            self.assertEqual(restarted.route_prompts["英语句子"], "翻译成中文。")

        self._assert_real_config_untouched()


class TestNewCategoryGenericTemplateEndToEnd(_TempConfigHome):
    def test_new_category_gets_generic_template_user_edit_survives_restart(self):
        with patch.object(gateway_mod.urllib.request, "urlopen",
                          side_effect=AssertionError("票据25不得发起真实网络调用")):
            self.dlg = SettingsDialog(load_defaults())
            b = json.loads(self.dlg.decision_model_json_edit.toPlainText())
            self.assertNotIn(NEW_CATEGORY, b, "夹具类别必须是出厂配置里没有的")
            b[NEW_CATEGORY] = NEW_CRITERIA
            self.dlg.decision_model_json_edit.setPlainText(
                json.dumps(b, ensure_ascii=False, indent=2))
            self.dlg.decision_model_json_apply.click()

            route = self.dlg.data["routes"].get(NEW_CATEGORY)
            self.assertIsNotNone(route, "B 新类别必须派生同名路由")
            self.assertEqual(self.dlg.data["route_prompts"].get(NEW_CATEGORY),
                             GENERIC_PROMPT)
            self.assertEqual(list(route.get("fields") or []), GENERIC_FIELDS)
            self.assertEqual(list(route.get("sections") or {}),
                             GENERIC_SECTION_KEYS)

            # 用户在模板页（真实控件）改成自己的文案，随后只点右下角确定
            idx = self.dlg.route_combo.findData(NEW_CATEGORY)
            self.assertGreaterEqual(idx, 0)
            self.dlg.route_combo.setCurrentIndex(idx)
            # v10 票据 26：编辑区是完整 JSON，走真实控件只改 prompt 字段
            set_editor_prompt(self.dlg, USER_TEMPLATE)
            self.assertEqual(self.dlg.data["route_prompts"][NEW_CATEGORY],
                             USER_TEMPLATE)

            ok = self.dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
            ok.click()
            self.assertEqual(self.dlg.result(), QDialog.Accepted)

            disk = self._disk()
            self.assertEqual(disk["route_prompts"][NEW_CATEGORY], USER_TEMPLATE)
            self.assertEqual(list(disk["routes"][NEW_CATEGORY]["fields"]),
                             GENERIC_FIELDS)
            self.assertEqual(list(disk["routes"][NEW_CATEGORY]["sections"]),
                             GENERIC_SECTION_KEYS)

            restarted = config_mod.load_config()  # 重启：从磁盘重新加载
            self.assertEqual(restarted.route_prompts[NEW_CATEGORY], USER_TEMPLATE)
            composed = _composed(restarted, NEW_CATEGORY).prompt
            self.assertTrue(composed.startswith(USER_TEMPLATE),
                            f"约束链末环没读到用户文案：{composed[:80]}")

        self._assert_real_config_untouched()


class TestSettingsDialogRoutesJsonBlockRemoved(unittest.TestCase):
    """v9 change 3: hide the routes JSON block, keep the two radio buttons."""

    def setUp(self):
        self._urlopen_patch = patch.object(
            gateway_mod.urllib.request, "urlopen",
            side_effect=AssertionError("票据25不得发起真实网络调用"))
        self._urlopen_patch.start()
        self.addCleanup(self._urlopen_patch.stop)
        self.dlg = SettingsDialog(load_defaults())

    def tearDown(self):
        self.dlg.close()

    def test_routes_json_block_is_absent_from_real_dialog(self):
        with patch.object(gateway_mod.urllib.request, "urlopen",
                          side_effect=AssertionError("票据25不得发起真实网络调用")):
            labels = [label.text() for label in self.dlg.findChildren(QLabel)]
            self.assertNotIn("路由 JSON（唯一事实源）", labels,
                             "设置页仍在显示「路由 JSON（唯一事实源）」区块")
            self.assertIsNone(self.dlg.findChild(QPlainTextEdit, "routesJsonEdit"),
                              "设置页仍暴露 routes JSON 编辑框")
            buttons = [b.text() for b in self.dlg.findChildren(QPushButton)]
            self.assertNotIn("从 JSON 载入路由与模板", buttons,
                             "设置页仍暴露 JSON 载入按钮")

    def test_two_mutually_exclusive_source_radios_remain(self):
        radios = self.dlg.findChildren(QRadioButton)
        texts = [r.text() for r in radios]
        self.assertIn("本地分类器（正则、离线、零 token）", texts)
        self.assertIn("决策模型（Agnes SystemOne、需联网）", texts)
        local = next(r for r in radios if r.text().startswith("本地分类器"))
        decision = next(r for r in radios if r.text().startswith("决策模型"))
        self.assertIsNotNone(self.dlg.short_source_group)
        self.assertEqual(self.dlg.short_source_group.exclusive(), True)
        local.setChecked(True)
        decision.setChecked(True)
        self.assertFalse(local.isChecked(), "两个开关必须互斥")
        self.assertTrue(decision.isChecked())

    def test_routes_single_source_data_contract_still_exists(self):
        # 只移除 UI 展示；数据层仍由 B 单向派生、仍禁止反向写回。
        self.assertTrue(callable(getattr(settings_mod, "apply_routes_json", None)))
        self.assertTrue(callable(
            getattr(settings_mod, "apply_decision_model_json", None)))
        self.assertTrue(getattr(settings_mod, "JSON_WRITEBACK_FORBIDDEN"))


if __name__ == "__main__":
    unittest.main()
