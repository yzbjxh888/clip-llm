"""一次查询 = 一个标签页，内部两层清晰分区。

顶部 crumb 行两层共用：搜索 / 在新标签查询 / 复制再查询 / 换一种解释 / 设置 / 关闭。
第一层 初判：预览、硬拦截、领域候选、大量领域专属查询方式（网格随窗口宽度
自适应 1~3 列）、其他角度（流式换行）、自由输入。第二层 结果：模型返回排版。
"""
from __future__ import annotations

import html
import time
from uuid import uuid4

from PySide6.QtCore import QEvent, QObject, QPointF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QIcon, QPainter, QPen, QPixmap,
                           QPolygonF)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from ..models import InputEnvelope, MenuModel, PolicyResult, Suggestion
from .latex_unicode import latex_to_unicode, strip_math_delimiters
from .theme_tokens import (FRAME_INSET, GRID_GAP, SPACING_LG, SPACING_MD,
                           SPACING_SM, build_qss, html_theme, tokens)

MAX_INTENTS = 24
MAX_CHIPS = 6
MAX_OTHERS = 12

# 秒：IME 取消组词后，短时间内 Esc 仍算输入法的，不关窗口
IME_GRACE = 1.0


class ImeWatcher(QObject):
    """记录输入框的组词 / 打字状态，不拦截任何按键。

    窗口从不激活（Esc 由 X11 键位图兜底），QApplication.focusWidget()
    在这里并不可靠；拼音候选还没上屏时 lineEdit.text() 也是空的。
    所以这里维护一个明确的「正在组词」状态，外加最近一次打字时刻。
    """

    def __init__(self, owner, edit: QLineEdit):
        super().__init__(edit)
        self._owner = owner
        edit.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.InputMethod:
            self._owner._ime_composing = bool(event.preeditString())
            self._owner._ime_last = time.monotonic()
        elif (event.type() == QEvent.Type.KeyPress
              and event.key() != Qt.Key_Escape):
            self._owner._ime_last = time.monotonic()
        return super().eventFilter(obj, event)


def send_icon(size: int = 18, color: str | None = None) -> QIcon:
    """自绘纸飞机：无外框图标按钮用，不依赖系统图标主题。"""
    painter_color = color or tokens()["on_primary"]
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor(painter_color), 1.5, Qt.SolidLine,
                        Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    scale = size / 18.0
    body = QPolygonF([
        QPointF(2.4 * scale, 9.0 * scale),
        QPointF(15.6 * scale, 2.4 * scale),
        QPointF(10.6 * scale, 15.6 * scale),
        QPointF(8.2 * scale, 10.0 * scale),
    ])
    painter.drawPolygon(body)
    painter.drawLine(QPointF(8.2 * scale, 10.0 * scale),
                     QPointF(15.6 * scale, 2.4 * scale))
    painter.end()
    return QIcon(pixmap)

# 保留模块级名称（既有回归用例会 import 它），内容由统一 token 生成。
STYLE = build_qss(False)


class QueryTab(QWidget):
    intent_selected = Signal(object)
    domain_changed = Signal(str)
    category_changed = Signal(str)         # 切换提示词模板类别
    new_tab_requested = Signal(str)
    search_requested = Signal(str, str)
    settings_requested = Signal()
    copy_query_requested = Signal(str)
    vision_image_requested = Signal()
    dismiss_requested = Signal()
    ime_requested = Signal()             # 自由输入走独立中文输入进程
    followup_requested = Signal(str)     # 针对本轮结果继续追问
    self_copy_requested = Signal(str)    # 「复制结果」写回剪贴板，通知主控抑制

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setObjectName("queryTab")
        self.setStyleSheet(build_qss(bool(settings.get("desktop_dark_theme", False))))
        self.settings = settings
        self.env: InputEnvelope | None = None
        self.policy: PolicyResult | None = None
        self.suggestion: Suggestion | None = None
        self.menu: MenuModel | None = None
        # 请求票据全部挂在标签上：随标签生命周期回收，避免用 id(tab) 作键
        # 带来的关闭后残留与对象 id 复用错配。
        self.request_generation: int = 0
        self.last_selection: dict | None = None
        self.invalidated: bool = False
        self.pending_confirmation: dict | None = None
        self._chip_ids: list[str] = []
        self._chip_kind: str = "domain"
        self._chip_options: list[tuple[str, str]] = []
        self._intent_options: list = []
        self._other_options: list = []
        self._grid_cols = 0
        self._layout_tier = ""        # 04：档位幂等守卫
        self._grid_initialized = False
        self._ime_last = 0.0           # 最近一次拼音组词 / 打字的时刻
        self._ime_composing = False    # 是否正处于输入法组词中
        self._ime_watchers: list[ImeWatcher] = []
        self._font_size = int(settings.get("desktop_font_size", 14))
        self._dark = bool(settings.get("desktop_dark_theme", False))
        # 对话式结果：已完成的问答段 + 复用的样式 + 进行中的占位段
        self.chat_blocks: list[str] = []
        self.chat_css = ""
        self._loading_block = ""
        self._chat_chars = 0          # 上一次渲染的字符数，用于追问定位
        self.last_result_text = ""

        root = QVBoxLayout(self)
        self._root_layout = root
        root.setContentsMargins(0, 8, 0, 0)
        root.setSpacing(SPACING_LG)

        # 不设独立的操作导航栏：操作以文字按钮内联到各页的标题行 / 状态行，
        # 关闭按钮不再独占一行；所有图标一律改文字。
        self._chip_buttons: list[QPushButton] = []
        for idx in range(MAX_CHIPS):
            button = QPushButton(self)
            button.setVisible(False)
            button.clicked.connect(lambda _=False, i=idx: self._select_chip(i))
            self._chip_buttons.append(button)

        def action(text: str, tooltip: str, slot) -> QToolButton:
            button = QToolButton()
            button.setObjectName("textAction")
            button.setText(text)
            button.setToolTip(tooltip)
            button.clicked.connect(slot)
            return button

        self.back_button = action("返回", "换一种解释（返回初判，Alt+Left）",
                                  self._goto_detect)
        self.copy_result_button = action("复制", "复制当前结果全文",
                                         self._copy_result)
        self.dismiss_button = action("关闭", "关闭初判窗口（Esc）",
                                     self.dismiss_requested.emit)
        self.dismiss_result_button = action("关闭", "关闭初判窗口（Esc）",
                                            self.dismiss_requested.emit)

        self.stack = QStackedWidget()
        self.detect_page = self._build_detect_page()
        self.result_page = self._build_result_page()
        self.stack.addWidget(self.detect_page)
        self.stack.addWidget(self.result_page)
        root.addWidget(self.stack, 1)
        self._goto_detect()

    # -- 第一层 ------------------------------------------------------------
    def _build_detect_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._detect_layout = layout   # 04：外框内边距按档位调整
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACING_LG)

        # 内容预览卡片整栏移除：输入内容在顶栏查询框里已可见，重复展示
        # 会占去大半屏。保留 preview 对象供 new_tab / 回归用例读写文本。
        head = QFrame(self)
        head.setVisible(False)
        self.preview = QLabel("等待输入…", head)
        self.preview.setObjectName("preview")
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.warn = QLabel("")
        self.warn.setObjectName("warn")
        self.warn.setWordWrap(True)
        self.warn.setVisible(False)
        layout.addWidget(self.warn)

        intent_header = QHBoxLayout()
        self.intent_caption = QLabel("")
        self.intent_caption.setObjectName("section")
        intent_header.addWidget(self.intent_caption)
        self.intent_count = QLabel("")
        self.intent_count.setObjectName("count")
        self.intent_count.setVisible(False)     # 不再报条数
        intent_header.addWidget(self.intent_count)
        self.category_drawer = QToolButton()
        self.category_drawer.setObjectName("drawer")
        self.category_drawer.setText("其他 ▾")
        self.category_drawer.setVisible(False)  # 首个空标签页不显示抽屉
        self.category_drawer.setToolTip("展开其他可选类别")
        self.category_drawer.clicked.connect(self._open_category_drawer)
        intent_header.addWidget(self.category_drawer)
        intent_header.addStretch(1)
        self.free_toggle = QToolButton()
        self.free_toggle.setObjectName("textAction")
        self.free_toggle.setText("自定义")
        self.free_toggle.setCheckable(True)
        self.free_toggle.toggled.connect(self._on_toggle_free)
        intent_header.addWidget(self.free_toggle)
        intent_header.addWidget(self.dismiss_button)
        layout.addLayout(intent_header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 条目多于可视区时必须能滚：之前 AlwaysOff 会把第 6 行之后直接裁掉，
        # latex 的 18 条在界面上只看到 10 个，剩下的点不到。
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        grid_host = QWidget()
        self._intent_grid = QGridLayout(grid_host)
        self._intent_grid.setContentsMargins(0, SPACING_SM, SPACING_SM, 0)
        self._intent_grid.setSpacing(GRID_GAP["medium"])
        self._intent_buttons: list[QPushButton] = []
        for idx in range(MAX_INTENTS):
            button = QPushButton()
            button.setObjectName("intent")
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumHeight(42)          # 04：双列紧凑行
            button.setVisible(False)
            button.clicked.connect(lambda _=False, i=idx: self._submit_intent(i))
            self._intent_buttons.append(button)
            self._intent_grid.addWidget(button, idx // 2, idx % 2)
        scroll.setWidget(grid_host)
        layout.addWidget(scroll, 1)

        self.other_caption = QLabel("其他角度")
        self.other_caption.setObjectName("section")
        layout.addWidget(self.other_caption)
        other_host = QWidget()
        self._other_grid = QGridLayout(other_host)
        self._other_grid.setContentsMargins(0, 0, SPACING_SM, 0)
        self._other_grid.setSpacing(GRID_GAP["medium"])
        self._other_buttons: list[QPushButton] = []
        for idx in range(MAX_OTHERS):
            button = QPushButton()
            button.setObjectName("other")
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumHeight(30)
            button.setVisible(False)
            button.clicked.connect(lambda _=False, i=idx: self._submit_other(i))
            self._other_buttons.append(button)
            self._other_grid.addWidget(button, idx // 2, idx % 2)
        layout.addWidget(other_host)

        self.free_box = QFrame()
        self.free_box.setObjectName("freeCard")
        free_layout = QHBoxLayout(self.free_box)
        free_layout.setContentsMargins(10, 9, 10, 9)
        free_layout.setSpacing(7)
        self.free_edit = QLineEdit()
        self.free_edit.setObjectName("free")
        self.free_edit.setPlaceholderText("描述你想怎么解释…")
        self.free_edit.returnPressed.connect(self._submit_free)
        self._ime_watchers.append(ImeWatcher(self, self.free_edit))
        free_layout.addWidget(self.free_edit, 1)

        # 冒烟输入区只保留「输入框 + 发送图标」：模式与输入法按钮都撤掉，
        # 中文输入仍可用 Ctrl+I 走独立进程。
        self._free_mode = "instruction"
        self.free_send = QToolButton()
        self.free_send.setObjectName("sendIcon")
        self.free_send.setIcon(send_icon(18))
        self.free_send.setIconSize(QSize(18, 18))
        self.free_send.setFixedSize(34, 34)
        self.free_send.setCursor(Qt.PointingHandCursor)
        self.free_send.setToolTip("发送（回车）")
        self.free_send.clicked.connect(self._submit_free)
        free_layout.addWidget(self.free_send)
        layout.addWidget(self.free_box)
        self.free_box.setVisible(False)

        return page

    # -- 第二层 ------------------------------------------------------------
    def _build_result_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)
        self.result_label = QLabel("结果")     # 保留属性（测试引用），不显示
        self.result_label.setObjectName("chip")
        self.result_label.setVisible(False)
        self.view = QTextBrowser()
        self.view.setReadOnly(True)
        self.view.setOpenLinks(False)
        self.view.setOpenExternalLinks(False)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._result_context_menu)
        layout.addWidget(self.view, 1)

        # 追问：只对「本轮结果」生效，输入框常驻结果页底部
        self.followup_box = QFrame()
        self.followup_box.setObjectName("freeCard")
        follow_layout = QHBoxLayout(self.followup_box)
        follow_layout.setContentsMargins(10, 9, 10, 9)
        follow_layout.setSpacing(7)
        self.followup_edit = QLineEdit()
        self.followup_edit.setObjectName("followup")
        self.followup_edit.setPlaceholderText("针对这次结果继续追问…")
        self.followup_edit.returnPressed.connect(self._submit_followup)
        self._ime_watchers.append(ImeWatcher(self, self.followup_edit))
        follow_layout.addWidget(self.followup_edit, 1)
        self.followup_send = QToolButton()
        self.followup_send.setObjectName("sendIcon")
        self.followup_send.setIcon(send_icon(18))
        self.followup_send.setIconSize(QSize(18, 18))
        self.followup_send.setFixedSize(34, 34)
        self.followup_send.setCursor(Qt.PointingHandCursor)
        self.followup_send.setToolTip("发送追问（回车）")
        self.followup_send.clicked.connect(self._submit_followup)
        follow_layout.addWidget(self.followup_send)
        layout.addWidget(self.followup_box)

        # 底部只保留一条细线：左侧状态文字，右侧三个无边框文字链，
        # 高度压到 20px，不再是一整条「导航条」
        self.status = QLabel("")
        self.status.setObjectName("meta")
        self.status.setFixedHeight(20)
        for button in (self.back_button, self.copy_result_button,
                       self.dismiss_result_button):
            # 这些是 QToolButton（textAction 已是透明无边框文字样式），
            # 不要再改 objectName，否则会掉回 Qt 默认的灰底方块
            button.setFixedHeight(20)
            button.setCursor(Qt.PointingHandCursor)
        # 定稿：底部区域不再出现在界面上。控件、尺寸与信号全部保留
        # （Esc / Alt+Left / 右键复制仍是入口），整行装进不加入布局的容器并
        # 移出可视区；不调用 hide，避免破坏既有的可见性断言与信号连接。
        self.status_bar = QWidget(page)
        status_row = QHBoxLayout(self.status_bar)
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(2)
        status_row.addWidget(self.status)
        status_row.addStretch(1)
        status_row.addWidget(self.back_button)
        status_row.addWidget(self.copy_result_button)
        status_row.addWidget(self.dismiss_result_button)
        self.status_bar.move(-10000, -10000)
        return page

    def _submit_followup(self) -> None:
        text = self.followup_edit.text().strip()
        if not text:
            return
        self.followup_edit.clear()
        self.followup_requested.emit(text)

    # -- 状态切换 ----------------------------------------------------------
    def _goto_detect(self) -> None:
        self.stack.setCurrentIndex(0)
        self.back_button.setVisible(False)
        self.copy_result_button.setVisible(False)
        self.dismiss_result_button.setVisible(False)
        self.dismiss_button.setVisible(True)
        self.followup_box.setVisible(False)

    def _goto_result(self) -> None:
        self.stack.setCurrentIndex(1)
        self.back_button.setVisible(True)
        self.copy_result_button.setVisible(True)
        self.dismiss_result_button.setVisible(True)
        self.dismiss_button.setVisible(False)
        self.followup_box.setVisible(True)

    def esc_veto(self) -> bool:
        """Esc 是否该留给输入框，而不是切页 / 关闭窗口。

        结果页的追问框：窗口从不激活（window_base 用 X11 键位图兜底），
        QApplication.focusWidget() 不可靠，拼音组词时 text() 又是空的，
        所以直接看追问框自己的状态，不靠焦点。初判页保持原来的判断。
        """
        busy = (self._ime_composing
                or time.monotonic() - self._ime_last < IME_GRACE)
        if self.stack.currentIndex() == 1:
            if busy:
                return True                    # 组词 / 打字中：Esc 归输入法
            if self.followup_edit.text().strip():
                self.followup_edit.clear()
                return True                    # 有文字：只清空，不切页
            return False

        if QApplication.focusWidget() is self.free_edit:
            if busy:
                return True
            if self.free_edit.text().strip():
                self.free_edit.clear()
                return True
        return False

    def dismiss(self) -> bool:
        """Esc：结果页先回初判；已在初判页则返回 False 让主窗口隐藏。"""
        if self.stack.currentIndex() == 1:
            self._goto_detect()
            return True
        return False

    # -- 自适应 ------------------------------------------------------------
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout_intents()

    def _relayout_intents(self) -> None:
        """04 落地：两处网格恒定两列；档位只调间距与外框，档位相同直接返回（幂等）。

        _grid_cols 仍保留 2/3 宽度档位（既有回归用例读取），但不参与按钮摆放，
        也不再重复 removeWidget / addWidget，避免反复 resize 累积或抖动。
        """
        width = max(360, self.width())
        cols = 2 if width < 900 else 3
        tier = "narrow" if width < 460 else ("medium" if width < 700 else "wide")
        if cols == self._grid_cols and tier == self._layout_tier:
            return
        self._grid_cols = cols
        self._layout_tier = tier
        gap = GRID_GAP[tier]
        self._intent_grid.setSpacing(gap)
        self._other_grid.setSpacing(gap)
        inset = FRAME_INSET[tier]
        detect = getattr(self, "_detect_layout", None)
        if detect is not None:
            detect.setContentsMargins(inset, 0, inset, 0)
        if not self._grid_initialized:
            rows = (len(self._intent_buttons) + 1) // 2
            self._intent_grid.setRowStretch(rows, 1)
            self._grid_initialized = True

    # -- 数据 --------------------------------------------------------------
    def begin_new_session(self) -> None:
        """清掉上一轮对话，让新的等待卡片成为第一屏。"""
        self.chat_blocks = []
        self.chat_css = ""
        self._loading_block = ""
        self._chat_chars = 0
        self.chat_history = []
        self.chat_system = ""

    def set_context(self, env: InputEnvelope, policy: PolicyResult,
                    suggestion: Suggestion, menu: MenuModel) -> None:
        # 新上下文写入标签 = 该标签此前所有在途请求立即作废。
        # 无论新建标签还是复用当前标签，apply_context 都会走这里。
        self.request_generation += 1
        # 新输入意味着新会话：上一轮对话与缓存段落全部作废
        self.begin_new_session()
        self.session_id = uuid4().hex[:10]
        self.last_selection = None
        self.invalidated = False
        self.pending_confirmation = None
        self.env = env
        self.policy = policy
        self.suggestion = suggestion
        self.menu = menu
        self._render_detect()
        self._goto_detect()

    def refresh_menu(self, env, policy, suggestion, menu) -> None:
        """设置保存后原地刷新初判层，保持当前所处的层（初判 / 结果）。"""
        layer = self.stack.currentIndex()
        self.set_context(env, policy, suggestion, menu)
        if layer == 1:
            self._goto_result()
        else:
            self._goto_detect()

    def set_look(self, font_size: int, dark: bool) -> None:
        self._font_size = font_size
        self._dark = bool(dark)
        self.setStyleSheet(build_qss(self._dark))
        icon_color = tokens(self._dark)["on_primary"]
        for button in self.findChildren(QToolButton):
            if button.objectName() == "sendIcon":
                button.setIcon(send_icon(18, color=icon_color))
        font = QFont()
        font.setPointSize(font_size)
        self.view.setFont(font)
        self.preview.setFont(font)

    def _selected_text(self) -> str:
        selected = self.view.textCursor().selectedText().strip() \
            if self.stack.currentIndex() == 1 else ""
        return selected.replace("\u2029", " ")

    def _render_detect(self) -> None:
        env, menu = self.env, self.menu
        if env is None or menu is None:
            return
        text = (env.text or "").replace("\n", " ")
        # quote=False：预览按纯文本显示，转成 &#x27; 反而会露出实体字面量
        self.preview.setText(
            html.escape(latex_to_unicode(strip_math_delimiters(text[:140])),
                        quote=False)
            if text else "[图片输入]")
        warnings = list(self.policy.warnings) + list(self.policy.confirmations) \
            if self.policy else []
        self.warn.setVisible(bool(warnings))
        self.warn.setText("⚠ " + "；".join(warnings) if warnings else "")

        # 类别初判只占「一行文字 + 一个抽屉」：判定结果写在标题里，
        # 其余类别收进抽屉，不并列铺开、不画边框。
        chips = list(menu.category_chips) or list(menu.domain_chips)
        self._chip_kind = "category" if menu.category_chips else "domain"
        self._chip_ids = [c.id for c in chips]
        self._chip_options = [(c.id, c.label) for c in chips]
        if self._chip_options:
            cid, clabel = self._chip_options[0]
            self._chip_options[0] = (cid, f"初判 · {clabel}")
        for button in self._chip_buttons:
            button.setVisible(False)
        self.category_drawer.setVisible(len(self._chip_options) > 1)
        self.category_drawer.setText("其他 ▾")

        self._intent_options = list(menu.recommended[:MAX_INTENTS])
        # 标题只保留初判类别名，不重复「常用查询方式」也不再报条数
        label = menu.category_label or (chips[0].label if chips else "通用")
        self.intent_caption.setText(label)
        self.intent_count.setVisible(False)
        for idx, button in enumerate(self._intent_buttons):
            if idx < len(self._intent_options):
                option = self._intent_options[idx]
                shortcut = f"{idx + 1}  " if idx < 9 else "   "
                button.setText(f"{shortcut}{option.intent.label}")
                button.setToolTip(option.intent.hint or option.intent.id)
                button.setProperty("default",
                                   "true" if menu.default_intent == option.intent.id
                                   else "false")
                button.style().unpolish(button)
                button.style().polish(button)
                button.setVisible(True)
            else:
                button.setVisible(False)

        self._other_options = list(menu.others[:MAX_OTHERS])
        self.other_caption.setVisible(bool(self._other_options))
        for idx, button in enumerate(self._other_buttons):
            if idx < len(self._other_options):
                option = self._other_options[idx]
                if option.intent.id.startswith("view."):
                    # 通用视角：纯名称；hint 为空时不能引用未定义的 domain_label
                    button.setText(option.intent.label)
                    button.setToolTip(option.intent.hint or option.intent.label)
                else:
                    domain_label = option.intent.id.split(".")[0]
                    button.setText(f"{domain_label} · {option.intent.label}")
                    button.setToolTip(option.intent.hint or domain_label)
                button.setVisible(True)
            else:
                button.setVisible(False)

    def _search_query(self) -> str:
        return self._selected_text() or self.current_text()

    # -- 交互 --------------------------------------------------------------
    def current_text(self) -> str:
        return self.env.text if self.env and self.env.text else ""

    def _emit_chip(self, value: str) -> None:
        if self._chip_kind == "category":
            self.category_changed.emit(value)
        else:
            self.domain_changed.emit(value)

    def _open_category_drawer(self) -> None:
        """抽屉：全部可选类别收在一个菜单里，只占一行文字的位置。"""
        from PySide6.QtWidgets import QMenu
        if not self._chip_options:
            return
        menu = QMenu(self)
        for cid, label in self._chip_options:
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, value=cid: self._emit_chip(value))
        menu.exec(self.category_drawer.mapToGlobal(
            self.category_drawer.rect().bottomLeft()))

    def _select_chip(self, index: int) -> None:
        if 0 <= index < len(self._chip_ids):
            self._emit_chip(self._chip_ids[index])

    def _emit_intent(self, option) -> None:
        if option is None:
            self.free_toggle.setChecked(True)
            self.free_edit.setFocus()
            return
        intent = option.intent
        self.result_label.setText(intent.label)
        self.intent_selected.emit({
            "envelope_id": self.env.envelope_id if self.env else "",
            "domain_id": (self.menu.domain_chips[0].id
                          if self.menu and self.menu.domain_chips else "*"),
            "intent_id": intent.id if not intent.free_input_only else None,
            "format_id": intent.default_format,
            "custom_instruction": None,
            "free_input_mode": "instruction",
            "style_ref": None,
            "allowed_route_ids": [intent.primary_route] if intent.primary_route else [],
        })
        # CONFIRM 首次 / 静默拒绝时留在初判层，不切到空白结果页
        if self.pending_confirmation is None and not self.invalidated:
            self._goto_result()

    def _submit_intent(self, index: int) -> None:
        if 0 <= index < len(self._intent_options):
            self._emit_intent(self._intent_options[index])

    def _submit_other(self, index: int) -> None:
        if 0 <= index < len(self._other_options):
            self._emit_intent(self._other_options[index])

    def _submit_free(self) -> None:
        custom = self.free_edit.text().strip()
        if not custom:
            return
        self.result_label.setText("自定义")
        self.intent_selected.emit({
            "envelope_id": self.env.envelope_id if self.env else "",
            "domain_id": "*", "intent_id": None, "format_id": "static",
            "custom_instruction": custom,
            "free_input_mode": self._free_mode,
            "style_ref": None, "allowed_route_ids": [],
        })
        if self.pending_confirmation is None and not self.invalidated:
            self._goto_result()

    def _on_toggle_free(self, checked: bool) -> None:
        self.free_box.setVisible(checked)

    def _new_tab(self) -> None:
        self.new_tab_requested.emit(self._search_query())

    def _copy_query(self) -> None:
        self.copy_query_requested.emit(self._search_query())

    def _copy_result(self) -> None:
        text = self.view.toPlainText().strip()
        if text:
            QApplication.clipboard().setText(text)
            self.self_copy_requested.emit(text)

    def _web_search_menu(self, selected: str):
        """选中文本的网页搜索子菜单；没有启用任何引擎时返回 None。"""
        from PySide6.QtWidgets import QMenu
        providers = self.settings.enabled_search_providers(("*",))
        if not providers:
            return None
        short = selected[:12] + "…" if len(selected) > 12 else selected
        menu = QMenu(f"用网页搜索「{short}」", self)
        for provider in providers:
            menu.addAction(
                provider.label,
                lambda _checked=False, pid=provider.id, text=selected:
                self.search_requested.emit(pid, text))
        return menu

    def _result_context_menu(self, pos) -> None:
        """结果页右键：复制 / 选中文本网页搜索 / 选中文本新标签查询。"""
        from PySide6.QtWidgets import QMenu
        selected = self._selected_text()
        menu = QMenu(self)
        menu.addAction("复制", self._copy_result)
        if selected:
            menu.addSeparator()
            short = selected[:12] + "…" if len(selected) > 12 else selected
            web = self._web_search_menu(selected)
            if web is not None:
                menu.addMenu(web)
            else:
                menu.addAction(f"浏览器搜索「{short}」",
                               lambda: self.search_requested.emit("", selected))
            menu.addAction(f"在新标签查询「{short}」",
                           lambda: self.new_tab_requested.emit(selected))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    # -- 结果 --------------------------------------------------------------
    # -- 对话式结果 ---------------------------------------------------------
    @staticmethod
    def _split_css(body_html: str) -> tuple[str, str]:
        """把渲染结果的 <style> 拆出来，多条消息复用同一份样式。"""
        head, sep, rest = (body_html or "").partition("</style>")
        if not sep:
            return "", body_html or ""
        return head + sep, rest

    @staticmethod
    def _user_bubble(text: str, theme: dict | None = None) -> str:
        """用户消息：右对齐浅底气泡，与模型卡片区分。

        气泡里也做公式符号化，避免把 ``$$q_x'' = -k\\frac{dT}{dx}$$`` 原样露出来。
        """
        t = theme or html_theme()
        raw = latex_to_unicode(strip_math_delimiters((text or "")[:300]))
        clean = html.escape(raw, quote=False).replace("\n", " ")
        return ("<p align='right' style='margin:10px 0 4px;'><span "
                f"style='background-color:{t['chip']}; color:{t['accent']}; "
                "padding:5px 11px;'>" + clean + "</span></p>")

    def _render_chat(self, anchor: str = "auto") -> None:
        """重绘对话并按语义定位滚动位置。

        ``anchor``：
          - ``top``    新查询：从提问处开始看，不要跳到末尾
          - ``new``    追问：定位到本轮追问之前的位置，刚好看得见自己的问题
          - ``bottom`` 加载中：跟到最新一条
        """
        previous = getattr(self, "_chat_chars", 0)
        parts = [self.chat_css] if self.chat_css else []
        parts.extend(self.chat_blocks)
        if self._loading_block:
            parts.append(self._loading_block)
        self.view.setHtml("".join(parts))
        document = self.view.document()
        self._chat_chars = document.characterCount()

        def settle() -> None:
            bar = self.view.verticalScrollBar()
            if anchor == "top":
                bar.setValue(0)
                return
            if anchor == "bottom":
                bar.setValue(bar.maximum())
                return
            # 追加：把光标放到本次新增内容的起点，ensureCursorVisible 会滚到那里
            limit = max(0, document.characterCount() - 1)
            cursor = self.view.textCursor()
            cursor.setPosition(min(previous, limit))
            self.view.setTextCursor(cursor)
            self.view.ensureCursorVisible()
            # ensureCursorVisible 只保证「可见」，会贴着下边缘；再上移
            # 一点，让本轮追问气泡和回答开头都进视野
            bar.setValue(min(bar.maximum(), bar.value() + 56))

        QTimer.singleShot(0, settle)

    def show_loading(self, text: str, hint: str = "") -> None:
        """等待卡片：只留一句短话，不堆说明。

        ``text`` 保留在签名里供调用方传原始内容，界面上不展示；
        QTextDocument 对 div 圆角支持不稳，用带底色的表格做卡片。
        """
        del text
        t = html_theme(self._dark)
        tip = html.escape(hint or "正在请求模型..", quote=False)
        self._loading_block = (
            f"<table width='100%' cellpadding='10' cellspacing='0' "
            f"border='0' bgcolor='{t['card']}'><tr><td>"
            f"<p style='color:{t['accent']}; font-weight:bold; margin:0;'>"
            f"{tip}</p></td></tr></table>")
        self._render_chat("bottom")
        self.status.setText(tip)
        self.status.setVisible(True)
        self._goto_result()

    def show_result(self, body_html: str, *, title: str = "",
                    error: str = "", preview_only: bool = False,
                    user_text: str = "", append: bool = False) -> None:
        """写一条助手消息；``append=True`` 表示追问，保留前面所有问答。"""
        css, body = self._split_css(body_html)
        if css and not self.chat_css:
            self.chat_css = css
        t = html_theme(self._dark)
        if not append:
            self.chat_blocks = []
            opening = (self.current_text() or "").strip()
            if opening:
                self.chat_blocks.append(self._user_bubble(opening, t))
        elif (user_text or "").strip():
            self.chat_blocks.append(
                f"<hr style='border:none; border-top:1px solid {t['line']}; "
                "margin:12px 0 0;'>"
                + self._user_bubble("追问：" + user_text.strip(), t))
        self.chat_blocks.append(body)
        self._loading_block = ""
        self._render_chat("new" if append else "top")
        # 追问时只需最近一段，取末尾避免拿到最早的对话
        self.last_result_text = self.view.toPlainText()[-1800:]
        self.result_label.setText(title or "解释结果")
        note = ("本地预览 · 未发送网络请求" if preview_only
                else (f"错误：{error}" if error else ""))
        self.status.setText(note)
        self.status.setVisible(bool(note))
        self._goto_result()
