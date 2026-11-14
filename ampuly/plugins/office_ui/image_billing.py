"""Per-image spend for an order, from the sidecars the image tool writes.

Every generated image has a ``<file>.prompt.json`` next to it (provider,
model, size). Counting those is the only honest per-image tally we have;
the price per image is a setting (the providers bill per image, not per
token), so the number stays "≈估算" like the model cost.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_PRICE_CNY = 0.20  # 千问生图 wanx 档位的公开牌价量级；以供应商后台为准
PRICE_ENV = "AMPULY_IMAGE_PRICE_CNY"
_SKIP_DIRS = {".ampuly-comms", "node_modules", ".git"}


def price_for(provider: str) -> float:
    """CNY per image: provider-specific env first, then the general one, then the default."""
    key = "".join(ch if ch.isalnum() else "_" for ch in str(provider or "").upper())
    for name in (f"{PRICE_ENV}_{key}" if key else "", PRICE_ENV):
        if not name:
            continue
        raw = str(os.environ.get(name, "") or "").strip()
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                continue
    return DEFAULT_PRICE_CNY


def image_spend(workspace_root: str) -> dict[str, Any]:
    """{count, cost_cny, by_provider} for every image sidecar under the workspace."""
    root = Path(str(workspace_root or "")) if workspace_root else None
    result: dict[str, Any] = {"count": 0, "cost_cny": 0.0, "by_provider": {}}
    if root is None or not root.is_dir():
        return result
    by_provider: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".prompt.json"):
                continue
            provider = ""
            try:
                data = json.loads(Path(dirpath, name).read_text(encoding="utf-8"))
                provider = str(data.get("provider", "") or "")
            except (OSError, ValueError):
                pass
            by_provider[provider] = by_provider.get(provider, 0) + 1
    count = sum(by_provider.values())
    cost = sum(n * price_for(p) for p, n in by_provider.items())
    result.update({"count": count, "cost_cny": round(cost, 2), "by_provider": by_provider})
    return result
