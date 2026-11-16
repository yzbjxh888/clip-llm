# 0.2 迁移计划

## 0. 当前进度（2026-11-06）

`src/clipllm02/desktop/` 已交付一个**可前台运行的 PySide6 桌面版**（不注册 systemd），
覆盖下面的 P1 菜单骨架、P2 选择驱动请求、P4 设置可插拔中的主要交互，
以及部分 P7 回归。逐项能力对照与剩余差异见 `docs/08_DESKTOP.md`。

已实现：托盘、剪贴板轮询与去重/自我复制抑制、五区意图菜单、
四档领域与换领域、自由输入双模式、只读结果卡片与右键菜单、
多搜索源跳转、设置草稿事务与热重载、可选真实模型调用、结果缓存、
`generation` 防乱序、拦截零弹窗。

未实现：GNOME 扩展事件通道、PRIMARY 划词、Portal 截图与视觉意图、
交互图表窗口、全局快捷键、窗口边缘缩放、独立设置进程的输入法隔离、
每日限额与拦截日志落盘、SQLite 缓存。

## 1. 原则

- **加一层，不改一层**：`auto` 触发策略下 0.1 全部行为与 77 个回归用例逐字不变；
- 0.1 源码目录 `0.1 旧项目目录（仓库外）` 保持只读；
- 每个阶段结束都能通过 `trigger_policy=auto` 一键回滚；
- 先让插件架构成立，再让插件数量增长（不要同时迁移与新增几十个领域）。

## 1. 文件改动清单

| 文件（相对 `src/clipllm/`） | 状态 | 改动 |
|---|---|---|
| `core/models.py` | 改 | 新增 `InputEnvelope` `AssetRef` `FeatureSet` `DomainCandidate` `Suggestion` `PolicyResult` `IntentOption` `MenuModel` `FreeInputSpec` `UserSelection` `SearchProvider` `InteractiveArtifact`；`Settings` 增加 0.2 字段 |
| `core/features.py` | 改 | 导出 `feature_catalog()`：名称 → 说明 → 判别式，供设置页只读展示 |
| `core/policy.py` | **新** | `preflight(envelope, settings) -> PolicyResult`，四档分级，独立于 `Classifier` |
| `core/modality.py` | **新** | `ModalityArbiter`（300ms 窗口）+ `AssetStore` |
| `core/suggest.py` | **新** | `DomainResolver` 非短路候选 + `build_menu()` |
| `core/prompts.py` | 改 | 新增 `compose_prompt()`（主路由 + includes + 环检测 + 预算），保留 `build_prompt()` |
| `core/pipeline.py` | 改 | `suggest()`、`run(selection=...)`、缓存键增 `intent_id/format_id/composition_hash` |
| `core/cache.py` | 改 | `make_key()` 支持新参数，旧签名兼容 |
| `core/settings.py` | 改 | 新字段默认值合并、校验、`_structure_signature()` 纳入新配置 |
| `core/image.py` | 改 | 支持视觉意图 prompt、原图 asset 保留、webP 文件头校验 |
| `clipboard/manager.py` | 改 | `trigger_policy=menu` 分岔；`_handle/_handle_image` 接入 arbiter |
| `clipboard/providers.py` | 改 | 事件带出 modality 线索；`ImagePollProvider` 在 menu 模式下不启动 |
| `ui/menu_window.py` | **新** | 独立进程 MenuCard，五区布局、事件表、socket 回传 |
| `ui/ipc.py` | **新** | Unix socket 协议：`MenuModel` 下行 / `UserSelection` 上行 |
| `ui/popup.py` | 改 | 搜索子菜单/锚点、"换一种解释"；MenuCard 继承时清空旧快捷键 |
| `ui/app.py` | 改 | `_start_query` 分岔、`_start_query_with_selection`、多 provider 搜索、asset 传递 |
| `ui/settings_window.py` | 改 | 3 个新页签 + 4 个改造页签 |
| `ui/chart_window.py` | **新** | Qt 原生交互图窗口（P5） |
| `resources/builtin.json` | 改 | 新增 `chem` / `image` 域路由与 `builtin_intents/policy/providers` |
| `docs/interaction.md` | 改 | 补 0.2 状态、IPC、菜单窗口 |
| `tests/` | 改 | 见第 3 节 |

## 2. 分阶段

### P0 冻结基线（不改代码）

- 跑 `PYTHONPATH=src python3 -m unittest discover -s tests -v`（77 用例）
  与 `python3 -m clipllm routes test`（135 用例），记录结果；
- 产出当前控制流/数据流/状态机与已知 Bug 清单；
- 建立功能开关 `trigger_policy` 的默认值快照，作为回滚基线。

**验收**：全绿；有可复现的基线日志。

### P1 菜单骨架（零 token）

- `trigger_policy=menu` 分支；`Suggestion` / `MenuModel` / `UserSelection` 数据对象；
- `MenuWindow` 独立进程 + IPC；五区静态 UI（区 A/B 先硬编码少量项验证交互）；
- `popup-visible`、Esc、`generation` 与子进程崩溃处理。

**验收**：复制 `NaCl` 弹菜单，抓包确认无任何 API 请求；
`auto` 模式 77 用例全绿；连续复制 10 次无窗口叠加。

### P2 选择驱动请求

- `UserSelection` → `Pipeline.run(selection=...)`；`compose_prompt()`；
- 缓存键扩展；`PreferenceStore`；默认高亮与数字键；
- "换一种解释"从结果卡片回菜单。

**验收**：同内容换意图得到不同结果且不串缓存；
`request_id` 防乱序覆盖；改 `includes` 立即失效。

### P3 领域四档与策略分级

- `DomainResolver` 非短路候选；HIGH/MID/LOW/MIXED 四档 UI；
- `PolicyEngine` 四档；`feature_catalog()`；长度守卫默认降为 WARN；
- 自由输入双模式（`instruction` / `prompt`）。

**验收**：测试集统计四档占比与用户改判率；
URL 从 BLOCK 改 WARN 后立即生效；`break the ice.` 不再被拦死。

### P4 设置可插拔

- 3 个新页签；`intents` / `search_providers` / `policy` 配置段；
- 校验、`_structure_signature()`、热重载；
- "关闭英语查询""关闭词组查询"两条端到端用例；
- 卡片内搜索锚点 + 多 provider 右键子菜单。

**验收**：`docs/04_SETTINGS_SPEC.md` 第 7 节全部通过；
新增 intent / provider 不改任何 Python 业务代码。

### P5 交互呈现

- Chart Spec 协议 + 白名单校验器 + Qt 原生图表窗口 + 静态降级；
- 受限表达式解析器（禁 `eval`）、点数与耗时上限。

**验收**：Logistic Map 与普通函数两个用例；非法 Spec 安全降级；
输入法回归测试不退化；日志无 `textinput ... Trying to disable`。

### P6 图片一等输入

- `ModalityArbiter` + `AssetStore`；视觉意图菜单；
- 保留一键 OCR 快捷预设；`image_text_conflict` 四策略；
- `menu` 模式强制禁用 `image_auto_recognize`。

**验收**：同一截图可选"提取公式"与"原图标注"两条不同路径；
写回 LaTeX 不产生二次查询；登录后不上传旧图。

### P7 回归与文档

- 全量测试；更新 `docs/interaction.md`；补 Intent Contract / Renderer Spec；
- 性能：菜单出现 < 200ms（本地规则 + 子进程启动）；
- 隐私审计：日志/拦截记录/诊断文本不含原文、URL 全文、Key、内容 hash。

## 3. 测试清单

| 类别 | 用例 |
|---|---|
| Plugin Contract | 新增 intent / domain / format / provider 不改核心代码 |
| Policy | 17 条拦截各级别、BLOCK 优先短路、evidence 完整、脱敏记录字段 |
| Feature | 全部 `FEATURES` 特征值表驱动；中英混排、LaTeX、化学式、代码块 |
| Domain | HIGH/MID/LOW/MIXED 四档阈值、MIXED 并列、LOW 不硬判 |
| Menu | 推荐区全量显示（ADR-7）、置灰规则、默认高亮、偏好只影响排序、四档 UI 差异 |
| IPC | MenuModel 序列化、UserSelection 回传、子进程崩溃、配置热重载刷新 |
| Prompt | 组合顺序、环检测、section 缺失降级、预算截断、契约 append-only |
| Cache | 意图/格式/includes/自定义指令各不串用；错误不缓存；模板变更不失效 |
| Search | provider 上下文过滤、URL 编码、超长截断、explicit_browser 白名单 |
| Settings | 草稿取消、数据层拒绝、热重载、旧配置兼容、`hide_from_menu` |
| Image | MIME/文件头/大小/digest/`_primed`、冲突四策略、回环抑制 |
| State | 连续复制、Esc、重试、取消、并行请求、popup-visible |
| E2E | 复制 → 菜单 → 选择 → 请求 → 渲染全链路（Gateway 用 mock） |
| Regression | 0.1 的 77 + 135 用例全绿，`auto` 模式行为不变 |

## 4. 风险与回滚

| 风险 | 缓解 | 回滚 |
|---|---|---|
| 独立子进程引入输入法/焦点问题 | 复用 0.1 设置窗口方案；offscreen GUI 测试 | `trigger_policy=auto` |
| `QTextBrowser` 无法内嵌交互控件 | 交互图放独立 Qt 原生窗口 | 关 `formats.interactive` |
| 提示词组合导致 schema 串字段 | 唯一 `query_type`、append-only 契约、`normalize()` 兜底 | `includes=[]` |
| 长度守卫降级改变 135 用例基线 | P3 单独阶段、功能开关、保留旧断言在 `auto` 模式 | `policy.*.level=BLOCK` |
| 图片与文本双触发 | `ModalityArbiter` 合并窗口 + digest 去重 | `image_text_conflict=text_only` |
| 配置热重载遗漏新字段 | 全部纳入 `_structure_signature()`；单测逐字段 | 重启服务 |
| 缓存键变长导致旧缓存全失效 | 一次性冷启动可接受，文档注明 | 保留旧键兼容读取 |

## 5. 不在 0.2 范围内

- 不重写 `QTextBrowser`，不重新引入 `QWebEngineView`；
- 不做 D-Bus 事件通道（继续用事件文件）；
- 不做流式输出、提示词 A/B 对比、独立历史窗口；
- 不维护 Chrome 插件的第二套识别逻辑（插件降级为可选网页图片输入桥）；
- 不删除 0.1 任何成熟能力（分类器规则、9 套 Prompt、`normalize()`、缓存、`generation`）。
