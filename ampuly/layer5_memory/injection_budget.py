"""Layered experience injection budget L0–L4 with progressive degradation.

Independent Ampuly pure functions. Idea only: inject experience by layer
(role skeleton → resident → catalog titles → body on demand → pending review)
and when over budget degrade full text → truncated sentence → name only.

No memory-runs ledger. No I/O. No Runtime.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


LAYER_ORDER = ("L0", "L1", "L2", "L3", "L4")

DEFAULT_LAYER_BUDGETS: dict[str, int] = {
    "L0": 400,   # role / duty skeleton — keep first
    "L1": 800,   # resident experience
    "L2": 400,   # catalog titles only
    "L3": 800,   # body text on demand
    "L4": 200,   # pending-review inbox (candidates)
}

DEFAULT_TOTAL_BUDGET = 2_400


def injection_budget_enabled_from_env(default: bool = True) -> bool:
    """Budget path is on by default for the pure helper; callers may still gate wiring."""
    raw = str(os.environ.get("AMPULY_INJECTION_BUDGET", "") or "").strip().lower()
    if not raw:
        return bool(default)
    if raw in {"0", "false", "no", "off"}:
        return False
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class InjectionItem:
    name: str
    body: str = ""
    layer: str = "L1"
    required: bool = False

    def __post_init__(self) -> None:
        if type(self.required) is not bool:
            raise ValueError("required must be a boolean")

    def full_text(self) -> str:
        name = str(self.name or "").strip()
        body = str(self.body or "").strip()
        if name and body:
            return f"{name}: {body}" if not body.startswith(name) else body
        return body or name

    def sentence_text(self) -> str:
        text = self.full_text()
        if not text:
            return ""
        # First sentence-ish chunk, keep short.
        m = re.split(r"(?<=[。！？.!?])\s+", text, maxsplit=1)
        first = (m[0] if m else text).strip()
        if len(first) > 160:
            first = first[:157].rstrip() + "..."
        return first

    def name_only(self) -> str:
        return str(self.name or "").strip() or str(self.body or "").strip()[:40]


@dataclass
class LayerBudget:
    total: int = DEFAULT_TOTAL_BUDGET
    per_layer: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_LAYER_BUDGETS))

    def limit_for(self, layer: str) -> int:
        return max(0, int(self.per_layer.get(layer, 0)))


@dataclass
class RenderedInjection:
    text: str
    used_chars: int
    degraded: list[dict[str, str]] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)
    layers_used: dict[str, int] = field(default_factory=dict)
    required_chars: int = 0

    def metadata(self) -> dict[str, Any]:
        """Serializable evidence; contains labels and counts, never item bodies."""
        return {
            "enabled": True, "unit": "unicode_chars", "used_chars": self.used_chars,
            "layers_used": dict(self.layers_used), "required_chars": self.required_chars,
            "degraded": [dict(item) for item in self.degraded], "omitted": list(self.omitted),
        }


class ContextBudgetInsufficient(ValueError):
    """Mandatory context cannot fit; callers must stop before model dispatch."""

    code = "context_budget_insufficient"

    def __init__(self, *, layer: str, required_chars: int, available_chars: int) -> None:
        self.layer = layer
        self.required_chars = required_chars
        self.available_chars = max(0, available_chars)
        super().__init__(f"{self.code}: mandatory context does not fit ({layer})")


class InjectionText(str):
    """Legacy string API with explicit metadata available before serialization.

    JSON encoders intentionally encode this as text. Consumers persisting a
    snapshot must also persist ``budget_metadata`` as a separate JSON field.
    """

    def __new__(cls, value: str, budget_metadata: Mapping[str, Any] | None = None):
        instance = super().__new__(cls, value)
        instance.budget_metadata = dict(budget_metadata or {})
        return instance


def _normalize_item(raw: Any, default_layer: str) -> InjectionItem | None:
    if isinstance(raw, InjectionItem):
        layer = raw.layer if raw.layer in LAYER_ORDER else default_layer
        return InjectionItem(name=raw.name, body=raw.body, layer=layer, required=raw.required)
    if isinstance(raw, Mapping):
        name = str(raw.get("name") or raw.get("title") or raw.get("id") or "").strip()
        body = str(raw.get("body") or raw.get("text") or raw.get("content") or "").strip()
        layer = str(raw.get("layer") or default_layer).strip().upper()
        if layer not in LAYER_ORDER:
            layer = default_layer
        if not name and not body:
            return None
        if not name:
            name = body.split("\n", 1)[0][:80]
        return InjectionItem(name=name, body=body, layer=layer, required=raw.get("required") is True)
    text = str(raw or "").strip()
    if not text:
        return None
    return InjectionItem(name=text.split("\n", 1)[0][:80], body=text, layer=default_layer)


def coerce_layers(payload: Mapping[str, Any] | None) -> dict[str, list[InjectionItem]]:
    """Accept ``{L0:[...], ...}`` or ``{role_skeleton, resident, catalog, bodies, pending}``."""
    raw = dict(payload or {})
    alias = {
        "role_skeleton": "L0",
        "skeleton": "L0",
        "resident": "L1",
        "catalog": "L2",
        "titles": "L2",
        "bodies": "L3",
        "pending": "L4",
        "inbox": "L4",
        "review": "L4",
    }
    out: dict[str, list[InjectionItem]] = {k: [] for k in LAYER_ORDER}
    for key, value in raw.items():
        layer = str(key).strip().upper()
        layer = alias.get(str(key).strip().lower(), layer)
        if layer not in LAYER_ORDER:
            continue
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            normalized = _normalize_item(item, layer)
            if normalized is not None:
                out[layer].append(normalized)
    return out


def _fit(item: InjectionItem, remaining: int) -> tuple[str, str] | None:
    """Return (text, mode) that fits remaining, or None if even name is too long."""
    if remaining <= 0:
        return None
    full = item.full_text()
    if full and len(full) <= remaining:
        return full, "full"
    sentence = item.sentence_text()
    if sentence and len(sentence) <= remaining:
        return sentence, "sentence"
    name = item.name_only()
    if name and len(name) <= remaining:
        return name, "name"
    if name and remaining >= 8:
        return name[: max(1, remaining - 3)].rstrip() + "...", "name"
    return None


def render_layered_injection(
    layers: Mapping[str, Any] | None,
    *,
    budget: LayerBudget | None = None,
    hard_context: str = "",
) -> RenderedInjection:
    """Render exact Unicode-character budgets, reserving mandatory context first.

    ``hard_context`` is copied byte-for-character, not normalized or clipped.
    Required items precede optional L0→L4 experience. They either fit in full or
    raise; priority alone does not make an ordinary L0/L1 item mandatory.
    Joining separators are charged to the following item's layer and total.
    """
    budget = budget or LayerBudget()
    normalized = coerce_layers(layers)
    total_limit = max(0, int(budget.total))
    parts: list[str] = []
    used = 0
    degraded: list[dict[str, str]] = []
    omitted: list[str] = []
    layers_used: dict[str, int] = {k: 0 for k in LAYER_ORDER}

    required_chars = 0
    if hard_context:
        if len(hard_context) > total_limit:
            raise ContextBudgetInsufficient(layer="hard_context", required_chars=len(hard_context), available_chars=total_limit)
        parts.append(hard_context)
        used = required_chars = len(hard_context)

    # Mandatory content is reserved before optional experience can consume any
    # capacity. No output is returned if any mandatory item does not fit.
    for required in (True, False):
        for layer in LAYER_ORDER:
            for item in normalized[layer]:
                if item.required is not required:
                    continue
                extra = 2 if parts else 0
                available = min(total_limit - used, budget.limit_for(layer) - layers_used[layer])
                remaining = available - extra
                if required:
                    text, mode = item.full_text(), "full"
                    if not text:
                        continue
                    if len(text) > remaining or remaining < 0:
                        raise ContextBudgetInsufficient(layer=layer, required_chars=len(text) + extra, available_chars=available)
                    fitted = (text, mode) if text else None
                else:
                    fitted = _fit(item, remaining)
                if fitted is None:
                    if not required:
                        omitted.append(item.name_only() or item.full_text()[:40])
                    continue
                text, mode = fitted
                if mode != "full":
                    degraded.append({"name": item.name_only(), "layer": layer, "mode": mode})
                parts.append(text)
                cost = len(text) + extra
                used += cost
                layers_used[layer] += cost
                if required:
                    required_chars += cost

    text = "\n\n".join(parts)
    return RenderedInjection(
        text=text,
        used_chars=len(text),
        degraded=degraded,
        omitted=omitted,
        layers_used=layers_used,
        required_chars=required_chars,
    )


def items_from_delta_sections(sections: Sequence[tuple[str, Sequence[str]]], *, layer: str = "L1") -> list[InjectionItem]:
    """Helper: turn ('Title', ['a','b']) section pairs into injection items."""
    out: list[InjectionItem] = []
    for title, values in sections:
        clean = [str(v).strip() for v in values if str(v).strip()]
        if not clean:
            continue
        out.append(InjectionItem(name=str(title), body="\n".join(f"- {v}" for v in clean), layer=layer))
    return out
