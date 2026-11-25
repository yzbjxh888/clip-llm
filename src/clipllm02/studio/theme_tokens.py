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

from pathlib import Path

# 勾选框图标：QSS 的 image: url() 需要文件系统的绝对路径；Qt 对 indicator
# 设 background 会吞掉原生对钩，所以用自定义图像明确画出 checked/unchecked。
# 源码树 src/clipllm02/studio/ 与安装包 /opt/clipllm02/src/clipllm02/studio/
# 都能用同一段相对层级找到 assets/。
_ASSETS = (Path(__file__).resolve().parents[3] / "assets" / "png")
_CHECK_ON = _ASSETS / "check-on.png"
_CHECK_OFF = _ASSETS / "check-off.png"


def _qss_url(path: Path) -> str:
    """QSS 的 url() 用正斜杠，Windows 也兼容。"""
    return path.as_posix()

# ---------------------------------------------------------------------------
# 1. Color Palette — 浅色（主）
# ---------------------------------------------------------------------------
LIGHT: dict[str, str] = {
    # 冷灰白 + 亮橙：干净、现代，向参考图靠拢。只改浅色表，DARK 不动。
    # 全项目颜色都从这里取（theme_tokens.py 之外无硬编码），改这一处即全局生效。
    "background": "#fafafa",        # 近纯白画布：与控件默认白几乎无色差
    "surface": "#ffffff",           # 纯白卡片
    "surface_lowest": "#ffffff",    # 输入框 / 结果区，最亮
    "surface_low": "#f4f5f8",       # 悬停 / 选中 / 气泡底
    "text": "#1b1d22",              # 近黑主文字（对比 14.8:1）
    "text_variant": "#5f6572",      # 次要文字，压深到 WCAG AA 4.5:1
    "outline": "#e9ebf0",           # 极淡分隔线，几乎不可见
    "outline_strong": "#d5d9e2",    # 稍强的分隔线
    "primary": "#bc4520",           # 橙：白字与浅底橙字均 ≥4.5:1（WCAG AA）
    "on_primary": "#ffffff",
    "primary_hover": "#a93c1b",
    "primary_disabled": "#f0c4b6",
    "primary_fixed": "#fff0eb",     # 选中态浅底
    "error": "#d0342c",
    "error_container": "#fdecea",
    "warning_text": "#8a6300",
    "warning_container": "#fff5da",
    "success": "#1f8a5b",
    "chip": "#f2f3f6",
    "scrollbar": "#d5d9e2",
    "edge": "#dfe2e9",             # 滚动条滑块，与底色同族
    "accent_soft": "#ffe6dc",      # 强调色的极淡底
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
        # 结果正文里的强调字用主文字色（近黑），不再用橙色；
        # 层次靠字重 + chip 底色，颜色只留给按钮背景与选中态。
        "accent": t["text"],
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
/* 一体化：整个窗口是一块连续表面，不靠描边卡片拼贴。
   边框只留给真正的输入控件；分组靠留白、字阶与对齐。 */
QWidget#clipLlmCard {{ background:{t['background']};
  border:none; border-radius:{RADIUS_XL}px; }}
QWidget#queryTab {{ background:{t['background']}; }}
/* 初判 / 结果两页是裸 QWidget：默认会画成纯白，和近纯白画布撞出
   一块刺眼的白。统一透明，让整窗保持同一种表面色。 */
QStackedWidget, QStackedWidget > QWidget {{ background:transparent; }}
QLabel {{ color:{t['text']}; }}
QLabel#muted, QLabel#meta, QLabel#hint, QLabel#count, QLabel#footer {{
  color:{t['text_variant']}; }}
QLabel#chip {{ background:{t['chip']}; color:{t['text']};
  border-radius:{RADIUS_DEFAULT}px;
  padding:{SPACING_XS}px {SPACING_SM}px; }}
QLabel#preview {{ color:{t['text']}; }}
QLabel#section {{ color:{t['text']}; font-size:{TYPO_LABEL_MD[0]}px;
  font-weight:bold; }}
QLabel#warn {{ color:{t['warning_text']}; background:transparent;
  padding:{SPACING_XS}px 0; }}
QFrame#card {{ background:transparent; border:none; }}
/* 输入条：透明无边框，与窗口表面同色，不再是一块白色卡片 */
QFrame#freeCard {{ background:transparent; border:none; }}
QFrame#grip {{ background:transparent; border:none; }}
QTabWidget::pane {{ border:none; background:{t['background']}; top:0; }}
/* 胶囊分段：整条浅底，选中项浮起；不用传统页签的「文件夹标签」感 */
QTabBar::tab {{ background:transparent; color:{t['text_variant']};
  padding:{SPACING_SM}px {SPACING_LG}px;
  border-radius:{RADIUS_FULL}px; margin-right:{SPACING_XS}px; }}
QTabBar::tab:selected {{ background:{t['surface_lowest']};
  color:{t['text']}; font-weight:bold; }}
QTabBar::tab:hover:!selected {{ background:{t['chip']}; }}
QTabBar::close-button {{ subcontrol-position: right; }}
QLineEdit {{ padding:{SPACING_SM}px {SPACING_MD}px;
  border:{BORDER_WIDTH}px solid transparent;
  border-radius:{RADIUS_DEFAULT}px; background:{t['surface_low']};
  color:{t['text']}; selection-background-color:{t['chip']}; }}
QLineEdit:focus {{ border-color:{t['primary']}; background:{t['surface_lowest']}; }}
QLineEdit#query {{ padding:0 {SPACING_MD}px;
  border:{BORDER_WIDTH}px solid transparent;
  border-radius:{RADIUS_DEFAULT}px; background:{t['surface_low']};
  font-size:{TYPO_BODY_MD[0]}px; }}
QLineEdit#query:focus {{ background:{t['surface_lowest']};
  border-color:{t['primary']}; }}
QLineEdit#free, QLineEdit#followup {{
  border:{BORDER_WIDTH}px solid transparent;
  border-radius:{RADIUS_DEFAULT}px; padding:0 {SPACING_MD}px;
  background:transparent; min-height:32px; }}
QLineEdit#free:focus, QLineEdit#followup:focus {{
  border-color:{t['outline_strong']}; background:{t['surface_low']}; }}
QComboBox, QSpinBox {{ padding:{SPACING_SM}px {SPACING_MD}px;
  border:{BORDER_WIDTH}px solid transparent;
  border-radius:{RADIUS_DEFAULT}px; background:{t['surface_low']};
  color:{t['text']}; }}
QComboBox:focus, QSpinBox:focus {{ border-color:{t['primary']}; }}
/* 数值框去掉原生上下箭头小窗（块状感来源）；数值仍可键盘输入 / 滚轮调 */
QSpinBox::up-button, QSpinBox::down-button {{ width:0; height:0; border:none;
  background:transparent; }}
QSpinBox::up-arrow, QSpinBox::down-arrow {{ image:none; }}
/* 下拉框：按钮区无边框，只留原生的小箭头符号 */
QComboBox::drop-down {{ border:none; width:20px; }}
/* 结果区无边框：内容直接落在连续表面上，不再像一块嵌进去的面板 */
QTextBrowser {{ background:transparent;
  border:none; border-radius:{RADIUS_LG}px; color:{t['text']}; }}
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
  color:{t['text']}; padding:0 {SPACING_XS}px;
  font-size:{TYPO_LABEL_SM[0]}px; }}
QToolButton#drawer:hover {{ color:{t['text']}; }}
/* 右上角动作：极轻的文字，不用「按钮」外形 */
QToolButton#textAction {{ background:transparent; border:none;
  color:{t['text_variant']}; padding:{SPACING_XS}px {SPACING_XS}px;
  font-size:{TYPO_LABEL_SM[0]}px; }}
QToolButton#textAction:hover {{ color:{t['text']}; }}
QToolButton#seg {{ background:{t['chip']}; color:{t['text_variant']};
  border:none; border-radius:{RADIUS_DEFAULT}px;
  padding:0 {SPACING_MD}px; min-height:32px; }}
QToolButton#seg:hover {{ background:{t['surface_low']}; }}
QToolButton#seg:checked {{ background:{t['chip']};
  color:{t['text']}; font-weight:bold; }}
/* 发送：无边框黑色图标，直接落在连续表面上 */
QToolButton#sendIcon {{ background:transparent; border:none;
  border-radius:{RADIUS_SM}px; }}
QToolButton#sendIcon:hover {{ background:{t['chip']}; }}
QPushButton {{ background:transparent;
  border:{BORDER_WIDTH}px solid transparent;
  border-radius:{RADIUS_DEFAULT}px;
  padding:{SPACING_SM}px {SPACING_MD}px; color:{t['text']}; }}
QPushButton:hover {{ background:{t['chip']}; }}
/* 查询方式 = 同一表面上的列表行：靠底色与左侧细线表示状态，不画边框卡片 */
QPushButton#intent {{ text-align:left;
  padding:{SPACING_SM}px {SPACING_MD}px;
  background:{t['surface_low']};
  border:none; border-left:{BORDER_ACTIVE_WIDTH}px solid transparent;
  border-radius:{RADIUS_ITEM}px; color:{t['text']};
  font-size:{TYPO_BODY_LG[0]}px; }}
QPushButton#intent:hover {{ background:{t['chip']}; }}
QPushButton#intent[default="true"] {{ border-left-color:{t['text']};
  background:{t['chip']}; font-weight:bold; }}
QPushButton#other {{ text-align:left;
  padding:{SPACING_XS}px {SPACING_MD}px;
  background:transparent; border:none;
  border-radius:{RADIUS_ITEM}px; color:{t['text_variant']};
  font-size:{TYPO_BODY_SM[0]}px; }}
QPushButton#other:hover {{ background:{t['chip']}; color:{t['text']}; }}
QPushButton#send {{ background:{t['primary']}; color:{t['on_primary']};
  border:none; border-radius:{RADIUS_DEFAULT}px;
  padding:{SPACING_SM}px {SPACING_LG}px; min-height:34px;
  font-weight:bold; }}
QPushButton#send:hover {{ background:{t['primary_hover']}; }}
QPushButton#send:disabled {{ background:{t['primary_disabled']}; }}
QPushButton#soft {{ background:{t['surface']};
  color:{t['text']}; border:{BORDER_WIDTH}px solid {t['outline']};
  border-radius:{RADIUS_DEFAULT}px; padding:0 {SPACING_MD}px;
  min-height:34px; }}
QPushButton#soft:hover {{ background:{t['chip']}; }}
QPushButton#tool {{ padding:{SPACING_XS}px {SPACING_SM}px;
  background:transparent; border:none; color:{t['text_variant']};
  font-size:{TYPO_LABEL_SM[0]}px; }}
QPushButton#tool:hover {{ color:{t['text']}; background:{t['chip']};
  border-radius:{RADIUS_SM}px; }}
/* 无障碍：键盘焦点必须可见。用 1px 实线强调环，不改变控件尺寸 / 布局。
   注意：QCheckBox 不能放进这条——给整个控件设 border-color 会让它推进
   原生绘制路径，未聚焦时方框渲染成黑块、聚焦时才显示对钩。复选框单独用
   下方 ::indicator 的自绘状态。 */
QWidget:focus {{ outline:none; }}
QPushButton:focus, QToolButton:focus, QLineEdit:focus,
QComboBox:focus, QSpinBox:focus, QTreeWidget:focus,
QTableWidget:focus, QListWidget:focus {{
  border-color:{t['primary']};
  }}
/* 所有勾选框统一用自定义图标：白底灰边的空心框 / 带对钩的框。
   原生 indicator 在 item view 上会退化成黑块，且 Qt 对 QSS 设了
   background/border 的 indicator 不再画对钩——所以只设 image。 */
QCheckBox {{ background:transparent; border:none; color:{t['text']}; }}
QCheckBox::indicator, QListWidget::indicator,
QTreeWidget::indicator, QTableWidget::indicator {{ width:16px; height:16px; }}
QCheckBox::indicator:unchecked, QListWidget::indicator:unchecked,
QTreeWidget::indicator:unchecked, QTableWidget::indicator:unchecked {{
  image: url({_qss_url(_CHECK_OFF)}); }}
QCheckBox::indicator:checked, QListWidget::indicator:checked,
QTreeWidget::indicator:checked, QTableWidget::indicator:checked {{
  image: url({_qss_url(_CHECK_ON)}); }}
QPushButton#send, QPushButton#soft, QToolButton#sendIcon {{
  min-height:36px; }}
"""
