"""Ego optional-backend config — hybrid path (builtin default + prefer Ego when present).

**Hybrid (owner-approved; B0 auto-prefer):**
- Fresh / default delivery: Playwright / built-in Chromium. New machines must NOT
  require installing Ego; staged Chromium comes from the offline runtime bundle
  (chromium_bundle.py).
- When AMPULY_BROWSER_BACKEND is **unset** and a local ego-browser binary is
  resolvable (env / PATH / known install locations), auto-prefer EgoBackend
  (PR#83). Explicit AMPULY_BROWSER_BACKEND=playwright|ego always wins.
- See the module contract.

Env (no secrets):
- AMPULY_BROWSER_BACKEND=playwright|ego (unset → auto: ego if binary else playwright)
- AMPULY_EGO_BROWSER=/path/to/ego-browser (or EGO_BROWSER / EGO_BROWSER_PATH)
- AMPULY_EGO_PROFILE_ID=<id from ego profiles> dedicated Ampuly profile only
- AMPULY_EGO_SPACES_ROOT=<dir> Ampuly BrowserSpaces root for scripts/artifacts
  (default: under AMPULY_HOME/browser/ego when AMPULY_HOME is set, else unset)

Never migrate the user's main Chrome profile. Never vendor the ego binary into
the release package. Ego remains first-class when the user already installed it.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

BACKEND_PLAYWRIGHT = "playwright"
BACKEND_EGO = "ego"
ALLOWED_BACKENDS = frozenset({BACKEND_PLAYWRIGHT, BACKEND_EGO})

SELECTION_EXPLICIT_PLAYWRIGHT = "explicit_playwright"
SELECTION_EXPLICIT_EGO = "explicit_ego"
SELECTION_AUTO_EGO = "auto_ego_binary_present"
SELECTION_AUTO_PLAYWRIGHT = "auto_playwright_binary_absent"

_EGO_BROWSER_NAMES = ("ego-browser", "ego-browser.exe", "ego-browser.cmd")


class EgoConfigRefused(ValueError):
    """Config or binary resolution refused; callers must not fall through silently."""


@dataclass(frozen=True)
class EgoLaunchConfig:
    backend: str
    binary: str | None
    profile_id: str | None
    spaces_root: Path | None
    binary_found: bool
    selection_reason: str = SELECTION_AUTO_PLAYWRIGHT


def resolve_backend_kind(env: Mapping[str, str] | None = None) -> str:
    """Return playwright|ego.

    Explicit AMPULY_BROWSER_BACKEND always wins. When unset, auto-prefer ego if
    a binary is resolvable; otherwise playwright (fresh-machine / CI safe).
    """
    source = env if env is not None else os.environ
    raw = str(source.get("AMPULY_BROWSER_BACKEND") or "").strip().casefold()
    if raw:
        if raw not in ALLOWED_BACKENDS:
            raise EgoConfigRefused(f"unknown AMPULY_BROWSER_BACKEND: {raw!r}")
        return raw
    if resolve_ego_binary(env):
        return BACKEND_EGO
    return BACKEND_PLAYWRIGHT


def resolve_backend_selection(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Return (backend, selection_reason) for diagnostics."""
    source = env if env is not None else os.environ
    raw = str(source.get("AMPULY_BROWSER_BACKEND") or "").strip().casefold()
    if raw:
        if raw not in ALLOWED_BACKENDS:
            raise EgoConfigRefused(f"unknown AMPULY_BROWSER_BACKEND: {raw!r}")
        if raw == BACKEND_EGO:
            return BACKEND_EGO, SELECTION_EXPLICIT_EGO
        return BACKEND_PLAYWRIGHT, SELECTION_EXPLICIT_PLAYWRIGHT
    if resolve_ego_binary(env):
        return BACKEND_EGO, SELECTION_AUTO_EGO
    return BACKEND_PLAYWRIGHT, SELECTION_AUTO_PLAYWRIGHT


def _file_if_exists(path: Path) -> str | None:
    try:
        if path.is_file():
            return str(path.resolve())
    except OSError:
        return None
    return None


def _scan_known_ego_locations() -> str | None:
    """Best-effort local install paths — never vendors or downloads."""
    homes: list[Path] = []
    try:
        homes.append(Path.home())
    except (RuntimeError, OSError):
        pass
    local_app = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app:
        homes.append(Path(local_app))

    candidates: list[Path] = []
    for home in homes:
        base = home / ".local" / "share" / "ego" / "active_version_dir"
        for name in _EGO_BROWSER_NAMES:
            candidates.append(base / name)
        # Also accept a bare ego root with active_version_dir sibling naming.
        for name in _EGO_BROWSER_NAMES:
            candidates.append(home / ".local" / "share" / "ego" / name)

    if sys.platform.startswith("win"):
        pf = Path(r"C:\Program Files\Citro Labs\ego lite\Application")
        for name in _EGO_BROWSER_NAMES:
            candidates.append(pf / name)
        try:
            if pf.is_dir():
                for child in pf.iterdir():
                    if child.is_dir():
                        for name in _EGO_BROWSER_NAMES:
                            candidates.append(child / name)
        except OSError:
            pass

    for path in candidates:
        found = _file_if_exists(path)
        if found:
            return found
    return None


def resolve_ego_binary(env: Mapping[str, str] | None = None) -> str | None:
    """Return absolute path to ego-browser, or None if not installed / not configured.

    Resolution order:
    1. AMPULY_EGO_BROWSER / EGO_BROWSER / EGO_BROWSER_PATH
    2. PATH lookup for ego-browser
    3. Known install locations (only when using live os.environ, i.e. env is None)

    When a caller-supplied env mapping is passed, known install locations are
    skipped so unit tests stay hermetic.
    """
    live = env is None
    source: Mapping[str, str] = os.environ if live else env
    for key in ("AMPULY_EGO_BROWSER", "EGO_BROWSER", "EGO_BROWSER_PATH"):
        raw = str(source.get(key) or "").strip().strip('"')
        if not raw:
            continue
        path = Path(raw).expanduser()
        found = _file_if_exists(path)
        if found:
            return found
        for name in _EGO_BROWSER_NAMES:
            found = _file_if_exists(path / name)
            if found:
                return found
        raise EgoConfigRefused(f"{key} does not point at ego-browser: {raw!r}")

    if live:
        which = shutil.which("ego-browser")
    else:
        path_override = source.get("PATH")
        if path_override is None:
            which = None
        else:
            which = shutil.which("ego-browser", path=str(path_override))
    if which:
        return str(Path(which).resolve())

    if live:
        return _scan_known_ego_locations()
    return None


def resolve_spaces_root(env: Mapping[str, str] | None = None) -> Path | None:
    source = env if env is not None else os.environ
    raw = str(source.get("AMPULY_EGO_SPACES_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    ampuly_home = str(source.get("AMPULY_HOME") or "").strip()
    if ampuly_home:
        return (Path(ampuly_home).expanduser() / "browser" / "ego").resolve()
    return None


def resolve_ego_profile_id(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    raw = str(source.get("AMPULY_EGO_PROFILE_ID") or "").strip()
    return raw or None


def load_ego_launch_config(env: Mapping[str, str] | None = None) -> EgoLaunchConfig:
    backend, reason = resolve_backend_selection(env)
    binary = resolve_ego_binary(env)
    return EgoLaunchConfig(
        backend=backend,
        binary=binary,
        profile_id=resolve_ego_profile_id(env),
        spaces_root=resolve_spaces_root(env),
        binary_found=bool(binary),
        selection_reason=reason,
    )
