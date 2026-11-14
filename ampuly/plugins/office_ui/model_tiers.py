"""Compatibility shim. The tier table lives in ``ampuly.core.model_tiers``."""
from __future__ import annotations

from ampuly.core.model_tiers import (
    DEFAULT_WORK_TIER,
    TIER_ORDER,
    TIER_PARAM_KEYS,
    TIERS,
    normalize_tier,
    plan_digest,
    plan_from_config,
    resolve_gate_model,
    resolve_model,
    suggest_tier,
    tier_of_model,
    tier_params,
)

__all__ = [
    "DEFAULT_WORK_TIER",
    "TIER_ORDER",
    "TIER_PARAM_KEYS",
    "TIERS",
    "normalize_tier",
    "plan_digest",
    "plan_from_config",
    "resolve_gate_model",
    "resolve_model",
    "suggest_tier",
    "tier_of_model",
    "tier_params",
]
