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
    FlowDecision, MidRoute,
)
from .features import (evaluate_condition, extract_features, feature_catalog,
                       content_kind_allowlist)
from .modality import AssetStore, MemoryAssetStore, ModalityArbiter, validate_image, input_kind_for
from .policy import (intercept_record, preflight, length_tier_of, run_flow,
                     FLOW_ORDER, route_short_text, compose_for_route,
                     route_mid_text, short_tier_classification)
from .prompts import (
    ComposedPrompt, GENERIC_TEMPLATE, build_request_prompt, compose_prompt, composition_hash,
    detect_cycle, followup_system, freeform_prompt, new_template_prompt,
    NEW_TEMPLATE_DEFAULT_PROMPT,
)
from .settings import (Settings, load_defaults, load_settings, structure_signature,
                       validate, sync_route_templates, apply_template_edit,
                       assert_no_json_writeback, ruleset_audit,
                       apply_routes_json, apply_decision_model_json,
                       sync_decision_model_templates,
                       assert_no_decision_model_writeback,
                       normalize_short_classifier_source,
                       SHORT_CLASSIFIER_SOURCE_LOCAL,
                       SHORT_CLASSIFIER_SOURCE_DECISION)
from .suggest import (build_menu, disabled_domains, resolve_domains, suggest,
                      suggest_with_short_source)

__version__ = "0.2.44"

__all__ = [
    "AssetRef", "DomainCandidate", "FeatureSet", "InputEnvelope", "IntentOption",
    "MenuModel", "MenuOption", "PolicyHit", "PolicyResult", "Preferences",
    "SearchProvider", "Suggestion", "UserSelection", "ValidationError",
    "new_id", "sha256_text", "FlowDecision", "MidRoute",
    "evaluate_condition", "extract_features", "feature_catalog",
    "content_kind_allowlist",
    "AssetStore", "MemoryAssetStore", "ModalityArbiter", "validate_image",
    "input_kind_for",
    "intercept_record", "preflight",
    "length_tier_of", "run_flow", "FLOW_ORDER",
    "route_short_text", "compose_for_route", "route_mid_text",
    "ComposedPrompt", "GENERIC_TEMPLATE", "build_request_prompt", "compose_prompt",
    "composition_hash", "detect_cycle", "followup_system", "freeform_prompt",
    "new_template_prompt", "NEW_TEMPLATE_DEFAULT_PROMPT",
    "Settings", "load_defaults", "load_settings", "structure_signature", "validate",
    "sync_route_templates", "apply_template_edit", "assert_no_json_writeback",
    "apply_routes_json",
    "apply_decision_model_json", "sync_decision_model_templates",
    "assert_no_decision_model_writeback",
    "ruleset_audit",
    "normalize_short_classifier_source", "SHORT_CLASSIFIER_SOURCE_LOCAL",
    "SHORT_CLASSIFIER_SOURCE_DECISION",
    "build_menu", "disabled_domains", "resolve_domains", "suggest",
    "suggest_with_short_source", "short_tier_classification",
]
