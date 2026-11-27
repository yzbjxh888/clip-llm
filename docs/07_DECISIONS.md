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

## ADR-13：两级拦截、闸门闭集与分类器定义域（2026-11-16）

**背景**：本产品是弹窗词典，「不该弹的内容弹了」就是缺陷。而旧的四级体系里
`WARN`（发但提醒）与 `CONFIRM`（点两次才发）**本身都要弹一次窗**，与目标直接
矛盾；同时 `path`/`url`/`filename` 只是「有风险」，却长期占着拦截级别，
和分类器争夺同一批输入的解释权。

**结论：**

1. **只保留两级**：`BLOCK`（静默，不弹窗、不发请求、只写脱敏计数）与
   `ALLOW`（正常弹窗）。`WARN`/`CONFIRM` 从配置与代码中删除。
   设置页「拦截」标签改为**勾选＝拦截、取消勾选＝放行**；`locked` 项显示为
   不可点击（空输入、1000 字符上限、每日配额）。
2. **闸门是闭集，判据只有三种形态**：
   - 整串形状匹配（`no_letters` / `single_latin` / `url` / `path` / `email` /
     `filename` / `token_blob`）；
   - 隐私子串匹配（`secret` / `pem_block`）；
   - 元数据（`too_long` 字符总数、`daily_limit`、`duplicate`）。
   **硬拦截不得引用任何语义特征**（`has_cn`、`code_like`、`cn_term_shape`…）。
   这一条是「闸门与分类器不会冲突」的形式化保证：闸门只判形状/隐私/元数据，
   分类器只判形状合法文本的内容特征，两者定义域互斥。
3. **分类器定义域 = 全部输入 − 闸门命中**，因此 `url` / `path` / `email` /
   `filename` 不再是「类别」，`filename` 规则从 L3 表移除；`general` 的含义
   变为「形状合法、非隐私、无任何签名」。
4. **中文四段派发只有一个实现**：`cn_shape`（疑问→段落→句解→词语），
   policy 的 `cn_*_shape` 与 intent 的 `applicable` 都消费同一组特征，
   不再各自实现 `cn_dominant` / `cn_sent_route`。
5. **软长度上限降为说明**（`policy.notices`），不参与判定、不弹窗：
   中文术语 20 / 句 120 / 段落 600、英文单词 15、短语 5 词全部不再提示，
   只保留 1000 字符绝对上限。理由：用户已表达查询意图，长度不应替他决定。
6. **纯图片信封不参与文本闸门**：有资产且无文本时，`no_letters` 之类的形状
   规则不得把它判成 BLOCK。
7. **旧配置自动迁移**（`settings.migrate_policy`）：`WARN`/`CONFIRM` → `BLOCK`；
   已删除的 `drive` / `*_too_long` / `english_sentence` 整条丢弃，避免残留级别
   触发校验错误挡住保存。
8. **代码形态优先于裸公式猜测**：`latex_strong`（`\begin{}`、`\(`、`$…$`、
   `\command`）无条件判公式；`code_form`（多行代码块或 `code_score ≥ 0.5`）
   成立时「裸公式」证据失效——否则 `auto mem_fn = std::mem_fn(...)` 里的
   下划线会被当成数学下标，整段 C++ 被判成公式。只有**以公式开头**的粘贴
   （   `\begin`、`$$`、`\frac…`）才让代码形态让位；`x_{n+1}` 的花括号下标作为
   独立公式证据。结构性证据（`code_block`）也排在长度证据（`long_text_code`）之前。
9. **代码证据分强弱，弱证据不得否决中文自然句**：`code_marks`（调用括号、
   `::`、`->`/`=>`、代码关键字、行尾 `;{}`、C 预处理指令）才是否掉裸公式猜测
   的结构性证据；`code_like` 的「强符号计数」与 `is_cli` 属于弱证据，不能把
   「这段代码 `print(1)` 是什么意思？」「git 怎么用？」从中文答疑里抢走。
   行首 `#` 只认 C 预处理指令（`#include`/`#define`…），否则 Markdown 的
   `## 结论` 会被判成代码块。

**作废**：ADR-4 的级别表（WARN/CONFIRM/ALLOW 语义）自本条起失效，
`docs/02_INPUT_POLICY_SPEC.md` §4.2 的判别表同步以本条为准。

## ADR-14：图片模板与文本模板分离，图片提示词单一权威（2026-11-16）

**背景**：图片识别提示词曾有两个来源——全局 `desktop_vision_prompt` 与
`routes.image_ocr.prompt`。视觉调用只读前者，于是用户在「提示词模板」里改
「图片文字」模板完全不生效，看起来像「我的提示词被删了，反而是另一条生效」；
同时图片模板和文本模板混在同一个下拉里，`image_table` / `image_explain` /
`image_annotate` 三套模板没有任何执行路径。

**结论：**

1. **单一权威**：视觉模型的提示词取 `desktop_vision_route` 指向的图片模板
   （默认 `image_ocr`）的 `prompt`；只有该模板为空时才回退
   `desktop_vision_prompt`。解析集中在 `studio/vision.py::vision_prompt()`。
2. **新增 `desktop_vision_route`**（默认 `image_ocr`），在设置页
   「提示词模板 → 图片模板」区内用下拉选择；`validate()` 校验它必须存在且
   确实是 `vision: true` 的模板。
3. **提示词模板页分组**：模板下拉按「文本模板 / 图片模板」两段列出，组标题
   不可选；选中图片模板时页内提示改为「识别图片时发给视觉模型的提示词」。
   （`vision: true` 是唯一的分类依据，模板由数据驱动，不硬编码 id。）
4. **非 OCR 图片模板直接出结果**：`image_table` / `image_explain` /
   `image_annotate` 的模型返回本身就是答案，渲染为结果卡片，不再当作新的
   文本查询二次请求；`image_ocr` 仍走「识别 → 初判 → 解释」链路。
5. **vision 来源不再无条件判公式**：只有默认模板是 `image_ocr`、且识别结果
   不是代码形态、也不是中文自然句时，才优先判 `latex`；否则按内容正常分类。
   **已被 ADR-15 的 `followup_route=auto` 取代**：auto 现在传
   `force_route=""` 走正常判类，不再钉死 latex。
6. 本条属于 ADR-7 冻结区的**定点解冻**：只改提示词来源与识别结果去向，
   不动取图、截图、MIME 校验与工作线程方式。

## ADR-15：图片是一等模态——先出菜单、选模板、再识别（2026-11-16）

**背景**：0.2 一直是「截图 → 立刻 OCR → 自动判 latex」，于是
（1）每截一次图都弹一次窗；（2）`image_table` / `image_explain` /
`image_annotate` 三套模板没有任何执行路径；（3）用户改图片模板看不到效果。

**结论：**

1. **总开关** `image.vision_enabled`（默认 true，设置 → 模型）。关闭后
   截图不取图、不弹菜单、不发视觉请求，只给一句提示。
2. **图片走信封**：`InputEnvelope.for_image(asset, text?)`，原图落
   `AssetStore`（数据对象只带 asset_id/摘要），然后与文本**共用**同一条
   `preflight → suggest → build_menu` 管线。纯图片信封不受文本闸门约束。
3. **菜单即选择**：截图后的初判页，图片模板就是**「查询方式」**（推荐区），
   设置里选定的 `desktop_vision_route` 排在第一位；用户点哪套，才用哪套的
   提示词识别。双模态（图 + 文）时文本类别占推荐区，图片模板补进
   「其他角度」，保证两类都点得到。
4. **识别在提交时发生**：`on_intent` 发现所选意图的 `primary_route` 是
   `vision: true` 的路由时，组装 `该模板 prompt + 用户附加要求` 调视觉模型。
   `image_ocr` 的结果回流文本初判；其余模板的返回已是答案，直接渲染结果，
   不再当新问题二次请求。
5. **双模态的定义**：`modality=mixed` = 同一信封里既有图又有文。图片由视觉
   模板消费，文本由文本模板消费，两者在菜单里同时可达（`build_menu` 的
   `image_pick` 把图片模板排进「其他角度」最前，避免写了几个字就选不到
   图片模板）。**不把图片字节塞进文本请求**：字节成本、上下文长度与
   兼容性都不划算。
6. **截图即弹初判窗，但不自动识别**：截图/剪贴板图片与文本一样自动弹出初判
   窗口（`ClipboardMonitor.image_found` → `on_vision`），只弹窗、不产生任何
   视觉请求；`image.vision_enabled` 关闭后连窗口都不弹。
7. **设置页分工**：`提示词模板` 页按「文本模板 / 图片模板」两段列出，图片
   模板的提示词就在该页编辑（`route_prompts`），旁边选「图片默认排最前」；
   `其他视角` 页只维护通用解读角度，不再放图片提示词，避免两个编辑入口。
   全局兜底 `desktop_vision_prompt` 只作为「模板为空」时的代码级回退，
   设置页不再提供编辑框。
8. 本条继续 ADR-7 的定点解冻范围：不动取图、截图、MIME 校验与线程模型。

**补充（同日）：图片查询方式与图片模板 1:1**

9. 图片侧只有一条视觉链路，不需要文本侧那样的分类派发。查询方式固定 4 条
   （文字 / 表格 / 解释 / 标注），与 4 条 `vision: true` 路由一一对应。
   原 `image.formula` / `image.translate` / `image.chart` 三条别名（各带一套
   重叠的 JSON 契约）合并进「理解这张图」：公式解释、图表趋势、译文用同一套
   可选字段表达；公式的深度解释仍走「图片文字 → LaTeX 模板」的文本链路。
10. 合并的迁移规则：`settings.migrate_intents()` 无条件丢弃三条已废弃 id（幂等）；
    `migrate_vision_prompts()` 只在用户仍原封沿用旧出厂文案时升级
    `image_explain`，自定义过的提示词一律不碰。
11. 顺带修掉一处隐患：`_on_vision_done` 按路由决定「回流文本」还是「直接渲染」，
   而这三条别名的路由是 `image_ocr` 却返回 JSON，导致 JSON 被当成新的文本
    查询再进一次初判。1:1 之后每条路由只有一种输出契约，该问题不存在。

12. **图片侧收敛为「一条模板 + 多条查询方式」**。4 条 `vision` 路由
    （图片文字/表格/解释/标注）在「提示词模板」里是 4 个条目，「查询方式」
    按模板分组也碎成 4 组，而图片侧根本没有分类器需要这种一一对应。
    现在只有一条模板 `image`（图片识别），4 条查询方式各自带
    `prompt_override`（文字 / 表格 / 解释 / 标注各一条输出契约），模板
    prompt 只作兜底。`_on_vision_done` 改按 **intent id** 分派
    （`image.ocr` 回流初判，其余直接渲染）。`desktop_vision_route` 迁移为
    `desktop_vision_intent`，老配置里 4 条路由的提示词按需搬进查询方式，
    自定义过的文案不覆盖。
13. **图片是一等类别**：只要信封带图，分类器直接判 `image`（图片识别），
    推荐区 = 该类别下的 4 条查询方式；文本框内容只作附加要求，判定出的文本
    类别降为可切换的候选（初判标签的「其他 ▾」）。切换类别后只显示新类别
    的查询方式，图片方式不再渗进别的类别——此前「推荐区按模态强行插入图片
    方式」，导致切到中文仍看到图片查询方式。
    链路因此与文本一致：输入 → 分类器 → 类别 → 模板（提示词）→ 查询方式。

14. **「复用模板 / 专属提示词」以实际发送的提示词为准**。`prompt_override`
    非空就是专属提示词（`compose_prompt` / `intent_vision_prompt` 都是
    override 优先、模板垫底），为空才是复用主路由模板。此前用
    `use_legacy_template` 做判据，图片 4 条同时带该标记与专属提示词，
    界面上写着「复用模板」、实际发的却是专属内容，模板内容也对不上。
    收敛为 `models.intent_prompt_source()` 一处判定；图片方式不再打
    legacy 标记，迁移幂等（存过盘也能纠正）。提示词模板页同时显示
    「当前 N/M 条查询方式复用此模板」，模板是否被用上一眼可见。

15. **图片回流一次点击走完**：截图 → 初判里选一条图片方式 → 视觉模型识别 →
    自动按初判结果用文本模板提问，不再弹第二次初判。开关
    `image.auto_continue`（默认开；设置 → 模型 可关，改为先看识别结果）。
    回流后若默认项仍是图片方式（即没判出文本类别），则不自动提交、退回手动
    选择，避免又发一次视觉请求；被硬拦截时给一行提示而不是静默——用户主动
    触发的链路里，静默等于没反应。

16. **图片方式声明「识别后怎么继续」**（`followup_route`）：
    `""` 直接渲染识别结果（表格 / 解释 / 标注这类模型已给结构化答案）；
    `"auto"` 按识别文字自动判类，选该类别的默认文本模板（图片文字、以及
    没有输出契约的自定义图片方式）；给具体模板 id（如 `latex`）则强制用该
    模板下的第一条查询方式。中间那次初判与两次请求全部自动完成，用户只点
    一次。注意首次实现漏了 `process_text` 内的触发调用（`_auto_submit`
    写好了却没被调用），已补应用层集成测试：截图 → `_on_vision_done` 必须
    落在结果层且 `last_selection` 是文本方式。

17. **「识别后」在「查询方式」页暴露，模板池动态引用**：选项 = `""` 直接展示 /
    `"auto"` 自动判类 / 某个文本模板 id。下拉里的模板实时取自提示词模板页，
    以后新增模板无需改代码。图片方式的「主路由」下拉只列图片模板（文本方式
    只列文本模板）——挂错模板会让图片方式不再被当作视觉请求而静默失效；
    迁移会把挂错的图片方式修回图片模板，校验里也会报一条错误。显式选「直接
    展示」不会被「老载荷缺字段」的兼容默认改写。

18. **「理解这张图」契约瘦身 + 空字段不渲染**：旧契约 9 个字段（3 种结构），
    8B 视觉模型只填 type/components，其余留空 → 渲染成一屏「—」。新契约 4-5
    个字段（type / summary / key_points / components 可选 / notes），并明确
    「没有内容就省略整个字段」。渲染器同步：空字段不再画出来、整列为空的表格
    列不占表头、`name`/`role` 补正确中文标签（名称 / 作用，此前是 Name /
    所在位置）。旧契约按标记串自动升级，用户自定义的不动。
19. **`route_prompts` 的空串等于「没有覆盖」**：它曾把模板提示词清空（页面空框、
    运行时也没有提示词），现于载入时丢弃空键、保存时删键、应用时要求非空。

20. **模型「半个 JSON + 文字清单」要合并，不能两份都显示**：8B 视觉模型常输出
    一个只含 components 的 JSON，再把 type/summary/key_points 用「- 字段: 值」
    另写一遍，旧渲染会把两段都贴出来（看起来像重复+乱码）。现在先尝试把这些
    字段并回 JSON，合并成功就不再显示原文；契约里也加了一句「只输出一个 JSON
    对象，不要把同样内容再列一遍」。

21. **图片模板只放跨方式通用的契约**：模板是类别级、会被所有图片方式继承，
    所以只写与内容形态无关的规则（只给结果不写过程、公式用 LaTeX 双写、
    不编造）。JSON 字段、坐标系、`$...$`/`$$...$$` 这类包裹符属于某一条方式
    的输出契约，必须写在方式自己的提示词里——否则「提取表格」「标注」继承
    到一段公式专用文案就是错的。出厂默认已改成通用版；用户改过的不覆盖。
    两侧都空时校验报错，避免请求静默落到全局兜底。

22. **图片模板提示词允许真正留空**：图片类别由模态判定、不经过分类器，所以
    它的模板不需要"分类器兜底"。留空 = 该模板不提供提示词，请求只用各条
    方式自己的提示词；此时校验要求每条图片方式都有提示词，否则报错（不再
    静默落到全局兜底）。此前"空 = 不覆盖"会把它回退成模板自带文案，导致
    想留空也留不了。

23. **图片结果的追问与文本一致**：直出的图片结果此前不记会话，追问会退化成
    「把问句当新内容再查一次」；若会话存在，又会因 `last_selection` 是图片
    方式而**再发一次视觉请求**（且忽略 history）。现在：视觉请求记下
    「[图片] 方式名 + 附加要求」与所用 system，直出结果后写入 chat_history；
    追问统一走文本链路（`env.assets and not followup` 才算视觉提交），
    system 换成自由作答版，history 带上上一轮。

24. **图片侧的「自定义」与文本侧对齐**：此前用户要求被降级成「用户附加要求」，
    挂在某段通用/OCR 提示词后面，模型只会照着那段落跑；图+文时更糟——条件
    判断把自由输入送进了文本链路，图片直接被忽略。现在：`intent is None`
    且有用要求时，要求就是主指令（`mode="prompt"` 时直接当提示词），
    且带图时自由输入一律走视觉（含双模态）。

25. **「图 + 一句问题」里的那句问题不能再被丢掉**：视觉提交只认
    `custom_instruction`，用户写在查询框里的文字虽进了 `env.text`，却从未
    发给视觉模型——于是选「公式识别」再问「这公式什么意思」，模型只做 OCR。
    现在带图时 `env.text` 会作为「用户附加要求」；拼进提示词时明确声明
    「以上要求优先于前面的输出格式」，避免方式的「只返回识别结果」压住
    用户真正的问题。

26. **追问带原图**：追问是纯文本请求，历史里只有上一轮答案（比如一个
    "3"），文本模型看不到图，只能顺着答案编——图片追问因此容易产生幻觉。
    现在追问时把原图作为 `image_url` 附在 user 消息里（OpenAI 兼容格式），
    文本模型若是多模态就能重新看图；遇到 400/404/415/422 自动去掉图重发，
    不会因为模型不支持图片而整条失败。开关 `desktop_followup_image`（默认开）。
27. **查询框不再串到下一张图**：截图时框里的文字已作为附加要求进了 env，
    捕获后清空输入框；否则下一张截图会把上一句当成新要求。

28. **视觉提示词把用户要求放在最前**：小视觉模型对开头最敏感，要求写在末尾
    会被方式的「只返回识别结果」压住（实测：问“有几个符号”，8B 只回一段
    LaTeX）。现在结构是「用户的要求 → 直接回答、不要只做识别 → 【参考格式】
    方式提示词」，方式契约降级为参考，冲突时以要求为准。

29. **图片追问的 system 必须中性**：此前把上一轮的完整视觉提示词（含
    「用户的要求：这张图有几个符号」）当作追问 system，模型于是把旧问题
    当成常驻指令，每次追问都在重答上一题。现在 system 固定为
    `VISION_FOLLOWUP_SYSTEM`（说明"就一张图回答后续问题、按图作答、不编造"），
    上一轮上下文只由 history 承载。

30. **「其他视角」在图片输入下也要能用**：视角是通用解读角度（历史背景、
    发展过程…），走文本模型；此前图片信封会被「请选择一种图片查询方式」的
    前置守卫挡住，点它就弹一句提示。现在：带图 + 视角 → 不拦截、不走视觉，
    正文用「请从「X」的角度解读这张图片。」，原图随请求附带（不支持图片的
    模型自动降级），结果可直接追问。

31. **表格里的 LaTeX 不能进 `<pre>`**：`cases/matrix` 等环境被 `_looks_like_code`
    误判为代码，塞进不换行的 `<pre>`，表格因此被撑到几千像素宽——界面上就是
    「大片空白 + 横向滚动」。现在：LaTeX 环境一律按公式处理；单行值不再用
    `<pre>`（改可折行的等宽 span），多行代码仍用 `<pre>`；CSS 保持 Qt 支持的
    子集（Qt 不支持 `white-space:pre-wrap`，已由结构修复代替）。

32. **同一张剪贴板图不会被当成新图**：`on_vision` 先取剪贴板图片，再退回截屏。
    截图工具常把截图写回剪贴板，而 `read_clipboard_image()` 是直接读剪贴板、
    不消费，于是每次「截图查询」都拿到上一张图——表现为「查不同公式，返回的
    总是第一次/上一次那个」。现在按摘要记账（最近 8 张，`_mark_image_used`）：
    命中已用过的图就当作没有新图，继续走截屏。

33. **长公式的 JSON 要能自愈**：模型在长公式里常把 `\cap` / `\subset` 写成
    单个反斜杠（`\c` 不是合法 JSON 转义），一处写错整段解析失败；旧兜底会
    扫描平衡括号**只抠到 `structure` 的第一项**当答案，余下的源码原样贴在下面
    ——这就是「部分/作用/说明 后面跟一串 JSON」。现在解析前先 `_repair_json`：
    非法转义补成 `\\`、去尾逗号、补未闭合字符串与括号（截断也能救回字段）。
    另外平衡扫描里若**前缀仍是 JSON**，说明抓到的是内层节点，直接放弃，
    宁可提示「按原文展示」也不把碎片当结论。

34. **图片请求也要票据**：文本链路一直校验 `request_generation` / `envelope_id`，
    图片链路却直接把结果画上去。视觉模型对长公式要十几秒，上一张图的请求迟到
    就会盖掉这一张的答案——表现就是「选『这张图在讲什么』，看到的还是上次的
    公式」。现在 `bridge_vision` 带上 generation 与 envelope_id，`_on_vision_done`
    丢弃过期/换图后的结果，失败提示也落在发起请求的那个标签。
35. **图片身份按像素算**：`wl-paste` / `xclip` 对同一份剪贴板内容每次重新出
    PNG，字节会变，按字节记账认不出「还是上次那张」，于是又串扰。改为
    256×256 灰度无关的像素摘要（同一张图不同编码 → 同一摘要）。

36. **查询框不能留上一次的正文**：`process_text()` 走的是内联流水线，漏了
    `_present_envelope()` 末尾的 `query_edit.clear()`。于是一次文本查询之后，
    查询框还握着那段公式；下一次截图时 `on_vision` 把它当「附加要求」拼进视觉
    提示词，模型就照着旧公式答（表现为「截八面体，回来的是上一个公式的解释」，
    且 `type` 还写着「公式」）。现在两处入口都清空查询框，并记住
    `_last_query_text`；截图时若框里正是它，按残留丢弃（只认用户新写的要求）。


## NC-001 结案登记（2026-11-27，L3 计划阶段）

- **结论**：中段 80–2000 三类放行沿用现有 `classify.py::CATEGORY_RULES` + `features.py`，不新增正则或阈值。判据：`cn_paragraph`=category=="chinese_paragraph"；`code_block`=category=="code" 且 reason∈{code_block,multiline_code_symbol}；`latex_formula`=category=="latex" 且 reason=="latex"。`long_text_code` / `code_like` 弱证据不放行。
- **证据来源**：`.factory/03-plan/research.md` §3 Decision（NC-001 结案）、`.factory/01-spec/spec.md` §7、`.factory/01-spec/schema.json`（`content_kind_allowlist` / `classification_result`）。
- **实现落点**：`classify.py::content_kind_of()`、`features.py::content_kind_allowlist()`、`policy.py::route_mid_text()`；diagnostic `classification_result` 序列化 category/reason，三类判定与 veto 轨迹见 `explain_text()`。
- 按 `.factory/01-spec/spec.md` §6 约定，本次只登记结论与来源，不回写 spec/schema/GLOSSARY。
