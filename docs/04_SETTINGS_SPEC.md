# 0.2 设置与可插拔规格

## 1. 先消歧："关闭英语查询"关的是什么

0.1 有四个可关的层，不定义清楚会得到一堆互相矛盾的开关：

| 层 | 实体 | 0.1 位置 | 关闭语义 |
|---|---|---|---|
| L1 领域 | `domain=en` | 不存在 | 英语相关意图整体不出现在菜单 |
| L2 路由/内容类型 | `en_word`、`en_phrase` | `routes.d/*/route.json` 的 `enabled` | 0.1 是 `block`（静默拦截、不弹窗） |
| L3 意图 | `read` / `define` / … | 不存在 | 选项从菜单移除或置灰 |
| L4 规则 | `rules.json` 的 `english_word` | `cb_rule_enabled` | 该规则不参与求值，落到下一条 |

0.1 设置页已有 L2（`cb_pack_enabled`）与 L4（`cb_rule_enabled`）开关，
但缺 L1/L3，且 `disabled_route_action` 默认 `block` 会让"关闭词组查询"
变成"什么都看不到"。0.2 用统一的三态收敛：

| `disabled_route_action` | 行为 |
|---|---|
| `block` | 静默拦截，写脱敏记录（0.1 行为，`auto` 模式默认） |
| `fallback` | 退回 `general` 路由（0.1 已支持） |
| `hide_from_menu` | 不出现在菜单、参考风格下拉、其他角度；规则仍在但 `Policy Engine` 前置阻止请求（`menu` 模式默认，ADR-3） |

已有缓存**一律保留不删**，重新启用后立刻命中。

## 2. 配置数据模型

新增/变更字段（`Settings` 完整 schema 见 `spec/settings.schema.json`）：

```json
{
  "trigger_policy": "menu",
  "disabled_route_action": "hide_from_menu",
  "menu": {
    "enabled": true,
    "show_other_domains": true,
    "show_disabled_items": "grayed",
    "recommended_limit": 6,
    "other_per_domain": 2,
    "remember_last_intent": true,
    "default_intent_policy": "last_used",
    "auto_submit": false,
    "free_input_enabled": true,
    "format_toggle_enabled": true,
    "search_buttons_enabled": true,
    "show_copy_route": true,
    "timeout_seconds": 0
  },
  "free_input": {
    "mode": "instruction",
    "max_chars": 2000,
    "max_prompt_chars": 4000
  },
  "feature_switches": {
    "en_query": true,
    "zh_query": true,
    "latex_query": true,
    "chem_query": true,
    "code_query": true,
    "image_intents": true
  },
  "detection": {
    "high": 0.80, "mid": 0.50, "gap": 0.15,
    "mixed_min_score": 0.80, "max_candidates": 3,
    "score_mode": "heuristic"
  },
  "policy": { "...": "见 spec/builtin_policy.json" },
  "intents": { "...": "见 spec/builtin_intents.json" },
  "search_providers": { "...": "见 spec/builtin_search_providers.json" },
  "formats": {
    "enabled": ["static", "table", "interactive", "annotate"],
    "default": "static"
  },
  "image": {
    "quick_preset_enabled": true,
    "asset_ttl_days": 7,
    "auto_recognize_in_menu": false
  },
  "image_text_conflict": "image_only",
  "modality_window_ms": 300,
  "search_default_provider": "google",
  "search_show_inline_buttons": true,
  "search_max_query_chars": 200
}
```

### 2.1 `feature_switches` 与 `intents[].enabled` 的关系

- `feature_switches.<domain>_query=false` 是**领域级总闸**：该 domain 从
  `domain_chips` 消失，其下所有 intent 视为 disabled；
- `intents[].enabled=false` 是**单意图开关**：只影响这一项；
- `RoutePack.enabled=false` 是**内容类型开关**：影响它作为 `primary_route`
  或被 `includes` 引用的所有意图；
- 三者是 AND 关系，命中任意一个就按 `show_disabled_items` 处理；
- 诊断页分别记录三类命中次数，便于判断"用户到底关了哪一层"。

## 3. 两个例子的精确行为链

### 3.1 关闭"英语查询"

1. `feature_switches.en_query = false` → `domain=en` 从 `domain_chips` 消失；
2. 所有 `domains` 含 `en` 的 intent 标记 disabled；`show_disabled_items=grayed`
   时置灰显示"已关闭英语查询"；
3. `en_word` / `en_phrase` 按 `hide_from_menu` 不出现在"参考风格"下拉和"其他角度"；
4. `rules.json` 中 `english_word` / `english_phrase` 规则保留用于诊断，
   但 `Policy Engine` 前置过滤使它们不产生请求；
5. 已有缓存保留；重新开启后立刻命中；
6. `NaCl` 仍可查化学，中文/公式/图片不受影响；
7. 诊断页记 `user_disabled_domain=en`，**不含原文**。

### 3.2 关闭"词组查询"

只把绑定 `en_phrase` 的 intent 置为 disabled + `RoutePack("en_phrase").enabled=false`；
`en_word` 保持正常，`feature_switches.en_query` 仍为 `true`。
`break the ice` 不再走 `en_phrase`，单词查询照常。

## 4. 设置页 UI 规格

0.1 有 8 个页签（`ui/settings_window.py:48-55`）。0.2 新增 3 个、改造 4 个：

| 页签 | 状态 | 内容 |
|---|---|---|
| 通用 | 改 | 「触发策略」下拉增加 `menu`；菜单总开关（自动提交、记忆上次意图、显示已禁用项、推荐条数、超时） |
| **意图菜单** | **新增** | 左 `QTreeWidget` 按 domain 分组列出全部 intent；右 `QFormLayout`：启用勾选、显示名、hint、绑定主路由（下拉来自 `routes.d`）、includes 编辑表（增删 route+section）、默认呈现、支持呈现、排序、快捷键、适用模态、token 预算。勾选框联动"提示词不可编辑"，复用 0.1 `_sync_prompt_lock()` |
| **搜索与跳转** | **新增** | provider 表格（启用 / 名称 / URL 模板 / 模式 / 浏览器 / 参数 / 顺序 / 适用上下文多选）；默认 provider 下拉；卡片内搜索按钮开关；查询词最大长度；「测试打开」按钮 |
| **输入与拦截** | **新增** | 分级策略表：每条拦截一行，级别下拉 `BLOCK/WARN/CONFIRM/ALLOW` + 只读判别特征说明（来自 `feature_catalog()`）+ 阈值输入；图片与文本冲突策略；菜单模式下禁用自动识别；AssetStore TTL；旧 `block_url/email/path/...` 五个开关迁移进来并标注"旧字段兼容" |
| 分类路由与提示词 | 改 | `cb_pack_enabled` 旁增加"禁用后行为"下拉 `block/fallback/hide_from_menu`；显示该路由被哪些 intent 引用 |
| 分类规则 | 改 | 规则表增加"级别"列与"所属 domain"列；`english_sentence` 规则加提示"菜单模式下建议关闭" |
| 图片识别 | 改 | 增加"图片意图"列表（启用/排序）、"原图保留"开关、视觉意图提示词模板编辑 |
| 诊断 | 改 | 增加"用户改判领域次数""默认意图命中率""各意图点击频次""各拦截级别命中数"四组统计，全部脱敏 |

**设置窗口继续在独立进程运行**（0.1 已如此），保存后通过本地 socket 发 `reload`
让主进程热重载配置。所有控件只改草稿副本，取消整体丢弃；确定/应用才校验、
原子写盘、热重载（`SettingsController.apply()` 现有机制）。

## 5. 校验规则

`SettingsController.validate()`（`core/settings.py:240-277`）新增：

| 校验 | 失败页签 |
|---|---|
| `search_providers[].url_template` 必须 http(s) 且含 `{q}`（沿用 0.1 `search_url_template` 校验） | search |
| `explicit_browser` 的 `executable` 必须 `shutil.which()` 可解析 | search |
| intent 的 `primary_route` / `includes[].route_id` 必须**存在**；被禁用不是错误，运行时按 `disabled_route_action` 置灰或回退 | intents |
| `includes[].sections` 必须在目标 schema 的 `fields` 中 | intents |
| `includes` 无环（拓扑排序） | intents |
| `token_budget ≤ profile.max_tokens` | intents |
| `policy[].level ∈ {BLOCK,WARN,CONFIRM,ALLOW}`；`empty`/`too_long`/`daily_limit` 不可下调 | policy |
| `menu.recommended_limit ∈ [1,12]`（前 9 项带数字键，其余可点击） | general |
| `trigger_policy=menu` 时 `image_auto_recognize` 必须为 false | image |
| 规则/路由引用的特征名必须在 `FEATURES` 中（0.1 已有） | rules |
| 未启用的路由/意图不允许修改其提示词（0.1 已有，数据层再拒一次） | intents |

每个 `ValidationError` 带 `tab` 字段，保存失败自动跳到对应页签并高亮。

## 6. 热重载

现有链路：`apply()` → `_structure_signature()` 变化 → `ruleset_version++` →
`Pipeline._on_reload()` 重建 classifier/cache 参数 → `ClipboardManager._on_reload()`
重跑 `_sync()`。

0.2 必须把 `intents` / `search_providers` / `policy` / `menu` / `feature_switches`
纳入 `_structure_signature()`，否则会出现"改了设置不生效"或"旧缓存不失效"。
菜单窗口收到新 `MenuModel` 时原地刷新，不重开进程。

**不缓存的东西**：分类/领域决策、菜单模型、偏好之外的用户选择。
**只缓存 raw 结构化数据，不缓存 HTML**（0.1 不变量）。

## 7. 可插拔验收清单

| 操作 | 期望 |
|---|---|
| 关闭"英语查询" | 菜单无英语项、参考风格下拉无 `en_word/en_phrase`、`NaCl` 仍可查化学、无任何英语请求 |
| 关闭"词组查询" | `break the ice` 不再走 `en_phrase`，单词查询照常 |
| 新增一条 intent（只改配置） | 出现在菜单、可提交、不改 `app.py` / `pipeline.py` |
| 新增一个搜索 provider | 出现在右键子菜单与卡片锚点、不改 `popup.py` |
| 把 `url` 从 BLOCK 改 WARN | 复制 URL 弹菜单并显示警告条，用户可选"总结网页" |
| 禁用某 `RoutePack` | 按 `hide_from_menu` 从菜单与下拉消失，旧缓存保留 |
| 改 intent 的 `includes` | 缓存立即失效（`composition_hash` 变化） |
| 菜单模式下开 `image_auto_recognize` | 设置页直接拒绝并给提示 |
| 新增一个 domain | 不改 UI 核心代码，chip 自动出现 |
| 新增一个 format | 不改 Intent 与 Gateway，只加渲染器 |
| 任意配置改动 | 草稿取消整体丢弃；确定后热重载、不重启服务 |

## 8. 兼容性

- 旧 `settings.json` 缺新字段 → 默认值合并加载（0.1 `hotkeys` 已用此策略）；
- 旧 `route.json` 无 `kind` → 视为 `kind="composite"`，行为不变；
- 旧 `search_url_template` → 自动转成 `search_providers[0]`；
- 旧 `block_url` / `block_email` / `block_path` / `block_secret_hint` 布尔值
  → 映射为对应 `policy[].level`；
- `trigger_policy=auto` 时全部新逻辑旁路，0.1 的 77 个回归用例必须逐字通过。
