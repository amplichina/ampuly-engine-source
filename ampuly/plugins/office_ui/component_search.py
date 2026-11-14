"""总控检索组件: what master control can find to build a workflow from, before anyone downloads anything.

Owner's decision : master control, drafting or pre-building a workflow, calls every component that
may help -- the internal library, the local resource catalog, the online market -- and people adjust and test.
This module is the looking-up half:

* **Local sources need no permission to read.** The internal library (``distribution/skills``), the local resource
  catalog (``distribution/component_catalog/skills.yaml``) and the market listing the console already fetched for
  the owner's 组件库 page (its ranking and the owner's own searches) are all on this machine.
* **Master control does not go online.** The master runtime asks nobody, so none of its tools reaches the network
  (tests/test_policyless_runtimes_cannot_reach.py). The market part is what the owner's 组件库 page already loaded or
  searched; to see more of the market, the owner searches there.
* **Paid skills are never offered** (no budget or payment channel), from any source.
* **Nothing is downloaded or admitted here.** A component from outside the library that master control puts on a
  step becomes a *candidate*: the owner tries it (调用测试) and admits it before a draft naming it can be saved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

MAX_RESULTS = 25
DEFAULT_RESULTS = 10


def _terms(query: str) -> list[str]:
    return [term for term in str(query or "").casefold().split() if term][:8]


def _matches(terms: list[str], *fields: Any) -> bool:
    if not terms:
        return True
    haystack = " ".join(str(field or "") for field in fields).casefold()
    return any(term in haystack for term in terms)


def _limit(value: Any) -> int:
    try:
        return max(1, min(int(value), MAX_RESULTS))
    except (TypeError, ValueError):
        return DEFAULT_RESULTS


def cached_market_rows(market_cache: Any) -> list[dict[str, Any]]:
    """The market rows the console already fetched (``WSHandler._market_cache``), newest listing first, one per slug."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    entries = list(market_cache.values()) if isinstance(market_cache, dict) else []
    for entry in sorted(entries, key=lambda item: item[0] if isinstance(item, tuple) and item else 0, reverse=True):
        result = entry[1] if isinstance(entry, tuple) and len(entry) > 1 and isinstance(entry[1], dict) else {}
        for row in list(result.get("skills") or []):
            slug = str((row or {}).get("slug") or "") if isinstance(row, dict) else ""
            if slug and slug not in seen:
                seen.add(slug)
                rows.append(row)
    return rows


def _market_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("slug") or ""),
        "source": "skillhub",
        "name": str(row.get("name") or row.get("slug") or ""),
        "summary": str(row.get("summary") or ""),
        "category": str(row.get("category") or ""),
        "stars": int(row.get("stars") or 0),
        "requires_api_key": bool(row.get("requires_api_key")),
        "paid": bool(row.get("paid")),
        "can_try": True,
    }


def _catalog_governance(skill: dict[str, Any]) -> dict[str, Any]:
    # The catalog file holds what the scan read; risk and admission blockers are derived (component_catalog), not stored.
    from ampuly.plugins.office_ui.component_catalog import skill_governance

    try:
        return skill_governance(skill)
    except Exception:  # noqa: BLE001 - an entry the derivation cannot read is shown as unknown risk, never as safe
        return {"risk_level": "unknown", "reasons": [], "admission_blockers": ["frontmatter_missing"]}


def _catalog_paid(skill: dict[str, Any], governance: dict[str, Any]) -> bool:
    return bool(dict(skill.get("pricing") or {}).get("paid")) or "owner_budget_approval" in list(governance.get("admission_blockers") or [])


def _catalog_row(skill: dict[str, Any], can_try: bool = False) -> dict[str, Any]:
    governance = _catalog_governance(skill)
    return {
        "id": str(skill.get("id") or ""),
        "source": "catalog",
        "name": str(skill.get("name") or skill.get("id") or ""),
        "summary": str(skill.get("summary") or ""),
        "category": str(skill.get("category") or ""),
        "risk_level": str(governance.get("risk_level") or ""),
        "admission_blockers": [str(item) for item in list(governance.get("admission_blockers") or [])],
        "reasons": [str(item) for item in list(governance.get("reasons") or [])],
        "paid": _catalog_paid(skill, governance),
        # Tried by importing it from the local resource library, which only works where the owner named that library.
        "can_try": can_try,
    }


def search_local_components(
    repo: Path, packages: list[Any], market_rows: Iterable[dict[str, Any]], query: str = "", *, limit: Any = DEFAULT_RESULTS,
) -> dict[str, Any]:
    """Internal library, local resource catalog and the already-fetched market listing, matched by ``query``."""
    from ampuly.plugins.office_ui.component_catalog import internal_skills, load_catalog, load_internal_categories

    terms = _terms(query)
    count = _limit(limit)
    _categories, filed, _problems = load_internal_categories(Path(repo))
    internal = [
        {
            "id": row["id"], "source": "internal", "name": row["name"], "summary": row["summary"], "category": row.get("category") or "",
            "contract_complete": bool(row["contract_complete"]), "used_by": [item["id"] for item in row["used_by"]],
        }
        for row in internal_skills(Path(repo) / "distribution" / "skills", packages, filed)
        if _matches(terms, row["id"], row["name"], row["summary"], row.get("category"))
    ]
    from ampuly.plugins.office_ui.catalog_trial import library_status

    loaded = load_catalog(Path(repo))
    library_ready = bool(library_status(Path(repo), expected=loaded.library)["ready"])
    catalog_rows = [_catalog_row(skill, library_ready) for skill in loaded.skills]
    catalog = [row for row in catalog_rows if not row["paid"] and _matches(terms, row["id"], row["name"], row["summary"], row["category"])]
    market = [
        row for row in (_market_row(item) for item in market_rows)
        if row["id"] and not row["paid"] and _matches(terms, row["id"], row["name"], row["summary"], row["category"])
    ]
    return {
        "query": str(query or ""),
        "internal": internal[:count],
        "catalog": catalog[:count],
        "market": market[:count],
        "counts": {"internal": len(internal), "catalog": len(catalog), "market": len(market)},
        "note": (
            "internal skills can go on steps as they are; market and catalog ones become candidates the owner tries and admits "
            "before saving (a catalog one is imported from the local resource library, so where can_try is false -- no library "
            "named on this machine -- it saves only once it is taken off the step). The market part is only what the console already loaded; "
            "ask the owner to search the market on the Components page for more. Paid skills are left out."
        ),
    }


def component_index(repo: Path, market_rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Every outside component this machine knows by id: the fetched market listing first, then the local catalog."""
    from ampuly.plugins.office_ui.component_catalog import load_catalog

    index: dict[str, dict[str, Any]] = {}
    for item in market_rows:
        row = _market_row(item)
        if row["id"]:
            index.setdefault(row["id"], row)
    for skill in load_catalog(Path(repo)).skills:
        row = _catalog_row(skill)
        if row["id"]:
            index.setdefault(row["id"], row)
    return index
