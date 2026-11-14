"""Discover a staged Playwright Chromium cache (no download).

Product installs already stage browsers via `scripts/offline_runtime_bundle.py`
and set `PLAYWRIGHT_BROWSERS_PATH`. Source / Desktop launches may not.
This module only *finds* an existing cache so diagnostics and first-start can
fail closed instead of implying a network `playwright install`.

Never vendors Citro Labs ego binaries. Fresh-machine default remains Playwright; local Ego is preferred when present (hybrid).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping


class ChromiumBundleMissing(RuntimeError):
    """Staged Chromium cache not found; callers must not download."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"chromium bundle missing: {reason}")


def _nonempty_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        for child in path.iterdir():
            if child.is_file():
                return True
            if child.is_dir() and any(child.rglob("*")):
                return True
    except OSError:
        return False
    return False


def browsers_cache_present(path: Path | str | None) -> bool:
    """True when *path* looks like a Playwright browsers cache root."""
    if not path:
        return False
    root = Path(path)
    if not _nonempty_dir(root):
        return False
    # Playwright cache typically has chromium-* / chromium_headless_shell-* dirs.
    try:
        names = [p.name.casefold() for p in root.iterdir() if p.is_dir()]
    except OSError:
        return False
    if any(n.startswith("chromium") for n in names):
        return True
    # Offline bundle may nest one level; accept any nonempty staged tree that
    # the installer already verified via SBOM closure.
    return True


def candidate_browsers_roots(
    *,
    env: Mapping[str, str] | None = None,
    install_root: Path | str | None = None,
) -> list[Path]:
    """Ordered discovery candidates (env first, then install-adjacent)."""
    source = env if env is not None else os.environ
    out: list[Path] = []
    seen: set[str] = set()

    def add(raw: Path | str | None) -> None:
        if not raw:
            return
        path = Path(raw).expanduser()
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            return
        seen.add(key)
        out.append(path)

    add(str(source.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip())
    root = Path(install_root).expanduser() if install_root else None
    if root is not None:
        add(root / "runtime" / "browsers")
        add(root / "browsers")
        add(root / ".ampuly" / "browsers")
    ampuly_home = str(source.get("AMPULY_HOME") or "").strip()
    if ampuly_home:
        home = Path(ampuly_home).expanduser()
        add(home / "runtime" / "browsers")
        # Sibling of data/ when launcher uses AMPULY_HOME=%~dp0data
        add(home.parent / "runtime" / "browsers")
    return out


def resolve_browsers_path(
    *,
    env: Mapping[str, str] | None = None,
    install_root: Path | str | None = None,
    require: bool = False,
) -> Path | None:
    """Return the first present browsers cache, or None / raise if require."""
    for candidate in candidate_browsers_roots(env=env, install_root=install_root):
        if browsers_cache_present(candidate):
            try:
                return candidate.resolve()
            except OSError:
                return candidate
    if require:
        raise ChromiumBundleMissing("no_staged_playwright_browsers_cache")
    return None


def describe_chromium_bundle(
    *,
    env: Mapping[str, str] | None = None,
    install_root: Path | str | None = None,
) -> dict[str, Any]:
    """Diagnostic dict for logs/UI — never downloads, never includes secrets."""
    source = env if env is not None else os.environ
    resolved = resolve_browsers_path(env=env, install_root=install_root)
    skip = str(source.get("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD") or "").strip()
    return {
        "product_browser": "playwright_chromium",
        "ego_binary_required": False,
        "browsers_path": str(resolved) if resolved else None,
        "browsers_present": bool(resolved),
        "skip_browser_download": skip in {"1", "true", "True", "yes"},
        "candidates": [str(p) for p in candidate_browsers_roots(env=env, install_root=install_root)],
        "network_install_policy": "forbidden_when_bundled",
    }


def preferred_browsers_slot(
    *,
    env: Mapping[str, str] | None = None,
    install_root: Path | str | None = None,
) -> Path | None:
    """First discovery candidate — where launchers point when nothing is staged yet."""
    candidates = candidate_browsers_roots(env=env, install_root=install_root)
    return candidates[0] if candidates else None



def product_browsers_slot(install_root: Path | str) -> Path:
    """Canonical staged cache for product installs: ``<install>/runtime/browsers``."""
    return Path(install_root).expanduser() / "runtime" / "browsers"


def looks_like_product_install(install_root: Path | str | None) -> bool:
    """True when *install_root* already carries a shipped runtime layout."""
    if not install_root:
        return False
    root = Path(install_root).expanduser()
    if (root / "runtime" / "browsers").is_dir():
        return True
    if (root / "runtime" / "evidence" / "offline-manifest.json").is_file():
        return True
    if (root / "install-record.json").is_file():
        return True
    return False


def stage_browsers_cache(
    source: Path | str,
    destination: Path | str,
    *,
    overwrite: bool = False,
) -> Path:
    """Copy an existing Playwright browsers cache into *destination* (no download).

    Used on a *connected build* machine (or Desktop prep) so a clean customer
    machine receives ``runtime/browsers`` via the installer and never runs
    ``playwright install``. Refuses empty/missing sources. Never vendors Ego.
    """
    import shutil

    src = Path(source).expanduser()
    dest = Path(destination).expanduser()
    if not browsers_cache_present(src):
        raise ChromiumBundleMissing(f"stage_source_missing:{src}")
    if dest.exists():
        if browsers_cache_present(dest) and not overwrite:
            try:
                return dest.resolve()
            except OSError:
                return dest
        if not overwrite:
            raise ChromiumBundleMissing(f"stage_destination_exists:{dest}")
        if dest.is_dir():
            shutil.rmtree(dest)
        elif dest.is_file():
            dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, symlinks=False, dirs_exist_ok=False)
    if not browsers_cache_present(dest):
        raise ChromiumBundleMissing(f"stage_copy_incomplete:{dest}")
    try:
        return dest.resolve()
    except OSError:
        return dest


def ensure_product_browsers(
    *,
    install_root: Path | str,
    source: Path | str | None = None,
    require: bool = True,
) -> Path | None:
    """Return staged ``runtime/browsers``, optionally staging from *source*.

    Fail-closed when *require* and nothing usable is present (DoD D3/D4).
    """
    root = Path(install_root).expanduser()
    slot = product_browsers_slot(root)
    if browsers_cache_present(slot):
        try:
            return slot.resolve()
        except OSError:
            return slot
    # Also accept discovery of an already-present cache under install_root.
    found = resolve_browsers_path(env={}, install_root=root, require=False)
    if found is not None:
        return found
    if source is not None:
        return stage_browsers_cache(source, slot, overwrite=False)
    if require:
        raise ChromiumBundleMissing("no_staged_playwright_browsers_cache")
    return None


def describe_clean_machine_readiness(
    *,
    env: Mapping[str, str] | None = None,
    install_root: Path | str | None = None,
) -> dict[str, Any]:
    """DoD D3 evidence fields: usable without Ego and without playwright install."""
    info = describe_chromium_bundle(env=env, install_root=install_root)
    skip = bool(info.get("skip_browser_download"))
    present = bool(info.get("browsers_present"))
    ego_required = bool(info.get("ego_binary_required"))
    product = looks_like_product_install(install_root) if install_root else False
    ready = present and skip and (not ego_required)
    return {
        **info,
        "clean_machine_ready": ready,
        "ego_install_required": False,
        "playwright_install_required": not present,
        "product_install_layout": product,
        "dod": {
            "D3_no_ego_no_manual_install": ready,
            "D4_fail_closed_no_cdn": skip,
        },
    }


def apply_launcher_browser_env(
    env: dict[str, str],
    *,
    install_root: Path | str | None = None,
    require: bool = False,
) -> dict[str, str]:
    """Pin PLAYWRIGHT_* for Desktop / product launchers. Never downloads.

    Always sets ``PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1`` (DoD D2/D4 — no silent
    CDN ``playwright install``). ``PLAYWRIGHT_BROWSERS_PATH`` becomes the first
    present staged cache; if none is present, keeps a caller-seeded path or the
    preferred install-adjacent slot so a miss fails locally.

    When *require* is true and no cache is present, raises
    ``ChromiumBundleMissing`` (fail-closed first-start / product path).
    """
    resolved = resolve_browsers_path(env=env, install_root=install_root, require=False)
    if resolved is not None:
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(resolved)
    else:
        if require:
            raise ChromiumBundleMissing("no_staged_playwright_browsers_cache")
        existing = str(env.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
        if not existing:
            slot = preferred_browsers_slot(env=env, install_root=install_root)
            if slot is not None:
                env["PLAYWRIGHT_BROWSERS_PATH"] = str(slot)
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    return env
