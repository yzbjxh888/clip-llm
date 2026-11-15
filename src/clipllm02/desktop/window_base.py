"""窗口基座：不抢焦点、可缩放/拖拽、QSettings 记忆尺寸与位置。

移植并简化 0.1 popup.py 的事件转发机制：子控件（viewport）会吞掉鼠标事件，
必须统一转发到窗口才能做边缘缩放与拖动。
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import (QEvent, QObject, QPoint, QRect, QSettings, Qt,
                            QTimer, Signal)
from PySide6.QtGui import QColor, QCursor, QPalette
from PySide6.QtWidgets import (QApplication, QFrame, QLineEdit, QTextBrowser,
                               QTextEdit, QWidget)

from . import x11_state
from ..studio.theme_tokens import build_qss, tokens

EDGE = 14
GRIP = 14
MIN_W, MIN_H = 320, 200
PROXIMITY = 60           # 诊断采样时「鼠标附近」的判定距离（像素）

# 保留模块级名称，内容改由统一 token 生成；原先三份互相矛盾的 STYLE 合并为一份。
STYLE = build_qss(False)


class _Forwarder(QObject):
    """把子控件的鼠标事件转交给窗口做边缘判定。"""

    def __init__(self, window: "ResizableWindow"):
        super().__init__(window)
        self.window = window

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.MouseMove:
            local = obj.mapTo(self.window, event.position().toPoint())
            self.window.handle_move(event.globalPosition().toPoint(), local,
                                    event.buttons())
            return False
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            local = obj.mapTo(self.window, event.position().toPoint())
            pressed = kind == QEvent.Type.MouseButtonPress
            suppress = self.window.handle_button(
                event.globalPosition().toPoint(), local, pressed,
                event.modifiers())
            if pressed and not suppress:
                # 子控件内容区的点击同样解除「不接受焦点」
                self.window._accept_focus_on_click()
            return bool(suppress and pressed)
        return False


class ResizableWindow(QWidget):
    """所有弹窗的公共基类。"""

    dismiss_requested = Signal()        # 窗口未激活时的全局 Esc 兜底

    def __init__(self, *, settings_key: str, default_size=(520, 480),
                 parent=None, persistent: bool = False):
        super().__init__(parent)
        self._settings_key = settings_key
        self._default_width, self._default_height = default_size
        self.setObjectName("clipLlmCard")
        # 默认仍是 Tool（跳过任务栏的词典弹窗）。persistent 时用普通 Window：
        # Qt 会给 Tool 打上 _NET_WM_STATE_SKIP_TASKBAR，Alt+Tab 里根本找不到。
        # 窗口标志只在构造时决定一次，绝不运行时 setWindowFlag 重映射——那会
        # 重建 X 窗口，正是历史上 Chrome 闪烁 / fcitx 被重置的根源。
        window_type = Qt.Window if persistent else Qt.Tool
        flags = window_type | Qt.FramelessWindowHint
        # 持久模式不再设静态 WindowStaysOnTopHint：Qt 会持续向 WM 重申 ABOVE，
        # 把 _unfloat() 发的「移除置顶」client message 覆盖掉，表现为「点好几次
        # 才沉底」「Alt+Tab 后词典仍压在别的窗口上」。置顶改由 set_above()
        # 纯动态控制。旧弹窗模式保持原样，零行为变化。
        if not persistent:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self._persistent = bool(persistent)
        # 无论哪种模式都不抢焦点：这是输入法串扰的关键防线，保持不变
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # XWayland 下窗口底色默认黑，布局未覆盖处会露出黑边——填主题底色
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(QPalette.Window, QColor(tokens(False)["background"]))
        self.setPalette(palette)
        self.setMinimumSize(MIN_W, MIN_H)
        self.setMouseTracking(True)
        self.setStyleSheet(build_qss(False))

        self._auto_hide = True
        self._mouse_entered = False       # 鼠标当前是否在窗口内（enterEvent/leaveEvent 维护）
        self._shown_at = 0.0              # 弹出时刻（monotonic），用于宽容期
        self._active_at_show: int | None = None  # 弹出时 X11 活动窗口（诊断/白名单）
        self._esc_held = False            # 全局 Esc 键位图的边沿状态
        self._poll_tick = 0               # 轮询计数，低频查活动窗口
        # 指针从弹窗（X surface）移出时，XWayland 会可靠发出 leaveEvent——这是
        # Wayland 下少数可靠信号（原生 Wayland 窗口上的指针坐标会冻结，轮询不可靠）。
        self._floated = False                 # 当前是否持有 _NET_WM_STATE_ABOVE
        self._pin_timer = QTimer(self)       # 查询置顶的自动解除
        self._pin_timer.setSingleShot(True)
        self._pin_timer.setInterval(400)
        self._pin_timer.timeout.connect(self._unfloat)
        # unpinned（GoldenDict 默认）：鼠标移出 400ms 直接 hide，而不是
        # 「取消置顶」——客户端 unmap 立即生效，不经过 Mutter 异步 restack。
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(400)
        self._hide_timer.timeout.connect(self._yield_to_other_window)
        self._idle_timer = QTimer(self)      # 从未进入弹窗的兜底超时
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(6000)
        self._idle_timer.timeout.connect(self._yield_to_other_window)
        self._mouse_poll = QTimer(self)      # X 窗口场景的快速响应（增强，保留）
        self._mouse_poll.setInterval(30)
        self._mouse_poll.timeout.connect(self._poll_mouse)

        self._drag_origin: QPoint | None = None
        self._drag_start_geom: QRect | None = None
        self._resize_edges: str | None = None
        self._resize_start: QPoint | None = None
        self._resize_geom: QRect | None = None
        self._system_interactive = False   # Wayland 合成器接管期间不手动改几何
        self._geometry_ready = False
        self._forwarder = _Forwarder(self)

        self.grip = QFrame(self)
        self.grip.setObjectName("grip")
        self.grip.setFixedSize(GRIP, GRIP)
        self.grip.setCursor(Qt.SizeFDiagCursor)
        self.grip.installEventFilter(self._forwarder)   # grip 拖动也走边缘缩放
        self._restore_geometry()
        self._place_grip()

    # -- 子控件事件转发 ----------------------------------------------------
    def watch_children(self, widgets) -> None:
        for widget in widgets:
            widget.setMouseTracking(True)
            widget.installEventFilter(self._forwarder)

    def apply_child_cursors(self) -> None:
        """给全部子控件显式设置光标。

        Qt 的光标沿父链继承：边缘缩放把窗口光标设为 SizeXXCursor 后，鼠标
        移到未开启鼠标追踪的子控件（按钮/标签）上时不再产生 move 事件，窗口
        光标无法恢复，就会残留 ↔/↕ 形状。显式设置子控件光标可彻底杜绝；
        文本类控件保留 IBeam 便于选择文字。
        """
        for child in self.findChildren(QWidget):
            if isinstance(child, (QTextEdit, QTextBrowser, QLineEdit)):
                child.setCursor(Qt.IBeamCursor)
            else:
                child.setCursor(Qt.ArrowCursor)

    # -- 几何记忆 ----------------------------------------------------------
    def _store(self) -> QSettings:
        return QSettings("clip-llm", "0.2")

    def _restore_geometry(self) -> None:
        store = self._store()
        size = store.value(f"{self._settings_key}/size")
        pos = store.value(f"{self._settings_key}/pos")
        width = self._default_width
        height = self._default_height
        if size is not None:
            try:
                width, height = int(size.width()), int(size.height())
            except (AttributeError, TypeError, ValueError):
                pass
        width = max(MIN_W, width)
        height = max(MIN_H, height)
        screen = QApplication.primaryScreen()
        if pos is not None and screen is not None:
            try:
                point = pos
                if screen.availableGeometry().contains(QRect(point.x(), point.y(),
                                                             width, height)):
                    self.setGeometry(point.x(), point.y(), width, height)
                    self._geometry_ready = True
                    return
            except (AttributeError, TypeError):
                pass
        if screen is not None:
            area = screen.availableGeometry()
            x = area.right() - width - 24
            y = area.top() + 24
            self.setGeometry(x, y, width, height)
        else:
            self.resize(width, height)
        self._geometry_ready = True

    def save_geometry(self) -> None:
        if not self._geometry_ready:
            return
        store = self._store()
        store.setValue(f"{self._settings_key}/size", self.size())
        store.setValue(f"{self._settings_key}/pos", self.pos())
        store.sync()

    def reset_geometry(self) -> None:
        store = self._store()
        store.remove(f"{self._settings_key}/size")
        store.remove(f"{self._settings_key}/pos")
        self._restore_geometry()
        self._place_grip()

    def _place_grip(self) -> None:
        self.grip.move(self.width() - GRIP - 2, self.height() - GRIP - 2)
        if QApplication.platformName() != "offscreen":
            self.grip.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_grip()

    # -- 显示：绝不抢焦点 --------------------------------------------------
    def _ensure_on_screen(self) -> None:
        """把窗口位置夹回主屏可见范围，防止历史几何把它放到了屏幕外。"""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        x = max(area.left(), min(self.x(), area.right() - self.width()))
        y = max(area.top(), min(self.y(), area.bottom() - self.height()))
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def _mouse_inside(self) -> bool:
        return self.frameGeometry().contains(QCursor.pos())

    def _mouse_near(self, proximity: int = 60) -> bool:
        """鼠标是否在窗口边缘附近（含内部），用于刚弹出时的宽容区。"""
        return self.frameGeometry().adjusted(-proximity, -proximity,
                                             proximity, proximity
                                             ).contains(QCursor.pos())

    def _flash_on_top(self) -> None:
        """新查询：置顶一小段时间后自动解除，让窗口既出现在最前、又不迟滞。

        迟滞的根源不是 ABOVE 本身，而是「等用户点击其它窗口时才撤销」：
        Mutter 撤销该状态是异步的，点击的瞬间才发就来不及。这里提前撤销 ——
        置顶 400ms 后自动 `set_above(False)`，窗口仍停在当前栈顶位置；用户
        之后点击任何窗口都没有待处理的异步撤销，因此零迟滞。
        """
        if self._is_offscreen():
            return
        x11_state.set_above(int(self.winId()), True)
        self._floated = True
        self._pin_timer.start()            # 400ms → _unfloat()

    def _float(self) -> None:
        """置顶弹窗（弹出时 / 用户移入时）。

        pinned：堆叠完全交 WM，**内容更新绝不 raise**——否则在 Chrome 里点
        一下（剪贴板变化）词典又压上去；新查询的置前由
        ``show_without_focus(bring_to_front=True)`` 走一次性 restack。
        unpinned（GoldenDict 取词弹窗）：静态 ``WindowStaysOnTopHint`` 已
        保证顶层，这里只 ``raise_()``；**绝不动态增删
        _NET_WM_STATE_ABOVE** —— Mutter 移除该状态是异步的，会让位迟滞。
        """
        if self._persistent:
            return
        self.raise_()

    def _unfloat(self) -> None:
        """解除 pinned 的置顶状态；unpinned 靠静态 flag + hide，无需处理。

        只撤销 ``_NET_WM_STATE_ABOVE``，窗口停在当前堆叠位置不动。
        """
        if not self._persistent or not self._floated:
            return
        self._floated = False
        if not self._is_offscreen():
            x11_state.set_above(int(self.winId()), False)
        self._diag("解除置顶（窗口仍在栈顶）")

    def _yield_to_other_window(self) -> None:
        """失去焦点时：pinned 解除置顶（窗口保留），unpinned 直接隐藏。"""
        if self._persistent:
            self._unfloat()
        else:
            self.hide()

    def _diag(self, reason: str) -> None:
        if os.environ.get("CLIPLLM_DIAG"):
            print(f"[diag] hide: {reason}", flush=True)

    def _diag_sample(self) -> None:
        if not os.environ.get("CLIPLLM_DIAG"):
            return
        ps = x11_state.pointer_state()
        elapsed = int((time.monotonic() - self._shown_at) * 1000)
        print(f"[diag] state: visible={self.isVisible()} inside={self._mouse_inside()} "
              f"near={self._mouse_near(PROXIMITY)} entered={self._mouse_entered} "
              f"elapsed_ms={elapsed} mask={ps[2] if ps else None} "
              f"active={x11_state.active_window()}", flush=True)

    def _poll_mouse(self) -> None:
        """X 窗口场景的快速让位增强。

        坐标类的「离开即关」已交给 enterEvent/leaveEvent + 定时器（事件驱动，
        在原生 Wayland 窗口上依然可靠）；这里的坐标轮询在原生 Wayland 窗口上会
        冻结，只保留两个对 X 窗口有效的信号：窗口外按下、活动窗口变化。
        """
        if not self.isVisible():
            return
        self._poll_tick += 1

        # ① 全局 Esc 兜底：窗口从不激活，QShortcut 收不到键盘。这里只读 X11
        #    键位图，不 grab、不改焦点、不拦截任何应用的按键。
        if not self._is_offscreen() and self._poll_tick % 2 == 0:
            pressed = x11_state.escape_pressed()
            if pressed and not self._esc_held:
                self._esc_held = True
                self._diag("全局 Esc（窗口未激活）")
                self.dismiss_requested.emit()
            elif not pressed:
                self._esc_held = False

        # ② pinned：只有「查询闪现置顶」仍持有时才兜底解除，其余时间完全
        #    不做判定，堆叠交给 WM。正常路径是 400ms 自动解除或 leaveEvent，
        #    这里只覆盖「鼠标没离开窗口就直接点别处」的边角情况。
        if self._persistent:
            if not (self._floated and not self._is_offscreen()):
                return
            # 兜底：窗口外按下时立刻解除置顶（正常路径是 400ms 自动解除
            # 或 leaveEvent，这里覆盖鼠标没离开窗口就直接点别处的边角情况）
            if not self._mouse_inside():
                buttons = x11_state.pointer_buttons()
                if buttons is not None and buttons & x11_state.ANY_BUTTON:
                    self._diag("窗口外按下，解除置顶")
                    self._unfloat()
            return
        if not self._auto_hide:
            return
        if self._is_offscreen():
            self._poll_mouse_qt()           # offscreen 下无 X 状态，走 Qt 回退
            return

        # ③ unpinned：Alt+Tab 切到别的程序也要隐藏。放在 mouse_inside 之前，
        #    否则鼠标停在词典上时切走，静态置顶窗口会一直压在其它窗口之上。
        if not self._persistent and self._poll_tick % 5 == 0:
            active = x11_state.active_window()
            if active is not None and self._active_at_show is not None:
                allowed = {self._active_at_show, int(self.winId())}
                if active not in allowed:
                    self._diag(f"活动窗口变化 0x{self._active_at_show:x}->0x{active:x}")
                    self._yield_to_other_window()
                    return
        if self._mouse_inside():
            return                          # 在窗口内：不做坐标类让位
        buttons = x11_state.pointer_buttons()
        if buttons is not None and buttons & x11_state.ANY_BUTTON:
            self._diag("窗口外按下鼠标（X surface）")
            self._yield_to_other_window()   # 其他窗口按下：一次即隐藏
            return
        if self._poll_tick % 30 == 0:
            self._diag_sample()

    def _poll_mouse_qt(self) -> None:
        """offscreen / X11 不可用时的回退：依赖 QCursor + Qt 鼠标状态。"""
        if self._mouse_inside():
            return
        if QApplication.mouseButtons() != Qt.NoButton:
            self._yield_to_other_window()

    def enterEvent(self, event) -> None:
        self._mouse_entered = True
        # 持久模式不做「移回来就重新置顶」：鼠标一晃回词典又压住 Chrome，
        # 正是「点好几次都沉不下去」的另一半原因。这里只取消待执行的让位。
        if self._persistent:
            # 用户在看：保持当前置顶，不再自动解除
            self._pin_timer.stop()
        else:
            self._hide_timer.stop()
            self._idle_timer.stop()
            self._float()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._mouse_entered = False
        if self._persistent:
            # 鼠标离开就立即解除置顶：用户接下来很可能去点别的窗口，
            # 不能留一个「待异步撤销」的 ABOVE 状态给那一刻。
            self._pin_timer.stop()
            self._unfloat()
        elif self._auto_hide:
            self._hide_timer.start()       # GoldenDict 400ms 宽限后隐藏
        super().leaveEvent(event)

    def set_auto_hide(self, enabled: bool) -> None:
        self._auto_hide = bool(enabled)
        if not enabled:
            self._hide_timer.stop()
            self._idle_timer.stop()
            if not self._persistent:        # 持久模式保留轮询供全局 Esc 使用
                self._mouse_poll.stop()

    def apply_theme(self, dark: bool) -> None:
        """整体切换 QSS / 窗口底色；MainWindow 覆写以额外重画自绘图标。"""
        self.setStyleSheet(build_qss(dark))
        palette = self.palette()
        palette.setColor(QPalette.Window, QColor(tokens(dark)["background"]))
        self.setPalette(palette)

    def _follow_mouse(self) -> None:
        """把窗口定位到鼠标右下方（GoldenDict engagePopup 同款），越界翻转。"""
        cursor = QCursor.pos()
        screen = QApplication.screenAt(cursor) or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        x = cursor.x() + 14
        y = cursor.y() + 14
        if x + self.width() > area.right():
            x = cursor.x() - self.width() - 14
        if y + self.height() > area.bottom():
            y = cursor.y() - self.height() - 14
        x = max(area.left(), min(x, area.right() - self.width()))
        y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(x, y)

    def show_without_focus(self, *, bring_to_front: bool = False) -> None:
        """首次显示时定位到鼠标附近；后续更新内容不 activate。

        ``bring_to_front`` 只在新查询 / 用户主动唤起时传 True：持久窗口从不
        隐藏，必须主动提到前面才能像 GoldenDict 那样「直接弹出词典」；普通
        内容刷新保持不动堆叠，避免 Chrome 里随便一点又把词典拉回来。
        ``raise_()`` 只改堆叠、不请求激活焦点。

        大小由用户拖拽后通过 QSettings 记住（hide 时 save_geometry），
        位置每次新弹出时跟随鼠标。注意：绝不能用 ``setWindowFlag`` 或
        ``windowHandle().isVisible()`` 做运行时切换/重映射——会重建窗口，
        表现为闪烁、fcitx 被重置。"""
        was_visible = self.isVisible()
        if not was_visible:
            self._follow_mouse()
            self._mouse_entered = False      # 重新弹出，重置进入标记
            self._poll_tick = 0
            self._shown_at = time.monotonic()
            self.show()
            self.raise_()               # unpinned 静态置顶 / pinned 首次弹出版
        elif bring_to_front:
            self.raise_()               # 新查询：常驻窗口也要可见
        if not self._is_offscreen():
            # 抑制 GNOME「「clip-llm · …」已就绪」通知：以不抢焦点方式 show
            # 时 Mutter 会把窗口标记为请求注意力，用户得点通知才能看到词典。
            # WM 可能在 map 之后才置位，故补发一次；只捕获 winId，不捕获 self。
            wid = int(self.winId())
            x11_state.clear_demands_attention(wid)
            QTimer.singleShot(
                120, lambda: x11_state.clear_demands_attention(wid))
        if not self._is_offscreen():
            # 两种模式都要记录：unpinned 的 Alt+Tab 白名单、pinned 的置顶边沿
            self._active_at_show = x11_state.active_window()
        if os.environ.get("CLIPLLM_DIAG"):
            g = self.frameGeometry()
            print(f"[diag] SHOW t={time.monotonic():.2f} "
                  f"geom=({g.left()},{g.top()},{g.right()},{g.bottom()}) "
                  f"active_at_show={self._active_at_show}", flush=True)
        self._esc_held = False              # 新内容：允许下一次 Esc 再触发
        self._float()                       # 置顶并提到最前（不抢焦点）
        if bring_to_front:
            # 新查询：先出现在最上层，400ms 后自动解除置顶（窗口位置不变）。
            # 提前解除是关键——等用户点击时才撤，Mutter 异步处理就迟滞。
            self.raise_()
            if self._persistent:
                self._flash_on_top()
        # 持久模式即使关掉自动隐藏也要保留轮询，否则全局 Esc 兜底失效
        if self._auto_hide or self._persistent:
            self._mouse_poll.start()
            if self._auto_hide and not self._persistent and not was_visible:
                self._idle_timer.start()    # 旧弹窗：从未进入则 6s 兜底取消置顶
        self._place_grip()

    def _accept_focus_on_click(self) -> None:
        """用户点击内容区：让窗口可以接收键盘（不改窗口标志，不重建）。"""
        self.activateWindow()

    def hideEvent(self, event) -> None:
        self.save_geometry()
        self._hide_timer.stop()
        self._idle_timer.stop()
        self._pin_timer.stop()
        self._unfloat()                     # 关闭时清掉 ABOVE，避免残留置顶
        self._mouse_poll.stop()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self.save_geometry()
        super().closeEvent(event)

    # -- 边缘判定 ----------------------------------------------------------
    def _edges_at(self, local: QPoint) -> str:
        edges = ""
        rect = self.rect()
        if local.x() <= EDGE:
            edges += "L"
        elif local.x() >= rect.right() - EDGE:
            edges += "R"
        if local.y() <= EDGE:
            edges += "T"
        elif local.y() >= rect.bottom() - EDGE:
            edges += "B"
        return edges

    def _is_offscreen(self) -> bool:
        return QApplication.platformName() == "offscreen"

    def _system_window(self):
        """有真实窗口句柄时优先交给窗口管理器缩放/移动（xcb 下有效）。"""
        return None if self._is_offscreen() else self.windowHandle()

    def _qt_edges(self, edges: str):
        result = Qt.Edges(Qt.NoEdge)
        if "L" in edges:
            result |= Qt.LeftEdge
        if "R" in edges:
            result |= Qt.RightEdge
        if "T" in edges:
            result |= Qt.TopEdge
        if "B" in edges:
            result |= Qt.BottomEdge
        return result

    def handle_move(self, global_pos: QPoint, local: QPoint, buttons) -> None:
        # Wayland 下由合成器接管（startSystemResize / startSystemMove），
        # 客户端自行 setGeometry 会被忽略 x/y，导致「拖左边缘却改右宽」。
        if self._system_interactive:
            return
        if buttons & Qt.LeftButton and self._resize_start is not None \
                and self._resize_edges and self._resize_geom is not None:
            dx = global_pos.x() - self._resize_start.x()
            dy = global_pos.y() - self._resize_start.y()
            geom = QRect(self._resize_geom)
            if "L" in self._resize_edges:
                geom.setLeft(geom.left() + dx)
            if "R" in self._resize_edges:
                geom.setRight(geom.right() + dx)
            if "T" in self._resize_edges:
                geom.setTop(geom.top() + dy)
            if "B" in self._resize_edges:
                geom.setBottom(geom.bottom() + dy)
            if geom.width() >= MIN_W and geom.height() >= MIN_H:
                self.setGeometry(geom)
            return
        if buttons & Qt.LeftButton and self._drag_origin is not None \
                and self._drag_start_geom is not None:
            delta = global_pos - self._drag_origin
            self.move(self._drag_start_geom.topLeft() + delta)
            return
        edges = self._edges_at(local)
        cursor = {
            "L": Qt.SizeHorCursor, "R": Qt.SizeHorCursor,
            "T": Qt.SizeVerCursor, "B": Qt.SizeVerCursor,
            "LT": Qt.SizeFDiagCursor, "RB": Qt.SizeFDiagCursor,
            "RT": Qt.SizeBDiagCursor, "LB": Qt.SizeBDiagCursor,
        }.get(edges)
        self.setCursor(cursor or Qt.ArrowCursor)

    def handle_button(self, global_pos: QPoint, local: QPoint,
                      pressed: bool, modifiers) -> bool:
        if not pressed:
            self._system_interactive = False
            self._resize_start = None
            self._resize_edges = None
            self._drag_origin = None
            self.save_geometry()
            return False
        window = self._system_window()
        edges = self._edges_at(local)
        if edges:
            if window is not None:
                try:
                    if window.startSystemResize(self._qt_edges(edges)):
                        self._system_interactive = True
                        return True
                except (RuntimeError, AttributeError):
                    pass
            self._resize_edges = edges
            self._resize_start = global_pos
            self._resize_geom = QRect(self.geometry())
            return True
        # 顶部 34px 或按住 Alt：拖动窗口（内容区普通拖动留给选文本）
        if local.y() <= 34 or bool(modifiers & Qt.AltModifier):
            if window is not None:
                try:
                    if window.startSystemMove():
                        self._system_interactive = True
                        return True
                except (RuntimeError, AttributeError):
                    pass
            self._drag_origin = global_pos
            self._drag_start_geom = QRect(self.geometry())
            return True
        return False

    def mouseMoveEvent(self, event) -> None:
        self.handle_move(event.globalPosition().toPoint(),
                         event.position().toPoint(), event.buttons())
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        handled = self.handle_button(event.globalPosition().toPoint(),
                                     event.position().toPoint(), True,
                                     event.modifiers())
        if not handled:
            # 点击内容区（非边缘缩放、非拖动条）：解除「不接受焦点」
            self._accept_focus_on_click()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self.handle_button(event.globalPosition().toPoint(),
                           event.position().toPoint(), False, event.modifiers())
        super().mouseReleaseEvent(event)
