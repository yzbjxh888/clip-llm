# 0.2 UI 交互规格

本文件定义到控件级、事件级。实现与本文件冲突时以本文件为准并同步修订。

> **实现更新（2026-11-06）**：桌面版 `menu_window.py` 采用**简化呈现**——
> 控件只创建一次、只显示领域专属意图、次要信息（其他角度 / 呈现形态 / 置灰项 /
> 自由输入）默认折叠，见 `docs/08_DESKTOP.md` 第 7 节。本文件中的「五区」描述
> 仍是逻辑分区，但视觉上合并为「头部 / 主选项 / 折叠更多 / 工具行」四层，
> 且窗口不抢焦点、可缩放、尺寸位置由 `QSettings` 记忆。

## 1. 三种窗口

| 窗口 | 进程 | 类 | 承载 | 焦点策略 | 关闭 |
|---|---|---|---|---|---|
| **MenuCard 意图菜单** | 独立子进程 | `MenuWindow` | 可点击选项、自由输入框、搜索按钮 | 默认 `WA_ShowWithoutActivating`；仅点击输入框 / 按 Tab 才 `activateWindow()` | Esc → `CANCELLED`，清 `popup-visible`，零请求 |
| **ResultCard 结果卡片** | 主进程 | `PopupWindow`（0.1 复用） | 只读 `QTextBrowser` | 永不激活、不可聚焦 | Esc → `IDLE` |
| **InteractiveWindow 交互图** | 主进程 | 新增 | Qt 原生 `QChart`/滑块 | 独立焦点域 | 独立关闭，不影响卡片 |

三者共用 `clip-llm-popup` 之外的独立 `objectName`（建议 `clipLlmMenu` /
`clipLlmInteractive`），避免 Shell 扩展 `make_above()` 误匹配。

`popup-visible` 标记在**菜单显示时就要置位**（菜单也是窗口），Esc / 关闭必须清除，
否则扩展会误转发全局 Esc/F3。

## 2. MenuCard 五区布局

```
┌──────────────────────────────────────────────────────────┐
│ [文本 · 4 字符]  来源: gnome-extension   14:52:03   ⚙ F3 │ 头部
│  NaCl                                    （预览，截断 80）│
├──────────────────────────────────────────────────────────┤
│ 初判：化学 / 标识符   MIXED   [换领域 ▾]  证据：2 元素符号 │ 判定条
│ ⚠ 检测到 URL，将发送到第三方模型                          │ WARN 条（条件显示）
├──────────────────────────────────────────────────────────┤
│ 推荐                                                     │
│  1 🧪 它是什么物质            （chem.substance）  ⏎默认  │ 区 A
│  2 🔊 怎么读 / 英文什么意思     （en_word 片段）          │
│  3 📖 作为术语的定义           （chinese 片段）          │
│  4 ⚖️ 和相似物质比较           （chem.compare）          │
├──────────────────────────────────────────────────────────┤
│ 其他角度  ▸ 展开（3）                                     │ 区 B
├──────────────────────────────────────────────────────────┤
│ 呈现： ● 静态文字  ○ 表格  ○ 交互图表  ○ 原图标注          │ 区 C
├──────────────────────────────────────────────────────────┤
│ ✏️ 或者，告诉我你想要什么：  [想要什么 ▾]                  │ 区 D
│ [____________________________________] [发送]             │
│ 参考风格：[默认 ▾ 英语单词 / 中文术语 / 通用 …]            │
├──────────────────────────────────────────────────────────┤
│ 🔍 搜索此内容 ▾   🌐 用浏览器打开   📋 复制路由配置       │ 区 E
│ 1-9 选择 · ⏎ 提交 · Esc 取消 · Ctrl+1..9 切领域           │
└──────────────────────────────────────────────────────────┘
```

### 2.1 头部（HeaderBar）

| 元素 | 内容 | 规则 |
|---|---|---|
| 模态标签 | `文本` / `图片` / `混合` + 字符数或 `image/png · 1920×1080` | 图片时附缩略图，最大边 160px |
| 预览 | 原文前 80 字，`html.escape`；图片显示 `digest[:8]` | 点击可展开全文；**绝不显示 base64** |
| 来源 | `gnome-extension` / `poll` / `primary` / `screenshot` / `manual` / `bridge` / `image-followup` | 枚举值，不含原文 |
| 时间 | `HH:MM:SS` | 本地时间 |

### 2.2 判定条（DetectionBar）

| 档 | 文案 | 领域 chip | 换领域 |
|---|---|---|---|
| HIGH | `化学 · 高置信` | 显示 1 个 | 隐藏，可从 `▾` 打开 |
| MID | `看起来像物理，不确定？换领域` | 显示 1 个 + 次要候选 | 常显按钮 |
| LOW | `需要你选一个角度` | 平铺全部启用 domain | chip 常显，区 A/B 隐藏 |
| MIXED | `数学 / 物理 都有可能` | 并列 2~3 个 | chip 常显 |

证据 tooltip 显示 `DomainCandidate.evidence` 列表（例如"2 元素符号""检测到 LaTeX 定界符"），
用于解释"为什么判成 X"，不暴露内部特征函数名给普通用户（诊断页才展示）。

### 2.3 区 A 推荐（RecommendedSection）

- 数据：`INTENTS` 中 `domains` 含 Top1 或 `applicable="*"` 且 `enabled` 的项；
- 排序：`order` 升序 → 偏好频次降序；偏好**只影响排序，不覆盖本次明确选择**；
- 数量：`min(menu.recommended_limit, 9)`（数字键只有 1~9，默认 6）；
- 每项显示 `label` + `hint`（悬停 300ms）；带 `⏎默认` 标记的项由
  `menu.default_intent_policy` 决定：`last_used` / `first` / `none`；
- 不适用项按 `menu.show_disabled_items` 处理：`grayed`（置灰 + 原因 tooltip，
  可强制使用需二次确认）/ `hidden` / `hidden_in_fold`。

### 2.4 区 B 其他角度（OtherDomainsSection）

- 默认折叠（`menu.show_other_domains=true` 时才存在）；
- 每个非 Top1 domain 最多 `menu.other_per_domain` 个代表意图（默认 2）；
- label 用**目标语言**（"查英文里的读法"），不出现 `en_word` 这类内部 ID；
- 被 `feature_switches` 关闭的 domain 整体不出现，除非 `show_disabled_items=grayed`。

### 2.5 区 C 呈现（FormatSection）

- 单选按钮组，来自 `IntentOption.supported_formats ∩ settings.formats.enabled`；
- 默认取所选 intent 的 `default_format`；不支持的置灰；
- 切换只改 `UserSelection.format_id`，**不重算 Suggestion、不重排菜单、不产生请求**；
- 出厂 formats：`static` / `table` / `interactive` / `annotate`。

### 2.6 区 D 自由输入（FreeInputSection）

| 控件 | 类型 | 行为 |
|---|---|---|
| 模式下拉 | `QComboBox` | `instruction`（默认）/ `prompt`，见 ADR-2 |
| 输入框 | `QPlainTextEdit`（`prompt` 模式支持多行）/ `QLineEdit` | 上限 `free_input.max_chars` |
| 参考风格 | `QComboBox` | 由 `enabled_route_ids()` 动态生成，禁用路由不出现 |
| 发送 | `QPushButton` | 构造 `UserSelection(intent_id=None)` |

自由输入必须能正常使用中文输入法，这是 ADR-1 独立进程的唯一理由。

### 2.7 区 E 工具（ToolsSection）

| 按钮 | 行为 | 可插拔来源 |
|---|---|---|
| `🔍 搜索此内容 ▾` | 下拉列出启用的 `search_providers`，点击打开 | `search_providers[].contexts` |
| `🌐 用浏览器打开` | 用默认 provider 打开 | 同上 |
| `📋 复制路由配置` | 复制 `UserSelection` 的 JSON（不含原文、不含 Key） | `menu.show_copy_route` |

## 3. 事件表

| 用户动作 | 控件 | 处理 | 状态迁移 | 产生 API 请求 |
|---|---|---|---|---|
| 复制文本/图片 | — | `ClipboardManager._dispatch` → `_start_query` | `IDLE → SUGGESTING → INTENT_MENU` | ❌ |
| 单击推荐项 | `QPushButton` | 设置 `selected_intent`，刷新高亮 | `INTENT_MENU` | ❌ |
| 数字键 `1`~`9` | `QShortcut` | 选中第 N 项并**立即提交** | `→ READY → REQUESTING` | ✅ |
| `Enter` | `QShortcut` | 提交当前高亮 | `→ READY → REQUESTING` | ✅ |
| `↑` / `↓` / `Tab` | `QShortcut` | 高亮 / 焦点移动 | 不变 | ❌ |
| `Ctrl+1`~`Ctrl+9` | `QShortcut` | 切换领域，重建区 A/B | `INTENT_MENU → INTENT_MENU` | ❌ |
| 点"换领域" | `QComboBox` | 同上，写一条"用户改判"统计 | 同上 | ❌ |
| 改呈现形态 | `QRadioButton` | 写 `format_id` | `→ CONFIGURING` | ❌ |
| 自由输入 + 发送 | 输入框 + 按钮 | `UserSelection(intent_id=None)` | `→ READY → REQUESTING` | ✅ |
| `Esc` | `QShortcut` | 关闭 MenuCard、清 marker、发 `CANCELLED` | `→ IDLE` | ❌ |
| 窗口关闭按钮 | — | 同 Esc | `→ IDLE` | ❌ |
| 提交 | — | `generation += 1`，生成 `request_id`，取消在途 | `REQUESTING` | ✅ |
| 结果返回 | — | `normalize → ViewModel → render_text` | `→ RESULT` | — |
| 结果卡片"换一种解释" | 右键菜单新增项 | 用 `current_text` + 原 `asset_id` 重建菜单，**不重新读剪贴板** | `RESULT → INTENT_MENU` | ❌ |
| 结果卡片"重试" | 现有 `Ctrl+R` | 沿用上次 `UserSelection` | `RESULT → REQUESTING` | ✅ |

## 4. 键盘映射与冲突

| 键 | MenuCard | ResultCard（0.1 已有） |
|---|---|---|
| `1`~`9` | 选中并提交第 N 项 | — |
| `Enter` | 提交默认项 | — |
| `↑` `↓` `Tab` | 高亮 / 焦点移动 | — |
| `Esc` | 取消并关闭 | 关闭 |
| `Ctrl+1`~`Ctrl+9` | 切领域 | — |
| `Ctrl+C` | 复制输入框选中文本 | 复制选中内容 / 整卡 |
| `Ctrl+R` | 重新展示建议（不请求） | 重新查询 |
| `F3` | 打开设置 | 打开设置 |
| `Ctrl+P` | 固定菜单窗口 | 固定卡片 |
| `Ctrl+Alt+S` | — | 框选截图 |
| `Ctrl+Alt+Q` | — | 查询当前剪贴板 |

⚠️ 0.1 的 `PopupWindow.bind_shortcuts()` 已绑定 `Ctrl+C / Ctrl+R / Ctrl+P / F3 / Escape`
（`ui/popup.py:234-252`）。MenuCard 若继承它，**必须先 `bind_shortcuts({})` 清空再绑自己的**，
否则 Escape 被两套逻辑同时处理；`Ctrl+C` 在菜单里应恢复为"复制选中文本"而不是"复制整张卡片"。

## 5. 四档 UI 精确差异

| | 判定条 | 区 A | 区 B | 默认高亮 | 领域选择器 | 自动提交 |
|---|---|---|---|---|---|---|
| HIGH | `化学 · 高置信` | 该域意图 | 折叠 | 上次意图 | 隐藏（▾ 可开） | 从不 |
| MID | `看起来像物理，不确定？换领域` | 该域意图 | 折叠 | 上次意图 | 常显 | 从不 |
| LOW | `需要你选一个角度` | **隐藏** | **隐藏** | 无 | 平铺 chip 常显 | 从不 |
| MIXED | `数学 / 物理 都有可能` | Top1 域意图 | **展开**，并列 2~3 域代表意图 | 无 | chip 常显 | 从不 |

LOW 档点领域 → 立即重建区 A/B 进入 `INTENT_MENU`；点"我说不清/通用解释" → 直接以
`general` 提交。**任何档位都不自动提交**，因为自动提交等于退回"程序替用户决定"。

## 6. 卡片内搜索与外部浏览器

### 6.1 搜索词来源

优先级：选中文本 → 当前 `query` 原文 → 卡片 `title`。
截断 `search_max_query_chars`（默认 200），`quote(text, safe='')`。
**不写日志、不写剪贴板**（因此不会触发自我复制回环或被监听当成新查询）。

### 6.2 三种打开方式

| 方式 | 实现 | 配置 |
|---|---|---|
| `default_browser` | `QDesktopServices.openUrl(QUrl(url))`（0.1 现有） | `mode: default_browser` |
| `explicit_browser` | `subprocess.Popen([exe, *args, url])`，exe 走 `shutil.which` 白名单 | `mode / executable / args` |
| `internal` | 独立窗口内嵌（暂缓，与禁 WebEngine 冲突） | — |

### 6.3 卡片内可点击锚点

0.1 的 `QTextBrowser` 显式 `setOpenLinks(False)` / `setOpenExternalLinks(False)`
（`ui/popup.py:108-118`），所以普通 `<a href>` 不会生效。方案：

- `render_text()` 生成自定义 scheme：`<a href="clipsearch://google?q=...">搜索</a>`；
- `setOpenLinks(True)`，但**拦截 `anchorClicked` 信号且不调用 `setSource()`**，
  这样不会触发 QTextBrowser 自身导航，也不会走 `openExternalLinks`；
- 信号带出 `(provider_id, query)`，交给 `_search_selection()`；
- 这比塞一排 QWidget 按钮更能保住"卡片骨架与 prompt 隔离"的不变量。

### 6.4 右键菜单

`search_action` 改为动态子菜单，遍历 `enabled_providers_for(current_route_id, modality)`：

```
搜索「NaCl」▸  Google
              剑桥词典
              Google Scholar
              ─────────
              管理搜索引擎…（打开设置页）
复制「NaCl」
─────────
固定窗口 / 重新查询 / 强制刷新 / 复制结果
换一种解释（0.2 新增）
...
```

provider 的 `contexts` 决定它在哪些 `route_id` / 模态下出现。

## 7. 边界情况

| 场景 | 行为 |
|---|---|
| 菜单未选时连续复制两段内容 | 新 `envelope_id` 覆盖旧菜单，`generation` 不变（还没请求），旧进程若存在先发 `CANCELLED` 再拉起新菜单 |
| 菜单显示期间用户在别处复制 | 同上；不排队、不叠加窗口 |
| 提交后立刻按 Esc | 取消在途请求（`cancel.set()`），`generation` 已递增，过期结果按 `app.py:201-203` 丢弃 |
| 菜单窗口失焦 | **不自动关闭**（用户可能去别处查资料） |
| 菜单超时 | 默认不超时；`menu.timeout_seconds=0` 表示常驻，>0 时到点 `CANCELLED` |
| `pin` 状态下新查询 | 0.1 行为：新结果覆盖旧卡片；菜单同样覆盖，不因为 pin 阻止 |
| 图片 + 文本同窗口 | 由 `ModalityArbiter` 按 `image_text_conflict` 决定，见 `02_INPUT_POLICY_SPEC.md` |
| 每日限额耗尽 | 菜单可显示但置灰提交按钮，并提示"今日请求已达上限" |
| 配置热重载 | 菜单窗口收到新 `MenuModel` 则原地刷新，不重开进程 |
| 子进程崩溃 | 主进程检测到 socket 断开 → 清 marker → 回 `IDLE`，写一条脱敏诊断，不弹错误卡片 |
