"""票据 36 拆分保留：原 tests/test_always_on_domain_combo_v12.py 中的纯 build_menu 用例。

删除的是依赖 MenuWindow / DesktopApp 的控件与端到端用例；领域列表、判定项置顶、
未动契约三条保留。全部 offscreen；真实网络调用会被 NoNetworkCase 拦截成失败。
"""
import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import unittest  # noqa: E402
from dataclasses import replace as dc_replace  # noqa: E402
from unittest.mock import patch  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import (  # noqa: E402
    DomainCandidate, InputEnvelope, Preferences, build_menu, load_defaults,
    preflight, suggest,
)
from clipllm02.models import Classification  # noqa: E402
from clipllm02.desktop import gateway as gateway_mod  # noqa: E402


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


# 手工从 spec/builtin_settings.json 抄出的出厂文本领域顺序（image 是图片专用，不算换领域）。
ALL_TEXT_DOMAINS = ("math", "physics", "chem", "code", "en", "zh")
EXPECTED_ALL = set(ALL_TEXT_DOMAINS)
# 判定项故意取顺序靠后的 zh：避免"恰好本来就是第一个"的巧合绿。
JUDGED_ID = "zh"
JUDGED_LABEL = "中文 / 术语"
EXPECTED_JUDGED_FIRST = (JUDGED_ID, "math", "physics", "chem", "code", "en")


class NoNetworkCase(unittest.TestCase):
    def setUp(self):
        blocker = patch.object(
            gateway_mod.urllib.request, "urlopen",
            side_effect=AssertionError("票据 28 禁止真实网络调用"))
        blocker.start()
        self.addCleanup(blocker.stop)


def make_high_context(text="serendipity", judged_id=JUDGED_ID,
                      judged_label=JUDGED_LABEL):
    """构造 HIGH（单候选）判定上下文；候选 id 可指定，便于验证置顶。"""
    settings = load_defaults()
    env = InputEnvelope.for_text(text, source="test")
    policy = preflight(env, settings)
    base = suggest(env, policy, settings)
    judged = DomainCandidate(id=judged_id, label=judged_label, score=0.95,
                             evidence=("初判规则：测试夹具",))
    s = dc_replace(base, domain_candidates=(judged,), level="HIGH")
    return settings, env, policy, s, build_menu(s, settings, Preferences())


class TestChipsCoverAllDomains(NoNetworkCase):
    """变更 1a：HIGH 单候选时下拉也要列出全部可用领域。"""

    def test_high_chips_cover_all_available_domains(self):
        _, _, _, _, menu = make_high_context()
        ids = [c.id for c in menu.domain_chips]
        self.assertEqual(set(ids), EXPECTED_ALL)
        self.assertEqual(len(ids), len(ALL_TEXT_DOMAINS))   # 不重复
        self.assertNotIn("image", ids)


class TestJudgedItemFirst(NoNetworkCase):
    """变更 1b：判定项置顶，其余按 settings.domains 顺序。"""

    def test_high_judged_item_first_rest_in_settings_order(self):
        _, _, _, _, menu = make_high_context()
        ids = tuple(c.id for c in menu.domain_chips)
        self.assertEqual(ids, EXPECTED_JUDGED_FIRST)
        self.assertEqual(ids[0], JUDGED_ID)


class TestUntouchedContracts(NoNetworkCase):
    """回归守卫：v12 明确不动的字段/链路，RED 阶段就应绿。"""

    def test_high_category_chips_still_reflect_classification(self):
        settings, env, policy, s, menu = make_high_context()
        cls = Classification(category="英语短语", label="英语短语",
                             reason="测试夹具", candidates=())
        with_cls = dc_replace(s, classification=cls)
        built = build_menu(with_cls, settings, Preferences())
        self.assertEqual(built.category_chips[0].id, "英语短语")
        self.assertEqual(built.level, "HIGH")


if __name__ == "__main__":
    unittest.main()
