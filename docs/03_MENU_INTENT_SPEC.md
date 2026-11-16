# 0.2 菜单、意图与提示词组合规格

> **实现更新（2026-11-06）**：
> - 每条意图可带 `prompt_override`（该查询类型专属提示词），`compose_prompt()`
>   优先使用它，只有未填写时才回退到主路由的 `prompt.md`；
> - 推荐区**只放当前领域专属意图**，`"*"` 通用兜底仅在该领域无可用专属意图时出现；
> - LOW 档**自动出菜单**（通用兜底 + 各领域代表意图 + 领域切换 chip），
>   不再强制用户先选领域；
> - 出厂意图共 41 条，见 `spec/builtin_intents.json`；推荐区全量显示
>   （ADR-7，不截断），数字键只映射前 9 项。
> - 设置 →「意图库」可开关、改名、改提示词、改顺序、增删自定义意图。
> 详见 `docs/08_DESKTOP.md` 第 7 节。

## 1. 三个概念必须分开

| 概念 | 定义 | 0.1 对应 | 谁可见 |
|---|---|---|---|
| **RoutePack** | 一条路由 = `prompt.md` + `schema.json` + `template.j2` + `cases.json`，是**内容类型预设** | `routes.d/<id>/`，9 条 | 设置页 |
| **Domain** | 知识领域候选，只影响菜单排序 | 0.1 混在 `rules.json` 的 `route_id` 里 | 判定条 chip |
| **Intent** | 用户可见的"我想看什么"，绑定主路由 + 可选片段 + 呈现形态 | 0.1 不存在 | 菜单 |

⚠️ 0.1 的 9 条 `RoutePack` **不是 9 个意图**。例如 `SYSTEM_PROMPT_EN_WORD`
一条提示词里同时打包音标、释义、词源、搭配、例句、记忆钩子，实际是 6 个意图
被塞进一个 schema。迁移期把它们当作 `kind="composite"` 的**默认预设包**，
逐步拆细，不直接改名为意图。

## 2. 菜单四区与数据来源

| 区 | 数据来源 | 显示规则 |
|---|---|---|
| A 推荐 | 当前类别下全部启用的查询方式 | 按 `order` + 偏好频次排序，全量显示（ADR-7，不截断） |
| B 其他角度 | 其他 domain 的代表意图（每域 ≤ `other_per_domain`）+ 被禁用项 | 默认折叠；置灰而非隐藏 |
| C 呈现 | `IntentOption.supported_formats ∩ formats.enabled` | 单选，默认 `default_format` |
| D 自由输入 | 运行时 | `instruction` / `prompt` 双模式 + 参考风格下拉 |
| E 工具 | `search_providers` | 下拉 + 按钮 |

### 2.1 生成算法

```python
def build_menu(s: Suggestion, settings: Settings, prefs: Preferences) -> MenuModel:
    domains = rank_domains(s.domain_candidates, prefs)   # 偏好只影响排序
    intents = [i for i in settings.intents
               if i.enabled and s.modality in (i.modalities or ("*",))]

    recommended = sorted(
        [i for i in intents if i.visible_in(domains[0] if domains else None)
         or i.applicable == "*"],
        key=lambda i: (i.order, -prefs.frequency(i.id, domains[0] if domains else None))
    )

    others = group_by_domain(
        [i for i in intents if i not in recommended],
        domains[1:], per_domain=settings.menu.other_per_domain
    )

    grayed = [i.as_disabled(reason) for i in settings.intents
              if not i.enabled or i.domain in disabled_domains(settings)]

    return MenuModel(
        envelope_id=s.envelope_id,
        level=s.level,
        domain_chips=domains,
        warnings=s.warnings,
        recommended=recommended,
        others=others,
        grayed=grayed if settings.menu.show_disabled_items == "grayed" else (),
        default_intent=prefs.last_intent(s.text_hash)
                       if settings.menu.default_intent_policy == "last_used" else None,
        formats=enabled_formats(recommended, settings),
        free_input=FreeInputSpec(
            enabled=settings.menu.free_input_enabled,
            modes=("instruction", "prompt"),
            style_refs=enabled_route_ids(settings),
        ),
        tools=enabled_search_providers(s.modality, recommended, settings),
    )
```

**三条不变量**：

1. `build_menu()` 纯本地、零 token、零网络；
2. 偏好（`prefs`）只能改变 `order` 与 `default_intent`，**不能覆盖本次明确选择，
   也不能反哺为领域判定的证据**；
3. 菜单 label 用"动词 + 读者收益"（"它是什么物质"），不暴露内部 ID
   （"chem.substance"），只有"复制路由配置"里才出现 ID。

### 2.2 菜单项数据结构

```json
{
  "id": "chem.substance",
  "kind": "intent",
  "label": "它是什么物质",
  "hint": "化学组成、结构、性质",
  "domains": ["chem"],
  "modalities": ["text", "image", "mixed"],
  "applicable": {"all": [{"feature": "latin_symbol_run", "op": "eq", "value": true}]},
  "primary_route": "chem_substance",
  "includes": [],
  "default_format": "static",
  "supported_formats": ["static", "table"],
  "order": 10,
  "enabled": true,
  "prompt_version": "1.0.0",
  "schema_version": "1.0.0"
}
```

`applicable` 的语义是**"这个菜单项对这段输入有没有意义"（预筛选）**，
不是"这段输入是不是化学式"（分类）。判错了没有后果：置灰了用户可强制使用，
显示了不适用项用户也不会选。

## 3. 提示词组合协议（ADR-2a）

### 3.1 定义

```json
{
  "id": "phrase_deep",
  "kind": "intent",
  "label": "这个词组怎么来的、怎么用",
  "hint": "含词源、读音与补充例句",
  "domains": ["en"],
  "primary_route": "en_phrase",
  "includes": [
    {"route_id": "en_word", "sections": ["etymology_roots", "phonetic"], "as": "词源与读音"},
    {"route_id": "general", "sections": ["examples"], "as": "补充例句"}
  ],
  "conflict_policy": "primary_wins",
  "token_budget": 1600,
  "default_format": "static",
  "supported_formats": ["static", "table"],
  "order": 20,
  "prompt_version": "1.0.0",
  "schema_version": "1.0.0"
}
```

### 3.2 组合规则（写死在 `compose_prompt()`，不交给模型）

1. **唯一输出契约**：`query_type` 只取 `primary_route.effective_query_type`；
   `includes` 的字段合并进 `fields` 列表，不引入第二个 `query_type`，
   否则 `normalize()` 无法判定。
2. **片段切分**：从被引用 `RoutePack.prompt` 中按 `schema.json` 的字段标题切出
   对应段落；找不到就跳过并记一条诊断，**不静默拼错**。
3. **循环检测**：`includes` 构成有向图，拓扑排序；发现环直接校验失败
   （设置页保存时拦下，不允许运行时才炸）。
4. **冲突策略**：`primary_wins | include_first | error`，默认 `primary_wins`。
5. **Token 预算**：按字符估算（`len/4`），先截 include，**不截主 prompt 与系统契约**。
6. **系统契约 append-only**：`CONTRACT_TEMPLATE` + `JSON_ESCAPE_RULES` 永远最后追加，
   用户编辑的 `prompt.md` 与自由输入都看不到、删不掉、覆盖不了它。
7. **缓存键**：
   `sha256(composed_prompt) + composition_hash + model + route_id + intent_id +
   format_id + schema_version + ruleset_version + query_hash`，
   其中 `composition_hash = sha256(sorted(includes))`；禁止"改了 includes 却命中旧结果"。
8. **校验**：引用的 route 存在且已启用；section 在该 schema 的 `fields` 中；
   无环；`token_budget ≤ profile.max_tokens`。

### 3.3 拼接骨架

```python
def compose_prompt(intent, packs, settings) -> tuple[str, str]:
    primary = packs[intent.primary_route]
    parts = [render_prompt(primary.prompt, settings)]      # 主 prompt
    order = topo_sort(intent.includes, packs)              # 环 → ValueError
    for inc in order:
        pack = packs[inc.route_id]
        for section in inc.sections:
            text = extract_section(pack.prompt, pack.schema, section)
            if text:
                parts.append(f"【{inc.as or section}】\n{text}")
    parts = truncate_includes(parts, intent.token_budget)  # 不动 parts[0] 与契约
    parts.append(contract(primary, merged_fields(primary, order)))
    parts.append(settings.json_escape_rules)
    composed = "\n".join(p for p in parts if p)
    return composed, composition_hash(intent.includes)
```

## 4. 自由输入（ADR-2b）

### 4.1 `instruction` 模式（默认）

```
你是内容解释助手。用户想要：{custom_instruction}
输入内容：{raw_text / 图片}
参考风格（可选）：{style_ref_route_id 的 prompt 摘要，非全文}
按用户描述生成解析；严格 JSON，至少含 title 与 answer，其余字段自定。
【系统输出契约 append-only】
```

### 4.2 `prompt` 模式

用户输入直接作为 prompt 主体，系统契约仍最后 append。安全约束：

- 用户输入中若含输出契约段落标识，拼接时剥离并记诊断；
- 只进缓存 hash，**不进任何日志、拦截记录、诊断文本**（可能含隐私）；
- 长度上限 `free_input.max_chars`（默认 2000 字）；
- 走 `normalize()` 通用兜底，任意字段都能渲染成完整卡片。

### 4.3 通用约束

- `custom_instruction` 与 `style_ref` 都进缓存键；
- "参考风格"下拉由 `enabled_route_ids()` 动态生成，关掉的路由不出现；
- 自由输入是"菜单不够全"的兜底，**不是把菜单越加越长**的理由；
- 自定义需求不需要新增固定 Prompt，也不需要改代码。

## 5. 出厂意图映射（9 条 RoutePack → 意图）

`spec/builtin_intents.json` 给出完整清单，摘要：

| 域 | 意图 | 主路由 | includes | 默认呈现 |
|---|---|---|---|---|
| `en` | 怎么读、什么意思 | `en_word` | — | static |
| `en` | 这个词组怎么来的、怎么用 | `en_phrase` | `en_word.etymology_roots` | static |
| `en` | 词源与搭配 | `en_word` | `en_phrase.common_mistakes` | table |
| `zh` | 这个词怎么判定、怎么区分 | `chinese` | — | static |
| `zh` | 这句话在说什么 | `chinese_sentence` | — | static |
| `zh` | 回答这个问题 | `chinese_question` | — | static |
| `zh` | 拆解这段论述 | `chinese_paragraph` | — | static |
| `math` | 这个公式怎么读 | `latex` | — | static |
| `math` | 推导过程 | `latex` | `general.examples` | static |
| `math` | 分析随参数怎么变 | `latex` | — | **interactive** |
| `chem` | 它是什么物质 | 新增 `chem_substance` | — | static |
| `chem` | 怎么读 | `en_word` | — | static |
| `code` | 这段代码在做什么 | `code` | — | static |
| `code` | 可能的坑 | `code` | `general.key_points` | table |
| `image` | 提取图片文字 | 视觉 OCR 预设 | — | static |
| `image` | 识别公式并解释 | 视觉 OCR 预设 → 续接 `latex` | — | static |
| `image` | 提取表格与数据 | 新增 `image_table` | — | table |
| `image` | 理解图片内容 | 新增 `image_explain` | — | static |
| `image` | 在原图标注关键内容 | 新增 `image_annotate` | — | annotate |
| `*` | 通用解释 | `general` | — | static |
| `*` | 自定义（自由输入） | — | — | static |

`chem` / `image` 域的路由在 0.1 不存在，属于 0.2 新增的**配置型插件**
（只加目录与文件，不改核心代码），正好验证可插拔是否成立。

## 6. 可交互输出协议（简述）

模型只出**声明式 Spec**，本地出控件；校验失败一律降级为静态 `ViewModel`：

```json
{
  "type": "interactive_chart",
  "title": "Logistic Map",
  "formula": "x_next = r * x * (1 - x)",
  "controls": [
    {"id":"r","label":"参数 r","type":"slider","min":0,"max":4,"default":3.5,"step":0.01}
  ],
  "chart": {"type":"line","x_axis":"iteration","y_axis":"x","points":100}
}
```

约束：字段白名单、控件类型白名单、参数范围与点数上限、表达式用受限解析器
（**禁 `eval`**）、不得执行模型返回的 HTML/JS/代码。0.1 弹窗是 `QTextBrowser`
且 README 明确禁止 `QWebEngineView`，因此交互图放在**独立 Qt 原生控件窗口**，
不内嵌卡片。
