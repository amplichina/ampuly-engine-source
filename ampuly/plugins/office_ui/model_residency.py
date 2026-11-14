"""Compatibility shim. Residency policy lives in ``ampuly.core.model_residency``."""
from __future__ import annotations

from ampuly.core.model_residency import (
    AGENT_REFUSAL,
    REFUSALS,
    RESIDENCY,
    agent_refusal,
    model_refusal,
    normalize,
)

__all__ = [
    "AGENT_REFUSAL",
    "REFUSALS",
    "RESIDENCY",
    "agent_refusal",
    "model_refusal",
    "normalize",
]
