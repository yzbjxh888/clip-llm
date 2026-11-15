# clip-llm 0.2

一个本地优先的桌面词典式**内容解释工具**：复制或选中一段文字，窗口自动弹出「初判」，由你决定要看哪一面，再请求大模型给出结构化解释。支持多轮追问、把一次会话落成一篇 Markdown 笔记、图片/截图识别、网页搜索与可插拔的提示词模板。

当前版本 `0.2.6`，基于 PySide6，主要面向 Linux 桌面（X11 / Wayland）。

> 设计原则：**分类器只缩小选项空间，用户决定解释意图，模型只负责执行。**
> 本地初判和菜单不消耗任何 token；不填模型配置时，软件只显示本地预览、不发网络请求。

---

## 它怎么工作

```text
复制 / 截图
   ↓
本地特征 + 分级拦截（零 token）
   ↓
第一层「初判」：领域候选 / 查询方式 / 其他角度 / 自由输入 / 网页搜索
   ↓ 用户选择
组装提示词 → 请求模型 → 校验与渲染
   ↓
第二层「结果」：结构化字段、对话式排版、可继续追问
   ↓
一次会话写进一篇 Markdown 笔记（供 Obsidian / Logseq 建图谱）
```

窗口分两层：第一层是初判菜单，第二层是结果与追问。`Esc` 在结果页先回初判，在初判页才关闭窗口。

---

## 主要功能

| 功能 | 说明 |
|---|---|
| 剪贴板监听 | 文本与图片两路，X11 用 `QClipboard.dataChanged`、Wayland 用常驻 `wl-paste --watch`；复制后自动弹窗 |
| 本地初判 | 34 个具名字符特征 + 7 个领域候选 + 分级拦截，全部本地计算，零 token |
| 查询方式 | 14 套提示词模板 / 44 条查询方式，可在设置里改名、改提示词、启停、新增 |
| 多轮追问 | 结果页底部追问，带上一轮问答作为上下文；追问用自由作答而不是重复 JSON 字段 |
| 结果排版 | JSON 与 Markdown 双通道，公式转 Unicode，表格、列表、气泡；深浅主题与字号可调 |
| 知识库 | 一次会话落成一篇带 YAML frontmatter 的 Markdown（`session` / `rounds` / `created` / `updated`），默认按模板分文件夹 |
| 结果缓存 | 磁盘缓存（`~/.cache/clip-llm02/results.json`），键覆盖提示词、意图、呈现、规则集与输入摘要 |
| 分级拦截 | `BLOCK / WARN / CONFIRM / ALLOW` 四档，出厂 18 条规则，可在设置页改命中方式与级别 |
| 网页搜索 | 7 个出厂搜索源，顶栏菜单与结果页「选中文本」右键都能用；设置页可增删、改名、改网址 |
| 图片识别 | 剪贴板图片 / 系统截图 → 视觉模型 → 识别出的文字回灌初判；`Ctrl+Alt+S` |
| 设置 | 8 个页签、草稿事务、改完立即生效、10 组可改快捷键、深浅主题、知识库目录 |
| 打包 | `scripts/build_deb.sh` 产出自带 venv 的 `.deb`，安装到 `/opt/clipllm02` |

出厂网页搜索：百度、百度百科、Google、Google 搜维基百科、Google 搜 alphaXiv、arXiv、Google 学术。网址支持 `%s` 和 `{q}` 两种占位符。

---

## 安装

### 方式一：`.deb`（推荐给桌面用户）

```bash
sudo dpkg -i clipllm02_0.2.6_amd64.deb
clipllm02-doctor          # 解释器 / 导入 / 依赖自检
clipllm02-desktop         # 启动
```

包内自带最小 venv（仅 `PySide6-Essentials`），源码放在 `/opt/clipllm02/src`，启动器用 `PYTHONPATH` 指向它，不依赖 editable 安装记录。

### 方式二：从源码

需要 Python ≥ 3.10；桌面界面需要 `PySide6-Essentials >= 6.8`。

```bash
git clone git@github.com:yzbjxh888/clip-llm.git
cd clip-llm
bash scripts/setup.sh                 # 重建最小 venv + 跑回归测试
.venv/bin/clipllm02-desktop           # 启动

# 或者手动
python3 -m venv .venv
.venv/bin/pip install -e '.[desktop]'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

打包与快照：

```bash
bash scripts/backup.sh      # 不含 venv 的源码快照（dist/）
bash scripts/build_deb.sh   # 生成 dist/clipllm02_0.2.6_amd64.deb
```

### 配置模型

启动后按 `F3` 打开「模型与图片」页，填写三项：**API 地址、API Key、模型名称**（视觉模型可单独填，留空复用文本配置）。任一项留空都不会发送网络请求，第二层只显示本地查询预览——可以放心先体验界面。

| 位置 | 用途 |
|---|---|
| `~/.config/clip-llm02/settings.json` | 用户配置（含 API Key，只在本机） |
| `~/.cache/clip-llm02/results.json` | 结果缓存 |
| 设置页「知识库」中指定的目录 | Markdown 笔记 |

安装包与仓库**不含任何 API Key**：出厂配置里没有这些字段，打包脚本会扫描源码，发现疑似密钥就中止构建。

---

## 常用快捷键

| 键 | 作用 |
|---|---|
| `Esc` | 结果页返回初判；初判页关闭窗口 |
| `Alt+Left` | 从结果返回初判 |
| `Ctrl+T` / `Ctrl+W` | 新标签 / 关闭标签 |
| `Ctrl+Tab` | 切换标签 |
| `Ctrl+R` | 重新查询 |
| `Ctrl+I` | 独立输入框（主进程禁用了 text-input，中文输入走独立进程） |
| `Ctrl+Alt+S` | 图片 / 截图识别 |
| `Ctrl+Shift+G` | 网页搜索 |
| `F3` | 设置 |

全部可在「设置 → 行为」里修改，冲突会在保存时拦下。

---

## 项目结构

```text
├── spec/                     出厂配置（settings / policy / intents / search providers / JSON Schema）
│   └── legacy_assets.json    0.1 旧脚本迁移来的 9 套完整提示词
├── src/clipllm02/
│   ├── models.py             不可变数据对象
│   ├── features.py           字符特征注册表 + 条件树求值
│   ├── classify.py           类别初判规则
│   ├── policy.py             分级拦截引擎
│   ├── suggest.py            领域候选与菜单构建
│   ├── prompts.py            提示词组合、输出契约、追问自由作答
│   ├── settings.py           配置深合并 / 校验 / 结构签名
│   ├── json_render.py        JSON / Markdown → Qt Rich Text
│   ├── modality.py           模态仲裁与资产存储（见「未实现」）
│   ├── desktop/              窗口基座、网关、缓存、classic 兼容层
│   └── studio/               统一桌面应用（默认入口）
│       ├── app.py            主控与请求调度
│       ├── main_window.py    查询栏 + 多标签
│       ├── query_tab.py      初判 / 结果 / 追问
│       ├── library.py        知识库 Markdown 落盘
│       ├── vision.py         图片与截图识别
│       ├── clipboard_monitor.py
│       └── settings_dialog.py
├── docs/                     规格与设计决策（01–10）
├── design/sketches/          早期布局草稿
├── scripts/                  setup / backup / build_deb
└── tests/                    370+ 单元测试
```

---

## 已知不足

这一节是如实记录，不是客套。**如果你打算基于它二次开发，请先看这里。**

### 1. 拦截逻辑还不够清晰

- 出厂实际是 **18 条**规则，但 `README`/`docs/09` 写 15 条、`docs/02` 列 17 项，三处对不上。
- `dedup` 规则是**死规则**：`policy.py` 需要调用方传 `runtime["is_duplicate"]`，而全仓没有任何调用方传；去重实际由各入口各自实现。
- `quota`（每日限额）只在 Studio 链路生效；`cli.py`、`serve.py`、classic 桌面版调用 `preflight()` 时不传 runtime，限额永不触发。
- `levels` 行为表运行时只读了 `request`，`popup` / `banner` / `log` 等字段没接线；规则里的 `locked` 只用于配置校验，不参与求值。
- `CONFIRM` 只在 Studio 的「首次点击改成授权说明、再次点击才发请求」一条路径上实现；classic / CLI / serve 不会阻止请求。
- 自定义敏感正则的级别在代码里被硬编码成 `CONFIRM`，与「按 secret 当前级别处理」的注释不一致。
- `BLOCK` 在 Studio 里静默丢弃并只写诊断日志，不写脱敏拦截记录；文档描述的「只写脱敏记录」只在 classic 版成立。
- 求值实现是**遍历全部规则收集命中、最后取最高级**，不是文档写的「BLOCK 优先短路」。

### 2. 拦截之后的分类器与字符特征也不够清晰

- `features.py` 的 34 个具名特征、条件树（`all/any/not` + 9 种比较）确实是数据驱动的；但**类别初判本身是硬编码 Python**：`classify.py` 里 14 条 lambda 规则表 + veto + 中文类型的 if 链，改规则需要改代码。文档里「所有组合逻辑都是数据、改规则不改代码」对这部分不成立。
- 领域候选的分数直接取自 `builtin_settings.json` 里写死的常量（如 `math 0.92`），**没有按证据动态评分，也没有测试集校准**；`detection.score_mode` 运行时不读。
- 领域亲和表 `NEAR_DOMAINS` 硬编码且不含 `physics`，但文档举例说 `E=mc²` 会并列物理角度。
- 部分阈值散落或缺失：`cn_dominant_min` 在两个文件各写一次默认值；`cn_sentence_min_len` 根本不在出厂配置里，永远用代码默认值；`thresholds.cn_mixed_max`、`cn_max_chars` 运行时 0 次引用。
- 文档示例里的 `char_count`、`cn_para_route`、`cn_sent_route` 等特征名**不在** `FEATURES` 注册表里，写进条件树会被判假；`spec/legacy_assets.json` 还留着一批引用这些旧名字的遗留规则数据，启动时并不加载。
- `_is_cli()` 用 `shutil.which()` 判断，结果随本机 PATH 变化，同一段文字在不同机器上可能初判不同。
- `use_legacy_template` 字段在配置里存在，但 `IntentOption` 不解析它，相关排序分支恒为假。
- 排查工具 `explain_text()` / `classify_intent()` 已实现但没接到 CLI，文档里写的排查入口实际不可用。

### 3. 未实现 / 未接线的功能

| 项 | 现状 |
|---|---|
| 模态仲裁 / `AssetStore` | 实现完整（`modality.py`），但桌面链路从不调用；所有输入都是纯文本 `InputEnvelope`，`assets` 恒空，图片领域永不入选，`image_text_conflict` / `modality_window_ms` 不生效；文档写的 TTL / LRU 清理也没有 |
| 本地图片文件选择 | `read_image_file()` 已实现，但 UI 没有入口；`QFileDialog` 在 `app.py` 里只有 import，没有调用。文档声称支持「图片文件」，实际只有剪贴板图片与截图 |
| 部分图片配置 | `auto_recognize_in_menu` 只用于「menu 模式下为 true 就报配置错误」，运行时不读；`quick_preset_enabled`、`asset_ttl_days` 运行时 0 次读取；只有 `max_bytes` 生效 |
| 呈现形态 | `static / table / interactive / annotate` 四种里，后两种只是标记，`json_render.py` 没有专用渲染分支 |
| `physics` 模板 | 领域有 `physics`，模板没有；两条物理意图挂在 `latex` 模板下靠专属提示词区分 |
| 模板覆盖 | 14 套模板里只有 9 套有从旧脚本迁移来的完整提示词，另外 5 套（化学 / 四个图像类）用的仍是出厂短版 |
| 多输入通道 | `docs/02` 描述的五条输入通道与「图片 → 写回剪贴板 → 内部提交」链路没有对应实现，实际是 Qt + `wl-paste` 两路 |
| 其它 | 设置页 `SWITCH_LABELS` 是死代码；`conflict_policy` / `prompt_version` / `schema_version` 只进数据不参与决策；`allowed_route_ids` 多选预留 |

### 4. 界面与体验仍在打磨

设置页刚做过一轮统一（分区标题、字号层级、搜索源表格），但提示文案、密度和深浅主题的细节还需要实机打磨；等待态目前只有一句短话，没有进度或流式输出；结果缺少「由模型生成、请核对」这类不确定性提示与停止/重生成入口；空标签页的首次引导也还比较简单。

### 5. 文档与实现有出入

`docs/` 是规格先行的产物，部分章节描述的是设计而非现状（规则条数、输入通道、图片文件入口、拦截记录字段等）。以本节和源码为准；`README` 早期版本里「留待迁移阶段实现」的清单已经过时。

---

## 路线图（暂定）

1. 把类别初判规则从 Python 硬编码迁到配置化条件树，补齐特征名与文档示例的一致性。
2. 打通拦截链路：`dedup` / `quota` / `levels` 行为表在各入口一致生效，统一拦截记录。
3. 接上模态仲裁与本地图片文件入口，清理未使用的配置字段。
4. 领域评分改成可校准的证据加权；补 `physics` 模板与剩余 5 套完整提示词。
5. 结果页补不确定性提示、停止/重生成、长结果的分隔与留白。

---

## 开发与测试

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests   # 370+ 用例
.venv/bin/clipllm02-desktop --offscreen --smoke "NaCl"          # 无显示器冒烟
.venv/bin/clipllm02-desktop --doctor                            # 环境诊断
```

核心库（`models` / `features` / `policy` / `settings` / `prompts`）不依赖 Qt，可以单独用标准库跑。诊断开关 `CLIPLLM_DIAG=1` 会在终端打印链路日志。

---

## 隐私

- 软件不上传遥测、不写日志到远端；只有你主动选择查询方式时才会请求你自己配置的模型接口。
- API Key 只存在本机 `~/.config/clip-llm02/settings.json`，既不进仓库也不进 `.deb`。
- 知识库笔记与结果缓存都写在你本机指定的目录；分享机器前请自行清理 `~/.config` 与 `~/.cache`。

---

## 许可证

[MIT](LICENSE)。欢迎 issue 与 PR；这是一个仍在打磨中的个人项目，接口与配置结构可能随版本调整。
