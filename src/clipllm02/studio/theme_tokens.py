"""视觉常量：浅 / 深两套颜色、字阶、间距、圆角，以及统一 QSS。

组织方式参照 pyqt6-ui-designer/references/design_tokens.md
（Color Palette / Typography / Spacing / Radius / Elevation 分节，两套主题，
QSS 用 f-string 插值 token）；色值来自 design/sketches/DESIGN.md 的暖纸质基准
与第 3 轮定稿，不使用参考文件里的企业蓝白。

本模块是纯常量 + 字符串生成，不 import Qt、不 import 任何项目模块，
因此 desktop/ 可以安全引用而不会产生循环依赖。

注意区分两套样式系统：
- build_qss() 生成的是 **QSS**，用于 Qt Widgets，支持 border-radius 等属性；
- html_theme() 供 json_render 生成 **QTextDocument 的 HTML**，那里只支持
  Qt Rich Text 子集，样式必须内联到 p/span/td，不能用 class 选择器。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. Color Palette — 浅色（主）
# ---------------------------------------------------------------------------
LIGHT: dict[str, str] = {
    "background": "#f5f4ed",        # 米白纸底
    "surface": "#faf9f5",           # 象牙卡片
    "surface_lowest": "#ffffff",    # 输入框 / 结果区，最亮
    "surface_low": "#f0eee6",       # 悬停 / 选中 / 气泡底
    "text": "#141413",              # 暖黑主文字
    "text_variant": "#5e5d59",      # 次要文字（只用这一档，不发虚）
    "outline": "#e8e6dc",           # 暖色细线
    "outline_strong": "#d3cec6",    # 稍强的分隔线
    "primary": "#c96442",           # 陶土，只用在可交互处
    "on_primary": "#ffffff",
    "primary_hover": "#ad5232",
    "primary_disabled": "#c9b8ae",
    "primary_fixed": "#f0eee6",     # 选中态浅底
    "error": "#b53333",
    "error_container": "#f7ecea",
    "warning_text": "#7a5b00",
    "warning_container": "#fff4d6",
    "success": "#2a6b4f",
    "chip": "#f0eee6",
    "scrollbar": "#d3cec6",
    "edge": "#e6e1d4",             # 04：滚动条滑块 / 纸边，与底色同族
    "accent_soft": "#f3e0d6",      # 04：强调色的极淡底
}

# Dark Mode Overrides — 与 LIGHT 同名键，切换时整体替换。
# 深色沿用项目原有的那套冷深色（#181b22 / #2b3448 ...），保留 desktop_dark_theme
# 开关的既有外观；DESIGN.md 的暖纸基准只用于浅色。
DARK: dict[str, str] = {
    "background": "#181b22",
    "surface": "#20242e",
    "surface_lowest": "#20242e",
    "surface_low": "#2b3448",
    "text": "#e8ebf2",
    "text_variant": "#9aa3b5",
    "outline": "#303645",
    "outline_strong": "#3e4453",
    "primary": "#7aa7ff",
    "on_primary": "#181b22",
    "primary_hover": "#9ab9ff",
    "primary_disabled": "#4a5570",
    "primary_fixed": "#2b3448",
    "error": "#e88a84",
    "error_container": "#4a2a28",
    "warning_text": "#e8c877",
    "warning_container": "#463d22",
    "success": "#8fc7a8",
    "chip": "#2b3448",
    "scrollbar": "#3e4453",
    "edge": "#303645",
    "accent_soft": "#2b3448",
}

# ---------------------------------------------------------------------------
# 2. Typography — (size_px, weight, line_height_px, letter_spacing_em)
# ---------------------------------------------------------------------------
FONT_HEADING = '"Noto Serif CJK SC","Songti SC",Georgia,serif'
FONT_BODY = '"Noto Sans CJK SC","Microsoft YaHei",system-ui,sans-serif'
FONT_MATH = "'DejaVu Serif','Latin Modern Math',serif"

TYPO_HEADLINE_LG = (20, 600, 28, 0.0)
TYPO_HEADLINE_MD = (17, 600, 24, 0.0)
TYPO_HEADLINE_SM = (15, 600, 22, 0.0)
TYPO_BODY_LG = (16, 400, 26, 0.0)      # 方案 2：查询方式大卡片
TYPO_BODY_MD = (15, 400, 24, 0.0)      # 正文
TYPO_BODY_SM = (13, 400, 20, 0.0)      # 表格、次要内容
TYPO_LABEL_MD = (13, 600, 18, 0.0)
TYPO_LABEL_SM = (12, 500, 16, 0.0)

# ---------------------------------------------------------------------------
# 3. Spacing — 4px 网格
# ---------------------------------------------------------------------------
SPACING_UNIT = 4
SPACING_XS = 4
SPACING_SM = 8
SPACING_MD = 12
SPACING_LG = 16
SPACING_XL = 24
SPACING_CONTAINER_MARGIN = 24         # 方案 2：内容区大留白
SPACING_GUTTER = 12

# ---------------------------------------------------------------------------
# 4. Border Radius
# ---------------------------------------------------------------------------
RADIUS_SM = 4
RADIUS_DEFAULT = 8               # 04：整体收敛圆角
RADIUS_MD = 8
RADIUS_LG = 8
RADIUS_XL = 16
RADIUS_FULL = 9999
RADIUS_ITEM = 6                      # 04：条目圆角，比窗口更收敛

# 04：两处网格的档位间距与外框内边距。只在档位变化时写入，
# 重复 resize 到同一档位不重复写布局，保证幂等。
GRID_GAP = {"narrow": 4, "medium": 6, "wide": 8}
FRAME_INSET = {"narrow": SPACING_SM, "medium": SPACING_MD, "wide": SPACING_LG}

# ---------------------------------------------------------------------------
# 5. Elevation — 只用 tonal background + 1px 边线，不加重投影
# ---------------------------------------------------------------------------
BORDER_WIDTH = 1
BORDER_ACTIVE_WIDTH = 2

# 无颜色硬编码的通用片段，供各文件零散 setStyleSheet 复用
QSS_SCROLL_AREA = "QScrollArea{background:transparent;border:none;}"


def tokens(dark: bool = False) -> dict[str, str]:
    """返回当前主题的颜色表（副本，调用方可自由修改）。"""
    return dict(DARK if dark else LIGHT)


def html_theme(dark: bool = False) -> dict[str, str]:
    """供 json_render 使用：键名保持原有 bg/card/fg/muted/line/accent/chip，
    下游渲染函数无需改动；额外键用于替换原先写死在 HTML 里的状态色。
    """
    t = tokens(dark)
    return {
        "bg": t["background"],
        "card": t["surface"],
        "fg": t["text"],
        "muted": t["text_variant"],
        "line": t["outline"],
        "accent": t["primary"],
        "chip": t["chip"],
        "surface_lowest": t["surface_lowest"],
        "warning_container": t["warning_container"],
        "warning_text": t["warning_text"],
        "error": t["error"],
        "error_container": t["error_container"],
        "on_primary": t["on_primary"],
        "font_heading": FONT_HEADING,
        "font_body": FONT_BODY,
        "font_math": FONT_MATH,
    }


def build_qss(dark: bool = False) -> str:
    """生成一整套 QSS（浅 / 深共用模板，只替换 token 值）。

    原先 window_base / main_window / query_tab 三份 STYLE 各写各的、同名规则
    互相矛盾；这里合并为唯一来源，各窗口在 set_look() 时整体重设。
    QSS 是 Qt Widgets 样式表，border-radius 在这里是合法属性；
    QTextBrowser **内容 HTML** 的限制见 json_render，与此无关。
    """
    t = tokens(dark)
    return f"""
QWidget#clipLlmCard {{ background:{t['background']};
  border:{BORDER_WIDTH}px solid {t['outline']}; border-radius:{RADIUS_XL}px; }}
QWidget#queryTab {{ background:{t['background']}; }}
QLabel {{ color:{t['text']}; }}
QLabel#muted, QLabel#meta, QLabel#hint, QLabel#count, QLabel#footer {{
  color:{t['text_variant']}; }}
QLabel#chip {{ background:{t['chip']}; color:{t['primary']};
  border-radius:{RADIUS_DEFAULT}px;
  padding:{SPACING_XS}px {SPACING_SM}px; }}
QLabel#preview {{ color:{t['text']}; }}
QLabel#section {{ color:{t['primary']}; font-size:{TYPO_LABEL_MD[0]}px;
  font-weight:bold; }}
QLabel#warn {{ color:{t['warning_text']}; background:transparent;
  padding:{SPACING_XS}px 0; }}
QFrame#card {{ background:transparent; border:none; }}
QFrame#freeCard {{ background:{t['surface']};
  border:{BORDER_WIDTH}px solid {t['outline']}; border-radius:{RADIUS_LG}px; }}
QFrame#grip {{ background:transparent; border:none; }}
QTabWidget::pane {{ border:none; background:{t['background']}; top:0; }}
QTabBar::tab {{ background:{t['chip']}; color:{t['text_variant']};
  padding:{SPACING_SM}px {SPACING_MD}px;
  border-top-left-radius:{RADIUS_DEFAULT}px;
  border-top-right-radius:{RADIUS_DEFAULT}px; margin-right:3px; }}
QTabBar::tab:selected {{ background:{t['surface']};
  color:{t['text']}; font-weight:bold; }}
QTabBar::close-button {{ subcontrol-position: right; }}
QLineEdit {{ padding:{SPACING_SM}px {SPACING_MD}px;
  border:{BORDER_WIDTH}px solid {t['outline']};
  border-radius:{RADIUS_DEFAULT}px; background:{t['surface']};
  color:{t['text']}; selection-background-color:{t['chip']}; }}
QLineEdit#query {{ padding:0 {SPACING_MD}px;
  border:{BORDER_WIDTH}px solid {t['outline']};
  border-radius:{RADIUS_DEFAULT}px; background:{t['surface']};
  font-size:{TYPO_BODY_MD[0]}px; }}
QLineEdit#free, QLineEdit#followup {{
  border:{BORDER_WIDTH}px solid {t['outline']};
  border-radius:{RADIUS_DEFAULT}px; padding:0 {SPACING_MD}px;
  background:{t['surface']}; min-height:32px; }}
QLineEdit#free:focus, QLineEdit#followup:focus {{
  border-color:{t['primary']}; }}
QComboBox, QSpinBox {{ padding:{SPACING_SM}px {SPACING_MD}px;
  border:{BORDER_WIDTH}px solid {t['outline']};
  border-radius:{RADIUS_DEFAULT}px; background:{t['surface']};
  color:{t['text']}; }}
QTextBrowser {{ background:{t['surface']};
  border:{BORDER_WIDTH}px solid {t['outline']};
  border-radius:{RADIUS_LG}px; color:{t['text']}; }}
QScrollArea {{ background:transparent; border:none; }}
QScrollBar:vertical {{ background:transparent; width:6px; margin:0; }}
QScrollBar::handle:vertical {{ background:{t['edge']};
  border-radius:3px; min-height:24px; }}
QScrollBar::handle:vertical:hover {{ background:{t['outline_strong']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
QToolButton::menu-indicator {{ image: none; }}
QToolButton#barAction {{ background:transparent; border:none; padding:0; }}
QToolButton#barAction:hover {{ background:{t['chip']};
  border-radius:{RADIUS_DEFAULT}px; }}
QToolButton#drawer {{ background:transparent; border:none;
  color:{t['primary']}; padding:0 {SPACING_XS}px;
  font-size:{TYPO_LABEL_SM[0]}px; }}
QToolButton#drawer:hover {{ color:{t['text']}; }}
QToolButton#textAction {{ background:transparent; border:none;
  color:{t['text_variant']}; padding:{SPACING_XS}px {SPACING_SM}px;
  font-size:{TYPO_LABEL_SM[0]}px; }}
QToolButton#textAction:hover {{ color:{t['text']}; }}
QToolButton#seg {{ background:{t['chip']}; color:{t['text_variant']};
  border:none; border-radius:{RADIUS_DEFAULT}px;
  padding:0 {SPACING_MD}px; min-height:32px; }}
QToolButton#seg:hover {{ background:{t['surface_low']}; }}
QToolButton#seg:checked {{ background:{t['primary']};
  color:{t['on_primary']}; font-weight:bold; }}
QToolButton#sendIcon {{ background:{t['primary']}; border:none;
  border-radius:{RADIUS_DEFAULT}px; }}
QToolButton#sendIcon:hover {{ background:{t['primary_hover']}; }}
QPushButton {{ background:{t['surface']};
  border:{BORDER_WIDTH}px solid {t['outline']};
  border-radius:{RADIUS_DEFAULT}px;
  padding:{SPACING_SM}px {SPACING_MD}px; color:{t['primary']}; }}
QPushButton:hover {{ background:{t['chip']}; }}
QPushButton#intent {{ text-align:left;
  padding:{SPACING_SM}px {SPACING_MD}px;
  background:{t['surface']};
  border:{BORDER_WIDTH}px solid {t['outline']};
  border-left:{BORDER_ACTIVE_WIDTH}px solid transparent;
  border-radius:{RADIUS_ITEM}px; color:{t['text']};
  font-size:{TYPO_BODY_LG[0]}px; }}
QPushButton#intent:hover {{ background:{t['surface_low']}; }}
QPushButton#intent[default="true"] {{ border-left-color:{t['primary']};
  background:{t['surface_low']}; font-weight:bold; }}
QPushButton#other {{ text-align:left;
  padding:{SPACING_XS}px {SPACING_MD}px;
  background:{t['surface']};
  border:{BORDER_WIDTH}px solid {t['outline']};
  border-radius:{RADIUS_ITEM}px; color:{t['text_variant']};
  font-size:{TYPO_BODY_SM[0]}px; }}
QPushButton#other:hover {{ background:{t['surface_low']}; color:{t['text']}; }}
QPushButton#send {{ background:{t['primary']}; color:{t['on_primary']};
  border:none; border-radius:{RADIUS_DEFAULT}px;
  padding:{SPACING_SM}px {SPACING_LG}px; min-height:34px;
  font-weight:bold; }}
QPushButton#send:hover {{ background:{t['primary_hover']}; }}
QPushButton#send:disabled {{ background:{t['primary_disabled']}; }}
QPushButton#soft {{ background:{t['surface']};
  color:{t['primary']}; border:{BORDER_WIDTH}px solid {t['outline']};
  border-radius:{RADIUS_DEFAULT}px; padding:0 {SPACING_MD}px;
  min-height:34px; }}
QPushButton#soft:hover {{ background:{t['chip']}; }}
QPushButton#tool {{ padding:{SPACING_XS}px {SPACING_SM}px;
  background:transparent; border:none; color:{t['text_variant']};
  font-size:{TYPO_LABEL_SM[0]}px; }}
QPushButton#tool:hover {{ color:{t['text']}; background:{t['chip']};
  border-radius:{RADIUS_SM}px; }}
"""
