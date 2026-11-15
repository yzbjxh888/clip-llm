# 0.2 目标架构

## 1. 五个维度（唯一命名）

| 维度 | 回答什么 | 0.2 模块 | 0.1 对应 | 权威 |
|---|---|---|---|---|
| **M 输入模态** | 给的是文本/图片/混合 | `modality.py` | `clipboard/providers.py`、`core/image.py` | 用户操作 |
| **F 内容特征** | 语言/粒度/符号/结构 | `features.py` | `core/features.py` 的 `FEATURES` | 本地规则 |
| **D 领域** | 大概属于哪个知识领域 | `suggest.py::DomainResolver` | 混在 `rules.json` 的 `route_id` 里 | 只给候选，**非权威** |
| **I 解释意图** | 想看它的哪一面 | `intents` 配置 + `prompts.py` | 等价于 `route_id`，与内容类型绑死 | **用户**，权威 |
| **R 呈现形态** | 结果长什么样 | `formats` 配置 + `render_text` / 独立窗口 | 只有静态一种 | 用户，可默认 |

铁律：**F 与 D 绝不选 Prompt；D 只排菜单顺序；I 才决定 Prompt；R 决定渲染器。**

## 2. 分层与进程边界

```mermaid
flowchart TB
  subgraph SRC["输入源"]
    A1["GNOME 扩展事件文件"] 
    A2["xclip 轮询兜底"]
    A3["PRIMARY 划词"]
    A4["图片 MIME 轮询"]
    A5["XDG Portal 截图"]
    A6["手动 / 命令通道"]
  end
  subgraph MAIN["常驻守护进程（禁用 Qt text-input）"]
    AR["ModalityArbiter<br/>300ms 合并 + 回环抑制"]
    PL["Policy Engine<br/>BLOCK / WARN / CONFIRM / ALLOW"]
    SG["Feature 提取 + DomainResolver<br/>Suggestion（零 token）"]
    MB["build_menu → MenuModel"]
    OR["Orchestrator<br/>generation / request_id / cancel"]
    PB["compose_prompt"]
    GW["LLM Gateway（文本 / 视觉）"]
    NM["normalize → ViewModel"]
    RC["render_text → ResultCard"]
    IW["InteractiveWindow（Qt 原生）"]
    CM["Cache / Preference / InterceptLog"]
  end
  subgraph SUB["独立子进程（保留输入法）"]
    MW["MenuWindow：五区 + 自由输入框"]
    ST["SettingsWindow（0.1 已有）"]
  end
  A1 --> AR
  A2 --> AR
  A3 --> AR
  A4 --> AR
  A5 --> AR
  A6 --> AR
  AR --> PL
  PL --> SG
  SG --> MB
  MB -->|"Unix socket"| MW
  MW -->|"UserSelection / CANCELLED"| OR
  OR --> PB --> GW --> NM --> RC
  NM -.->|"format=interactive"| IW
  OR <--> CM
  ST -->|"reload"| OR
```

## 3. 控制流

```mermaid
flowchart TD
  A["on_clipboard_change"] --> B["ModalityArbiter.arbitrate"]
  B --> C["InputEnvelope"]
  C --> D{"preflight"}
  D -->|BLOCK| X["脱敏拦截记录 · 零窗口"]
  D -->|CONFIRM| W["授权确认"]
  W -->|取消| X
  W -->|同意| E
  D -->|WARN / ALLOW| E["extract_features 本地"]
  E --> F["DomainResolver 非短路候选"]
  F --> G{"level"}
  G -->|HIGH| H["build_menu 推荐区"]
  G -->|MID| H
  G -->|MIXED| J["build_menu 多域并列"]
  G -->|LOW| I["build_menu 领域选择器"]
  I --> H
  J --> H
  H --> K["等待 UserSelection"]
  K -->|Esc / 关闭| Z["CANCELLED"]
  K -->|提交| L["generation++ · request_id · 取消在途"]
  L --> M["compose_prompt"]
  M --> N{"cache hit?"}
  N -->|是| P["加载 raw dict"]
  N -->|否| Q["LLM Gateway"]
  Q --> R["normalize 结构校验"]
  P --> S["Renderer Dispatcher"]
  R --> S
  S --> T["ResultCard / InteractiveWindow"]
  T --> U["保存偏好与结果缓存"]
```

关键点：

1. **菜单阶段零模型请求**：`F` 与 `D` 全是本地规则；只有 `UserSelection` 提交后
   才允许进入 `compose_prompt` → `GW`。
2. **只有一条查询入口**：所有来源最终走 `ClipboardManager._dispatch()` →
   `on_query` → `DesktopApp._start_query()`，图片续接不另开入口
   （0.1 `docs/interaction.md` 的不变量）。
3. **`generation` 在提交时递增**，菜单阶段不递增；过期结果直接丢弃。
4. **模型输出与呈现分离**：模型出内容或声明式 Spec，本地 Renderer 决定怎么展示。

## 4. 数据流

```mermaid
flowchart LR
  T["剪贴板文本 / 图片"] --> E["InputEnvelope<br/>raw_text + assets + derived"]
  E --> P["PolicyResult<br/>level / reason / evidence"]
  P --> S["Suggestion<br/>features + domain_candidates + level"]
  S --> M["MenuModel<br/>recommended / others / formats / tools"]
  M --> U["UserSelection<br/>intent_id / format_id / custom_instruction"]
  U --> R["RequestEnvelope<br/>composed_prompt + schema + assets"]
  R --> K["cache key"]
  K --> G["LLM Gateway"]
  G --> V["RawResponse"]
  V --> N["ViewModel"]
  N --> RT["render_text → ResultCard"]
  N -.-> IR["InteractiveArtifact → InteractiveWindow"]
  U -.-> PR["PreferenceStore"]
  P -.-> LG["InterceptLog（脱敏）"]
```

缓存键：

```text
key = sha256(
    sha256(composed_prompt)
  + composition_hash          # sorted(includes)
  + model + model_params
  + route_id + intent_id
  + format_id + schema_version
  + custom_instruction + style_ref
  + ruleset_version
  + query_hash                 # 文本 hash 或 image_digest
)
```

三条不变量（0.1 已有，0.2 继承）：

- 分类/领域决策**不缓存**；
- 只缓存解析后的 raw 结构化数据，**不缓存 HTML**；
- 错误结果不缓存；强制刷新绕过缓存。

偏好单独存轻量表，TTL 与结果缓存解耦——改提示词时不应把用户习惯也清掉。

## 5. 状态机

```mermaid
stateDiagram-v2
  [*] --> Idle
  Idle --> Silent: Preflight BLOCK（脱敏记录，零窗口）
  Silent --> Idle
  Idle --> Confirming: Preflight CONFIRM
  Confirming --> Idle: 拒绝
  Confirming --> Suggesting: 同意
  Idle --> Suggesting: 新输入，本地 F+D 分析（零 token）
  Suggesting --> DomainPicker: LOW
  Suggesting --> IntentMenu: HIGH / MID / MIXED
  DomainPicker --> IntentMenu: 用户选领域
  IntentMenu --> Configuring: 选意图 / 自由输入
  Configuring --> Ready: 确认呈现形态
  Ready --> Requesting: 提交，generation++ / request_id
  IntentMenu --> Idle: Esc / 取消（清 popup-visible）
  Configuring --> Idle: Esc
  Requesting --> Validating: 模型返回
  Requesting --> Error: 超时 / 取消 / 5xx
  Validating --> Rendering: 校验通过
  Validating --> Rendering: 校验失败 → 静态降级
  Rendering --> Result
  Error --> Ready: 重试
  Result --> IntentMenu: 换一种解释
  Result --> Configuring: 改呈现形态
  Result --> Idle: 关闭
  Idle --> ImageLoading: 截图 / 图片输入
  ImageLoading --> IntentMenu: 原图保留 + 视觉意图菜单
  ImageLoading --> FollowupLoading: 一键 OCR 预设（保留旧快捷路径）
  FollowupLoading --> Result: LaTeX 续接，沿用上次意图
```

`popup-visible` 只在确实展示了菜单/卡片/loading 时置位；拦截、静默、关闭后必须清除。
图片识别的中间状态允许单独取消、重新处理，不能和模型请求状态混在一起。

## 6. 模块契约表

| 模块 | 输入 | 输出 | 是否允许调模型 |
|---|---|---|---|
| `ModalityArbiter` | TextEvent / ImageEvent | `InputEnvelope` | ❌ |
| `PolicyEngine` | `InputEnvelope` + settings | `PolicyResult` | ❌ |
| `FeatureExtractor` | `InputEnvelope` | `FeatureSet` | ❌ |
| `DomainResolver` | `FeatureSet` + rules | `Suggestion` | ❌ |
| `MenuBuilder` | `Suggestion` + intents + prefs | `MenuModel` | ❌ |
| `MenuWindow` | `MenuModel` | `UserSelection` / `CANCELLED` | ❌ |
| `PromptComposer` | `UserSelection` + RoutePacks | `RequestEnvelope` | ❌ |
| `LLMGateway` | `RequestEnvelope` | `RawResponse` | ✅ 唯一 |
| `Validator` | `RawResponse` | `ViewModel` / `InteractiveArtifact` | ❌ |
| `RendererDispatcher` | `ViewModel` | 可见内容 | ❌ |
| `CacheManager` | key / raw dict | raw dict | ❌ |
| `PreferenceStore` | 用户选择 | 排序权重 | ❌ |

**唯一允许调用模型的模块是 `LLMGateway`**。插件不得自行决定请求发送、缓存写入
和界面状态，否则可插拔会退化成难以控制的耦合。

## 7. 与 0.1 的关系

| 0.1 能力 | 0.2 处理 |
|---|---|
| `RouteRegistry` / `routes.d/` 五文件 | 原样保留，新增 `kind` / includes 引用 |
| `FEATURES` 具名特征注册表 | 原样保留，导出 `feature_catalog()` 供设置页 |
| `rules.json` 声明式条件树 | 原样保留，`DomainResolver` 以非短路方式复用 `evaluate_condition` |
| `Decision(action, route_id)` | 原样保留（`auto` 模式），`menu` 模式由 `UserSelection` 覆盖 |
| `ViewModel` 唯一渲染契约 | 原样保留，交互 Spec 走旁路不替换 `render_text` |
| `ResultCache.make_key` | 扩展字段，旧键格式不变 |
| `generation` / 取消 / 拦截静默 | 原样保留 |
| Wayland 剪贴板三层 Provider | 原样保留，前面插入 `ModalityArbiter` |
| 图片 → LaTeX → 续接 | 保留为快捷预设，另加一等图片意图 |
| 9 条 `SYSTEM_PROMPT_*` | 原样保留为 `composite` 预设包 |
| 77 个测试 + 135 条分类用例 | 全部保留为回归基线 |
