"""六个用户反馈问题的回归测试（全部 offscreen）。

1 抢焦点 / 窗口不可调 / 无窗口记忆
2 界面杂乱 / 交互卡顿
3 初判后应按领域给常用查询类型，而非同一模板
4 复制后不弹窗 / 新查询不覆盖旧弹窗
5 初判自动但要有选项
6 操作后闪退
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QEvent, QPoint, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QEnterEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from clipllm02 import (  # noqa: E402
    InputEnvelope, build_menu, load_defaults, preflight, sha256_text, suggest,
)
from clipllm02.desktop.app import DesktopApp  # noqa: E402
from clipllm02.desktop.gateway import GatewayResult  # noqa: E402
from clipllm02.desktop.menu_window import MenuWindow  # noqa: E402
from clipllm02.desktop.result_window import ResultWindow  # noqa: E402
from clipllm02.desktop.settings_window import load_user_settings  # noqa: E402
from clipllm02.studio.config import CONFIG_VERSION  # noqa: E402
from clipllm02.desktop import x11_state  # noqa: E402
from clipllm02.desktop.window_base import ResizableWindow  # noqa: E402
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


class Issue1FocusAndGeometry(unittest.TestCase):
    def test_no_focus_steal(self):
        window = ResultWindow(())
        self.assertTrue(window.testAttribute(Qt.WA_ShowWithoutActivating))
        self.assertTrue(window.windowFlags() & Qt.Tool)
        window.show_without_focus()
        self.assertTrue(window.isVisible())
        window.close()

    def test_geometry_is_remembered(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = QSettings(str(Path(tmp) / "geom.ini"), QSettings.IniFormat)
            with patch.object(ResizableWindow, "_store", return_value=store):
                first = ResultWindow(())
                first.resize(700, 500)
                first.save_geometry()
                second = ResultWindow(())
                self.assertEqual(second.width(), 700)
                self.assertEqual(second.height(), 500)
                first.close()
                second.close()

    def test_edge_resize(self):
        window = ResultWindow(())
        window.resize(500, 400)
        self.assertEqual(window._edges_at(QPoint(0, 50)), "L")
        self.assertEqual(window._edges_at(QPoint(499, 399)), "RB")
        pressed = window.handle_button(QPoint(100, 100), QPoint(499, 399),
                                       True, Qt.NoModifier)
        self.assertTrue(pressed)
        window.handle_move(QPoint(160, 160), QPoint(499, 399), Qt.LeftButton)
        self.assertGreater(window.width(), 500)
        window.close()


class Issue2Performance(unittest.TestCase):
    def test_clipboard_polling_is_off_ui_thread(self):
        app = DesktopApp(load_defaults(), use_tray=False)
        self.assertIsNotNone(app.clip_thread)
        self.assertNotEqual(app.clip_worker.thread(),
                            QApplication.instance().thread())
        app.clip_worker.stop_work()
        app._alive = False

    def test_reader_tries_all_available_commands(self):
        commands = ClipboardReader().commands()
        self.assertTrue(commands)
        self.assertTrue(all(c[0] in {"wl-paste", "xclip"} for c in commands))

    def test_render_reuses_widgets(self):
        _, env1, policy1, s1, menu1 = make_context("NaCl")
        window = MenuWindow(load_defaults(), env1, policy1, s1, menu1)
        first_button = window._intent_buttons[0]
        _, env2, policy2, s2, menu2 = make_context("E=mc²")
        window.update_model(env2, policy2, s2, menu2)
        self.assertIs(window._intent_buttons[0], first_button)
        self.assertGreaterEqual(len(window._intent_buttons), 9)
        self.assertIn("E=mc²", window.header.text())
        window.close()


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


class Issue4PopupCoverage(unittest.TestCase):
    def _app(self):
        return DesktopApp(load_defaults(), use_tray=False)

    def test_new_input_overwrites_existing_menu(self):
        app = self._app()
        app.process_text("NaCl", source="test")
        window = app.menu_window
        before = window.header.text()
        app.process_text("E=mc²", source="test")
        self.assertIs(app.menu_window, window)
        self.assertIn("E=mc²", window.header.text())
        self.assertNotEqual(before, window.header.text())
        self.assertIn("逐项读这个式子",
                      [o.intent.label for o in window.menu.recommended])
        app._alive = False

    def test_duplicate_uses_ttl(self):
        app = self._app()
        app.process_text("NaCl", source="test")
        app.process_text("NaCl", source="test")
        self.assertEqual(app.stats["events"], 1)
        self.assertEqual(app.stats["duplicates"], 1)
        app._seen[sha256_text("NaCl")] = time.time() - 400
        app.process_text("NaCl", source="test")
        self.assertEqual(app.stats["events"], 2)
        app._alive = False

    def test_self_copy_does_not_poison_dedup(self):
        app = self._app()
        app.process_text("NaCl", source="test")
        app._on_self_copy("软件复制的结果")
        app.process_text("E=mc²", source="test")
        self.assertEqual(app.stats["events"], 2)
        self.assertIn("E=mc²", app.menu_window.header.text())
        app._alive = False

    def test_blocked_input_clears_state(self):
        app = self._app()
        app.process_text("NaCl", source="test")
        app.process_text("A" * 1200, source="test")
        self.assertIsNone(app.state.envelope)
        self.assertIsNone(app.state.suggestion)
        self.assertEqual(app.stats["menus"], 1)
        app._alive = False


class Issue5AutomaticWithOptions(unittest.TestCase):
    def test_low_level_auto_menu_keeps_domain_options(self):
        _, _, _, _, menu = make_context("12345")
        self.assertEqual(menu.level, "LOW")
        self.assertTrue(menu.recommended)
        self.assertGreater(len(menu.domain_chips), 1)
        self.assertTrue(menu.free_input["enabled"])

    def test_domain_change_rebuilds_menu(self):
        app = DesktopApp(load_defaults(), use_tray=False)
        app.process_text("NaCl", source="test")
        app.on_domain_changed("en")
        self.assertEqual(app.menu_window.menu.level, "HIGH")
        self.assertEqual(app.menu_window.menu.domain_chips[0].id, "en")
        self.assertTrue(app.menu_window.menu.recommended)
        self.assertEqual(app.stats["requests"], 0)
        app._alive = False


class ConfigUpgrade(unittest.TestCase):
    def test_stale_intents_refreshed_but_user_settings_kept(self):
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
                settings = load_user_settings()
            self.assertEqual(settings.get("config_version"), CONFIG_VERSION)
            self.assertIsNotNone(settings.intent("chem.naming"))
            self.assertIsNone(settings.intent("stale"))
            self.assertEqual(settings.get("desktop_api_url"),
                             "https://example.com/v1/chat/completions")


class Issue6NoCrash(unittest.TestCase):
    def test_worker_done_without_selection(self):
        app = DesktopApp(load_defaults(), use_tray=False)
        app.process_text("NaCl", source="test")
        app.state.selection = None
        app._on_worker_done((app.generation,
                             GatewayResult(html="", ok=False, error="boom"), None))
        self.assertEqual(app.errors, [])
        self.assertIn("boom", app.result_window.current_plain_text())
        app._alive = False

    def test_closing_menu_does_not_emit_cancelled(self):
        _, env, policy, s, menu = make_context()
        window = MenuWindow(load_defaults(), env, policy, s, menu)
        captured = []
        window.cancelled.connect(captured.append)
        window.close()
        self.assertEqual(captured, [])

    def test_late_signals_after_shutdown_are_ignored(self):
        app = DesktopApp(load_defaults(), use_tray=False)
        app.process_text("NaCl", source="test")
        generation = app.generation
        app._alive = False
        app.process_text("E=mc²", source="test")
        app._on_worker_done((generation,
                             GatewayResult(html="<p>late</p>", ok=True), None))
        self.assertEqual(app.stats["events"], 1)
        self.assertIsNone(app.result_window)
        self.assertEqual(app.errors, [])


class Issue7YieldOnSwitch(unittest.TestCase):
    """弹窗「切走即让位」：绕开 Qt 事件，读 X11 全局状态。

    覆盖两类此前未解决的场景：Alt+Tab 键盘切走（鼠标不动）、以及点击其他
    窗口（Qt 收不到对方窗口的鼠标事件，QApplication.mouseButtons 不更新）。
    """

    def _x11_window(self):
        w = ResultWindow(())
        w.resize(400, 300)
        w.move(0, 0)
        w._active_at_show = 100
        return w

    def test_active_window_change_hides_on_alttab(self):
        w = self._x11_window()
        with patch.object(ResizableWindow, "_is_offscreen", return_value=False), \
             patch.object(ResizableWindow, "_mouse_inside", return_value=False), \
             patch.object(ResizableWindow, "_mouse_near", return_value=True), \
             patch("clipllm02.desktop.window_base.x11_state.pointer_buttons",
                   return_value=0), \
             patch("clipllm02.desktop.window_base.x11_state.active_window",
                   return_value=200):
            w.show()
            self.assertTrue(w.isVisible())
            for _ in range(10):        # 第 10 次轮询触发活动窗口检测
                w._poll_mouse()
            self.assertFalse(w.isVisible())   # active 从 100 变 200 → 让位
        w.close()

    def test_button_press_outside_hides(self):
        w = self._x11_window()
        with patch.object(ResizableWindow, "_is_offscreen", return_value=False), \
             patch.object(ResizableWindow, "_mouse_inside", return_value=False), \
             patch("clipllm02.desktop.window_base.x11_state.pointer_buttons",
                   return_value=x11_state.ANY_BUTTON):
            w.show()
            self.assertTrue(w.isVisible())
            w._poll_mouse()
            self.assertFalse(w.isVisible())   # 窗口外按下 → 立即让位
        w.close()

    def test_inside_keeps_visible(self):
        w = self._x11_window()
        with patch.object(ResizableWindow, "_is_offscreen", return_value=False), \
             patch.object(ResizableWindow, "_mouse_inside", return_value=True), \
             patch("clipllm02.desktop.window_base.x11_state.pointer_buttons",
                   return_value=0), \
             patch("clipllm02.desktop.window_base.x11_state.active_window",
                   return_value=100), \
             patch("clipllm02.desktop.window_base.x11_state.escape_pressed",
                   return_value=False):
            w.show()
            for _ in range(12):
                w._poll_mouse()
            self.assertTrue(w.isVisible())    # 窗内且焦点没变 → 保持
        w.close()

    def test_leave_starts_hide_timer(self):
        """指针移出弹窗（XWayland 必发 leaveEvent）→ 启动 1.5s 关闭定时器。"""
        w = self._x11_window()
        w.show()
        w._hide_timer.stop()
        w.leaveEvent(QEvent(QEvent.Type.Leave))
        self.assertTrue(w._hide_timer.isActive())
        w.close()

    def test_enter_stops_idle_and_hide_timers(self):
        """指针进入弹窗 → 取消「移出关闭」与「从未进入超时」两个定时器。"""
        w = self._x11_window()
        w.show()
        w._idle_timer.start()
        w._hide_timer.start()
        w.enterEvent(QEnterEvent(QPoint(1, 1), QPoint(1, 1), QPoint(1, 1)))
        self.assertFalse(w._idle_timer.isActive())
        self.assertFalse(w._hide_timer.isActive())
        w.close()

    def test_idle_timeout_hides_when_never_entered(self):
        """unpinned：从未进入弹窗，6s 兜底直接隐藏（GoldenDict 让位语义）。"""
        w = self._x11_window()
        w.show_without_focus()
        self.assertTrue(w.isVisible())
        self.assertTrue(w._idle_timer.isActive())   # 弹出即启动兜底
        w._idle_timer.timeout.emit()
        self.assertFalse(w.isVisible())             # 客户端 unmap，立即让位
        w.close()


if __name__ == "__main__":
    unittest.main()
