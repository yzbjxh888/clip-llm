"""clip-llm 0.2 核心骨架（纯标准库，无 Qt / 无网络依赖）。

这是 0.2 规格的可执行参考实现，覆盖：
特征判别、输入模态仲裁、分级拦截、领域四档、菜单构建、
提示词组合、配置校验与结构签名。

不包含：桌面窗口、独立进程 IPC、LLM Gateway、Renderer —— 见 docs/06_MIGRATION_PLAN.md。
"""
from .models import (
    AssetRef, DomainCandidate, FeatureSet, InputEnvelope, IntentOption,
    MenuModel, MenuOption, PolicyHit, PolicyResult, Preferences, SearchProvider,
    Suggestion, UserSelection, ValidationError, new_id, sha256_text,
)
from .features import evaluate_condition, extract_features, feature_catalog
from .modality import AssetStore, MemoryAssetStore, ModalityArbiter, validate_image
from .policy import intercept_record, preflight
from .prompts import (
    ComposedPrompt, build_request_prompt, compose_prompt, composition_hash,
    detect_cycle, followup_system, freeform_prompt,
)
from .settings import Settings, load_defaults, load_settings, structure_signature, validate
from .suggest import build_menu, disabled_domains, resolve_domains, suggest

__version__ = "0.2.6"

__all__ = [
    "AssetRef", "DomainCandidate", "FeatureSet", "InputEnvelope", "IntentOption",
    "MenuModel", "MenuOption", "PolicyHit", "PolicyResult", "Preferences",
    "SearchProvider", "Suggestion", "UserSelection", "ValidationError",
    "new_id", "sha256_text",
    "evaluate_condition", "extract_features", "feature_catalog",
    "AssetStore", "MemoryAssetStore", "ModalityArbiter", "validate_image",
    "intercept_record", "preflight",
    "ComposedPrompt", "build_request_prompt", "compose_prompt",
    "composition_hash", "detect_cycle", "followup_system", "freeform_prompt",
    "Settings", "load_defaults", "load_settings", "structure_signature", "validate",
    "build_menu", "disabled_domains", "resolve_domains", "suggest",
]
