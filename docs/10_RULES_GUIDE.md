# 10 · 规则编写指南：硬拦截与领域初判

## 0. 设计分层（为什么"方便编辑"是成立的）

```
特征原语 features.py        ← Python 代码（30+ 个原子能力，改动少）
        ↓ 数值
条件树 / 规则  spec/*.json  ← 纯数据（增删改规则只动这里）
        ↓ 求值
通用引擎 policy.py / suggest.py ← 解释器（不需要动）
```

- **特征原语**（`url`、`is_word`、`chem_signal`…）是 Python 函数：从文本提取
  一个可判定的值。新增原语才需要写代码。
- **所有组合逻辑都是数据**：拦截规则在 `spec/builtin_policy.json`，
  领域初判条件在 `spec/builtin_settings.json` 的 `domains[].when`。
- 引擎只做两件事：算特征值、按条件树求值。**改规则 ≠ 改代码**。

## 1. 查看一条文本命中的特征与规则（调试入口）

```bash
PYTHONPATH=src python3 -m clipllm02.cli menu "你的文本"
# 输出：F（特征值）/ P（拦截结果）/ D（领域候选）/ I（推荐选项）

# 只看全部特征清单（名称 + 中文说明）
PYTHONPATH=src python3 -c "
from clipllm02.features import feature_catalog
for name, desc in feature_catalog().items():
    print(f'{name:24s} {desc}')
"
```

## 2. 硬拦截规则（`spec/builtin_policy.json`）

每条规则是一个 dict，公共字段：`level`（BLOCK/CONFIRM/WARN/ALLOW）、
`description`（设置页展示）、`locked`（true 则设置页不可改级别）。

按 `detector` 分五种写法：

| detector | 字段 | 命中条件 |
|---|---|---|
| `regex` | `pattern` | 文本匹配正则（忽略大小写） |
| `feature` | `feature` | 特征值为真；可加 `multiline_code_exempt: true`（多行代码豁免） |
| `threshold` | `feature` + `threshold`（键名指向 `thresholds` 配置）+ 可选 `applies_when` 条件树 | 特征值 > 阈值 |
| `quota` | — | 当日请求数达 `daily_request_limit` |
| `dedup` | — | 运行时判定为重复内容（静默跳过） |

**示例：加一条"超过 5000 字符直接拒绝"**

```json
"huge_text": {
  "detector": "threshold",
  "feature": "char_count",
  "threshold": "huge_text_limit",
  "applies_when": {"feature": "char_count", "op": "gt", "value": {"setting": "huge_text_limit", "default": 5000}},
  "level": "BLOCK",
  "description": "文本过长，超出单次解释能力"
}
```

（`thresholds` 里加 `"huge_text_limit": 5000`；`applies_when` 也可以不写，
只要 `threshold` 键存在即按阈值比较。）

## 3. 条件树语法（拦截 `applies_when` 与领域 `domains[].when` 通用）

```json
{"all": [ {"feature": "is_word", "op": "eq", "value": true},
          {"any": [ {"feature": "has_cn", "op": "truthy"},
                    {"feature": "char_count", "op": "lt", "value": 8} ]} ]}
```

- 组合键：`all` / `any` / `not`；
- 叶子键：`feature` + `op` + `value`；
- `op` 支持：`eq ne gt ge lt le in contains regex truthy`；
- `value` 可写 `{"setting": "某阈值名", "default": 0}` 引用配置阈值；
- **未知特征 / 非法 op 一律判假**（规则不会静默改判）。

**领域初判** = 同一棵条件树 + `score`：把新领域加进
`builtin_settings.json` 的 `domains[]`（`id / label / score / switch / when`），
候选排序按 score，初判界面自动出现对应领域 chip 与该领域意图。

## 4. 新增特征原语（唯一需要写代码的情况）

在 `features.py` 的 `FEATURES` 注册表加一个函数 + `_DESCRIPTIONS` 说明：

```python
def _has_latex_env(ctx) -> bool:
    return r"\begin{" in ctx.text

FEATURES["has_latex_env"] = _has_latex_env
_DESCRIPTIONS["has_latex_env"] = "文本包含 LaTeX 环境"
```

之后所有 JSON 条件树都能引用它。

## 5. 测试方法（规则即数据，测起来很快）

```python
# tests/test_my_rule.py
from clipllm02 import InputEnvelope, preflight, load_defaults

def test_my_rule_blocks():
    s = load_defaults()
    s.data["policy"]["rules"]["my_rule"] = {
        "detector": "regex", "pattern": r"内部资料",
        "level": "BLOCK", "description": "内部资料"}
    result = preflight(InputEnvelope.for_text("这是内部资料"), s)
    assert result.level == "BLOCK"
    assert "my_rule" in result.block_reasons
```

不需要网络、不需要 GUI；`pytest tests/test_my_rule.py` 秒级完成。
出厂规则自身的回归在 `tests/test_features_policy.py`。

改完规则记得 `clipllm02 validate` 校验配置合法性。

## 6. 用户级覆盖（不改 spec 文件）

个人调整写在 `~/.config/clip-llm02/settings.json`（深合并，只写差异字段）：

```json
{"policy": {"rules": {"url": {"level": "BLOCK"},
                      "my_rule": {"detector": "regex",
                                  "pattern": "TODO",
                                  "level": "WARN",
                                  "description": "调试残留"}}}}
```

设置界面 →「拦截」页可改每条规则级别；新增规则重启后自动出现在表格。
