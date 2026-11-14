"""Select optional synchronous BrowserBackend leaves; describe their scope.

Hybrid (owner-approved; B0+B2): built-in Playwright/Chromium must work
on new installs without Ego; when AMPULY_BROWSER_BACKEND is unset and a local
ego-browser exists, the leaf selector prefers EgoBackend (PR#83). Explicit
AMPULY_BROWSER_BACKEND=playwright|ego always wins.
This selector does not replace BrowserSessionRegistry's async BrowserRuntime
factory. The production registry default is still Playwright, irrespective of
the leaf candidate. Binary discovery is not Native readiness or a live session.
See the module contract.

C1: Playwright path binds BrowserSpaces Task Space for captures/receipts when a spaces root is resolvable (AMPULY_BROWSER_SPACES_ROOT / AMPULY_EGO_SPACES_ROOT / AMPULY_HOME/browser/spaces).

B2 optional provider entry: open_selected_browser_backend returns EgoBackend when
Ego is selected/resolvable, otherwise PlaywrightBackend (BrowserBackend). Soft
variant never raises. The ego-browser Skill CLI is never invoked outside
EgoBackend — tools still go through Ampuly permission / Effect / receipt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from ampuly.layer4_tools.browser_spaces import BrowserSpaces
from ampuly.layer4_tools.ego_backend import EgoBackend, EgoUnavailable, open_ego_backend
from ampuly.layer4_tools.ego_config import (
    BACKEND_EGO,
    BACKEND_PLAYWRIGHT,
    EgoConfigRefused,
    EgoLaunchConfig,
    load_ego_launch_config,
    resolve_backend_kind,
    resolve_backend_selection,
)
from ampuly.layer4_tools.playwright_backend import PlaywrightBackend, open_playwright_backend
from ampuly.layer4_tools.space_artifacts import (
    describe_directory_roles,
    open_space_artifacts,
    resolve_builtin_spaces_root,
)


def describe_browser_backend(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Describe the leaf candidate and the registry default, not a live runtime.

    ``backend`` / ``selection_reason`` remain the legacy leaf-selector fields.
    ``would_prefer_ego`` is only the leaf's binary-discovery fact. None proves
    that a browser opened or that the registry dispatched through that leaf.
    Production fields describe the unmodified default factory; an injected
    runtime must report its own actual backend after binding. Ambiguous legacy
    preference flags now describe production, with explicit ``leaf_*`` peers.
    """
    from ampuly.layer4_tools.chromium_bundle import describe_chromium_bundle

    chromium = describe_chromium_bundle(env=env)
    production = {
        "selection_scope": "leaf_selector",
        "production_scope": "registry_default",
        "production_backend": BACKEND_PLAYWRIGHT,
        "production_runtime": "ampuly.layer4_tools.browser.BrowserRuntime",
        "registry_ego_integrated": False,
        "runtime_observed": False,
        "product_path": BACKEND_PLAYWRIGHT,
        "default_is_playwright": True,
        "ego_required_for_product": False,
        "ego_preferred_when_present": False,
        "auto_prefer_enabled": False,
        "leaf_auto_prefer_enabled": True,
        "leaf_ego_preferred_when_present": True,
        "hybrid_strategy": "builtin_registry_default_with_optional_leaf_selection",
        "chromium": chromium,
    }
    try:
        cfg: EgoLaunchConfig = load_ego_launch_config(env)
        backend, selection_reason = cfg.backend, cfg.selection_reason
    except EgoConfigRefused as exc:
        return {
            **production,
            "backend": "invalid",
            "error": str(exc),
            "binary_found": False,
            "selection_reason": "config_refused",
            "leaf_default_is_playwright": False,
        }
    builtin_spaces = resolve_builtin_spaces_root(env)
    return {
        **production,
        "backend": backend,
        "binary_found": cfg.binary_found,
        "binary": cfg.binary,
        "ego_profile_id_set": bool(cfg.profile_id),
        "spaces_root": str(cfg.spaces_root) if cfg.spaces_root else None,
        "builtin_spaces_root": str(builtin_spaces) if builtin_spaces else None,
        "directory_roles": describe_directory_roles(),
        "leaf_default_is_playwright": backend == BACKEND_PLAYWRIGHT
        and selection_reason.startswith("auto_"),
        "would_prefer_ego": bool(cfg.binary_found),
        "selection_reason": selection_reason,
    }


def open_optional_ego_backend(
    *,
    profile_id: str,
    task_id: str,
    env: Mapping[str, str] | None = None,
    spaces: BrowserSpaces | None = None,
    runner=None,
    require_windows: bool = True,
) -> EgoBackend | None:
    """Return EgoBackend when backend resolves to ego; None when playwright.

    Raises EgoUnavailable when ego is selected but binary/spaces are missing.
    Prefer open_selected_browser_backend for the B2 hybrid sessions entry.
    """
    kind = resolve_backend_kind(env)
    if kind != BACKEND_EGO:
        return None
    return open_ego_backend(
        profile_id=profile_id,
        task_id=task_id,
        env=env,
        spaces=spaces,
        runner=runner,
        require_windows=require_windows,
    )


def try_open_optional_ego_backend(
    *,
    profile_id: str,
    task_id: str,
    env: Mapping[str, str] | None = None,
    spaces: BrowserSpaces | None = None,
    runner=None,
    require_windows: bool = True,
) -> tuple[EgoBackend | None, str]:
    """Soft open Ego only: (backend_or_None, reason). Never raises.

    When playwright is selected this returns (None, selection_reason). For the
    hybrid sessions entry that always yields a BrowserBackend, use
    try_open_selected_browser_backend instead.
    """
    try:
        kind, selection_reason = resolve_backend_selection(env)
    except EgoConfigRefused as exc:
        return None, f"config_refused:{exc}"
    if kind != BACKEND_EGO:
        return None, selection_reason
    try:
        backend = open_ego_backend(
            profile_id=profile_id,
            task_id=task_id,
            env=env,
            spaces=spaces,
            runner=runner,
            require_windows=require_windows,
        )
    except EgoUnavailable as exc:
        return None, exc.reason
    return backend, "ego_open"


def open_selected_browser_backend(
    *,
    profile_id: str,
    task_id: str,
    env: Mapping[str, str] | None = None,
    spaces: BrowserSpaces | None = None,
    runner=None,
    require_windows: bool = True,
    artifact_dir: str | Path | None = None,
    runtime=None,
    runtime_factory: Callable[[], Any] | None = None,
) -> tuple[EgoBackend | PlaywrightBackend, str, str]:
    """B2 hybrid entry: EgoBackend if Ego selected, else PlaywrightBackend.

    Returns (backend, kind, selection_reason). Raises EgoConfigRefused on bad
    env; EgoUnavailable when ego is selected but cannot open. Playwright path
    does not launch Chromium until the backend is used (runtime may be injected).
    """
    kind, selection_reason = resolve_backend_selection(env)
    if kind == BACKEND_EGO:
        backend = open_ego_backend(
            profile_id=profile_id,
            task_id=task_id,
            env=env,
            spaces=spaces,
            runner=runner,
            require_windows=require_windows,
        )
        return backend, BACKEND_EGO, selection_reason
    backend = _open_playwright_with_optional_space(
        profile_id=profile_id,
        task_id=task_id,
        env=env,
        spaces=spaces,
        artifact_dir=artifact_dir,
        runtime=runtime,
        runtime_factory=runtime_factory,
    )
    return backend, BACKEND_PLAYWRIGHT, selection_reason


def try_open_selected_browser_backend(
    *,
    profile_id: str,
    task_id: str,
    env: Mapping[str, str] | None = None,
    spaces: BrowserSpaces | None = None,
    runner=None,
    require_windows: bool = True,
    artifact_dir: str | Path | None = None,
    runtime=None,
    runtime_factory: Callable[[], Any] | None = None,
) -> tuple[EgoBackend | PlaywrightBackend | None, str, str]:
    """Soft hybrid open: (backend_or_None, kind_or_empty, reason). Never raises.

    Playwright selection always yields PlaywrightBackend (unless factory blows
    up — then None + error reason). Ego selection yields EgoBackend or None
    with EgoUnavailable.reason / config_refused.
    """
    try:
        kind, selection_reason = resolve_backend_selection(env)
    except EgoConfigRefused as exc:
        return None, "", f"config_refused:{exc}"
    if kind == BACKEND_EGO:
        try:
            backend = open_ego_backend(
                profile_id=profile_id,
                task_id=task_id,
                env=env,
                spaces=spaces,
                runner=runner,
                require_windows=require_windows,
            )
        except EgoUnavailable as exc:
            return None, BACKEND_EGO, exc.reason
        return backend, BACKEND_EGO, selection_reason
    try:
        backend = _open_playwright_with_optional_space(
            profile_id=profile_id,
            task_id=task_id,
            env=env,
            spaces=spaces,
            artifact_dir=artifact_dir,
            runtime=runtime,
            runtime_factory=runtime_factory,
        )
    except Exception as exc:  # noqa: BLE001 — soft path for boot/tests
        return None, BACKEND_PLAYWRIGHT, f"playwright_open_failed:{type(exc).__name__}"
    return backend, BACKEND_PLAYWRIGHT, selection_reason


def _open_playwright_with_optional_space(
    *,
    profile_id: str,
    task_id: str,
    env: Mapping[str, str] | None,
    spaces: BrowserSpaces | None,
    artifact_dir: str | Path | None,
    runtime,
    runtime_factory: Callable[[], Any] | None,
) -> PlaywrightBackend:
    """C1: when a spaces root (or injected store) is available, bind Task Space artifacts."""
    layout = None
    store = spaces
    if store is None:
        root = resolve_builtin_spaces_root(env)
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
            store = BrowserSpaces(root)
    if store is not None and profile_id and task_id:
        layout = open_space_artifacts(store, profile_id, task_id)
    return open_playwright_backend(
        runtime=runtime,
        artifact_dir=artifact_dir,
        runtime_factory=runtime_factory,
        space_artifacts=layout,
    )
