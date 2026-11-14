"""Compatibility shim. Direct-task fixed output lives in ``ampuly.core.direct_task_delivery``."""
from __future__ import annotations

from ampuly.core.direct_task_delivery import (
    finish_direct_task_body,
    record_direct_fixed_output,
)

__all__ = [
    "finish_direct_task_body",
    "record_direct_fixed_output",
]
