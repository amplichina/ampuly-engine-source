"""Thin re-export for sessions/bridge opt-in (shared-surface minimal).

Prefer importing from `browser_provider`. This alias exists so call sites can
depend on a stable name without pulling ego_backend eagerly in type checkers.
B2: also re-exports open_selected_browser_backend / try_open_selected_*.
"""

from ampuly.layer4_tools.browser_provider import (  # noqa: F401
    describe_browser_backend,
    open_optional_ego_backend,
    open_selected_browser_backend,
    try_open_optional_ego_backend,
    try_open_selected_browser_backend,
)
