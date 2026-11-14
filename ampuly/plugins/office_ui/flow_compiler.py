"""需求 → 流程草案 (4.7 Q04-B): compile an IntentSpec into a node table the builder can open. Writes nothing.

The model reads the owner's words and files and hands over what the flow should do as structured data (an
IntentSpec: steps, who does them, what each delivers and needs, what capability each wants). Everything a model
could get wrong on purpose or by accident is decided here instead, deterministically:

* **Components come from the real internal library only.** A step names a capability (or a skill it found with
  master_search_components); the library is searched here, the pick and why are recorded, and a step nothing fits
  keeps a visible placeholder -- never an invented component id.
* **Who does a step comes from the chosen team's roles.** A role hint that matches no role leaves the step
  unassigned (the builder's staging lane), for the owner to choose.
* **The graph is checked before anyone sees it:** unique step keys, known dependencies, no cycle.

The result is a FlowDraftProposal: shown on a card in the conversation, opened on the builder as an unsaved draft,
saved through the ordinary save with all its checks. Compiling twice from the same spec gives the same table, which
is what lets the console recompile from the stored spec instead of trusting a node table sent by a page.
"""

from __future__ import annotations

import hashlib
import json
import re
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

MAX_STEPS = 30
MAX_LIST = 10
KINDS = {"ai": "ai_do", "human": "human_do", "review": "human_review"}
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_FIELD_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class IntentRefused(ValueError):
    """The spec cannot be compiled as given; the message says what to fix."""


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _texts(value: Any, limit: int = 200, count: int = MAX_LIST) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for text in (_text(item, limit) for item in value) if text][:count]


def _terms(query: str) -> list[str]:
    return [term for term in re.split(r"[\s,，、/]+", str(query or "").casefold()) if term][:8]


# Bumped when the compiler's own rules change, so a draft made under the old rules is not mistaken for one made
# under these. Q05 B6 added the browser fallback, which changes what a step with no component gets; b7 
# lets a step lock the site the owner named into its allowed sites (``lock_site``).
COMPILER_REVISION = "b7"


def library_revision(library: list[dict[str, Any]]) -> str:
    """Which library the matching was done against, and under which rules: ids, versions and kinds, hashed.

    The kind is in there because a skill and a registered connector can share an id space, and the matching
    order treats them differently; the compiler's own revision is in there because 「what this step got」 is
    decided as much by the rules as by the library.
    """
    listed = sorted((str(row.get("id", "")), str(row.get("version", "")), str(row.get("kind", "skill")))
                    for row in library)
    body = {"compiler": COMPILER_REVISION, "library": listed}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


# The step's own connector field names this one; it is not a library entry, because it is what a step gets when
# the library had nothing.
BROWSER_UNIVERSAL = "browser_universal"

# Words that say a step is about something outside this machine, when the spec did not set ``external``.
_SITE_WORDS = ("http://", "https://", "www.", "网站", "后台", "官网", "门户", "登录", "网页")


def _reaches_a_site(step: dict[str, Any]) -> bool:
    """Whether this step is about a website at all.

    ``site`` or ``external`` said so outright; otherwise the step's own words are read. A step that never
    mentions the outside does not get a browser: an empty connector on an internal step is a field the owner has
    to clear, not a help.
    """
    if str(step.get("site", "") or "").strip() or bool(step.get("external")):
        return True
    text = " ".join(str(step.get(key, "") or "") for key in ("does", "name", "capability")).casefold()
    return any(word in text for word in _SITE_WORDS)


def _locked_site(step: dict[str, Any]) -> str:
    """The host a step locks into its allowed sites, or ''.

    Only when the spec set ``lock_site`` AND named a site that reads as a host: the lock writes nothing a person did
    not name (the prompt tells the model to lock only a host the owner actually named, never an invented one).
    """
    if not step.get("lock_site"):
        return ""
    from ampuly.plugins.office_ui.browser_connector import clean_domain

    return clean_domain(step.get("site", ""))


def _browser_connector_for(step: dict[str, Any]) -> dict[str, Any]:
    """The connector instance a drafted step carries.

    Nothing is prefilled that a person has to stand behind: no thresholds, no postconditions and no output. Those
    are listed as 「待补」 on the draft and confirmed on the canvas. ``goal`` is the step's own words, which the owner
    wrote, and ``sites`` carries the host they named if they named one. The allowed sites stay empty too, unless the
    step locked the site the owner named (``lock_site``, b7): then that one host is the step's only allowed site.
    """
    site = str(step.get("site", "") or "").strip()
    locked = _locked_site(step)
    return {
        "goal": str(step.get("does", "") or step.get("name", "") or "")[:500],
        "sites": [site] if site else [],
        "allowed_domains": [locked] if locked else [],
        "postconditions": [],
        "output": {},
        "by": "fallback_browser",
    }


def _match_component(step: dict[str, Any], library: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(match, missing): the library entry a step gets, or why it gets none.

    A skill first, then a registered API/MCP connector. Both are real components with a contract; the browser is
    the thing that comes after both of them fail, and it is decided by the caller, not here.
    """
    by_id = {str(row.get("id", "")): row for row in library}
    named = _text(step.get("component"), 64)
    capability = _text(step.get("capability"), 80)
    if named:
        row = by_id.get(named)
        if row is not None:
            return {"step": step["key"], "by": "named", "component": named, "name": str(row.get("name") or named)}, None
        # A name the library does not hold is not put on the step: the step keeps a placeholder instead.
        return None, {"step": step["key"], "capability": capability or named, "reason": f"{named} is not in the internal library"}
    if not capability:
        return None, None
    terms = _terms(capability)

    def score(row: dict[str, Any]) -> int:
        fields = {
            "id": str(row.get("id", "")).casefold(), "name": str(row.get("name", "")).casefold(),
            "summary": str(row.get("summary", "")).casefold(), "category": str(row.get("category", "")).casefold(),
        }
        # A match in a component's name or id is more specific than a match in its summary.
        return sum(3 * (term in fields["name"] or term in fields["id"]) + (term in fields["summary"]) + (term in fields["category"]) for term in terms)

    ranked = sorted(((score(row), str(row.get("id", ""))) for row in library), key=lambda item: (-item[0], item[1]))
    ranked = [(points, skill_id) for points, skill_id in ranked if points > 0]
    if not ranked:
        return None, {"step": step["key"], "capability": capability, "reason": "nothing in the internal library matches"}
    best = ranked[0][1]
    return {
        "step": step["key"], "by": "search", "query": capability, "component": best,
        "name": str(by_id[best].get("name") or best),
        "alternatives": [skill_id for _points, skill_id in ranked[1:3]],
    }, None


def _role_for(hint: str, roles: list[dict[str, Any]]) -> str:
    clean = hint.casefold()
    if not clean:
        return ""
    for role in roles:
        if clean in {str(role.get("id", "")).casefold(), str(role.get("name", "")).casefold()}:
            return str(role.get("id", ""))
    for role in roles:
        if clean in str(role.get("name", "")).casefold() or clean in str(role.get("id", "")).casefold():
            return str(role.get("id", ""))
    return ""


def normalize_intent(raw: Any) -> dict[str, Any]:
    """The spec as it will be stored and compiled: known fields only, bounded, shape-checked. Raises IntentRefused."""
    if not isinstance(raw, dict):
        raise IntentRefused("the requirement spec must be an object")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise IntentRefused("the requirement spec needs at least one step")
    if len(steps_raw) > MAX_STEPS:
        raise IntentRefused(f"at most {MAX_STEPS} steps in one draft")
    steps: list[dict[str, Any]] = []
    keys: set[str] = set()
    for index, item in enumerate(steps_raw):
        if not isinstance(item, dict):
            raise IntentRefused(f"step {index + 1} must be an object")
        key = _text(item.get("key"), 32)
        if not _KEY.match(key):
            raise IntentRefused(f"step {index + 1}: key must be lowercase letters, digits or _ and start with a letter")
        if key in keys:
            raise IntentRefused(f"step key {key!r} is used twice")
        kind = _text(item.get("kind"), 10) or "ai"
        if kind not in KINDS:
            raise IntentRefused(f"step {key}: kind must be one of {', '.join(KINDS)}")
        keys.add(key)
        steps.append({
            "key": key, "name": _text(item.get("name"), 40) or key, "kind": kind,
            "does": _text(item.get("does"), 500), "delivers": _texts(item.get("delivers")), "needs": _texts(item.get("needs")),
            "after": _texts(item.get("after"), 32), "capability": _text(item.get("capability"), 80),
            # Q05 B6: which site this step is about, and whether it reaches outside this machine at all. Both are
            # hints for matching, never a destination: nothing here opens anything.
            "site": _text(item.get("site"), 120), "external": bool(item.get("external")),
            # b7: the site the owner named is this step's only allowed site. A request, not a check: the compiler
            # locks it only when ``site`` reads as a host (_locked_site); the builder's save still validates it.
            "lock_site": bool(item.get("lock_site")),
            "component": _text(item.get("component"), 64), "role": _text(item.get("role"), 40),
            "sign_off": bool(item.get("sign_off")), "source": _text(item.get("source"), 120),
        })
    for step in steps:
        unknown = [key for key in step["after"] if key not in keys]
        if unknown:
            raise IntentRefused(f"step {step['key']} comes after steps the spec does not have: {', '.join(unknown)}")
    try:
        tuple(TopologicalSorter({step["key"]: set(step["after"]) for step in steps}).static_order())
    except CycleError as exc:
        raise IntentRefused(f"the steps wait for each other in a circle: {' → '.join(map(str, exc.args[1]))}") from exc
    intake: list[dict[str, Any]] = []
    for field in list(raw.get("intake") or [])[:MAX_LIST]:
        if isinstance(field, dict) and _FIELD_ID.match(_text(field.get("id"), 32)):
            intake.append({"id": _text(field.get("id"), 32), "label": _text(field.get("label"), 60) or _text(field.get("id"), 32),
                           "required": bool(field.get("required"))})
    return {
        "name": _text(raw.get("name"), 60) or "新业务流",
        "summary": _text(raw.get("summary"), 500),
        "team": _text(raw.get("team"), 64),
        "steps": steps,
        "intake": intake,
        "open_questions": _texts(raw.get("open_questions"), 200, 20),
        "assumptions": _texts(raw.get("assumptions"), 200, 20),
    }


def compile_intent(intent: dict[str, Any], *, roles: list[dict[str, Any]], library: list[dict[str, Any]]) -> dict[str, Any]:
    """A normalized spec, the chosen team's roles and the internal library → the FlowDraftProposal."""
    nodes: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    unassigned: list[str] = []
    incomplete: list[dict[str, Any]] = []
    for step in intent["steps"]:
        node_type = KINDS[step["kind"]]
        match, gap = (_match_component(step, library) if node_type == "ai_do" else (None, None))
        # Q05 B6: a step that reaches a website and matched neither a skill nor a registered API/MCP connector
        # gets the browser universal connector. The order is deliberate -- a real API is better than driving a
        # page -- and 「the browser」 is what is left when nothing matched, never a first choice.
        connector = None
        if node_type == "ai_do" and match is None and _reaches_a_site(step):
            connector = _browser_connector_for(step)
            gap = None
        if match:
            matches.append(match)
        if connector:
            matches.append({"step": step["key"], "by": "fallback_browser", "component": BROWSER_UNIVERSAL,
                            "name": "浏览器万能连接器"})
            # What the owner still has to fill in. Not guessed here: a made-up domain or a made-up 「done」 rule
            # is worse than an empty field, because it looks decided. A site the step locked is filled (b7).
            owed = ["postconditions", "output"] if connector["allowed_domains"] else ["allowed_domains", "postconditions", "output"]
            incomplete.append({"step": step["key"], "missing": owed})
        if gap:
            missing.append(gap)
        role = _role_for(step["role"], roles)
        if not role:
            unassigned.append(step["key"])
        responsibility = step["does"] or step["name"]
        if gap:
            responsibility = f"{responsibility}（待补组件：{gap['capability']}）"
        # Every step names what it hands on -- a review too (its verdict); the save refuses a step without one. Listed
        # for the owner rather than made up: a missing deliverable stops the save, not the drawing of the draft.
        if not step["delivers"]:
            incomplete.append({"step": step["key"], "missing": ["deliverables"]})
        nodes.append({
            "id": step["key"], "name": step["name"], "type": node_type, "role": role,
            "assignee": "" if node_type == "ai_do" else "owner",
            "gate": "human_required" if step["sign_off"] else "none", "gate_to": "owner" if step["sign_off"] else "",
            "depends_on": list(step["after"]), "deliverables": list(step["delivers"]), "inputs": list(step["needs"]),
            "skills": [match["component"]] if match else [], "tools": [], "responsibility": responsibility, "invoke": {},
            **({"connector": connector} if connector else {}),
        })
    # Inputs no step before this one delivers: advice for the owner, the same rule the canvas shows.
    from ampuly.plugins.office_ui.workflow_packages import WorkflowPackage, input_findings

    undelivered = [
        {"step": finding["node_id"], "input": finding["input"]}
        for finding in input_findings(WorkflowPackage(root=Path("."), manifest={"nodes": nodes}))
    ]
    return {
        "kind": "new_flow",
        "name": intent["name"], "summary": intent["summary"], "team": intent["team"],
        "nodes": nodes, "intake": intent["intake"],
        "matches": matches, "missing_components": missing, "unassigned_steps": unassigned,
        "incomplete_steps": incomplete, "undelivered_inputs": undelivered,
        "open_questions": intent["open_questions"], "assumptions": intent["assumptions"],
        "sources": {step["key"]: step["source"] for step in intent["steps"] if step["source"]},
        "library_revision": library_revision(library),
        "intent": intent,
    }


def team_roles(org_path: Path) -> list[dict[str, Any]]:
    """The roles a saved team's config names: what a step's role may be."""
    import yaml

    try:
        org = yaml.safe_load(Path(org_path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    roles = org.get("roles") if isinstance(org, dict) else None
    return [
        {"id": str(role.get("id", "")), "name": str(role.get("name", "") or role.get("id", ""))}
        for role in (roles if isinstance(roles, list) else [])
        if isinstance(role, dict) and str(role.get("id", "")).strip()
    ]


def flow_draft_team(team: str, config_dir: Path | None = None) -> tuple[str, list[dict[str, Any]]]:
    """(team, its roles) for a draft: the team the spec named if it is saved; else the only saved team; else none."""
    from ampuly.core.config import get_ampuly_home
    from ampuly.core.org_config import list_org_config_paths

    import yaml

    paths = list_org_config_paths(Path(config_dir) if config_dir is not None else get_ampuly_home() / "config")
    teams: dict[str, Path] = {}
    for path in paths:
        try:
            org = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        org_id = str((org or {}).get("organization_id") or path.stem.removeprefix("org_").removesuffix("_config")) if isinstance(org, dict) else ""
        if org_id:
            teams[org_id] = path
    named = str(team or "").strip()
    if named and named in teams:
        return named, team_roles(teams[named])
    if not named and len(teams) == 1:
        only = next(iter(teams))
        return only, team_roles(teams[only])
    # A team that is not saved, or several to choose from: the owner chooses on the card; roles stay unassigned.
    return "", []


def flow_draft_library(repo: Path, packages: list[Any], *, registry_root: Path | None = None) -> list[dict[str, Any]]:
    """The internal library as the compiler matches against it.

    Three kinds, and the ``kind`` field is what tells them apart when the compiler walks them in order:

    * ``skill``     -- the internal skill library;
    * ``connector`` -- an API or MCP connector this project has actually registered. ``access_mode`` comes from
      the component catalogue, not from the registration: a registration row does not carry that field.

    A step that matches neither gets the browser universal connector, which is not in this library at all -- it
    is what is left when nothing matched, and the compiler says so by naming its source ``fallback_browser``.
    """
    from ampuly.plugins.office_ui.component_catalog import internal_skills, load_internal_categories

    _categories, filed, _problems = load_internal_categories(Path(repo))
    from ampuly.layer5_memory.skill_library import RETIRED_SKILLS

    # A retired skill is never offered to a new draft (policy); its files stay.
    rows: list[dict[str, Any]] = [
        {**{key: row.get(key, "") for key in ("id", "name", "summary", "category", "version")}, "kind": "skill"}
        for row in internal_skills(Path(repo) / "distribution" / "skills", packages, filed)
        if str(row.get("id", "") or "") not in RETIRED_SKILLS
    ]
    rows.extend(_registered_connectors(Path(repo), registry_root))
    return rows


# The access modes that mean 「there is a real API or MCP behind this」. A catalogue entry whose mode is
# 「browser」 is not one of these: reaching it through a browser is what the fallback already does.
API_ACCESS_MODES = ("existing_mcp", "fastmcp_wrap")


def _registered_connectors(repo: Path, registry_root: Path | None) -> list[dict[str, Any]]:
    """The API/MCP connectors this project has registered, as library rows."""
    if registry_root is None:
        return []
    from loguru import logger

    try:
        from ampuly.plugins.office_ui.connector_candidate import list_registrations

        registered = list_registrations(Path(registry_root))
    except Exception:  # noqa: BLE001 - an unreadable registry means no connectors, not a failed draft
        logger.opt(exception=True).warning("flow draft: the connector registry could not be read")
        return []
    if not registered:
        return []
    try:
        from ampuly.plugins.office_ui.component_catalog import load_catalog

        catalogue = load_catalog(Path(repo)).connectors
    except Exception:  # noqa: BLE001 - no catalogue means no API/MCP entries to prefer over the browser
        logger.opt(exception=True).warning("flow draft: the component catalogue could not be read")
        catalogue = []
    by_id = {str(entry.get("id", "")): entry for entry in catalogue}
    rows: list[dict[str, Any]] = []
    for row in registered:
        connector_id = str(row.get("connector", "") or "")
        entry = by_id.get(connector_id)
        if entry is None:
            continue
        if str(entry.get("access_mode", "") or "") not in API_ACCESS_MODES:
            continue
        rows.append({
            "id": connector_id, "name": str(entry.get("name") or connector_id),
            "summary": str(dict(entry.get("capability") or {}).get("summary", "") or ""),
            "category": str(entry.get("domain", "") or ""), "version": str(row.get("version", "") or ""),
            "kind": "connector", "access_mode": str(entry.get("access_mode", "") or ""),
        })
    return rows
