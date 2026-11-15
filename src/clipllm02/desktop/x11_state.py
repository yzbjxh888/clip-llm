"""绕过 Qt 事件队列，直接读 X11 全局鼠标 / 活动窗口状态。

Qt 的 ``QApplication.mouseButtons()`` 依赖 Qt 收到鼠标事件；弹窗从不 grab 或
激活，在其他窗口上的点击、以及纯键盘的 Alt+Tab 切换都不会更新该状态。这里用
ctypes 调 libX11 的 ``XQueryPointer`` / ``XGetWindowProperty``，拿到全局按钮掩码
与 ``_NET_ACTIVE_WINDOW``，供弹窗「切走即让位」使用。

仅供 xcb / X11 会话；offscreen 或 libX11 缺失时各函数返回 ``None``，调用方回退。
"""
from __future__ import annotations

import ctypes

_BUTTON1 = 1 << 8
_BUTTON2 = 1 << 9
_BUTTON3 = 1 << 10
ANY_BUTTON = _BUTTON1 | _BUTTON2 | _BUTTON3

_XA_WINDOW = 33        # 预定义 atom：窗口 ID

_x11 = None
_disp = None
_na_atom = None        # _NET_ACTIVE_WINDOW 的 atom id 缓存


def _load():
    global _x11
    if _x11 is not None:
        return _x11
    try:
        x = ctypes.CDLL("libX11.so.6")
    except OSError:
        _x11 = False
        return None

    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XDefaultRootWindow.restype = ctypes.c_ulong
    x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x.XInternAtom.restype = ctypes.c_ulong
    x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x.XFree.argtypes = [ctypes.c_void_p]

    x.XQueryPointer.restype = ctypes.c_int
    x.XQueryPointer.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint),
    ]
    x.XGetWindowProperty.restype = ctypes.c_int
    x.XGetWindowProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
    ]
    x.XSendEvent.restype = ctypes.c_int
    x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                             ctypes.c_long, ctypes.c_void_p]
    x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x.XSync.restype = ctypes.c_int
    x.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    x.XQueryKeymap.restype = ctypes.c_int
    x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x.XKeysymToKeycode.restype = ctypes.c_ubyte
    _x11 = x
    return x


def _display():
    global _disp
    if _disp is None:
        x = _load()
        if not x:
            return None
        _disp = x.XOpenDisplay(None)
    return _disp


def pointer_state() -> tuple[int, int, int] | None:
    """返回 ``(root_x, root_y, button_mask)``；不可用时返回 ``None``。"""
    d = _display()
    x = _load()
    if not d or not x:
        return None
    root = ctypes.c_ulong()
    child = ctypes.c_ulong()
    rx = ctypes.c_int()
    ry = ctypes.c_int()
    wx = ctypes.c_int()
    wy = ctypes.c_int()
    mask = ctypes.c_uint()
    try:
        ok = x.XQueryPointer(d, x.XDefaultRootWindow(d),
                             ctypes.byref(root), ctypes.byref(child),
                             ctypes.byref(rx), ctypes.byref(ry),
                             ctypes.byref(wx), ctypes.byref(wy),
                             ctypes.byref(mask))
    except Exception:            # noqa: BLE001
        return None
    if not ok:
        return None
    return rx.value, ry.value, mask.value


def pointer_buttons() -> int | None:
    """只返回全局按钮掩码（``None`` 表示不可用）。

    掩码本身不涉及坐标缩放，可放心与 Qt 坐标判断配合使用。
    """
    st = pointer_state()
    return None if st is None else st[2]


class _ClientMessage(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("data", ctypes.c_ulong * 5)]


def _set_wm_state(win_id: int, atom: bytes, on: bool) -> bool:
    """通过 WM 协议 client message 增删一个 ``_NET_WM_STATE_*`` 状态。

    不重建窗口、不改焦点，返回是否成功发送。
    """
    d = _display()
    x = _load()
    if not d or not x:
        return False
    wm_state = x.XInternAtom(d, b"_NET_WM_STATE", 0)
    state_atom = x.XInternAtom(d, atom, 0)
    root = x.XDefaultRootWindow(d)
    ev = _ClientMessage()
    ev.type = 33                       # ClientMessage
    ev.window = win_id
    ev.message_type = wm_state
    ev.format = 32
    ev.data[0] = 1 if on else 0        # 1=add, 0=remove
    ev.data[1] = state_atom
    ev.data[2] = 0
    ev.data[3] = 1                     # source: application
    try:
        mask = (1 << 20) | (1 << 19)   # SubstructureRedirect | SubstructureNotify
        rc = x.XSendEvent(d, root, 0, mask, ctypes.byref(ev))
        x.XSync(d, 0)
        return bool(rc)
    except Exception:                  # noqa: BLE001
        return False


def set_above(win_id: int, on: bool) -> bool:
    """动态添加/移除 ``_NET_WM_STATE_ABOVE``（置顶），仅旧弹窗模式使用。"""
    return _set_wm_state(win_id, b"_NET_WM_STATE_ABOVE", on)


def clear_demands_attention(win_id: int) -> bool:
    """清掉 ``_NET_WM_STATE_DEMANDS_ATTENTION``。

    窗口以「不抢焦点」方式 show 时，Mutter/GNOME 会把它登记为请求注意力，
    弹出「「窗口标题」已就绪」通知，用户还得点一下通知才把词典唤到前面。
    显式移除该状态即可抑制通知；纯 WM 状态消息，不影响键盘焦点。
    """
    return _set_wm_state(win_id, b"_NET_WM_STATE_DEMANDS_ATTENTION", False)


_XK_ESCAPE = 0xFF1B


def escape_pressed() -> bool:
    """X11 全局键位图里 Esc 是否按下；不可用时返回 ``False``。

    只读状态：不 grab 按键、不改焦点、不拦截任何应用的 Esc。窗口从不激活
    时 ``QShortcut`` 收不到键盘，这是不抢焦点前提下最轻的兜底。原生
    Wayland 焦点窗口的按键 XWayland 看不到，此时返回 False，用户仍可点击
    窗口一次后走原有 Qt 快捷键。
    """
    d = _display()
    x = _load()
    if not d or not x:
        return False
    try:
        code = int(x.XKeysymToKeycode(d, _XK_ESCAPE))
        if not code or code >= 256:
            return False
        keys = (ctypes.c_ubyte * 32)()
        x.XQueryKeymap(d, ctypes.cast(keys, ctypes.c_void_p))
        return bool(keys[code // 8] & (1 << (code % 8)))
    except Exception:            # noqa: BLE001
        return False


def active_window() -> int | None:
    """返回当前 X11 活动窗口 id（``_NET_ACTIVE_WINDOW``），失败返回 ``None``。"""
    global _na_atom
    d = _display()
    x = _load()
    if not d or not x:
        return None
    if _na_atom is None:
        _na_atom = x.XInternAtom(d, b"_NET_ACTIVE_WINDOW", 0)
    root = x.XDefaultRootWindow(d)
    actual_type = ctypes.c_ulong()
    actual_format = ctypes.c_int()
    nitems = ctypes.c_ulong()
    bytes_after = ctypes.c_ulong()
    prop = ctypes.POINTER(ctypes.c_ubyte)()
    try:
        rc = x.XGetWindowProperty(d, root, _na_atom, 0, 1, 0, _XA_WINDOW,
                                  ctypes.byref(actual_type),
                                  ctypes.byref(actual_format),
                                  ctypes.byref(nitems),
                                  ctypes.byref(bytes_after),
                                  ctypes.byref(prop))
    except Exception:            # noqa: BLE001
        return None
    if rc != 0 or nitems.value == 0 or actual_format.value != 32 or not prop:
        return None
    value = ctypes.cast(prop, ctypes.POINTER(ctypes.c_ulong))[0]
    x.XFree(prop)
    return int(value)
