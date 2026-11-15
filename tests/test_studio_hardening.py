"""第 1 轮硬化回归：请求票据、硬否决统一防线、策略配置语义。

覆盖的是异步/跨入口链路，不是纯函数：这些缺陷在旧的 210 个用例里全部漏网。
"""
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import (  # noqa: E402
    InputEnvelope, build_menu, load_defaults, preflight, suggest,
)
from clipllm02.classify import classify_text, explain_text  # noqa: E402
from clipllm02.models import Preferences, sha256_text  # noqa: E402
from clipllm02.desktop.gateway import GatewayResult  # noqa: E402
from clipllm02.studio.app import StudioApp  # noqa: E402
from clipllm02.studio.config import load_config  # noqa: E402
from clipllm02.studio.json_render import (  # noqa: E402
    RENDER_SCHEMA, render_json_result,
)


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def make_app(pinned: bool = True):
    """默认固定窗口模式，锁定已有窗口回归；unpinned 用例显式传 False。"""
    from clipllm02 import load_defaults

    settings = load_config()
    settings.data["desktop_persistent_window"] = pinned
    # 与用户真实配置解耦：策略级别一律用出厂值，否则 ~/.config 里把
    # secret 改成 BLOCK 之类会让 CONFIRM / 硬否决用例的语义悄悄漂移。
    settings.data["policy"] = copy.deepcopy(load_defaults().policy)
    settings.policy = settings.data["policy"]
    # 固定走本地预览路径：不产生任何网络请求、不启请求线程
    for key in ("desktop_api_url", "desktop_api_key", "desktop_model",
                "desktop_vision_api_url", "desktop_vision_api_key",
                "desktop_vision_model"):
        settings.data[key] = ""
    return StudioApp(settings, poll_interval=5.0)


def stop(app):
    app._alive = False
    app.clip_worker._active = False


SELECTION = {"intent_id": "chem.legacy", "domain_id": "chem",
             "format_id": "static"}


def ok_payload(tab, generation, envelope_id, *, text="A", key="k"):
    return (tab, generation,
            GatewayResult(html="<p>STALE</p>", ok=True,
                          raw='{"title":"A","answer":"A"}'),
            key, 14, False, envelope_id, "req_test")


class TestRequestTicket(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def tearDown(self):
        stop(self.app)

    def test_new_query_reusing_tab_drops_inflight_result(self):
        """S1：查 A → 请求在途 → 复制 B，A 的回包不得覆盖 B 的第一层。"""
        app = self.app
        app.process_text("NaCl", source="test")
        tab = app.window.current_tab()
        app.on_intent(tab, dict(SELECTION))
        eid, generation = tab.env.envelope_id, tab.request_generation

        app.process_text("E=mc²", source="test")
        self.assertIs(app.window.current_tab(), tab)
        self.assertNotEqual(tab.env.envelope_id, eid)
        self.assertEqual(tab.stack.currentIndex(), 0)

        with patch.object(app.cache, "put") as put:
            app._on_worker_done(ok_payload(tab, generation, eid))
            self.assertEqual(put.call_count, 0)
        self.assertEqual(tab.stack.currentIndex(), 0)
        self.assertNotIn("STALE", tab.view.toPlainText())
        self.assertEqual(app.state.errors, [])

    def test_closed_tab_drops_result(self):
        app = self.app
        app.on_new_tab("NaCl")
        tab = app.window.current_tab()
        app.process_text("NaCl", source="test", force_dedup_bypass=True)
        app.on_intent(tab, dict(SELECTION))
        eid, generation = tab.env.envelope_id, tab.request_generation

        app.window._close_tab(app.window.tabs.currentIndex())
        with patch.object(app.cache, "put") as put:
            app._on_worker_done(ok_payload(tab, generation, eid))
            self.assertEqual(put.call_count, 0)
        self.assertEqual(app.state.errors, [])

    def test_matching_ticket_still_renders(self):
        """正向对照：票据匹配的结果必须正常渲染、正常写缓存。"""
        app = self.app
        app.process_text("NaCl", source="test")
        tab = app.window.current_tab()
        app.on_intent(tab, dict(SELECTION))
        eid, generation = tab.env.envelope_id, tab.request_generation
        with patch.object(app.cache, "put") as put:
            app._on_worker_done(ok_payload(tab, generation, eid))
            self.assertEqual(put.call_count, 1)
        self.assertEqual(tab.stack.currentIndex(), 1)
        self.assertIn("A", tab.view.toPlainText())

    def test_requery_reads_selection_from_tab(self):
        app = self.app
        app.process_text("NaCl", source="test")
        tab = app.window.current_tab()
        app.on_intent(tab, dict(SELECTION))
        gen = tab.request_generation
        app.on_requery()
        self.assertEqual(tab.request_generation, gen + 1)
        self.assertEqual(tab.last_selection["intent_id"], "chem.legacy")

    def test_no_global_selection_table(self):
        """关闭标签后不允许残留任何以 id(tab) 为键的全局状态。"""
        self.assertFalse(hasattr(self.app, "_last_selections"))


class TestHardVeto(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def tearDown(self):
        stop(self.app)

    def test_block_invalidates_stale_tab_without_touching_window(self):
        """BLOCK 静默，但上一轮残留的意图按钮不得再发请求。"""
        app = self.app
        app.process_text("NaCl", source="test")
        tab = app.window.current_tab()
        gen = tab.request_generation
        layer, visible = tab.stack.currentIndex(), app.window.isVisible()
        app.process_text("x" * 1200, source="test")
        self.assertTrue(tab.invalidated)
        self.assertEqual(tab.stack.currentIndex(), layer)
        self.assertEqual(app.window.isVisible(), visible)   # 不得触碰窗口显隐

        app.on_intent(tab, dict(SELECTION))
        self.assertEqual(tab.request_generation, gen)
        self.assertEqual(tab.stack.currentIndex(), 0)
        self.assertEqual(app.state.errors, [])

    def test_new_valid_query_clears_invalidation(self):
        app = self.app
        app.process_text("x" * 1200, source="test")
        tab = app.window.current_tab()
        self.assertTrue(tab.invalidated)
        app.process_text("NaCl", source="test")
        self.assertFalse(tab.invalidated)
        self.assertIsNotNone(tab.env)

    def test_settings_reload_tightens_rule_on_existing_tab(self):
        """把 url 从 WARN 改成 BLOCK 后，已打开标签也不得发请求。"""
        app = self.app
        app.process_text("https://example.com", source="test")
        tab = app.window.current_tab()
        gen = tab.request_generation
        data = app.settings.as_dict()
        data["policy"]["rules"]["url"]["level"] = "BLOCK"
        app.reload_settings(data)
        app.on_intent(tab, dict(SELECTION))
        self.assertEqual(tab.request_generation, gen)

    def test_quota_is_enforced_on_production_path(self):
        """生产链路必须把 runtime 传给 preflight，否则 daily_limit 恒不命中。"""
        app = self.app
        app.state.quota_day = time.strftime("%Y-%m-%d")
        app.state.requests_today = int(app.settings.get("daily_request_limit", 300))
        before = app.state.stats["blocked"]
        app.process_text("NaCl", source="test")
        self.assertEqual(app.state.stats["blocked"], before + 1)
        self.assertIsNone(app.window.current_tab().env)

    def test_sensitive_patterns_from_settings(self):
        app = self.app
        data = app.settings.as_dict()
        data["policy"]["sensitive_patterns"] = [r"INTERNAL-\d+"]
        app.reload_settings(data)
        before = app.state.stats["blocked"]
        # 自定义敏感正则默认 CONFIRM，但把 levels 配成禁止请求后应硬拦
        data2 = app.settings.as_dict()
        data2["policy"]["levels"]["CONFIRM"]["request"] = False
        app.reload_settings(data2)
        app.process_text("INTERNAL-42", source="test")
        self.assertEqual(app.state.stats["blocked"], before + 1)


class TestPolicySemantics(unittest.TestCase):
    def test_enabled_false_disables_rule(self):
        settings = load_defaults()
        settings.data["policy"]["rules"]["empty"]["enabled"] = False
        result = preflight(InputEnvelope.for_text(""), settings)
        self.assertNotIn("empty", result.block_reasons)

    def test_levels_request_false_blocks(self):
        settings = load_defaults()
        settings.data["policy"]["levels"]["WARN"]["request"] = False
        result = preflight(InputEnvelope.for_text("https://example.com"), settings)
        self.assertEqual(result.level, "BLOCK")
        self.assertTrue(result.block_reasons)

    def test_sensitive_patterns_argument(self):
        settings = load_defaults()
        result = preflight(InputEnvelope.for_text("NaCl"), settings,
                           sensitive_patterns=(r"^Na",))
        self.assertIn("secret", [h.reason for h in result.hits])

    def test_default_levels_unchanged(self):
        """回归护栏：出厂 levels 表不得改变既有 WARN / CONFIRM / ALLOW 语义。"""
        settings = load_defaults()
        self.assertEqual(preflight(
            InputEnvelope.for_text("https://example.com"), settings).level, "WARN")
        self.assertEqual(preflight(
            InputEnvelope.for_text("api_key=abcdef"), settings).level, "CONFIRM")
        self.assertEqual(preflight(
            InputEnvelope.for_text("break the ice."), settings).level, "ALLOW")


class TestSettingsTransaction(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def tearDown(self):
        stop(self.app)

    def test_open_settings_keeps_current_result(self):
        """取消设置时子进程不回传信号：标签内容必须从头到尾没被覆盖。"""
        app = self.app
        app.process_text("NaCl", source="test")
        tab = app.window.current_tab()
        app.on_intent(tab, dict(SELECTION))
        before, layer = tab.view.toPlainText(), tab.stack.currentIndex()
        self.assertIn("未发送任何网络请求", before)
        with patch.object(app.input_bridge, "request_settings"):
            app.open_settings()
        self.assertEqual(tab.view.toPlainText(), before)
        self.assertEqual(tab.stack.currentIndex(), layer)
        self.assertEqual(app.state.errors, [])

    def test_deleted_rule_is_not_restored_by_deep_merge(self):
        from clipllm02.studio.config import settings_from_data
        from clipllm02.studio.settings_dialog import SettingsDialog

        dialog = SettingsDialog(self.app.settings)
        dialog._current_policy_id = "url"
        dialog._delete_policy_rule()
        data = dialog.data
        self.assertNotIn("url", data["policy"]["rules"])
        self.assertIn("url", data["policy"]["removed_rules"])
        loaded = settings_from_data(data)
        self.assertNotIn("url", loaded.policy["rules"])
        dialog.close()


class TestConfirmGate(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def tearDown(self):
        stop(self.app)

    def test_first_click_asks_then_second_click_authorizes(self):
        """CONFIRM 必须真正拦住首次请求，且不新增窗口/控件。"""
        app = self.app
        app.process_text("api_key=abcdef", source="test")
        tab = app.window.current_tab()
        self.assertTrue(tab._intent_options)
        gen = tab.request_generation

        tab._submit_intent(0)
        self.assertIsNotNone(tab.pending_confirmation)
        self.assertIn("授权", tab.warn.text())
        self.assertEqual(tab.stack.currentIndex(), 0)     # 留在初判层
        self.assertEqual(tab.request_generation, gen)     # 零请求

        tab._submit_intent(0)
        self.assertIsNone(tab.pending_confirmation)
        self.assertEqual(tab.request_generation, gen + 1)

    def test_different_selection_needs_fresh_confirmation(self):
        app = self.app
        app.process_text("api_key=abcdef", source="test")
        tab = app.window.current_tab()
        app.on_intent(tab, dict(SELECTION))
        first = tab.pending_confirmation
        self.assertIsNotNone(first)
        app.on_intent(tab, {"intent_id": None, "domain_id": "*",
                            "format_id": "static",
                            "custom_instruction": "换个角度解释",
                            "free_input_mode": "instruction",
                            "allowed_route_ids": []})
        self.assertIsNotNone(tab.pending_confirmation)
        self.assertNotEqual(tab.pending_confirmation, first)

    def test_new_context_clears_pending_confirmation(self):
        app = self.app
        app.process_text("api_key=abcdef", source="test")
        tab = app.window.current_tab()
        app.on_intent(tab, dict(SELECTION))
        self.assertIsNotNone(tab.pending_confirmation)
        app.process_text("NaCl", source="test")
        self.assertIsNone(tab.pending_confirmation)

    def test_domain_change_cannot_bypass_tightened_rule(self):
        app = self.app
        app.process_text("https://example.com", source="test")
        tab = app.window.current_tab()
        data = app.settings.as_dict()
        data["policy"]["rules"]["url"]["level"] = "BLOCK"
        app.reload_settings(data)
        self.assertTrue(tab.invalidated)
        menu = tab.menu
        app.on_domain(tab, "en")
        self.assertIs(tab.menu, menu)                     # 未被重建


class TestResultRendering(unittest.TestCase):
    def test_no_browser_only_css(self):
        """QTextDocument 不支持的 CSS 不得再出现在结果 HTML 里。"""
        out = render_json_result('{"title":"T","answer":"A","examples":["x","y"]}')
        for bad in ("border-radius", "display:inline-block",
                    "white-space:pre-wrap", "width:100%"):
            self.assertNotIn(bad, out)
        self.assertIn("background-color:", out)      # 关键样式已内联
        self.assertIn("class='card'", out)

    def test_markdown_is_structured_not_literal(self):
        raw = ("## 结论\n\n- 第一点\n- 第二点\n\n**重点**在这里\n\n"
               "| 列A | 列B |\n|---|---|\n| 1 | 2 |")
        out = render_json_result(raw)
        self.assertIn("<h2>", out)
        self.assertIn("<ul>", out)
        self.assertIn("<b>重点</b>", out)
        self.assertIn("<table>", out)
        self.assertNotIn("## 结论", out)

    def test_json_with_surrounding_prose_still_renders(self):
        raw = '好的，结果如下：\n{"title":"T","answer":"A"}\n希望有帮助。'
        out = render_json_result(raw)
        self.assertIn("<h3>T</h3>", out)
        self.assertIn("回答", out)

    def test_script_is_escaped_in_markdown_channel(self):
        out = render_json_result("## 标题\n\n<script>alert(1)</script>")
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>", out)

    def test_long_title_is_body_not_heading(self):
        raw = json.dumps({"title": "这是一段非常长的内容" * 20, "answer": "A"},
                         ensure_ascii=False)
        out = render_json_result(raw)
        self.assertIn("<h3>解释结果</h3>", out)
        self.assertNotIn(">标题</p>", out)

    def test_dark_theme_reaches_table_cells(self):
        raw = json.dumps({"title": "T", "definitions": [
            {"pos": "v.", "cn": "建造"}, {"pos": "n.", "cn": "构想"}]},
            ensure_ascii=False)
        out = render_json_result(raw, dark=True)
        self.assertIn("#181b22", out)
        self.assertIn("#2b3448", out)      # 深色 chip 必须传到 th / 色条

    def test_unknown_chinese_field_is_not_titlecased(self):
        out = render_json_result('{"核心观点":"中文键"}')
        self.assertIn("核心观点", out)
        self.assertIn("中文键", out)

    def test_fenced_and_unclosed_fence_keep_content(self):
        self.assertIn("回答", render_json_result(
            '```json\n{"title":"T","answer":"A"}\n```'))
        out = render_json_result("```\n第一行\n第二行\n")
        self.assertIn("第一行", out)
        self.assertIn("第二行", out)

    def test_inline_math_is_rendered_without_dollars(self):
        """$...$ 必须变成公式样式，而不是原样带美元符号。"""
        out = render_json_result(json.dumps(
            {"title": "T", "answer": "公式 $E = mc^2$ 成立"}, ensure_ascii=False))
        body = out.split("</style>", 1)[1]
        self.assertNotIn("$", body)
        self.assertIn("font-style:italic", body)
        self.assertIn("mc²", body)          # 上下标已符号化

    def test_block_math_is_centered(self):
        """独立公式必须是居中的块级段落（Qt 忽略 span 的 text-align）。"""
        from PySide6.QtGui import QTextDocument
        out = render_json_result(json.dumps(
            {"answer": "$$a^2 + b^2 = c^2$$"}, ensure_ascii=False))
        body = out.split("</style>", 1)[1]
        self.assertNotIn("$", body)
        self.assertIn("align='center'", body)
        doc = QTextDocument()
        doc.setHtml(out)
        block, found = doc.begin(), None
        while block.isValid():
            if "a²" in block.text():
                found = int(block.blockFormat().alignment())
            block = block.next()
        self.assertEqual(found, 4)          # Qt.AlignHCenter，真的居中

    def test_parenthesis_and_bracket_delimiters(self):
        """旧提示词的 \\(...\\) / \\[...\\] 也必须能渲染。"""
        inline = render_json_result(json.dumps(
            {"answer": "\\(E=mc^2\\) 成立"}, ensure_ascii=False))
        body = inline.split("</style>", 1)[1]
        self.assertNotIn("\\(", body)
        self.assertIn("mc²", body)
        block = render_json_result(json.dumps(
            {"answer": "\\[x^2+y^2=z^2\\]"}, ensure_ascii=False))
        self.assertIn("align='center'", block.split("</style>", 1)[1])

    def test_backtick_quoted_math_is_not_code(self):
        """模型照抄提示词里的 `$x$` 时，反引号不能把公式包成代码。"""
        body = render_json_result(json.dumps(
            {"answer": "前文 `$E=mc^2$` 后文"}, ensure_ascii=False)
        ).split("</style>", 1)[1]
        self.assertNotIn("monospace", body)
        self.assertIn("mc²", body)

    def test_math_title_is_centered_and_quotes_stay_literal(self):
        """标题是独立公式时不能套 <h3>，且单引号不能变成 &#x27; 实体。"""
        from PySide6.QtGui import QTextDocument
        body = render_json_result(json.dumps(
            {"title": "$$q_x'' = -k\\frac{dT}{dx}$$", "answer": "解释"},
            ensure_ascii=False))
        self.assertNotIn("&#x27;", body)
        self.assertNotIn("&amp;#", body)
        doc = QTextDocument()
        doc.setHtml(body)
        first = doc.begin()
        self.assertEqual(int(first.blockFormat().alignment()), 4)
        self.assertIn("qₓ''", first.text())

    def test_vision_style_multiline_math_layout(self):
        """图片识别的多行结果：行内公式同行，独立公式与矩阵居中。"""
        from PySide6.QtGui import QTextDocument
        text = ("说明\n行内 $a^2$ 与 $b^2$。\n$$c^2$$\n"
                "$$\\begin{pmatrix}1&0\\\\0&1\\end{pmatrix}$$\n结尾\n")
        body = render_json_result(json.dumps({"text": text}, ensure_ascii=False))
        doc = QTextDocument()
        doc.setHtml(body)
        centered, block = [], doc.begin()
        while block.isValid():
            if int(block.blockFormat().alignment()) == 4:
                centered.append(block.text())
            block = block.next()
        self.assertEqual(len(centered), 2, centered)
        self.assertTrue(any("c²" in t for t in centered))
        self.assertTrue(any("1" in t and "0" in t for t in centered))

    def test_currency_is_not_treated_as_math(self):
        """价格里的 $ 不能被当成公式吞掉。"""
        out = render_json_result(json.dumps(
            {"answer": "价格 $5 与 $10 之间"}, ensure_ascii=False))
        body = out.split("</style>", 1)[1]
        self.assertIn("$5", body)
        self.assertIn("$10", body)
        self.assertNotIn("font-style:italic", body)

    def test_numeric_only_dollar_stays(self):
        body = render_json_result(json.dumps(
            {"answer": "$10$"}, ensure_ascii=False)).split("</style>", 1)[1]
        self.assertIn("$10$", body)

    def test_text_command_with_chinese_still_renders(self):
        body = render_json_result(json.dumps(
            {"answer": "$\\text{速度}=3$"}, ensure_ascii=False)
        ).split("</style>", 1)[1]
        self.assertNotIn("$", body)
        self.assertIn("速度=3", body)

    def test_preview_keeps_currency_but_strips_math(self):
        from clipllm02.studio.latex_unicode import strip_math_delimiters
        self.assertEqual(strip_math_delimiters("价格 $5 与 $10"),
                         "价格 $5 与 $10")
        self.assertEqual(strip_math_delimiters("$E=mc^2$"), "E=mc^2")

    def test_math_in_plain_text_result(self):
        """非 JSON 的纯文本结果同样要渲染公式。"""
        out = render_json_result("结论：$\\alpha + \\beta$ 是角度和")
        body = out.split("</style>", 1)[1]
        self.assertNotIn("$", body)
        self.assertIn("α", body)
        self.assertIn("β", body)


class TestCompactHeaderAndSettings(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def tearDown(self):
        stop(self.app)

    def test_header_is_compact(self):
        """顶部只留类别名，不报条数、不带「常用查询方式」。"""
        app = self.app
        app.process_text("NaCl", source="test")
        tab = app.window.current_tab()
        self.assertEqual(tab.intent_caption.text(), "化学物质")
        self.assertTrue(tab.intent_count.isHidden())
        self.assertEqual(tab.category_drawer.text(), "其他 ▾")
        self.assertEqual(tab.free_toggle.text(), "自定义")

    def test_empty_tab_has_no_legacy_caption(self):
        """启动时的空标签页不应显示旧文案「常用查询方式 / 其他类别」。"""
        app = self.app
        tab = app.window.tabs.widget(0)
        self.assertEqual(tab.intent_caption.text(), "")
        self.assertTrue(tab.category_drawer.isHidden())

    def test_other_view_without_hint_renders(self):
        """新建视角默认 hint 为空，渲染按钮时不能引用未定义变量。"""
        app = self.app
        data = app.settings.as_dict()
        data["other_views"]["items"] = [
            {"id": "nohint", "label": "无说明视角", "hint": "",
             "prompt": "请解释。", "enabled": True}]
        app.reload_settings(data)
        app.process_text("NaCl", source="test")
        tab = app.window.current_tab()
        texts = [b.text() for b in tab._other_buttons if not b.isHidden()]
        self.assertIn("无说明视角", texts)
        self.assertEqual(app.state.errors, [])

    def test_preview_shows_quotes_not_entities(self):
        """输入预览按纯文本显示，单引号不能变成 &#x27;。"""
        app = self.app
        app.process_text("$$q_x'' = -k\\frac{dT}{dx}$$", source="test")
        text = app.window.current_tab().preview.text()
        self.assertNotIn("&#x27;", text)
        self.assertIn("qₓ''", text)
        self.assertIn("(dT)/(dx)", text)

    def test_first_item_is_template_entry(self):
        app = self.app
        app.process_text("NaCl", source="test")
        tab = app.window.current_tab()
        first = [b.text() for b in tab._intent_buttons if not b.isHidden()][0]
        self.assertIn("综合化学条目", first)

    def test_settings_fits_screen_and_scrolls(self):
        from PySide6.QtWidgets import QApplication, QScrollArea
        from clipllm02.studio.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self.app.settings)
        try:
            area = QApplication.primaryScreen().availableGeometry()
            self.assertLessEqual(dialog.height(), area.height())
            self.assertLessEqual(dialog.width(), area.width())
            self.assertIsInstance(dialog.tabs.widget(0), QScrollArea)
        finally:
            dialog.close()


class TestPersistentWindow(unittest.TestCase):
    """弹窗词典模式：Alt+Tab 可见、失去焦点不隐藏，但绝不抢焦点。"""

    def setUp(self):
        self.app = make_app()

    def tearDown(self):
        stop(self.app)

    def test_alt_tab_visible_without_stealing_focus(self):
        from PySide6.QtCore import Qt
        w = self.app.window
        flags = w.windowFlags()
        self.assertTrue(flags & Qt.Window)
        self.assertNotEqual(flags & Qt.Tool, Qt.Tool)   # Tool 会被 skip-taskbar
        self.assertTrue(w.testAttribute(Qt.WA_ShowWithoutActivating))

    def test_losing_focus_never_hides_and_never_touches_above(self):
        w = self.app.window
        w.show()
        w._float()
        with patch.object(w, "hide") as hide, \
                patch("clipllm02.desktop.window_base.x11_state.set_above") as above:
            w._yield_to_other_window()
        self.assertTrue(w.isVisible())
        hide.assert_not_called()
        above.assert_not_called()      # 堆叠交给 WM，不发 client message
        w.hide()

    def test_mouse_return_does_not_refloat(self):
        w = self.app.window
        w.show()
        with patch.object(w, "_is_offscreen", return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.set_above") as above:
            w.enterEvent(None)
            w.leaveEvent(None)
            above.assert_not_called()
        self.assertTrue(w.isVisible())
        w.hide()

    def test_unfloat_is_noop_in_persistent_mode(self):
        w = self.app.window
        w.show()
        with patch.object(w, "_is_offscreen", return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.set_above") as above:
            w._float()
            w._unfloat()
            w._unfloat()
            above.assert_not_called()
        w.hide()

    def test_no_on_top_state_in_persistent_mode(self):
        from PySide6.QtCore import Qt
        w = self.app.window
        # GoldenDict-ng pinned：既没有静态 hint，也不动态增删 ABOVE
        self.assertFalse(bool(w.windowFlags() & Qt.WindowStaysOnTopHint))
        with patch.object(w, "_is_offscreen", return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.set_above") as above:
            w.show_without_focus()
            above.assert_not_called()
        w.hide()

    def test_alt_tab_and_clicks_are_left_to_window_manager(self):
        w = self.app.window
        w.show()
        with patch.object(w, "_is_offscreen", return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.set_above") as above, \
                patch("clipllm02.desktop.window_base.x11_state.escape_pressed",
                      return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.active_window") as active, \
                patch("clipllm02.desktop.window_base.x11_state.pointer_buttons") as buttons, \
                patch.object(w, "hide") as hide:
            for _ in range(6):
                w._poll_mouse()
            active.assert_not_called()
            buttons.assert_not_called()
            above.assert_not_called()
            hide.assert_not_called()
        self.assertTrue(w.isVisible())
        w.hide()

    def test_new_query_flashes_on_top_then_auto_releases(self):
        """查询时置顶，400ms 后自动解除；窗口不隐藏、位置不变。"""
        w = self.app.window
        w.show()
        with patch.object(w, "raise_") as raised, \
                patch.object(w, "_is_offscreen", return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.set_above") as above, \
                patch("clipllm02.desktop.window_base.x11_state.clear_demands_attention"), \
                patch("clipllm02.desktop.window_base.x11_state.active_window",
                      return_value=0x100):
            w.show_without_focus(bring_to_front=True)
            self.assertTrue(raised.called)
            self.assertEqual([c.args[1] for c in above.call_args_list], [True])
            self.assertTrue(w._pin_timer.isActive())      # 自动解除已排队
            w._pin_timer.timeout.emit()
            self.assertEqual([c.args[1] for c in above.call_args_list],
                             [True, False])
            self.assertTrue(w.isVisible())                # 只解除置顶
        w.hide()

    def test_ordinary_refresh_does_not_touch_stacking(self):
        w = self.app.window
        w.show()
        with patch.object(w, "raise_") as raised, \
                patch("clipllm02.desktop.window_base.x11_state.set_above") as above:
            w.show_without_focus()
            raised.assert_not_called()
            above.assert_not_called()
        w.hide()

    def test_enter_keeps_pin_and_leave_releases(self):
        w = self.app.window
        w.show()
        with patch.object(w, "_is_offscreen", return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.set_above") as above, \
                patch("clipllm02.desktop.window_base.x11_state.clear_demands_attention"), \
                patch("clipllm02.desktop.window_base.x11_state.active_window",
                      return_value=0x100):
            w.show_without_focus(bring_to_front=True)
            self.assertTrue(w._pin_timer.isActive())
            w.enterEvent(None)                    # 用户在看：不再自动解除
            self.assertFalse(w._pin_timer.isActive())
            w.leaveEvent(None)                    # 要走：立即解除，不留异步尾巴
            self.assertEqual([c.args[1] for c in above.call_args_list],
                             [True, False])
        w.hide()

    def test_outside_press_releases_pin_immediately(self):
        w = self.app.window
        with patch.object(w, "_is_offscreen", return_value=False), \
                patch.object(w, "_mouse_inside", return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.set_above") as above, \
                patch("clipllm02.desktop.window_base.x11_state.clear_demands_attention"), \
                patch("clipllm02.desktop.window_base.x11_state.active_window",
                      return_value=0x100), \
                patch("clipllm02.desktop.window_base.x11_state.escape_pressed",
                      return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.pointer_buttons",
                      return_value=1 << 8):
            w.show_without_focus(bring_to_front=True)
            self.assertEqual([c.args[1] for c in above.call_args_list], [True])
            w._poll_mouse()
            self.assertEqual([c.args[1] for c in above.call_args_list],
                             [True, False])
        w.hide()

    def test_hide_clears_pin_state(self):
        w = self.app.window
        with patch.object(w, "_is_offscreen", return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.set_above") as above, \
                patch("clipllm02.desktop.window_base.x11_state.clear_demands_attention"), \
                patch("clipllm02.desktop.window_base.x11_state.active_window",
                      return_value=0x100):
            w.show_without_focus(bring_to_front=True)
            w.hide()
            self.assertEqual([c.args[1] for c in above.call_args_list],
                             [True, False])
            self.assertFalse(w._floated)

    def test_process_text_brings_window_to_front(self):
        app = self.app
        with patch.object(app.window, "show_without_focus") as show:
            app.process_text("NaCl", source="test")
            self.assertTrue(show.called)
            self.assertTrue(show.call_args.kwargs.get("bring_to_front"))
        self.assertEqual(app.state.errors, [])

    def test_show_clears_demands_attention(self):
        w = self.app.window
        with patch.object(w, "_is_offscreen", return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.clear_demands_attention") as clear, \
                patch("clipllm02.desktop.window_base.x11_state.set_above"), \
                patch("clipllm02.desktop.window_base.x11_state.active_window",
                      return_value=None):
            w.show_without_focus(bring_to_front=True)
            self.assertTrue(clear.called)
        w.hide()

    def test_clear_demands_attention_safe_without_x11(self):
        from clipllm02.desktop import x11_state
        self.assertIsInstance(x11_state.clear_demands_attention(1), bool)

    def test_pin_switch_not_exposed_in_settings(self):
        # 置顶改为「查询后 400ms 自动解除」，不再需要用户开关
        from clipllm02.studio.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self.app.settings)
        try:
            self.assertFalse(hasattr(dialog, "pin_on_query_check"))
        finally:
            dialog.close()

    def test_content_update_does_not_raise_window(self):
        # GoldenDict engagePopup：已可见时只在 raiseWindowOnSearch 才 raise。
        # 否则 Chrome 里点一下（剪贴板变化）词典就又被拉到前面。
        w = self.app.window
        w.show()
        with patch.object(w, "raise_") as raised, \
                patch("clipllm02.desktop.window_base.x11_state.set_above") as above:
            w.show_without_focus()          # 已可见：仅内容更新
            raised.assert_not_called()
            above.assert_not_called()
        w.hide()

    def test_unpinned_uses_static_hint_and_hides_instead_of_above(self):
        """GoldenDict unpinned：静态置顶，绝不动态增删 ABOVE，让位靠 hide。"""
        from PySide6.QtCore import Qt
        from clipllm02.desktop.window_base import ResizableWindow
        legacy = ResizableWindow(settings_key="test_above")
        try:
            self.assertTrue(legacy.windowFlags() & Qt.WindowStaysOnTopHint)
            legacy.show()
            with patch.object(legacy, "_is_offscreen", return_value=False), \
                    patch("clipllm02.desktop.window_base.x11_state.set_above") as above:
                legacy._float()
                legacy._unfloat()
                above.assert_not_called()
            legacy._yield_to_other_window()
            self.assertFalse(legacy.isVisible())
        finally:
            legacy.close()

    def test_global_esc_dismisses_without_clicking_window(self):
        w = self.app.window
        w.show()
        with patch.object(w, "_is_offscreen", return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.escape_pressed",
                      return_value=True), \
                patch("clipllm02.desktop.window_base.x11_state.active_window",
                      return_value=None), \
                patch("clipllm02.desktop.window_base.x11_state.pointer_buttons",
                      return_value=0), \
                patch("clipllm02.desktop.window_base.x11_state.set_above"), \
                patch.object(w, "_mouse_inside", return_value=True):
            w._poll_mouse()
            w._poll_mouse()                 # tick 2：触发一次
            w._poll_mouse()
            w._poll_mouse()                 # 仍按住：不得重复触发
        self.assertFalse(w.isVisible())     # 已连到 MainWindow._on_dismiss

    def test_escape_pressed_is_safe_without_x11(self):
        from clipllm02.desktop import x11_state
        self.assertIsInstance(x11_state.escape_pressed(), bool)

    def test_content_update_refreshes_active_baseline(self):
        w = self.app.window
        w.show()
        with patch.object(w, "_is_offscreen", return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.active_window",
                      return_value=0xABC), \
                patch("clipllm02.desktop.window_base.x11_state.set_above"):
            w.show_without_focus()
            self.assertEqual(w._active_at_show, 0xABC)
        w.hide()

    def test_legacy_popup_mode_still_hides(self):
        from PySide6.QtCore import Qt
        from clipllm02.desktop.window_base import ResizableWindow
        legacy = ResizableWindow(settings_key="test_legacy")
        try:
            legacy.show()
            self.assertEqual(legacy.windowFlags() & Qt.Tool, Qt.Tool)
            self.assertTrue(legacy.testAttribute(Qt.WA_ShowWithoutActivating))
            legacy._yield_to_other_window()
            self.assertFalse(legacy.isVisible())
        finally:
            legacy.close()

    def test_opt_out_restores_tool_window(self):
        from PySide6.QtCore import Qt
        from clipllm02.studio.main_window import MainWindow
        settings = load_config()
        settings.data["desktop_persistent_window"] = False
        window = MainWindow(settings)
        try:
            self.assertEqual(window.windowFlags() & Qt.Tool, Qt.Tool)
        finally:
            window.close()

    def test_settings_toggle_never_remaps_live_window(self):
        from clipllm02.studio.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self.app.settings)
        try:
            self.assertTrue(dialog.persistent_check.isChecked())
            dialog.persistent_check.setChecked(False)
            self.assertFalse(dialog.data["desktop_persistent_window"])
            before = self.app.window.windowFlags()
            self.app.window.set_settings(self.app.settings)
            self.assertEqual(self.app.window.windowFlags(), before)
        finally:
            dialog.close()


class TestGoldenDictScanPopup(unittest.TestCase):
    """默认 unpinned：静态置顶 + 查询 show + 400ms / 点击即隐藏。"""

    def setUp(self):
        self.app = make_app(pinned=False)

    def tearDown(self):
        stop(self.app)

    def test_default_is_unpinned_with_static_hint(self):
        from PySide6.QtCore import Qt
        w = self.app.window
        self.assertFalse(w._persistent)
        self.assertEqual(w.windowFlags() & Qt.Tool, Qt.Tool)
        self.assertTrue(w.windowFlags() & Qt.WindowStaysOnTopHint)
        self.assertTrue(w.testAttribute(Qt.WA_ShowWithoutActivating))

    def test_query_shows_on_top_without_dynamic_above(self):
        w = self.app.window
        w.hide()
        with patch.object(w, "_is_offscreen", return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.set_above") as above, \
                patch("clipllm02.desktop.window_base.x11_state.clear_demands_attention"), \
                patch("clipllm02.desktop.window_base.x11_state.active_window",
                      return_value=0x100), \
                patch.object(w, "raise_") as raised:
            w.show_without_focus(bring_to_front=True)
            self.assertTrue(w.isVisible())
            self.assertTrue(raised.called)
            above.assert_not_called()          # 只靠静态 hint
        w.hide()

    def test_leave_hides_after_400ms(self):
        w = self.app.window
        w.show_without_focus(bring_to_front=True)
        w.leaveEvent(None)
        self.assertTrue(w._hide_timer.isActive())
        self.assertEqual(w._hide_timer.interval(), 400)
        w._hide_timer.timeout.emit()
        self.assertFalse(w.isVisible())         # 客户端 unmap，零迟滞

    def test_enter_cancels_pending_hide(self):
        w = self.app.window
        w.show_without_focus(bring_to_front=True)
        w.leaveEvent(None)
        w.enterEvent(None)
        self.assertFalse(w._hide_timer.isActive())
        self.assertTrue(w.isVisible())
        w.hide()

    def test_outside_press_hides_immediately(self):
        w = self.app.window
        with patch.object(w, "_is_offscreen", return_value=False), \
                patch.object(w, "_mouse_inside", return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.pointer_buttons",
                      return_value=1 << 8), \
                patch("clipllm02.desktop.window_base.x11_state.active_window",
                      return_value=0x100), \
                patch("clipllm02.desktop.window_base.x11_state.escape_pressed",
                      return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.clear_demands_attention"):
            w.show_without_focus(bring_to_front=True)
            w._poll_mouse()
            self.assertFalse(w.isVisible())

    def test_alt_tab_switch_away_hides(self):
        w = self.app.window
        with patch.object(w, "_is_offscreen", return_value=False), \
                patch.object(w, "_mouse_inside", return_value=True), \
                patch("clipllm02.desktop.window_base.x11_state.active_window",
                      side_effect=[0x100, 0x200, 0x200, 0x200]), \
                patch("clipllm02.desktop.window_base.x11_state.pointer_buttons",
                      return_value=0), \
                patch("clipllm02.desktop.window_base.x11_state.escape_pressed",
                      return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.clear_demands_attention"):
            w.show_without_focus(bring_to_front=True)   # 基准 0x100
            self.assertEqual(w._active_at_show, 0x100)
            for _ in range(6):
                w._poll_mouse()
            self.assertFalse(w.isVisible())

    def test_next_query_reshows_window(self):
        w = self.app.window
        w.show_without_focus(bring_to_front=True)
        w._yield_to_other_window()
        self.assertFalse(w.isVisible())
        w.show_without_focus(bring_to_front=True)
        self.assertTrue(w.isVisible())          # 新 map 天然在顶层
        w.hide()

    def test_global_esc_works_in_unpinned(self):
        w = self.app.window
        w.show()
        with patch.object(w, "_is_offscreen", return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.escape_pressed",
                      return_value=True), \
                patch("clipllm02.desktop.window_base.x11_state.active_window",
                      return_value=None), \
                patch("clipllm02.desktop.window_base.x11_state.pointer_buttons",
                      return_value=0), \
                patch("clipllm02.desktop.window_base.x11_state.set_above"), \
                patch("clipllm02.desktop.window_base.x11_state.clear_demands_attention"), \
                patch.object(w, "_mouse_inside", return_value=True):
            w._poll_mouse()
            w._poll_mouse()
        self.assertFalse(w.isVisible())

    def test_settings_default_is_pinned_and_can_opt_out(self):
        from PySide6.QtCore import Qt
        from clipllm02.studio.config import load_config
        from clipllm02.studio.main_window import MainWindow
        from clipllm02.studio.settings_dialog import SettingsDialog
        settings = load_config()
        # 默认 pinned：常驻、Alt+Tab 可见、点其它窗口只沉底
        self.assertTrue(bool(settings.get("desktop_persistent_window", True)))
        window = MainWindow(settings)
        try:
            self.assertNotEqual(window.windowFlags() & Qt.Tool, Qt.Tool)
        finally:
            window.close()
        dialog = SettingsDialog(settings)
        try:
            self.assertTrue(dialog.persistent_check.isChecked())
            dialog.persistent_check.setChecked(False)
            self.assertFalse(dialog.data["desktop_persistent_window"])
        finally:
            dialog.close()


class TestFollowupConversation(unittest.TestCase):
    """追问必须是多轮对话：沿用上一轮领域设定，并带上问答历史。"""

    def setUp(self):
        self.app = make_app()

    def tearDown(self):
        stop(self.app)

    def test_followup_reuses_system_and_history(self):
        """追问只把追问文本作为正文，领域设定与历史都沿用上一轮。"""
        tab = self.app.window.current_tab()
        tab.chat_history = [
            {"role": "user", "content": "NaCl"},
            {"role": "assistant", "content": '{"title":"T","answer":"A"}'},
        ]
        tab.chat_system = "SYS-ORIGINAL"
        tab.last_selection = {"intent_id": "chem.legacy", "domain_id": "chem",
                              "format_id": "static"}
        with patch.object(self.app, "on_intent") as intent:
            self.app.on_followup(tab, "为什么？")
            self.assertTrue(intent.called)
        payload = tab._followup
        self.assertEqual(payload["question"], "为什么？")     # 不是拼接后的长文
        self.assertTrue(payload["system"].startswith("SYS-ORIGINAL"))
        self.assertIn("【追问模式 · 自由作答】", payload["system"])
        self.assertIn("不要输出 JSON", payload["system"])
        self.assertNotIn("【输出契约", payload["system"])
        self.assertEqual([m["role"] for m in payload["history"]],
                         ["user", "assistant"])
        self.assertEqual(payload["history"][0]["content"], "NaCl")

    def test_followup_without_history_falls_back(self):
        tab = self.app.window.current_tab()
        tab.chat_history = []
        with patch.object(self.app, "on_query_submitted") as submit:
            self.app.on_followup(tab, "独立问题")
            self.assertTrue(submit.called)
            self.assertEqual(submit.call_args[0][0], "独立问题")

    def test_followup_appends_instead_of_replacing(self):
        """追问结果追加到对话，前几轮仍可回看。"""
        from clipllm02.studio.json_render import render_json_result
        self.app.process_text("NaCl", source="test")
        tab = self.app.window.current_tab()
        tab.show_result(render_json_result('{"title":"第一轮","answer":"A1"}'),
                        title="解释结果")
        tab.show_result(render_json_result('{"title":"第二轮","answer":"A2"}'),
                        title="解释结果", append=True, user_text="为什么？")
        tab.show_result(render_json_result('{"title":"第三轮","answer":"A3"}'),
                        title="解释结果", append=True, user_text="再展开")
        text = tab.view.toPlainText()
        for mark in ("第一轮", "第二轮", "第三轮"):
            self.assertIn(mark, text)
        self.assertEqual(text.count("追问："), 2)
        # 新查询会清空上一轮会话
        tab.set_context(tab.env, tab.policy, tab.suggestion, tab.menu)
        self.assertEqual(tab.chat_blocks, [])

    def test_user_bubble_renders_formula(self):
        bubble = self.app.window.current_tab()._user_bubble("$$E=mc^2$$")
        self.assertNotIn("$$", bubble)
        self.assertIn("mc²", bubble)

    def test_scroll_anchor_per_action(self):
        """新查询从顶部看、追问定位到本轮：交给 _render_chat 的锚点控制。"""
        from clipllm02.studio.json_render import render_json_result
        from unittest.mock import patch
        self.app.process_text("NaCl", source="test")
        tab = self.app.window.current_tab()
        body = render_json_result(json.dumps(
            {"title": "T", "answer": "A"}, ensure_ascii=False))
        with patch.object(tab, "_render_chat") as render:
            tab.show_result(body, title="解释结果")
            render.assert_called_with("top")

            tab.show_result(body, title="解释结果",
                            append=True, user_text="为什么？")
            render.assert_called_with("new")

            tab.show_loading("NaCl", hint="正在处理…")
            render.assert_called_with("bottom")

    def test_bottom_actions_are_flat_and_compact(self):
        """底部操作是 20px 纯文字链，不再是一整条导航栏。"""
        tab = self.app.window.current_tab()
        for name in ("back_button", "copy_result_button",
                     "dismiss_result_button"):
            button = getattr(tab, name)
            self.assertEqual(button.height(), 20, name)
            self.assertEqual(button.objectName(), "textAction", name)

    def test_field_head_has_no_full_width_background(self):
        """分节标题不再铺整行底色，避免连成灰块。"""
        from clipllm02.studio.json_render import render_json_result
        body = render_json_result(json.dumps(
            {"read_aloud": "读法"}, ensure_ascii=False)).split("</style>", 1)[1]
        self.assertNotIn("background-color", body.split("读法")[0])

    def test_free_input_box_is_minimal(self):
        """自定义输入卡片只留输入框与发送图标。"""
        from PySide6.QtWidgets import QComboBox
        self.app.process_text("NaCl", source="test")
        tab = self.app.window.current_tab()
        tab.free_box.setVisible(True)
        self.assertEqual(tab.free_box.findChildren(QComboBox), [])
        self.assertFalse(hasattr(tab, "ime_button"))
        self.assertFalse(hasattr(tab, "mode_prompt"))
        self.assertEqual(tab.free_send.objectName(), "sendIcon")
        self.assertFalse(tab.free_send.icon().isNull())
        self.assertEqual(tab.free_send.text(), "")


class TestGatewayMessages(unittest.TestCase):
    """网关必须把 history 编进 messages，而不是拼成一条 user 文本。"""

    def test_multiturn_messages_order(self):
        from clipllm02.desktop.gateway import Gateway, GatewayConfig
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"{}"}}]}'

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResp()

        gateway = Gateway(GatewayConfig(api_url="http://x", api_key="k",
                                        model="m"))
        with patch("urllib.request.urlopen", fake_urlopen):
            gateway.explain(
                "SYS", "Q2",
                history=[{"role": "user", "content": "Q1"},
                         {"role": "assistant", "content": "A1"},
                         {"role": "system", "content": "注入无效"},
                         {"role": "user", "content": ""}])
        roles = [m["role"] for m in captured["body"]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertEqual(captured["body"]["messages"][0]["content"], "SYS")
        self.assertEqual(captured["body"]["messages"][-1]["content"], "Q2")

    def test_no_history_is_single_turn(self):
        from clipllm02.desktop.gateway import Gateway, GatewayConfig
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"{}"}}]}'

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResp()

        gateway = Gateway(GatewayConfig(api_url="http://x", api_key="k",
                                        model="m"))
        with patch("urllib.request.urlopen", fake_urlopen):
            gateway.explain("SYS", "Q1")
        self.assertEqual([m["role"] for m in captured["body"]["messages"]],
                         ["system", "user"])


class TestRenderCache(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def tearDown(self):
        stop(self.app)

    def test_worker_result_caches_raw_text(self):
        app = self.app
        app.process_text("NaCl", source="test")
        tab = app.window.current_tab()
        app.on_intent(tab, dict(SELECTION))
        raw = '{"title":"X","answer":"Y"}'
        payload = (tab, tab.request_generation,
                   GatewayResult(html="", ok=True, raw=raw),
                   "key", 14, False, tab.env.envelope_id, "rid")
        with patch.object(app.cache, "put") as put:
            app._on_worker_done(payload)
        self.assertEqual(put.call_args[0][1], raw)

    def test_cache_hit_rerenders_with_current_look(self):
        from clipllm02.desktop.gateway import Gateway, GatewayConfig
        from clipllm02.studio import app as app_module

        app = self.app
        app.settings.data["desktop_dark_theme"] = True
        app.settings.data["desktop_font_size"] = 20
        app.gateway = Gateway(GatewayConfig(
            api_url="http://example.invalid", api_key="k", model="m"))
        app.process_text("NaCl", source="test")
        tab = app.window.current_tab()
        raw = '{"title":"X","answer":"Y"}'
        with patch.object(app.cache, "get", return_value=raw), \
                patch.object(app_module, "render_json_result",
                             wraps=app_module.render_json_result) as render:
            app.on_intent(tab, dict(SELECTION))
        self.assertTrue(render.called)
        self.assertTrue(render.call_args.kwargs["dark"])
        self.assertEqual(render.call_args.kwargs["font_size"], 20)
        self.assertIn("Y", tab.view.toPlainText())

    def test_schema_version_changes_cache_key(self):
        from clipllm02.prompts import compose_prompt
        from clipllm02.studio.config import load_config
        composed = compose_prompt(
            load_config().intent("chem.legacy"), load_config())
        kwargs = dict(model="m", route_id=composed.query_type, intent_id="i",
                      format_id="static", schema_version=RENDER_SCHEMA,
                      ruleset_version="r", query_hash="q")
        self.assertNotEqual(
            composed.cache_key(**kwargs),
            composed.cache_key(**{**kwargs, "schema_version": "1.0.0"}))

    def test_result_view_has_scrollbar(self):
        from PySide6.QtCore import Qt
        from clipllm02.studio.config import load_config
        from clipllm02.studio.query_tab import QueryTab
        tab = QueryTab(load_config())
        self.assertEqual(tab.view.verticalScrollBarPolicy(), Qt.ScrollBarAsNeeded)


class TestAutoIntent(unittest.TestCase):
    """恢复旧分类器 gd_llm.py 的自动意图识别：默认高亮 + 置顶，仍不替用户提交。"""
    def setUp(self):
        self.settings = load_defaults()   # 出厂配置，与用户个人配置解耦

    def _check(self, text, *, source="test", prefs=None):
        env = InputEnvelope.for_text(text, source=source)
        policy = preflight(env, self.settings)
        sug = suggest(env, policy, self.settings)
        menu = build_menu(sug, self.settings, prefs)
        return policy, sug, menu

    def test_chinese_four_way_dispatch(self):
        cases = [
            ("逻辑斯谛映射是数学中描述非线性动力系统的经典模型。", "zh.sentence"),
            ("逻辑斯谛映射是数学中描述非线性动力系统的经典模型。它的形式很简单。",
             "zh.paragraph"),
            ("为什么参数变化会让迭代结果进入混沌状态？", "zh.question"),
            ("非线性动力学", "zh.legacy"),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected):
                policy, sug, menu = self._check(text)
                self.assertTrue(policy.allowed)
                self.assertEqual(sug.suggested_intent, expected)
                self.assertEqual(menu.default_intent, expected)
                ids = [o.intent.id for o in menu.recommended]
                self.assertIn(expected, ids)
                # 该类别若存在复用模板的条目，它必须排第一
                legacy = [i for i in ids if i.endswith(".legacy")]
                if legacy:
                    self.assertEqual(ids[0], legacy[0])

    def test_other_modalities_dispatch(self):
        cases = [
            ("E=mc²", "math.legacy"), ("NaCl", "chem.legacy"),
            ("construct", "en.legacy"), ("break the ice", "en.phrase.legacy"),
            ("def f(x):\n    return x + 1", "code.legacy"),
        ]
        for text, expected in cases:
            with self.subTest(text=text[:12]):
                _, sug, menu = self._check(text)
                self.assertEqual(sug.suggested_intent, expected)
                self.assertEqual(menu.default_intent, expected)

    def test_vision_source_prefers_formula_intent(self):
        _, sug, menu = self._check(r"\(E = mc^2\)", source="vision")
        self.assertEqual(sug.suggested_intent, "math.legacy")
        ids = [o.intent.id for o in menu.recommended]
        self.assertTrue(ids[0].endswith(".legacy"))   # 模板条目仍占首位
        self.assertIn("math.read", ids)

    def test_history_preference_wins_over_classifier(self):
        # 用 chinese 模板（下挂 5 条查询方式）验证用户习惯优先于自动判定
        text = "非线性动力学"
        prefs = Preferences()
        prefs.record(sha256_text(text), "zh.rewrite", "zh")
        _, _, menu = self._check(text, prefs=prefs)
        self.assertEqual(menu.default_intent, "zh.rewrite")

    def test_legacy_template_stays_in_menu(self):
        _, _, menu = self._check("NaCl")
        ids = [o.intent.id for o in menu.recommended]
        self.assertEqual(ids[0], "chem.legacy")   # 首项即提示词模板条目
        self.assertIn("chem.legacy", ids)      # 自动推荐紧随其后

    def test_other_angles_still_available(self):
        _, _, menu = self._check("逻辑斯谛映射是数学中描述非线性动力系统的经典模型。")
        self.assertTrue(menu.others)
        self.assertTrue(menu.free_input["enabled"])


class TestChinesePolicySplit(unittest.TestCase):
    def setUp(self):
        self.settings = load_defaults()

    def test_fake_word_only_matches_ascii(self):
        result = preflight(InputEnvelope.for_text("bcdfghjklmnpqrstvwxz"),
                           self.settings)
        self.assertIn("fake_word", [h.reason for h in result.hits])
        for text in ("这是一段普通的中文说明文字，长度已经超过十三个汉字了。",
                     "哈哈哈哈", "非线性动力学"):
            with self.subTest(text=text[:10]):
                result = preflight(InputEnvelope.for_text(text), self.settings)
                self.assertNotIn("fake_word", [h.reason for h in result.hits])

    def test_sentence_uses_120_not_20(self):
        result = preflight(InputEnvelope.for_text("深" * 130 + "。"), self.settings)
        self.assertTrue(result.allowed)
        hits = [h.reason for h in result.hits]
        self.assertIn("chinese_sentence_too_long", hits)
        self.assertNotIn("chinese_too_long", hits)

    def test_paragraph_uses_600(self):
        result = preflight(InputEnvelope.for_text(
            "深" * 300 + "。" + "深" * 301 + "。"), self.settings)
        self.assertTrue(result.allowed)
        hits = [h.reason for h in result.hits]
        self.assertIn("chinese_paragraph_too_long", hits)
        self.assertNotIn("chinese_too_long", hits)

    def test_term_still_uses_20(self):
        result = preflight(InputEnvelope.for_text("深" * 25), self.settings)
        self.assertIn("chinese_too_long", [h.reason for h in result.hits])
        self.assertNotIn("chinese_sentence_too_long", [h.reason for h in result.hits])

    def test_question_uses_its_own_limit(self):
        result = preflight(InputEnvelope.for_text("深" * 130 + "？"), self.settings)
        self.assertIn("chinese_question_too_long", [h.reason for h in result.hits])
        self.assertNotIn("chinese_too_long", [h.reason for h in result.hits])

    def test_condition_tree_validation(self):
        from clipllm02.settings import Settings, validate
        data = self.settings.as_dict()
        data["policy"]["rules"]["custom_tree"] = {
            "level": "WARN", "locked": False, "detector": "regex",
            "pattern": "x",
            "applies_when": {"all": [
                {"feature": "has_cn", "op": "eq", "value": True},
                {"not": {"feature": "cn_question", "op": "eq", "value": True}},
            ]},
            "description": "复合条件树",
        }
        self.assertEqual(validate(Settings(data)), [])


class TestCategoryClassification(unittest.TestCase):
    """类别初判：判定最可能的类别 + 动态提供其他可选类别。"""
    def setUp(self):
        self.settings = load_defaults()

    def _build(self, text, *, source="test"):
        env = InputEnvelope.for_text(text, source=source)
        policy = preflight(env, self.settings)
        sug = suggest(env, policy, self.settings)
        return env, policy, sug, build_menu(sug, self.settings)

    def test_sentence_is_judged_as_sentence_not_term(self):
        text = "逻辑斯谛映射是数学中描述非线性动力系统的经典模型。"
        c = classify_text(text, self.settings)
        self.assertEqual(c.category, "chinese_sentence")
        self.assertEqual(c.label, "中文句解")
        _, _, _, menu = self._build(text)
        self.assertEqual(menu.category, "chinese_sentence")
        self.assertEqual(menu.category_label, "中文句解")
        self.assertEqual(menu.default_intent, "zh.sentence")

    def test_four_chinese_categories(self):
        cases = [
            ("为什么天空是蓝色的？", "chinese_question"),
            ("第一句是定义。第二句给出了形式。第三句说明参数变化。",
             "chinese_paragraph"),
            ("非线性动力学", "chinese"),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_text(text, self.settings).category,
                                 expected)

    def test_half_selected_sentence_is_not_term(self):
        text = "当参数逐渐增大时，迭代结果会从收敛变为周期振荡，再进入混沌"
        self.assertEqual(classify_text(text, self.settings).category,
                         "chinese_sentence")

    def test_other_modalities(self):
        cases = [("E=mc²", "latex"), ("NaCl", "chem_substance"),
                 ("construct", "en_word"), ("break the ice", "en_phrase"),
                 ("def f(x):\n    return x + 1", "code")]
        for text, expected in cases:
            with self.subTest(text=text[:12]):
                self.assertEqual(classify_text(text, self.settings).category,
                                 expected)

    def test_vision_source_prefers_formula(self):
        self.assertEqual(classify_text(r"\(E = mc^2\)", self.settings,
                                       source="vision").category, "latex")

    def test_categories_are_dynamic_from_routes(self):
        settings = load_defaults()
        settings.routes.pop("chinese_paragraph", None)
        settings.routes["my_custom"] = {"enabled": True, "label": "我的类别"}
        c = classify_text("为什么天空是蓝色的？", settings)
        ids = [item.id for item in c.candidates]
        self.assertIn("my_custom", ids)
        self.assertNotIn("chinese_paragraph", ids)

    def test_menu_exposes_other_categories_and_multi_faceted_intents(self):
        _, _, _, menu = self._build(
            "逻辑斯谛映射是数学中描述非线性动力系统的经典模型。")
        self.assertEqual(menu.category_chips[0].id, menu.category)
        self.assertGreater(len(menu.category_chips), 3)
        self.assertTrue(menu.others)                   # 其他角度仍在
        self.assertTrue(menu.free_input["enabled"])
        # 同一模板下的查询方式全量显示，不再被旧上限截断
        _, _, _, math_menu = self._build("E=mc²")
        expected = sum(1 for i in self.settings.intents
                       if i.primary_route == math_menu.category and i.enabled)
        self.assertEqual(len(math_menu.recommended), expected)
        self.assertGreater(len(math_menu.recommended), 9)

    def test_switching_category_rebuilds_intents(self):
        _, _, sug, _ = self._build(
            "逻辑斯谛映射是数学中描述非线性动力系统的经典模型。")
        menu = build_menu(sug, self.settings, category_id="chinese_paragraph")
        self.assertEqual(menu.category, "chinese_paragraph")
        self.assertEqual(menu.recommended[0].intent.id, "zh.paragraph")
        self.assertEqual(menu.default_intent, "zh.paragraph")

    def test_caption_shows_category_not_fixed_domain_label(self):
        from clipllm02.studio.query_tab import QueryTab
        env, policy, sug, menu = self._build(
            "逻辑斯谛映射是数学中描述非线性动力系统的经典模型。")
        tab = QueryTab(self.settings)
        tab.set_context(env, policy, sug, menu)
        self.assertIn("中文句解", tab.intent_caption.text())
        self.assertNotIn("中文 / 术语", tab.intent_caption.text())
        self.assertFalse(tab.category_drawer.isHidden())
        options = [label for _, label in tab._chip_options]
        self.assertTrue(options[0].startswith("初判 ·"))
        self.assertGreater(len(options), 1)
        self.assertTrue(all(b.isHidden() for b in tab._chip_buttons))

    def test_category_switch_through_app_does_not_submit(self):
        app = make_app()
        try:
            app.process_text("逻辑斯谛映射是数学中描述非线性动力系统的经典模型。",
                             source="test")
            tab = app.window.current_tab()
            tab._chip_buttons[1].click()            # 点击「其他类别」
            self.assertEqual(tab.stack.currentIndex(), 0)
            self.assertEqual(app.state.stats["requests"], 0)
            self.assertEqual(app.state.errors, [])
        finally:
            stop(app)

    def test_explain_trace_is_available(self):
        out = explain_text("逻辑斯谛映射是数学中描述非线性动力系统的经典模型。",
                           self.settings)
        self.assertIn("L3", out)
        self.assertIn("判定类别", out)
        self.assertIn("chinese_sentence", out)


class TestDrawerAndTemplates(unittest.TestCase):
    """初判层「文字 + 抽屉」、脚本模板全文、类别开关随模板增删。"""
    def setUp(self):
        self.settings = load_defaults()

    def _tab(self):
        from clipllm02.studio.query_tab import QueryTab
        text = "逻辑斯谛映射是数学中描述非线性动力系统的经典模型。"
        env = InputEnvelope.for_text(text, source="test")
        policy = preflight(env, self.settings)
        sug = suggest(env, policy, self.settings)
        menu = build_menu(sug, self.settings)
        tab = QueryTab(self.settings)
        tab.set_context(env, policy, sug, menu)
        return tab

    def test_drawer_emits_category_change(self):
        tab = self._tab()
        captured = []
        tab.category_changed.connect(captured.append)
        target = tab._chip_options[1][0]
        tab._emit_chip(target)
        self.assertEqual(captured, [target])

    def test_preview_card_removed_but_text_still_available(self):
        tab = self._tab()
        self.assertTrue(tab.preview.parent().isHidden())
        self.assertIn("逻辑斯谛", tab.preview.text())
        self.assertFalse(tab.category_drawer.isHidden())
        self.assertTrue(all(b.isHidden() for b in tab._chip_buttons))

    def test_no_chip_borders_in_style(self):
        from clipllm02.studio.query_tab import STYLE
        self.assertNotIn("QPushButton#chipBtn", STYLE)
        self.assertIn("QFrame#card { background:transparent; border:none; }",
                      STYLE)
        self.assertIn("QToolButton#drawer", STYLE)

    def test_defaults_use_full_script_prompts(self):
        self.assertGreater(len(self.settings.routes["en_word"]["prompt"]), 1000)
        self.assertEqual(self.settings.routes["en_word"]["label"], "英语单词")
        self.assertEqual(self.settings.routes["chinese_sentence"]["label"],
                         "中文句解")

    def test_category_switches_match_route_count(self):
        from clipllm02.studio.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self.settings)
        try:
            self.assertEqual(set(dialog.domain_checks),
                             set(dialog.data["routes"]))
            dialog.data["routes"]["zz_new"] = {
                "id": "zz_new", "enabled": True, "label": "新类别", "prompt": ""}
            dialog._rebuild_switches()
            self.assertIn("zz_new", dialog.domain_checks)
            cb = dialog.domain_checks["zz_new"]
            cb.setChecked(False)
            self.assertFalse(dialog.data["routes"]["zz_new"]["enabled"])
            dialog.data["routes"].pop("zz_new")
            dialog._rebuild_switches()
            self.assertNotIn("zz_new", dialog.domain_checks)
        finally:
            dialog.close()

    def test_new_route_can_reuse_script_prompt(self):
        from clipllm02.studio.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self.settings)
        try:
            with patch("clipllm02.studio.settings_dialog.QInputDialog.getText",
                       return_value=("my_route", True)), \
                    patch("clipllm02.studio.settings_dialog.QInputDialog.getItem",
                          return_value=("en_word · 英语单词", True)):
                dialog._add_route()
            route = dialog.data["routes"]["my_route"]
            self.assertGreater(len(route["prompt"]), 1000)
            self.assertIn("英语单词", route["label"])
            self.assertIn("my_route", dialog.domain_checks)
        finally:
            dialog.close()


class TestThreeLayers(unittest.TestCase):
    """模板 → 查询方式 → 其他视角 三层对应，以及设置改动即时反映到 UI。"""
    def setUp(self):
        self.settings = load_defaults()

    def _menu(self, text, settings=None):
        settings = settings or self.settings
        env = InputEnvelope.for_text(text, source="test")
        policy = preflight(env, settings)
        return env, policy, suggest(env, policy, settings), \
            build_menu(suggest(env, policy, settings), settings)

    def test_recommended_count_matches_route_intents(self):
        for text in ("E=mc²", "NaCl", "非线性动力学", "construct"):
            with self.subTest(text=text):
                _, _, _, menu = self._menu(text)
                expected = sum(
                    1 for i in self.settings.intents
                    if i.primary_route == menu.category and i.enabled)
                self.assertEqual(len(menu.recommended), expected)

    def test_other_views_are_editable_generic_angles(self):
        settings = load_defaults()
        settings.data["other_views"] = {
            "max_items": 4,
            "items": [
                {"id": "origin", "label": "历史背景与起源", "prompt": "p1",
                 "enabled": True},
                {"id": "mine", "label": "我的自定义视角", "prompt": "p2",
                 "enabled": True},
                {"id": "off", "label": "停用的视角", "prompt": "p3",
                 "enabled": False},
            ]}
        _, _, _, menu = self._menu(
            "逻辑斯谛映射是数学中描述非线性动力系统的经典模型。", settings)
        labels = [o.intent.label for o in menu.others]
        self.assertEqual(labels, ["历史背景与起源", "我的自定义视角"])
        self.assertTrue(all(o.intent.id.startswith("view.")
                            for o in menu.others))

    def test_three_settings_layers_share_route_count(self):
        from clipllm02.studio.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self.settings)
        try:
            count = len(dialog.data["routes"])
            self.assertEqual(dialog.prompt_tree.topLevelItemCount(), count)
            self.assertEqual(len(dialog.domain_checks), count)
            # 其他视角与模板无关：数量 = 通用视角条目数
            self.assertEqual(dialog.other_view_list.count(),
                             len(dialog.data["other_views"]["items"]))
            titles = [dialog.tabs.tabText(i)
                      for i in range(dialog.tabs.count())]
            self.assertTrue(any("提示词模板" in t for t in titles))
            self.assertTrue(any("查询方式" in t for t in titles))
            self.assertTrue(any("其他视角" in t for t in titles))
        finally:
            dialog.close()

    def test_settings_tab_order_is_fixed(self):
        from clipllm02.studio.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self.settings)
        try:
            titles = [dialog.tabs.tabText(i).replace(" ", "")
                      for i in range(dialog.tabs.count())]
            self.assertEqual(titles, [
                "外观", "拦截", "提示词模板", "查询方式", "其他视角",
                "搜索", "模型与图片", "知识库", "行为"])
            # 快捷键不再是独立页签，并入「行为」
            self.assertEqual(len(dialog.hotkey_edits), 10)
        finally:
            dialog.close()

    def test_edit_intent_reflects_in_open_tab(self):
        app = make_app()
        try:
            app.process_text("NaCl", source="test")
            tab = app.window.current_tab()
            data = app.settings.as_dict()
            for item in data["intents"]:
                if item["id"] == "chem.legacy":
                    item["label"] = "改过的查询名"
            app.reload_settings(data)
            texts = [b.text() for b in tab._intent_buttons if not b.isHidden()]
            self.assertTrue(any("改过的查询名" in t for t in texts))
            self.assertEqual(app.state.errors, [])
        finally:
            stop(app)

    def test_other_view_edit_reflects_in_open_tab(self):
        app = make_app()
        try:
            app.process_text("逻辑斯谛映射是数学中描述非线性动力系统的经典模型。",
                             source="test")
            tab = app.window.current_tab()
            data = app.settings.as_dict()
            data["other_views"]["items"][0]["label"] = "改过的通用视角"
            app.reload_settings(data)
            texts = [b.text() for b in tab._other_buttons if not b.isHidden()]
            self.assertIn("改过的通用视角", texts)
            self.assertEqual(app.state.errors, [])
        finally:
            stop(app)

    def test_all_actions_are_text_only(self):
        from clipllm02.studio.query_tab import QueryTab
        tab = QueryTab(self.settings)
        for button in (tab.back_button, tab.copy_result_button,
                       tab.dismiss_button, tab.dismiss_result_button):
            self.assertFalse(any(ch in button.text() for ch in "⌫⎘✕"),
                             button.text())
        # 关闭按钮内联在页面里，不再有独立导航栏
        self.assertIsNotNone(tab.dismiss_button.parent())
        self.assertIsNotNone(tab.dismiss_result_button.parent())


class TestCacheHitSession(unittest.TestCase):
    """缓存命中也必须登记会话并落盘，否则追问会带旧上下文或串进旧笔记。"""

    def setUp(self):
        self.app = make_app()
        self.tmp = tempfile.TemporaryDirectory()
        self.app.settings.data["desktop_library_dir"] = self.tmp.name
        self.app.settings.data["desktop_library_enabled"] = True

    def tearDown(self):
        stop(self.app)
        self.tmp.cleanup()

    def test_cache_hit_records_history_and_writes_note(self):
        from dataclasses import replace

        tab = self.app.window.current_tab()
        tab.env = InputEnvelope.for_text("NaCl", source="test")
        raw = '{"title":"氯化钠","answer":"食盐"}'
        with patch.object(self.app.gateway, "config",
                          replace(self.app.gateway.config, api_url="http://local",
                                  api_key="k", model="m")), \
                patch.object(self.app.cache, "get", return_value=raw):
            self.app.on_intent(tab, dict(SELECTION))
        self.assertEqual(len(tab.chat_history), 2)       # 追问有上下文可用
        self.assertEqual(tab.chat_history[1]["content"], raw)
        notes = list(Path(self.tmp.name).rglob("*.md"))
        self.assertEqual(len(notes), 1)                  # 笔记与界面同步
        text = notes[0].read_text(encoding="utf-8")
        self.assertIn("食盐", text)
        self.assertIn('session: "', text)


class TestUiPolish(unittest.TestCase):
    """等待卡片与模板名称中文化。"""

    def setUp(self):
        self.app = make_app()

    def tearDown(self):
        stop(self.app)

    def test_loading_is_one_short_sentence(self):
        tab = self.app.window.current_tab()
        tab.show_loading("NaCl", hint="正在请求模型..")
        self.assertEqual(tab.stack.currentIndex(), 1)   # 等待界面要看得见
        block = tab._loading_block
        self.assertIn("正在请求模型..", block)
        self.assertIn("bgcolor", block)                 # 卡片有底色
        self.assertNotIn("正在处理：", block)
        self.assertNotIn("通常需要几秒", block)

    def test_image_refs_are_stripped_before_query(self):
        from clipllm02.studio.app import strip_image_refs
        self.assertEqual(
            strip_image_refs("@image:/tmp/a.png $$E=mc^2$$"), "$$E=mc^2$$")
        self.assertEqual(strip_image_refs("@image:/tmp/a.png"), "")
        with patch("clipllm02.studio.app.preflight") as pre:
            self.app.process_text("@image:/tmp/a.png", source="clipboard")
            self.assertFalse(pre.called)

    def test_second_vision_starts_a_clean_screen(self):
        tab = self.app.window.current_tab()
        tab.show_result(render_json_result('{"title":"第一轮","answer":"A1"}'))
        self.assertGreater(len(tab.chat_blocks), 0)
        tab.begin_new_session()
        tab.show_loading("x", hint="正在识别图片..")
        self.assertEqual(tab.chat_blocks, [])
        self.assertIn("正在识别图片", tab.view.toPlainText())
        self.assertNotIn("第一轮", tab.view.toPlainText())

    def test_broken_json_fragments_are_not_shown(self):
        from clipllm02.studio.json_render import render_json_result
        raw = ('{"query_type":"latex","title":"T","answer":"A"}\n'
               '{"query_type":"latex","title":"S = \\hbar"')
        html = render_json_result(raw)
        self.assertIn("A", html)
        self.assertNotIn("hbar", html)       # 半截 JSON 不再贴出来

    def test_selected_text_menu_offers_all_seven_engines(self):
        from clipllm02 import load_defaults
        from clipllm02.studio.query_tab import QueryTab
        tab = QueryTab(load_defaults())
        try:
            menu = tab._web_search_menu("NaCl")
            self.assertIsNotNone(menu)
            self.assertEqual(len(menu.actions()), 7)
            received = []
            tab.search_requested.connect(
                lambda pid, text: received.append((pid, text)))
            menu.actions()[0].trigger()
            self.assertEqual(received[0], ("baidu", "NaCl"))
        finally:
            tab.close()

    def test_search_settings_show_and_edit_providers(self):
        from PySide6.QtCore import Qt
        from clipllm02 import load_defaults
        from clipllm02.studio.settings_dialog import SettingsDialog
        dialog = SettingsDialog(load_defaults())
        try:
            table = dialog.provider_table
            self.assertEqual(table.rowCount(), 7)
            self.assertIn("baidu.com", table.item(0, 2).text())
            table.item(0, 0).setCheckState(Qt.Unchecked)
            self.assertFalse(dialog.data["search_providers"][0]["enabled"])
            with patch("clipllm02.studio.settings_dialog.QInputDialog.getText",
                       side_effect=[("豆瓣", True),
                                    ("https://search.douban.com/?q=%s", True)]):
                dialog._add_provider()
            self.assertEqual(table.rowCount(), 8)
            self.assertEqual(dialog.data["search_providers"][-1]["label"], "豆瓣")
            table.setCurrentCell(7, 0)
            dialog._delete_provider()
            self.assertEqual(table.rowCount(), 7)
        finally:
            dialog.close()

    def test_settings_window_can_minimize_and_maximize(self):
        from PySide6.QtCore import Qt
        from clipllm02.studio.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self.app.settings)
        try:
            flags = dialog.windowFlags()
            self.assertTrue(flags & Qt.WindowMinimizeButtonHint)
            self.assertTrue(flags & Qt.WindowMaximizeButtonHint)
            self.assertGreaterEqual(dialog.minimumWidth(), 600)
        finally:
            dialog.close()

    def test_template_names_are_chinese_and_editable(self):
        from clipllm02 import load_defaults
        from clipllm02.studio.settings_dialog import SettingsDialog
        dialog = SettingsDialog(load_defaults())
        try:
            labels = [dialog.route_combo.itemText(i)
                      for i in range(dialog.route_combo.count())]
            self.assertNotIn("en_word", labels)
            self.assertIn("英语单词", labels)
            index = dialog.route_combo.findData("en_word")
            self.assertGreaterEqual(index, 0)
            dialog.route_combo.setCurrentIndex(index)
            dialog.route_label_edit.setText("英语词条")
            self.assertEqual(dialog.data["routes"]["en_word"]["label"],
                             "英语词条")
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
