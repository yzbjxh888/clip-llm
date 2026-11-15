# 0.2 决策记录（ADR）

本文件记录 2026-11-06 讨论中已拍板或由实现方代为拍板的事项，后续实现与本文件冲突时以本文件为准。

## ADR-1：MenuCard 放在独立进程

**结论：接受。**

`clip-llm` 0.1 的常驻守护进程通过 systemd unit 禁用了 Qt Wayland text-input
（`QT_WAYLAND_TEXT_INPUT_PROTOCOL=`、`QT_WAYLAND_DISABLED_INTERFACES=...`），
这是修复 fcitx5 / Chrome 输入法串扰的关键措施。0.2 的意图菜单包含自由输入框，
必须在能正常使用中文输入法的进程里运行。

因此：

- `MenuWindow` 由主进程以 `Popen` 拉起，复用 `clip-llm settings` 的独立进程方案；
- 启动该子进程时移除上述环境变量限制；
- 主进程通过本地 Unix socket 把 `MenuModel` 推给子进程，子进程回传
  `UserSelection` 或 `CANCELLED`；
- 主进程侧不创建任何可编辑控件，继续禁用 text-input；
- `ResultCard` 仍是只读 `QTextBrowser`，`InteractiveWindow` 用 Qt 原生控件，
  两者都不重新引入 `QWebEngineView`。

**代价**：多一次进程启动（约 100~200ms）与一套 IPC 协议；换来输入法与
Chrome 焦点不退化。若后续 GNOME / Qt 版本解决了 text-input 冲突，可回退为同进程。

## ADR-2：允许输入框，且提示词组合按"主路由 + 片段引用"实现

**结论：输入框保留，且扩展为两种模式。**

对"提示词可能包含其他路由"的实现理解确认为 **ADR-2a**：

- 一个 Intent 由 `primary_route` 的 `prompt.md` 作主体；
- `includes` 可以引用其他 `RoutePack` 的指定 section，拼进同一份 prompt；
- 输出契约（`query_type`、字段列表、JSON 转义铁律）仍由系统 append-only 注入，
  且 `query_type` 只取主路由，避免 `normalize()` 无法判定。

在用户确认"可以有输入框"之后，进一步把自由输入扩展为 **ADR-2b 两种模式**，
由 `free_input.mode` 配置，菜单里以下拉切换：

| 模式 | 输入框含义 | 请求构造 |
|---|---|---|
| `instruction`（默认） | 用户描述"想要什么" | 套 `FREEFORM_TEMPLATE`，用户输入作为 `custom_instruction` |
| `prompt` | 用户直接写/改完整提示词 | 用户输入作为 prompt 主体，系统契约仍 append-only 追加在最后 |

`prompt` 模式下的安全约束：

- 用户输入不得包含输出契约段落（拼接时若检测到则剥离并记诊断）；
- 用户输入只进缓存 hash，不进任何日志、拦截记录、诊断文本；
- 长度上限 `free_input.max_chars`（默认 2000 字）；
- 仍走 `normalize()` 通用兜底，任意字段都能渲染成完整卡片。

**假设说明**：用户回答"可以有输入框"未明确指向"提示词组合"还是"菜单输入"。
此处按"两者都要"实现，因为 ADR-2a 与 ADR-2b 互不冲突，且都覆盖用户原话
「相同的内容 A 可以以许多不同的方式进行解释」「提示词可能包含其他路由」。

## ADR-3：`disabled_route_action` 默认值（实现方决定）

**结论：按触发策略分模式给默认值，不做全局一刀切。**

| `trigger_policy` | 默认 `disabled_route_action` | 理由 |
|---|---|---|
| `auto` | `block` | 保持 0.1 行为与 77 个回归用例不变 |
| `confirm` / `hotkey` | `block` | 同上 |
| `menu` | `hide_from_menu` | 用户明确要求"关闭英语查询 / 关闭词组查询"= 菜单里不出现，而不是静默不弹窗 |

三个取值：

- `block`：命中禁用路由 → 静默拦截，写脱敏记录（0.1 行为）；
- `fallback`：退回 `general` 路由（0.1 已支持）；
- `hide_from_menu`（新）：该路由不出现在菜单、参考风格下拉、其他角度；
  但规则表仍可保留用于诊断，且 `Policy Engine` 前置阻止它产生请求。

已有缓存一律保留不删，重新启用后立刻命中。

## ADR-4：拦截分级调整（实现方决定）

**结论：采用下表默认值，且每一条都可在设置页覆盖。**

| 项 | 0.1 行为 | 0.2 默认 | 理由 |
|---|---|---|---|
| `empty` | BLOCK | BLOCK | 无解释对象，不可关闭 |
| `url` | BLOCK | **WARN** | 用户可能想总结网页；菜单提供"总结网页"意图 |
| `email` | BLOCK | **WARN** | 用户可能想翻译/分析邮件 |
| `path` / `drive` | BLOCK | **WARN** | 用户可能想解释项目结构 |
| `filename` | BLOCK | **WARN** | 0.1 把 `config.py` 拦死，菜单模式下应由用户决定 |
| `secret` | BLOCK | **CONFIRM** | 疑似密钥默认不外发，需显式授权；多行代码仍豁免 |
| `fake_word` | BLOCK | **WARN** | `aaaa` 之类交给用户判断 |
| `*_too_long`（word/phrase/中文各类） | BLOCK | **WARN** | 用户已表达意图，长度不该替他决定 |
| `english_sentence` | BLOCK | **不拦截** | 修复 `break the ice.` 被判整句拦死的真实痛点；对应规则默认 `enabled=false` |
| `too_long`（绝对上限 `code_block_max`） | BLOCK | BLOCK | 成本硬上限 |
| `daily_limit` | BLOCK | BLOCK | 配额硬上限 |
| `duplicate` | 跳过 | 跳过 | 不变 |

WARN 的界面行为：菜单正常出现，判定条下方多一行黄色提示
`「检测到 URL，将发送到第三方模型」`，提交即视为授权；不改变请求内容。
CONFIRM 的界面行为：先弹一次明确的授权确认，未授权不产生请求。

## ADR-5：图片与文本冲突

**结论**：新增 `ModalityArbiter`，300ms 合并窗口，`image_text_conflict` 默认
`image_only`（丢弃同窗口内的文本事件），可选 `text_only` / `mixed` / `ask`。
`mixed` 时原图与文本一起进请求，原图存 `AssetStore`，数据对象只放路径与摘要。

`trigger_policy=menu` 下 `image_auto_recognize` 强制禁用（设置页保存时拒绝），
因为被动上传剪贴板图片违反"用户选择前不请求模型"。手动按 `Ctrl+Alt+S`、
托盘「识别图片」属于用户主动选择，允许走快捷预设直接调视觉模型。

## ADR-6：不改动 0.1 源码

0.2 目录是**独立交付**：规格文档 + 出厂配置 + 纯标准库可运行核心骨架。
`0.1 旧项目目录（仓库外）` 保持只读，迁移按
`docs/06_MIGRATION_PLAN.md` 分阶段进行，随时可回滚。

## ADR-7：交互硬化与呈现修复边界（2026-11-08）

**结论：以下模块为冻结区，不得以"优化交互"为由改动**——它们是为规避
Ubuntu / Wayland 下窗口闪烁、置顶异常、输入法与 Chrome 串扰而做的既定补偿：

- `desktop/window_base.py`：焦点防护、自动隐藏、置顶、几何与定时器；
- `studio/clipboard_monitor.py`：`dataChanged` + 常驻 `wl-paste --watch`；
- `studio/vision.py` 与 `studio/app.py::on_vision`：图片识别取图与请求流程；
- `studio/input_bridge.py` / `input_host.py` 的环境隔离与子进程启动方式。

在此基础上拍板：

1. **请求票据挂在标签上**：`request_generation` / `last_selection` /
   `pending_confirmation` / `invalidated` 随 `QueryTab` 生命周期回收，
   废弃 `id(tab)` 全局表；回包须同时通过标签存活、generation、
   `envelope_id` 三重校验。
2. **硬否决只有一个执行点**：`on_intent` 与 `process_text` 都做 `preflight`；
   `runtime`（配额）、`sensitive_patterns`、`levels` 行为表与 `enabled`
   必须真正参与判定。BLOCK 只作数据层面的作废，不隐藏/重绘窗口。
3. **CONFIRM 用二次点击同一入口授权**：复用已存在的黄色提示条，
   不新增窗口、控件或焦点动作；首次点击零请求。
4. **结果排版按 `QTextDocument` 能力实现**：内联样式 + JSON/Markdown
   双通道；缓存只存模型原始文本，键含 `RENDER_SCHEMA`。
5. **UI 外观（控件、布局、配色）另行单独一轮处理**，本轮不改；
   回归以 `tests/test_studio_hardening.py` 为基线（254 个用例）。

## ADR-8：恢复自动意图识别（2026-11-08）

**结论：0.2 是「根据复制或图片内容自动识别意图的辞典」。分类器负责初判
默认意图，用户仍保留全部选择权；初判层可切换其他路由与自由输入。**

背景：0.2 初版把 `gd_llm.py` 的 L3 有序规则表降级成「领域候选 + 用户手选」，
`default_intent` 恒为 `None`、推荐区首项是旧脚本模板。用户反馈「复制句子不会
自动判定为句子路由」，且 `fake_word` / 中文长度闸门把中文句子静默拦死。

1. **自动初判 ≠ 自动请求**：新增 `classify_intent()`，按旧分类器的四段派发
   （疑问 → 段落 → 句解 → 术语）+ 公式 / 化学 / 代码 / 英语分支产出
   `Suggestion.suggested_intent`；`build_menu` 把它置顶并设为默认高亮。
   按 `primary_route` 匹配意图而非硬编码意图 id，保持可插拔；
   有历史偏好（`last_used`）时用户习惯优先。菜单阶段仍零模型请求。
   「常用查询方式」= 初判意图下的多面向解读，「其他角度」= 其他领域视角，
   两者都不缩减。
2. **图片以公式为主**：`source="vision"` 优先 `latex` / `image_ocr` 路由；
   不改 `vision.py` 与剪贴板监听（ADR-7 冻结区）。
3. **中文句型分流**：`fake_word` 正则限定 ASCII（此前任何 ≥13 字中文都命中）；
   中文长度拆为术语 20 / 句子 120 / 疑问 120 / 段落 600 四条独立规则
   （WARN 而非静默 BLOCK，对齐 ADR-4），不再共用 `cn_pure_max=20`。
4. **配置迁移**：`load_config()` 幂等把旧 `chinese_too_long.applies_when`
   从 `has_cn` 迁到 `cn_term_like`；不升级 `CONFIG_VERSION`、不触发
   `_merge_intents` 重建意图列表，也不改用户设定的 `level`。
   用户配置中「13 条规则全 BLOCK」属个人设置，不由代码擅自重置。

## ADR-9：类别初判复用旧分类器（2026-11-08）

**结论：初判的目的是「根据输入内容特征判定最可能的类别」；类别 =
提示词模板（`settings.routes`），判定逻辑复用 `gd_llm.py` 的 L3 有序规则表。**

背景：用户复制中文句子时初判层显示「中文 / 术语」。定位后确认那不是分类
结果，而是 `zh` 领域固定 label（`builtin_settings.json`）；真正的分类能力
在 0.2 里只剩「领域候选 + 用户手选」。

1. **新增 `classify.py`**：照搬旧脚本三层结构（L0 归一化 → L2 特征 →
   L3 有序规则表，每条规则带 `requires` 与 `vetoes`），补齐旧脚本特有的
   `cn_sent_route` / `cn_para_route` / `cn_sentence` / 引注剔除等谓词。
   硬否决仍归 `policy.preflight`，分类器不做拦截。
   输出 `Classification(category, label, reason, candidates, trace)`。
   `suggest.py` 删除上一版的第二套判定，统一调用本模块（单一事实源）。
2. **类别动态化**：`available_categories()` 遍历 `settings.routes`，
   显示名优先 `pack.label`，回退到内置中文名；设置页「提示词模板」
   新增/删除的模板自动成为可选类别。判定类别被停用/删除时降级到
   `general`，绝不返回不存在的选项。
3. **两类相对旧脚本的放宽**（旧脚本为省 token 而静默限长，0.2 已改为
   只 WARN）：多句中文（≥2 句末标点且长于术语上限）判 `chinese_paragraph`；
   无句末标点但含中文分句标点的半截划选判 `chinese_sentence`。
   疑问优先、段落次之、句解再次、词语兜底的顺序不变。
4. **初判层呈现**：标题显示判定类别名而非固定领域 label；chips 首项
   是判定类别（✓），其余为其他可选类别，点击 `category_changed` 只重建
   菜单。`QueryTab` 在无类别初判时（纯图片资产）回退到原领域 chips。
   「常用查询方式」按类别主导 + 同领域多面向补全，用户主动切换类别时
   忽略 `applicable` 限制（沿用「换领域不该被默认适用性挡住」的既有约定）。
5. **可解释**：`explain_text()` 输出 L2 特征与 L3 逐条命中/否决轨迹；
   `CLIPLLM_DIAG=1` 时 `StudioApp` 每次初判打印 `category` 与 `reason`。

## ADR-10：模板统一、类别开关与初判层精简（2026-11-08）

1. **提示词模板复用脚本全文**：`settings.load_defaults()` 直接读
   `spec/legacy_assets.json` 的 9 套 `SYSTEM_PROMPT_*`，注入 routes 的
   `prompt` 与 `label`；核心层 / CLI / Studio 共用同一份，不再有短版占位。
   用户对 `route_prompts` 的覆盖仍在后写入，优先级更高。
   设置页「＋ 新增模板」可选脚本全文作起点。
2. **类别总开关数量 = 模板数量**：原「领域总开关」硬编码 6 个
   `feature_switches`，与模板数量不符且不能增减。改为遍历 `routes`
   动态生成 checkbox，绑定 `routes[id].enabled`；增删模板后
   `_rebuild_switches()` 自动同步。`feature_switches` 仍供
   `disabled_domains()` 使用，只是不再单独出现在设置页。
3. **初判层只占一行文字 + 一个抽屉**：原 6 枚并列 chip 会截断文字，
   内容预览卡片又重复展示输入且占大半屏。现在判定类别写在标题里，
   全部类别收进 `QToolButton` 抽屉菜单（首项标注「初判」），
   预览卡片整栏移除（`preview` 对象保留，兼容 `new_tab` 与回归用例）；
   卡片、标签与抽屉一律无边框、无圆角、无背景。
   ADR-7 冻结区与结果排版不受影响。

## ADR-11：三层对应与界面去图标（2026-11-08）

背景：用户发现设置里「提示词模板 14 个、查询方式顶层只有 8 类」，
改查询方式后查询界面也不变；同时要求去掉图标与导航栏。

1. **三层数量对应**：设置页「② 查询方式」树按 `settings.routes` 分组，
   顶层数量恒等于提示词模板数；「类别总开关」与「③ 其他视角」表格
   行数同样由 routes 驱动，增删模板自动同步。查询方式保留 `domains`
   属性供领域判定与回退使用，编辑区可改「所属领域」。
2. **常用查询方式全量显示**：`build_menu` 的 `recommended` = 该模板下
   启用的全部查询方式（按 order，自动判定项置顶），不再按领域二次
   过滤、不再被 `recommended_limit` 截断；Studio / classic 按钮容量
   同步放宽到 24。设置里增删改名，UI 数量与文案一一对应。
3. **第三层是通用视角，不是模板映射**：`other_views.items` 是一组
   可增删的解读角度（id / 名称 / 说明 / 适用领域 / 提示词 / 启用），
   `IntentOption.from_other_view()` 把它们变成可点击意图，
   `build_menu` 直接放进「其他角度」。条目数量与提示词模板无关，
   设置里改完保存即反映到初判页；条目为空时才回退到 `NEAR_DOMAINS`
   领域亲和。
4. **设置即时生效**：`reload_settings` 对每个已打开标签重跑
   `preflight / suggest / build_menu`，用 `QueryTab.refresh_menu()`
   原地刷新并保持当前层，不再只替换 settings 等下一次复制。
5. **去导航栏 + 顶栏自绘图标**：删除 `QueryTab` 独立 crumb 行，返回 /
   复制 / 关闭改为无图标纯文字按钮，内联到标题行与结果状态行；
   顶栏查询 / 搜索 / 新标签 / 设置改为无外框自绘矢量图标
   （放大镜 / 地球 / 加号 / 齿轮），尺寸与查询框一致（34×34），
   不依赖系统图标主题。ADR-7 冻结区不受影响。

## ADR-12：弹窗词典模式（2026-11-13）

背景：用户重新评估 ADR-7 的窗口策略，认为「Alt+Tab 找不到窗口、切到别的
程序就消失」是较严重的交互缺陷，希望像多数弹窗词典那样**长期置于最下层**；
但明确要求只做细致修改，不推翻已解决的 Wayland 补偿。

结论：**ADR-7 仅对 `desktop/window_base.py` 作一次有边界的解锁**；
剪贴板监听、图像识别、输入子进程三项继续冻结。

1. **窗口类型只在构造时决定**：studio 主窗口由 `Qt.Tool` 改为 `Qt.Window`
   （`desktop_persistent_window` 默认开）。Qt 会给 `Tool` 打上
   `_NET_WM_STATE_SKIP_TASKBAR`，这正是 Alt+Tab 里找不到窗口的原因。
   绝不运行时 `setWindowFlag` 重映射——重建 X 窗口是历史上 Chrome 闪烁 /
   fcitx 被重置的根源。classic 仍用 `Qt.Tool`；设置 → 行为关掉开关并重启
   即可完全回退旧行为。
2. **失去焦点只沉底、不隐藏**：`_poll_mouse` / `_poll_mouse_qt` 的 `hide()`
   统一走 `_yield_to_other_window()`；持久模式调 `_unfloat()` 移除
   `_NET_WM_STATE_ABOVE`，窗口保留且可被 Chrome 盖住，鼠标移回时
   `enterEvent` 重新 `_float()`。1.5s / 6s 定时器语义不变（取消置顶）。
3. **基准刷新**：持久窗口不会隐藏，每次 `show_without_focus()` 都要重取
   `x11_state.active_window()`，否则首次弹出时的旧基准会让下一轮 300ms
   轮询立刻把窗口判成「已切走」而沉底。
4. **不变量**：`WA_ShowWithoutActivating` 与 `show_without_focus()` 不
   activate；不改几何、Esc 语义、剪贴板与输入法链路；`_unfloat()` 加幂等
   守卫避免重复发 WM 消息。新增 `TestPersistentWindow` 回归锁定以上不变量。
5. **三处体验缺陷的定点修复（同日追加）**：
   - 持久模式**取消静态 `Qt.WindowStaysOnTopHint`**。Qt 会持续向 WM 重申
     ABOVE，覆盖 `_unfloat()` 发出的 remove 消息，表现为「点好几次才沉底」；
     置顶改由 `set_above()` 纯动态控制。旧弹窗模式保留静态标志，零行为变化。
   - 活动窗口判定一度改为持久模式专用 `60ms + 边沿检测`；该项**已被第 6
     条整体取代**，仅保留在旧弹窗模式里的原 300ms 白名单。
   - **Esc 兜底**：窗口从不激活时 `QShortcut` 收不到键盘。新增
     `x11_state.escape_pressed()` 只读 `XQueryKeymap` 键位图（不 grab、
     不抢焦点、不拦截其它应用），边沿触发 `dismiss_requested` →
     `MainWindow._on_dismiss`。原生 Wayland 焦点窗口的按键 XWayland 看不到，
     此时仍保留「点击窗口一次后走 Qt 快捷键」的原有路径。
6. **对齐 GoldenDict-ng pinned popup（最终方案）**：用户反馈 Chrome 里仍要点
   多次、Alt+Tab 仍不顺，遂对照 `xiaoyifang/goldendict-ng` 的
   `src/ui/scanpopup.cc`。它的两类窗口是：
   `defaultUnpinnedWindowFlags = Qt::Tool | Frameless | StaysOnTop`
   （鼠标移出 400ms 直接 `hideWindow()`）与
   `pinnedWindowFlags = Qt::Window`（普通窗口、不置顶、不拦截鼠标）。
   **它完全不做 `_NET_WM_STATE_ABOVE` 动态增删** —— 我们此前在 XWayland /
   Mutter 下反复 add/remove ABOVE 本身就是不稳的来源。据此撤销第 5 条的
   两项中间方案，持久模式改为：
   - `_float()` / `_unfloat()` 不再发送任何 ABOVE client message；
     `enterEvent` / `leaveEvent` 不再触发置顶与定时器；`_poll_mouse` 在
     Esc 检测后直接返回，不再查活动窗口 / 窗口外按下。堆叠全交 WM。
   - 对齐 `engagePopup`：只在**首次** `show()` 时 `raise_()`；窗口已可见时
     内容更新绝不重新 raise，否则在 Chrome 里点一下（剪贴板变化 →
     `show_without_focus`）词典又被拉到最前，这正是「点多次」的另一半。
   - 旧弹窗模式（classic / 关掉开关）仍走静态 `StaysOnTop` + 动态 ABOVE +
     300ms 白名单 + 自动隐藏，零行为变化。
   - GoldenDict 靠首次 show 时 `activateWindow()` 让 Esc 生效；本项目为守住
     「不抢焦点」红线暂不采用，保留为将来可选开关。
7. **新查询必须直接弹出词典**：第 6 条把已可见窗口的 `raise_()` 一刀切去掉
   后，常驻窗口从不隐藏，新词进来时完全没有动作、被 Chrome 盖住；GNOME 把
   窗口登记为「请求注意力」，弹出「「clip-llm · 新查询 (1/1)」已就绪」通知，
   用户得点通知才能看到词典。
   - `show_without_focus(bring_to_front=True)`：仅 `process_text`（剪贴板 /
     查询栏 / 自由输入）与托盘点击传入；已可见时 `raise_()` 只改堆叠、
     不请求激活焦点。普通内容刷新仍不动堆叠，第 6 条的修复不回退。
   - `x11_state.clear_demands_attention()` 用 WM client message 移除
     `_NET_WM_STATE_DEMANDS_ATTENTION`，show 后立即 + 120ms 补发各一次，
     抑制「已就绪」通知；纯状态消息，不影响键盘焦点。
8. **查询时短暂置顶（可选，默认开）**：补查 `scanpopup.hh` / `config.cc` 后
   修正第 6 条的表述——GoldenDict-ng **默认其实是 unpinned**
   （`pinPopupWindow=false`、`popupWindowAlwaysOnTop=false`）：窗口标志
   `Qt::Tool | Frameless | WindowStaysOnTopHint` **静态置顶**，查询一 `show()`
   必在顶层；鼠标移出 400ms → `hideTimerExpired() → hideWindow()` 直接隐藏，
   观感就是「点别处自动让到底层」。pinned 才是第 6 条的 `Qt::Window`。
   本项目要「窗口长期存在、可 Alt+Tab 找回」，不能照搬 unpinned 的 hide，
   故做混合，新增 `desktop_popup_on_top`（默认 True，**不改 windowFlags，
   设置保存即热生效**）：
   - 仅 `show_without_focus(bring_to_front=True)` 发一次 `set_above(True)`；
     `leaveEvent` 给 400ms 宽限（对齐 GoldenDict 的 `hideTimer`）、
     `_poll_mouse` 在 300ms 边沿检测 Alt+Tab，二者都只发一次
     `set_above(False)` 让窗口沉底但保留。
   - `_set_above_once()` 幂等：沉底后 `_floated=False`，轮询不再查活动窗口，
     从机制上杜绝早期「反复增删 ABOVE」的抖动。
   - 鼠标移回只取消待执行的让位，**绝不重新置顶**（早期「点多次」的另一半
     根因）。不抢焦点、不重建窗口、窗口永不自动隐藏。
   - 关掉开关即逐字回到第 6 条纯 WM 堆叠（已验证流畅的那套），作为回退。
9. **默认撤销查询置顶（真机实测反馈）**：开启后用户实测「第一次点击只让
   窗口失活（边框阴影消失），第二次点击才沉到底层」，迟滞明显，体验不如
   第 7 条的纯 WM 堆叠版本。原因是 Mutter 对 XWayland 窗口移除
   `_NET_WM_STATE_ABOVE` 是异步的，叠加原来的 400ms 宽限 + 300ms 边沿检测
   被放大。**结论：动态 ABOVE 不适合在 XWayland 下做「顶层弹出 → 立刻让位」**。
   - `desktop_popup_on_top` 默认改回 `False`，逐字回到第 6/7 条已验证流畅的
     纯 WM 堆叠；开关保留供自愿尝试，设置页文案标注实验性与迟滞风险。
   - 仍开启时尽量压低让位延迟：窗口外按下在第一个 30ms tick 立即
     `_unfloat()`（不再等宽限定时器），`_pin_timer` 400→150ms，
     活动窗口边沿 300→150ms；沉底后依旧立即停手、不重新置顶。
   - 若要「查询必在顶层」且零迟滞，只剩 GoldenDict 默认 unpinned 的 hide
     语义：移出后隐藏、下次查询重新 `show()`（新 map 天然置前、完全不碰
     ABOVE）。代价是窗口不再常驻、隐藏时 Alt+Tab 找不到，与 ADR-12 第 2
     条的诉求冲突，留待用户确认，不擅自实施。
10. **双模式落地（用户确认「体验和 GoldenDict 一样」）**：按 GoldenDict 的
    `pinButton` 把两种窗口状态都交给用户，`desktop_persistent_window` 默认
    改为 `False`。
    - **unpinned（默认）**：`Qt.Tool | Frameless | WindowStaysOnTopHint`
      静态置顶。查询 `show_without_focus(bring_to_front=True)` → 重新
      `show()` + `raise_()`，新 map 天然在顶层；让位一律 `hide()` ——
      `leaveEvent` 400ms 宽限、窗口外按下 30ms、Alt+Tab 150ms 活动窗口
      边沿、从未进入 6s 兜底。客户端 unmap 立即生效，不经过 Mutter 异步
      restack，因此零迟滞；**完全不动态增删 `_NET_WM_STATE_ABOVE`**。
    - **pinned（勾「固定窗口」）**：`Qt.Window` 常驻、Alt+Tab 可见、
      堆叠交 WM、不自动隐藏，即第 6/7 条已验证流畅的那套。窗口类型在
      构造时决定，改动需重启；实验性「查询时短暂置顶」只在此模式生效。
    - `_float()` 只 `raise_()`；`_unfloat()` 仅服务 pinned 的实验开关；
      全局 Esc 兜底与 `clear_demands_attention` 对两种模式都生效。
    - 与 GoldenDict 唯一保留的差异：继续用 `WA_ShowWithoutActivating`
      不抢焦点（守住 Chrome / fcitx 串扰红线），Esc 由 X11 键位图兜底；
      GoldenDict 的剪贴板查词会 `activateWindow()`。
11. **持久 ABOVE → 一次性 RESTACK（第三次真机反馈后定型）**：用户明确要
    「常驻 + Alt+Tab 可见 + 查询时在最前 + 点其它窗口只沉底不关闭 +
    只有 Esc 退出」，即 pinned 语义；unpinned 的 400ms 自动隐藏不是他要的
    （隐藏后 Alt+Tab 找不到，且第一次点击只让窗口失活、要第二次才盖住）。
    - 默认改回 `desktop_persistent_window=True`（pinned）。
    - 新增 `x11_state.restack_top()`：新查询发一次
      `_NET_RESTACK_WINDOW`（Above，sibling=0），只改**这一次**堆叠，
      **不写 `_NET_WM_STATE_ABOVE`**。之后点击任何其它窗口由 Mutter 正常
      盖住，不存在异步撤销导致的 ~0.5s 迟滞。
    - `_float()` 在 pinned 下仍为 no-op：普通内容刷新不动堆叠，避免 Chrome
      里一点又把词典拉回来；只有 `bring_to_front=True` 才 `raise_()` +
      restack。
    - 从设置页移除「查询时短暂置顶」勾选（ABOVE 方案弃用）；内部代码路径
      保留且默认关，仅配置文件可手动开启。
    - unpinned 保留为「不勾固定窗口」的可选模式（GoldenDict 默认取词弹窗：
      查完自动隐藏），不再是默认。
12. **RESTACK 被 Mutter 拒绝 → 改写「闪现置顶」（最终定型）**：真机验证
    `_NET_RESTACK_WINDOW` 未生效（窗口仍停在底层），同时 GNOME 闪了一下
    「已就绪」通知，说明请求被焦点保护拦下。
    - 改为：新查询时 `set_above(True)` + `raise_()`，**400ms 后自动
      `set_above(False)`**。窗口先出现在最上层；解除置顶不会改变当前堆叠
      顺序，窗口仍停在栈顶位置，视觉上不动。
    - 关键是**提前解除**：早期版本等用户点击其它窗口时才 remove，Mutter
      异步撤销来不及，表现为「点两次才沉底」；现在用户点击时已无待处理
      状态，零迟滞。
    - `enterEvent` 停掉自动解除（用户在看，保持置顶）；`leaveEvent`
      立即解除（用户要走，不留异步尾巴）；窗口外按下与 `hide()` 各兜底
      解除一次。
    - 删除 `_NET_RESTACK_WINDOW` 实现与 `desktop_popup_on_top` 配置项；
      「查询置顶」并入 pinned 模式的恒定行为，不再是用户开关。
