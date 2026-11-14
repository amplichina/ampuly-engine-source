"""AMPULY_* is the name the code reads. SUPERONES_* and OPC_* fill it when it is unset.

``OPC_*`` is the deprecated name. It still wins over ``SUPERONES_*`` when both
are set and ``AMPULY_*`` is empty, because an explicit legacy override used to
beat the previous product alias. An ``AMPULY_*`` that is set is never overwritten.
Nothing is written back to the user's shell.
"""
from __future__ import annotations

import os
from typing import Mapping, MutableMapping

CANONICAL_PREFIX = "AMPULY_"
# OPC_ is the deprecated data-home name and outranks the earlier SUPERONES_ alias.
LEGACY_PREFIXES = ("OPC_", "SUPERONES_")


def alias_pairs(environ: Mapping[str, str]) -> list[tuple[str, str, str]]:
    """(source_name, AMPULY_name, value) for each legacy variable whose canonical twin is unset."""
    pairs: list[tuple[str, str, str]] = []
    claimed: set[str] = set()
    for prefix in LEGACY_PREFIXES:
        for name, value in environ.items():
            if not name.startswith(prefix):
                continue
            suffix = name[len(prefix):]
            if not suffix:
                continue
            canonical = CANONICAL_PREFIX + suffix
            if canonical in claimed or str(environ.get(canonical, "") or "").strip():
                continue
            if not str(value or "").strip():
                continue
            claimed.add(canonical)
            pairs.append((name, canonical, value))
    return pairs


def install_env_aliases(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Copy legacy names into AMPULY_* when that name is unset. Returns the canonical names filled."""
    target = os.environ if environ is None else environ
    filled: list[str] = []
    for source, canonical, value in alias_pairs(target):
        target[canonical] = value
        filled.append(canonical)
        if source.startswith("OPC_"):
            from ampuly.core.home_migration import note_once

            note_once(
                f"env:{source}",
                f"环境变量 {source} 已弃用，本次当作 {canonical} 使用。请改成 {canonical}。",
            )
    return filled


def preferred_name(name: str) -> str:
    """The public AMPULY_* spelling of a legacy or canonical variable."""
    for prefix in LEGACY_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return CANONICAL_PREFIX + name[len(prefix):]
    return name
