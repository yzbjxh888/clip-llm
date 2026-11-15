"""统一配置：版本化合并、旧脚本 9 套 Prompt 注入、用户提示词覆盖。

- 升级时只刷新结构性字段（intents / routes），用户改过的提示词、开关、
  字体、视觉提示词按 id 保留；
- ``spec/legacy_assets.json`` 的 9 套完整提示词作为路由模板注入；
- 用户可在设置里改「意图提示词」「路由模板」「视觉识别提示词」。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from ..settings import Settings, load_defaults, load_settings

CONFIG_VERSION = 7
# 历次出厂搜索源的 id。老配置里若只有这些（说明用户没自定义过），升级时
# 直接换成最新出厂值；含 custom.* 等自定义项则原样保留。
_FACTORY_PROVIDER_IDS = {
    "google", "bing", "cambridge", "scholar", "github", "pubchem",
    "chrome_newtab", "baidu", "baidu_baike", "google_wiki",
    "alphaxiv", "arxiv",
}
DEFAULT_VISION_PROMPT = (
    "请识别图片中的文字和数学公式，以 LaTeX 格式输出。"
    "行内公式用 $...$ 包裹，独立公式用 $$...$$ 包裹；"
    "不要用反引号、代码块或 Markdown 强调包裹公式，也不要添加额外解释。"
)

# 全部快捷键都可在「设置 → 快捷键」里修改；值为 QKeySequence 字符串。
DEFAULT_HOTKEYS = {
    "dismiss": "Escape",             # 第一层：关闭初判窗口；第二层：返回初判
    "back_to_detect": "Alt+Left",
    "new_tab": "Ctrl+T",
    "close_tab": "Ctrl+W",
    "open_input": "Ctrl+I",          # 独立输入进程（中文输入法）
    "settings": "F3",
    "vision": "Ctrl+Alt+S",          # 图片识别（也可注册为 GNOME 全局键）
    "requery": "Ctrl+R",
    "search": "Ctrl+Shift+G",
    "switch_tab": "Ctrl+Tab",
}

HOTKEY_LABELS = {
    "dismiss": "关闭初判 / 返回上层",
    "back_to_detect": "从结果返回初判",
    "new_tab": "在新标签页打开",
    "close_tab": "关闭当前标签",
    "switch_tab": "切换标签（Ctrl+Tab 循环）",
    "open_input": "打开输入框（中文）",
    "settings": "打开设置",
    "vision": "图片识别",
    "requery": "重新查询",
    "search": "网页搜索",
}


def config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME",
                               str(Path.home() / ".config")))
    return root / "clip-llm02" / "settings.json"


def legacy_path() -> Path:
    return Path(__file__).resolve().parents[3] / "spec" / "legacy_assets.json"


def load_legacy() -> dict:
    try:
        return json.loads(legacy_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"packs": {}, "rules": [], "json_escape_rules": ""}


def atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def _merge_intents(factory: dict, user: dict) -> list[dict]:
    old = {i.get("id"): i for i in (user.get("intents") or [])
           if isinstance(i, dict)}
    merged = []
    for item in factory.get("intents") or []:
        entry = copy.deepcopy(item)
        previous = old.get(entry.get("id"))
        if previous:
            for key in ("enabled", "prompt_override", "order", "default_format",
                        "supported_formats", "label"):
                if key in previous:
                    entry[key] = previous[key]
        merged.append(entry)
    return merged


def load_config() -> Settings:
    factory = load_defaults().as_dict()
    legacy = load_legacy()
    for route_id, pack in (legacy.get("packs") or {}).items():
        if route_id in factory.get("routes", {}) and pack.get("prompt"):
            factory["routes"][route_id]["prompt"] = pack["prompt"]
    factory.setdefault("config_version", CONFIG_VERSION)
    factory.setdefault("desktop_vision_prompt", DEFAULT_VISION_PROMPT)
    factory.setdefault("desktop_font_size", 14)
    factory.setdefault("desktop_dark_theme", False)
    factory.setdefault("desktop_new_query_in_new_tab", False)
    factory.setdefault("desktop_dup_ttl", 1)   # 1s 防抖：重复复制相同词也应再弹
    factory.setdefault("desktop_auto_hide", True)
    # 双模式（对齐 GoldenDict-ng 的 pinButton）：
    # True  = 默认 pinned：Qt.Window 常驻、Alt+Tab 可见、堆叠交 WM、不自动
    #         隐藏；查询时一次性 restack 到栈顶，点其它窗口自然沉底，
    #         只有 ESC 才关闭。
    # False = unpinned：Qt.Tool 静态置顶 + 400ms 自动隐藏（GoldenDict 默认
    #         的取词弹窗；隐藏时 Alt+Tab 找不到）。
    factory.setdefault("desktop_persistent_window", True)
    # 知识库：结果按提示词模板分文件夹落盘成 Markdown，供笔记软件建图谱
    factory.setdefault("desktop_library_enabled", True)
    factory.setdefault("desktop_library_dir", "")
    factory.setdefault("desktop_max_tokens", 2048)
    factory.setdefault("hotkeys", dict(DEFAULT_HOTKEYS))

    path = config_path()
    if not path.exists():
        return settings_from_data(factory)
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings_from_data(factory)
    if not isinstance(user, dict):
        return settings_from_data(factory)

    changed = user.get("config_version") != CONFIG_VERSION
    if changed:
        user["intents"] = _merge_intents(factory, user)
        user["config_version"] = CONFIG_VERSION
        for key in ("desktop_vision_prompt", "desktop_font_size",
                    "desktop_dark_theme", "desktop_new_query_in_new_tab",
                    "desktop_dup_ttl"):
            user.setdefault(key, factory[key])
        hotkeys = dict(DEFAULT_HOTKEYS)
        hotkeys.update(user.get("hotkeys") or {})
        user["hotkeys"] = hotkeys
        # 搜索源随出厂值升级。老配置里若全是历次出厂项（没自定义过），
        # 直接用新出厂值；用户自己加过搜索源则原样保留。
        old = user.get("search_providers") or []
        ids = {str(p.get("id")) for p in old if isinstance(p, dict)}
        if not ids or ids.issubset(_FACTORY_PROVIDER_IDS):
            user["search_providers"] = copy.deepcopy(
                factory["search_providers"])
            user["search_default_provider"] = factory.get(
                "search_default_provider", "google")
    # 旧配置里存的是上一版出厂值；只在这些值等于旧默认时同步新默认，
    # 且每次加载都检查（版本号可能已被上一版升级过）。
    menu = user.setdefault("menu", {})
    factory_menu = factory.get("menu", {})
    migrated = False
    if menu.get("recommended_limit") in (6, 24):
        # 24 是早期一次「放宽上限」的迁移值；全量显示已由菜单构建逻辑负责，
        # 配置值回到出厂范围即可，避免校验失败。
        menu["recommended_limit"] = factory_menu.get("recommended_limit", 9)
        migrated = True
    if menu.get("other_per_domain") == 2:
        menu["other_per_domain"] = factory_menu.get("other_per_domain", 3)
        migrated = True
    if user.get("desktop_dup_ttl") == 300:      # 旧默认 300s → 1s 防抖
        user["desktop_dup_ttl"] = 1
        migrated = True
    # 旧配置的中文长度规则用 has_cn 覆盖全部中文，把句子/段落/疑问
    # 一起压在 20 字闸门下。迁移为「仅术语」，其余三类走新拆分的独立规则；
    # 幂等且不改用户设定的 level，也不触发 _merge_intents 重建意图列表。
    ctl = (user.get("policy") or {}).get("rules", {}).get("chinese_too_long")
    if isinstance(ctl, dict) and (
            (ctl.get("applies_when") or {}).get("feature") == "has_cn"):
        ctl["applies_when"] = {"feature": "cn_term_like",
                               "op": "eq", "value": True}
        migrated = True
    if changed or migrated:
        try:
            atomic_write(path, user)
        except OSError:
            pass

    return settings_from_data(user)


def settings_from_data(data: dict) -> Settings:
    """从完整配置 dict 构造运行期 Settings（含旧脚本提示词注入）。

    供独立设置窗口保存后的热重载使用，避免再读一遍磁盘。
    """
    legacy = load_legacy()
    merged = copy.deepcopy(data) if isinstance(data, dict) else {}
    merged.setdefault("desktop_vision_prompt", DEFAULT_VISION_PROMPT)
    merged.setdefault("desktop_font_size", 14)
    merged.setdefault("desktop_dark_theme", False)
    merged.setdefault("desktop_new_query_in_new_tab", False)
    merged.setdefault("desktop_dup_ttl", 300)
    merged.setdefault("desktop_max_tokens", 2048)
    hotkeys = dict(DEFAULT_HOTKEYS)
    hotkeys.update(merged.get("hotkeys") or {})
    merged["hotkeys"] = hotkeys
    settings = load_settings(merged)
    # 用户显式删除的出厂规则：deep_merge 以出厂配置为底，会把缺失的键
    # 原样补回，因此加载后需按墓碑再删一次（写盘数据 = 运行期数据）。
    removed = (merged.get("policy") or {}).get("removed_rules") or ()
    if removed:
        rules = settings.policy.setdefault("rules", {})
        for rid in removed:
            rules.pop(rid, None)
    for route_id, pack in (legacy.get("packs") or {}).items():
        if route_id in settings.routes and pack.get("prompt"):
            settings.routes[route_id]["prompt"] = pack["prompt"]
    for route_id, prompt in (merged.get("route_prompts") or {}).items():
        if route_id in settings.routes:
            settings.routes[route_id]["prompt"] = prompt
    settings.data["desktop_vision_prompt"] = merged["desktop_vision_prompt"]
    settings.data["legacy_rules"] = legacy.get("rules", [])
    settings.data["legacy_cases_count"] = legacy.get("cases_count", 0)
    if not settings.data.get("json_escape_rules"):
        settings.data["json_escape_rules"] = legacy.get("json_escape_rules", "")
    return settings


def save_config(data: dict) -> None:
    data = copy.deepcopy(data)
    data["config_version"] = CONFIG_VERSION
    atomic_write(config_path(), data)
