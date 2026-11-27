"""六个用户反馈问题的回归：Esc、text-input 隔离、快捷键、选项数量、
窗口边缘调节方向、布局结构。"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QToolButton  # noqa: E402

from clipllm02 import InputEnvelope, build_menu, load_defaults, preflight, suggest  # noqa: E402
from clipllm02.desktop.window_base import ResizableWindow  # noqa: E402
from clipllm02.studio import __main__ as studio_main  # noqa: E402
from clipllm02.studio.app import StudioApp  # noqa: E402
from clipllm02.studio.config import DEFAULT_HOTKEYS, HOTKEY_LABELS  # noqa: E402
from clipllm02.studio.input_bridge import InputBridge  # noqa: E402
from clipllm02.studio.input_host import _clear_isolation  # noqa: E402
from clipllm02.studio.main_window import MainWindow  # noqa: E402
from clipllm02.studio.query_tab import QueryTab  # noqa: E402
from clipllm02.studio.settings_dialog import (  # noqa: E402
    SettingsDialog, format_includes, parse_includes,
)
from clipllm02.studio.vision import recognize  # noqa: E402


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def context(text="NaCl"):
    settings = load_defaults()
    env = InputEnvelope.for_text(text, source="test")
    policy = preflight(env, settings)
    s = suggest(env, policy, settings)
    return settings, env, policy, s, build_menu(s, settings)


class Issue1Escape(unittest.TestCase):
    def setUp(self):
        self.settings = load_defaults()
        self.window = MainWindow(self.settings)

    def test_escape_is_bound_and_configurable(self):
        sequences = [str(s.key().toString()) for s in self.window._shortcuts]
        self.assertIn("Esc", sequences)   # QKeySequence 归一化
        self.assertIn(self.window.hotkeys()["dismiss"], ("Escape", "Esc"))
        changed = load_defaults()
        changed.data["hotkeys"] = dict(DEFAULT_HOTKEYS)
        changed.data["hotkeys"]["dismiss"] = "Ctrl+Q"
        self.window.set_settings(changed)
        sequences = [str(s.key().toString()) for s in self.window._shortcuts]
        self.assertIn("Ctrl+Q", sequences)
        self.assertNotIn("Esc", sequences)

    def test_first_layer_escape_hides_window(self):
        _, env, policy, s, menu = context()
        self.window.apply_context(env, policy, s, menu)
        self.window.show_without_focus()
        hidden = []
        self.window.window_hidden.connect(lambda: hidden.append(1))
        self.window._on_dismiss()
        self.assertEqual(len(hidden), 1)
        self.assertFalse(self.window.isVisible())

    def test_second_layer_escape_returns_to_detect(self):
        _, env, policy, s, menu = context()
        tab = self.window.apply_context(env, policy, s, menu)
        tab.show_loading("NaCl", hint="组装中")
        self.assertEqual(tab.stack.currentIndex(), 1)
        self.assertTrue(tab.dismiss())
        self.assertEqual(tab.stack.currentIndex(), 0)
        self.assertFalse(tab.dismiss())

    def test_dismiss_button_exists(self):
        tab = self.window.current_tab()
        self.assertIsNotNone(tab.dismiss_button)
        self.assertIn("Esc", tab.dismiss_button.toolTip())


class Issue2TextInputIsolation(unittest.TestCase):
    def test_wayland_session_defaults_to_xwayland(self):
        old_platform = os.environ.pop("QT_QPA_PLATFORM", None)
        try:
            with patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland",
                                         "WAYLAND_DISPLAY": "wayland-0"}):
                result = studio_main._select_platform(False, False)
                self.assertEqual(result, "xcb")
                self.assertEqual(os.environ.get("QT_QPA_PLATFORM"), "xcb")
        finally:
            if old_platform is not None:
                os.environ["QT_QPA_PLATFORM"] = old_platform
            else:
                os.environ.pop("QT_QPA_PLATFORM", None)

    def test_wayland_native_opt_out(self):
        old_platform = os.environ.pop("QT_QPA_PLATFORM", None)
        try:
            with patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland"}):
                self.assertEqual(studio_main._select_platform(False, True), "wayland")
            self.assertNotIn("QT_QPA_PLATFORM", os.environ)
        finally:
            if old_platform is not None:
                os.environ["QT_QPA_PLATFORM"] = old_platform
            else:
                os.environ.pop("QT_QPA_PLATFORM", None)

    def test_default_launch_uses_xcb(self):
        source = Path(studio_main.__file__).read_text(encoding="utf-8")
        self.assertIn('os.environ["QT_QPA_PLATFORM"] = "xcb"', source)
        self.assertIn("XDG_SESSION_TYPE", source)
        self.assertIn("--wayland-native", source)

    def test_input_bridge_removes_isolation_for_child(self):
        bridge = InputBridge()
        env = bridge._env()
        self.assertEqual(env.value("QT_WAYLAND_TEXT_INPUT_PROTOCOL", ""), "")
        self.assertEqual(env.value("QT_WAYLAND_DISABLED_INTERFACES", ""), "")
        bridge.shutdown()

    def test_input_host_clears_isolation(self):
        os.environ["QT_WAYLAND_TEXT_INPUT_PROTOCOL"] = ""
        os.environ["QT_WAYLAND_DISABLED_INTERFACES"] = "x"
        _clear_isolation()
        self.assertNotIn("QT_WAYLAND_TEXT_INPUT_PROTOCOL", os.environ)
        self.assertNotIn("QT_WAYLAND_DISABLED_INTERFACES", os.environ)

    def test_input_host_writes_result(self):
        import json
        import tempfile
        from clipllm02.studio import input_host
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.json"
            rc = input_host.main(["--mode", "line", "--output", str(out),
                                  "--initial", "中文查询内容"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")),
                             {"ok": True, "text": "中文查询内容"})

    def test_input_bridge_end_to_end_child_process(self):
        """真的拉起独立进程并收到结果（offscreen 自动回传）。"""
        from PySide6.QtCore import QEventLoop, QTimer
        bridge = InputBridge()
        received = []
        bridge.text_ready.connect(lambda p, t: received.append((p, t)))
        loop = QEventLoop()
        QTimer.singleShot(8000, loop.quit)
        bridge.text_ready.connect(lambda *_: loop.quit())
        bridge.request_text("query", title="测试", initial="中文查询")
        loop.exec()
        self.assertEqual(received, [("query", "中文查询")])
        bridge.shutdown()

    def test_input_bridge_dispatches_result(self):
        import json
        bridge = InputBridge()
        serial = 7
        out = bridge._dir / f"out-{serial}.json"
        out.write_text(json.dumps({"ok": True, "text": "自由输入"}),
                       encoding="utf-8")
        bridge._pending[serial] = ("line", "free", out)
        received = []
        bridge.text_ready.connect(lambda p, t: received.append((p, t)))
        bridge._finished(serial)
        self.assertEqual(received, [("free", "自由输入")])
        bridge.shutdown()

    def test_settings_opens_in_child_process(self):
        app = StudioApp(load_defaults(), poll_interval=5.0)
        with patch.object(app.input_bridge, "request_settings") as request:
            app.open_settings()
            self.assertEqual(request.call_count, 1)
            self.assertIn("intents", request.call_args[0][0])
        app._alive = False
        app.clip_worker._active = False


class Issue3And4Hotkeys(unittest.TestCase):
    def test_all_default_hotkeys_defined(self):
        self.assertEqual(set(DEFAULT_HOTKEYS), set(HOTKEY_LABELS))
        self.assertEqual(DEFAULT_HOTKEYS["vision"], "Ctrl+Alt+S")
        self.assertEqual(DEFAULT_HOTKEYS["dismiss"], "Escape")

    def test_settings_has_hotkey_tab(self):
        dialog = SettingsDialog(load_defaults())
        self.assertEqual(len(dialog.hotkey_edits), len(DEFAULT_HOTKEYS))
        self.assertIn("vision", dialog.hotkey_edits)
        dialog.close()

    def test_conflicting_hotkeys_are_rejected(self):
        dialog = SettingsDialog(load_defaults())
        from PySide6.QtGui import QKeySequence
        dialog.hotkey_edits["vision"].setKeySequence(QKeySequence("Ctrl+T"))
        with patch("clipllm02.studio.settings_dialog.save_config"):
            self.assertFalse(dialog._apply())
        dialog.close()

    def test_hotkey_changes_are_saved(self):
        dialog = SettingsDialog(load_defaults())
        from PySide6.QtGui import QKeySequence
        dialog.hotkey_edits["vision"].setKeySequence(QKeySequence("Ctrl+Alt+V"))
        with patch("clipllm02.studio.settings_dialog.save_config") as save:
            self.assertTrue(dialog._apply())
            self.assertEqual(save.call_args[0][0]["hotkeys"]["vision"], "Ctrl+Alt+V")
        dialog.close()

    def test_ipc_triggers_vision(self):
        app = StudioApp(load_defaults(), poll_interval=5.0)
        (app.ipc_dir).mkdir(parents=True, exist_ok=True)
        cmd = app.ipc_dir / "vision.cmd"
        cmd.write_text("vision", encoding="utf-8")
        with patch.object(app, "on_vision") as vision:
            app._check_ipc()
            self.assertEqual(vision.call_count, 1)
        self.assertFalse(cmd.exists())
        app._alive = False
        app.clip_worker._active = False

    def test_vision_shortcut_bound(self):
        window = MainWindow(load_defaults())
        sequences = [str(s.key().toString()) for s in window._shortcuts]
        self.assertIn("Ctrl+Alt+S", sequences)

    def test_register_shortcuts_does_not_touch_systemd(self):
        source = Path(studio_main.__file__).read_text(encoding="utf-8")
        self.assertNotIn("systemctl", source)
        self.assertIn("gsettings", source)


class Issue5MoreIntents(unittest.TestCase):
    def test_every_domain_has_many_options(self):
        settings = load_defaults()
        counts: dict[str, int] = {}
        for intent in settings.intents:
            for domain in intent.domains:
                counts[domain] = counts.get(domain, 0) + 1
        for domain in ("chem", "en", "zh", "math", "physics", "code", "image"):
            self.assertGreaterEqual(counts.get(domain, 0), 2, domain)

    def test_menu_shows_all_options_of_category(self):
        settings, _, _, _, menu = context("NaCl")
        expected = sum(1 for i in settings.intents
                       if i.primary_route == menu.category and i.enabled)
        self.assertEqual(len(menu.recommended), expected)
        labels = [o.intent.label for o in menu.recommended]
        self.assertIn("这个式子该怎么念", labels)
        self.assertIn("反应条件与配平", labels)
        self.assertNotIn("通用解释", labels)

    def test_legacy_template_intents_exist(self):
        settings = load_defaults()          # 旧脚本 9 套模板在这里注入
        raw = settings.data["intents"]
        legacy = [i for i in raw if i.get("use_legacy_template")]
        self.assertGreaterEqual(len(legacy), 6)
        self.assertTrue(all(not i.get("prompt_override") for i in legacy))
        # 留空提示词时回退到旧脚本注入的路由模板
        from clipllm02.prompts import compose_prompt
        prompt = compose_prompt(settings.intent("en.legacy"), settings).prompt
        self.assertGreater(len(prompt), 1000)

    def test_settings_intent_library(self):
        dialog = SettingsDialog(load_defaults())
        self.assertGreaterEqual(dialog.intent_total, 40)
        before = dialog.intent_total
        # 用户自己的配置里可能已有自定义条目，这里只比较增量
        before_custom = len([i for i in dialog.data["intents"]
                             if i.get("custom")])
        dialog._add_intent()
        self.assertEqual(dialog.intent_total, before + 1)
        custom = [i for i in dialog.data["intents"] if i.get("custom")]
        self.assertEqual(len(custom), before_custom + 1)
        dialog._current_intent_id = custom[-1]["id"]
        dialog._delete_intent()
        self.assertEqual(dialog.intent_total, before)
        dialog.close()

    def test_domain_tree_groups_intents(self):
        dialog = SettingsDialog(load_defaults())
        tops = {dialog.prompt_tree.topLevelItem(i).data(0, Qt.UserRole)[1]
                for i in range(dialog.prompt_tree.topLevelItemCount())}
        # 第二层按提示词模板分组，顶层数量与模板数量一致
        self.assertIn("化学物质", tops)
        self.assertIn("英语单词", tops)
        self.assertIn("图片识别", tops)
        self.assertEqual(len(tops), len(dialog.data["routes"]))
        total = sum(dialog.prompt_tree.topLevelItem(i).childCount()
                    for i in range(dialog.prompt_tree.topLevelItemCount()))
        self.assertEqual(total, dialog.intent_total)
        dialog.close()

    def test_includes_roundtrip(self):
        includes = parse_includes("en_word:phonetic|etymology_roots, general:key_points")
        self.assertEqual(len(includes), 2)
        self.assertEqual(includes[0]["sections"], ["phonetic", "etymology_roots"])
        self.assertIn("general:key_points", format_includes(includes))

    def test_intent_prompt_editable(self):
        dialog = SettingsDialog(load_defaults())
        dialog.select_first_intent()
        dialog.intent_prompt.setPlainText("我自己写的提示词")
        current = dialog._intent_by_id(dialog._current_intent_id)
        self.assertEqual(current["prompt_override"], "我自己写的提示词")
        dialog.close()


class Issue6WindowResizeAndLayout(unittest.TestCase):
    def test_edge_detection(self):
        window = MainWindow(load_defaults())
        self.assertEqual(window._edges_at(QPoint(0, 50)), "L")
        self.assertIn("R", window._edges_at(QPoint(window.width() - 1, 50)))
        self.assertIn("T", window._edges_at(QPoint(100, 0)))
        self.assertIn("B", window._edges_at(QPoint(100, window.height() - 1)))

    def test_left_drag_keeps_right_fixed(self):
        window = MainWindow(load_defaults())
        start = window.geometry()
        window.handle_button(QPoint(500, 300), QPoint(0, 300), True, Qt.NoModifier)
        window.handle_move(QPoint(540, 300), QPoint(40, 300), Qt.LeftButton)
        after = window.geometry()
        self.assertEqual(after.right(), start.right())
        self.assertGreater(after.left(), start.left())
        self.assertLess(after.width(), start.width())

    def test_top_drag_keeps_bottom_fixed(self):
        window = MainWindow(load_defaults())
        start = window.geometry()
        window.handle_button(QPoint(300, 200), QPoint(300, 0), True, Qt.NoModifier)
        window.handle_move(QPoint(300, 230), QPoint(300, 30), Qt.LeftButton)
        after = window.geometry()
        self.assertEqual(after.bottom(), start.bottom())
        self.assertGreater(after.top(), start.top())

    def test_wayland_native_resize_is_used(self):
        import importlib
        source = Path(importlib.import_module(
            ResizableWindow.__module__).__file__)
        text = source.read_text(encoding="utf-8")
        self.assertIn("startSystemResize", text)
        self.assertIn("startSystemMove", text)
        self.assertTrue(source.name.endswith(".py"))

    def test_layout_has_scroll_and_cards(self):
        _, env, policy, s, menu = context()
        tab = QueryTab(load_defaults())
        tab.set_context(env, policy, s, menu)
        from PySide6.QtWidgets import QScrollArea, QFrame
        self.assertTrue(tab.findChildren(QScrollArea))
        self.assertTrue(any(w.objectName() in ("card", "freeCard")
                            for w in tab.findChildren(QFrame)))
        visible = [b for b in tab._intent_buttons if not b.isHidden()]
        self.assertEqual(len(visible), len(tab.menu.recommended))
        self.assertGreaterEqual(len(visible), 4)
        self.assertTrue(tab.intent_count.isHidden())   # 顶部不再显示条数


class IssueRound3(unittest.TestCase):
    """第三轮反馈：焦点防护、结果页搜索/新标签、小图标布局、自适应、截图。"""

    def setUp(self):
        self.settings = load_defaults()

    def _context(self, text="NaCl"):
        env = InputEnvelope.for_text(text, source="test")
        policy = preflight(env, self.settings)
        s = suggest(env, policy, self.settings)
        return env, policy, s, build_menu(s, self.settings)

    # 1. 焦点防护：Tool|Frameless|置顶（动态移除 ABOVE 让 Chrome 盖过）+ 不激活 + 自动隐藏
    def test_auto_show_never_activates(self):
        self.settings.data["desktop_persistent_window"] = True
        window = MainWindow(self.settings)
        with patch.object(window, "_is_offscreen", return_value=False), \
                patch("clipllm02.desktop.window_base.x11_state.set_above") as above, \
                patch.object(window, "raise_") as raised:
            window.show_without_focus()
            above.assert_not_called()      # 持久模式堆叠交给 WM
            self.assertTrue(raised.called)  # 只 raise，不改 ABOVE 状态
        self.assertTrue(window.testAttribute(Qt.WA_ShowWithoutActivating))
        self.assertFalse(bool(window.windowFlags() & Qt.WindowStaysOnTopHint))
        self.assertTrue(window._persistent)
        self.assertTrue(window._auto_hide)
        window.set_auto_hide(False)
        self.assertFalse(window._auto_hide)

    def test_no_window_flag_recreate_in_source(self):
        """禁止再用 setWindowFlag 动态切换（会重建窗口 → 闪烁）。"""
        import importlib
        text = Path(importlib.import_module(
            ResizableWindow.__module__).__file__).read_text(encoding="utf-8")
        self.assertNotIn("WindowDoesNotAcceptFocus", text)
        self.assertIn("WA_ShowWithoutActivating", text)
        self.assertIn("Qt.WindowStaysOnTopHint", text)   # 初始置顶，动态移除让位

    # 2. crumb 行只留层标签 / 换一种解释 / 关闭；搜索与新标签由主窗口顶栏承担
    def test_crumb_row_is_minimal(self):
        env, policy, s, menu = self._context()
        tab = QueryTab(self.settings)
        tab.set_context(env, policy, s, menu)
        self.assertFalse(hasattr(tab, "search_menu"))
        self.assertFalse(hasattr(tab, "copy_query_button"))
        self.assertFalse(tab.back_button.isVisibleTo(tab))
        tab.show_loading("NaCl", hint="组装中")
        self.assertEqual(tab.stack.currentIndex(), 1)
        self.assertTrue(tab.back_button.isVisibleTo(tab))

    def test_main_window_owns_search_and_newtab(self):
        window = MainWindow(self.settings)
        self.assertTrue(hasattr(window, "search_button"))
        self.assertTrue(hasattr(window, "new_tab_button"))
        self.assertFalse(hasattr(window, "footer"))

    def test_search_prefers_selection(self):
        env, policy, s, menu = self._context("光合作用")
        tab = QueryTab(self.settings)
        tab.set_context(env, policy, s, menu)
        self.assertEqual(tab._search_query(), "光合作用")
        tab.show_loading("光合作用", hint="组装中")   # 结果页才会取选中文本
        tab.view.setPlainText("被选中的内容")
        cursor = tab.view.textCursor()
        cursor.setPosition(1)
        cursor.setPosition(4, cursor.MoveMode.KeepAnchor)
        tab.view.setTextCursor(cursor)
        self.assertEqual(tab._search_query(), "选中的")

    def test_main_window_search_menu_and_signal(self):
        window = MainWindow(self.settings)
        providers = self.settings.enabled_search_providers(("text", "image"))
        window.fill_search_menu(providers)
        self.assertEqual(window.search_menu.actions().__len__(), len(providers))
        received = []
        window.search_provider_requested.connect(received.append)
        window.search_menu.actions()[0].trigger()
        self.assertEqual(received, [providers[0].id])

    # 3. 顶栏小图标 + 自适应布局
    def test_top_bar_uses_borderless_icons(self):
        window = MainWindow(self.settings)
        for button in (window.query_button, window.search_button,
                       window.new_tab_button):
            self.assertEqual(button.objectName(), "barAction")
            self.assertTrue(button.autoRaise())      # 无外框
            self.assertFalse(button.icon().isNull())  # 真实图标，不是文字
            self.assertEqual(button.text(), "")
            self.assertEqual((button.width(), button.height()), (34, 34))

    def test_intent_grid_adapts_to_width(self):
        env, policy, s, menu = self._context()
        tab = QueryTab(self.settings)
        tab.set_context(env, policy, s, menu)
        tab.resize(560, 600)
        tab._relayout_intents()
        self.assertEqual(tab._grid_cols, 2)
        tab.resize(980, 600)
        tab._relayout_intents()
        self.assertEqual(tab._grid_cols, 3)
        tab.resize(480, 600)
        tab._relayout_intents()
        self.assertEqual(tab._grid_cols, 2)   # 查询方式通常较短，默认两列

    def test_other_grid_compact(self):
        env, policy, s, menu = self._context()
        tab = QueryTab(self.settings)
        tab.set_context(env, policy, s, menu)
        visible = [b for b in tab._other_buttons if not b.isHidden()]
        self.assertTrue(visible)
        for button in visible:             # 紧凑；通用视角显示纯名称
            self.assertEqual(button.minimumHeight(), 30)
            self.assertTrue(button.text())
        self.assertFalse(hasattr(tab, "image_button"))   # 底部截图按钮已删

    # 4. 区域截图工具链
    def test_capture_screenshot_without_tools(self):
        from clipllm02.studio import vision
        with patch.object(vision.shutil, "which", return_value=None), \
             patch.object(vision, "_capture_with_portal", return_value=None), \
             patch.object(vision, "_capture_with_qt", return_value=None):
            self.assertIsNone(vision.capture_screenshot())

    def test_capture_screenshot_with_gnome_screenshot(self):
        import tempfile
        from clipllm02.studio import vision

        def fake_run(command, **_kwargs):
            # 只对真正写文件的命令造数据：["slurp"] 这类探测命令的
            # command[-1] 就是命令名，写盘会在项目根留下脏文件
            target = command[-1]
            if target.endswith(".png"):
                Path(target).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with patch.object(vision.shutil, "which",
                          side_effect=lambda name: "/usr/bin/" + name), \
             patch.object(vision.subprocess, "run", side_effect=fake_run):
            result = vision.capture_screenshot()
        self.assertIsNotNone(result)
        self.assertEqual(result[1], "image/png")
        self.assertTrue(result[0].startswith(b"\x89PNG"))

    # 5. 设置入口与反馈
    def test_settings_open_gives_feedback(self):
        app = StudioApp(self.settings, poll_interval=5.0)
        with patch.object(app.input_bridge, "request_settings") as request:
            app.open_settings()
            self.assertEqual(request.call_count, 1)
            self.assertTrue(app.window.testAttribute(Qt.WA_ShowWithoutActivating))
        app._alive = False
        app.clip_worker._active = False

    def test_hard_block_is_silent(self):
        app = StudioApp(self.settings, poll_interval=5.0)
        before = app.state.stats["blocked"]
        app.process_text("x" * 1200, source="test")   # 超过 code_block_max → BLOCK
        self.assertEqual(app.state.stats["blocked"], before + 1)
        # 静默：不弹结果页，标签保持第一层初判（或初始状态）
        self.assertEqual(app.window.current_tab().stack.currentIndex(), 0)
        app._alive = False
        app.clip_worker._active = False


class Issue9SettingsEditing(unittest.TestCase):
    """设置页可增删：拦截规则（UI 编辑 → 引擎直接生效）与提示词模板。"""

    def test_policy_rule_added_in_dialog_takes_effect(self):
        from clipllm02.settings import Settings
        from clipllm02.studio.settings_dialog import SettingsDialog
        dialog = SettingsDialog(load_defaults())
        before = set(dialog.data["policy"]["rules"])
        dialog._add_policy_rule()                  # 自动选中新规则
        rid = dialog._current_policy_id
        self.assertTrue(rid.startswith("custom_"))
        self.assertNotIn(rid, before)
        dialog.policy_pattern.setText("内部资料")
        dialog._flush_policy_rule()                # 编辑写回 data
        self.assertEqual(dialog.data["policy"]["rules"][rid]["pattern"],
                         "内部资料")
        # UI 编辑的规则直接被引擎执行（无需 GUI、无需重启逻辑验证）
        result = preflight(InputEnvelope.for_text("这是内部资料"),
                           Settings(dialog.data))
        self.assertEqual(result.level, "BLOCK")
        self.assertIn(rid, [h.reason for h in result.hits])
        dialog.close()

    def test_delete_policy_rule(self):
        from clipllm02.studio.settings_dialog import SettingsDialog
        dialog = SettingsDialog(load_defaults())
        dialog._add_policy_rule()
        rid = dialog._current_policy_id
        dialog._delete_policy_rule()
        self.assertNotIn(rid, dialog.data["policy"]["rules"])
        dialog.close()

    def test_add_and_delete_route_template(self):
        from clipllm02.studio.settings_dialog import SettingsDialog
        dialog = SettingsDialog(load_defaults())
        dialog.data["routes"]["zz_test_route"] = {
            "id": "zz_test_route", "enabled": True, "query_type": "free",
            "kind": "route", "prompt": "", "sections": [], "fields": []}
        dialog.route_combo.addItem("zz_test_route", "zz_test_route")
        dialog.route_combo.setCurrentIndex(dialog.route_combo.count() - 1)
        self.assertEqual(dialog.route_combo.currentData(), "zz_test_route")
        # 被意图引用的模板不允许删除
        dialog.data["intents"][0]["primary_route"] = "zz_test_route"
        dialog._delete_route()
        self.assertIn("zz_test_route", dialog.data["routes"])
        # 无引用时可删除
        dialog.data["intents"][0]["primary_route"] = "通用解析"
        dialog._delete_route()
        self.assertNotIn("zz_test_route", dialog.data["routes"])
        dialog.close()


if __name__ == "__main__":
    unittest.main()


class TestLibrarySettings(unittest.TestCase):
    """知识库页签：路径可见、分类可见、旧英文目录可整理。"""

    def _dialog(self, root):
        from clipllm02.studio.settings_dialog import SettingsDialog
        dialog = SettingsDialog(load_defaults())
        dialog.data["desktop_library_dir"] = str(root)
        dialog._reload_library_folders()
        return dialog

    def test_library_tab_exists(self):
        from clipllm02.studio.settings_dialog import SettingsDialog
        dialog = SettingsDialog(load_defaults())
        titles = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
        self.assertIn("知识库", titles)
        dialog.close()

    def test_shows_actual_path_and_folders(self):
        root = Path(tempfile.mkdtemp(prefix="lib-"))
        (root / "LaTeX 公式").mkdir()
        (root / "LaTeX 公式" / "a.md").write_text("x", encoding="utf-8")
        dialog = self._dialog(root)
        self.assertIn(str(root), dialog.library_actual.text())
        names = [dialog.library_table.item(r, 0).text()
                 for r in range(dialog.library_table.rowCount())]
        self.assertIn("LaTeX 公式", names)
        self.assertIn("1 篇", dialog.library_hint.text())
        shutil.rmtree(root)
        dialog.close()

    def test_tidy_merges_legacy_folder_without_overwrite(self):
        root = Path(tempfile.mkdtemp(prefix="lib-"))
        (root / "chem_substance").mkdir()
        (root / "chem_substance" / "a.md").write_text("a", encoding="utf-8")
        (root / "chem_substance" / "b.md").write_text("b", encoding="utf-8")
        (root / "化学物质").mkdir()
        (root / "化学物质" / "b.md").write_text("old", encoding="utf-8")
        dialog = self._dialog(root)
        dialog._tidy_library_folders()
        merged = root / "化学物质"
        self.assertTrue((merged / "a.md").exists())
        # 同名笔记不能被旧文件覆盖
        self.assertEqual((merged / "b.md").read_text(encoding="utf-8"), "old")
        self.assertTrue((root / "化学物质" / "b.md").exists())
        shutil.rmtree(root)
        dialog.close()

    def test_every_route_has_chinese_label(self):
        """每个模板都要有中文名，否则会建出 chem_substance 这种英文目录。"""
        for rid, pack in load_defaults().routes.items():
            self.assertTrue(str(pack.get("label") or "").strip(),
                            f"{rid} 缺中文分类名")
