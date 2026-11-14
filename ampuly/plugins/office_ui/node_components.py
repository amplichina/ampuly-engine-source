"""Component instances inside a workflow node (4.7 Q01, docs/4.7-Q01-FIELD-OWNERSHIP.zh.md).

A node has always named its skills and tools as bare ids (``skills``, ``tools``); the runtime reads only those, and
still does. An *instance* adds what the bare id cannot say: which version this step pinned, and parameters that
belong to this step alone -- editing them never writes back into the component's definition.

Instances live on the node, in ``workflow.yaml`` (``nodes[].components``), not in a second store::

    components:
      - instance: skill-document-summary  # unique within the node
        kind: skill                      # skill | tool
        ref: document-summary              # the component's definition id
        version: 1.2.0                   # pinned at save; empty is filled with the version the package locks
        params: {tone: 简洁}              # this step's own parameters

A node without ``components`` is read as the instances its ``skills`` / ``tools`` imply, and a save writes the key
only when it says more than that (a parameter, an instance name of its own) -- so packages written before this, and
every layout-only save, stay byte-for-byte what they were. ``skills`` / ``tools`` are always derived from the
instances when a node carries them, which is what keeps the runtime unchanged.

A third kind, ``connector`` (Q05), is the browser universal connector (browser_connector.py): it has no bare list of
its own -- its tools are granted at projection, never written into ``tools`` -- so a save that sends only ``skills`` /
``tools`` keeps it as stored, parameters and all.

Skill and tool parameters reach the running step as words (4.7-A16): projection copies them onto the role's node
table (``component_params``, see runtime_params), the task keeps them in ``workflow_node``, and the step's runtime
contract states one line per component (node_contract.step_settings_lines). Anything that could be a credential is
left out at both doors. They are not injected into a tool call's arguments.

Not yet: a tool ref is not checked against a registry, only a skill ref is (against what the package declares) and a
connector ref (against the console).
"""

from __future__ import annotations

import json
import re
from typing import Any

# "desktop" (4.7-A06): the desktop connector (desktop_connector.py), which, like the browser one, has no bare list.
COMPONENT_KINDS = ("skill", "tool", "connector", "desktop")
# The kinds a node's bare lists name; a connector has none (its instance is the only record of it).
_LISTED_KINDS = ("skill", "tool")
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
MAX_PARAMS_BYTES = 4096


def default_instance(kind: str, ref: str) -> str:
    return f"{kind}-{ref}"


def derived_components(node: dict[str, Any], skill_versions: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The instances a node's bare ``skills`` / ``tools`` imply: default names, the locked version, no parameters."""
    versions = dict(skill_versions or {})
    out: list[dict[str, Any]] = []
    for kind, key in (("skill", "skills"), ("tool", "tools")):
        seen: set[str] = set()
        for ref in [str(item or "").strip() for item in list(node.get(key, []) or [])]:
            if not ref or ref in seen:
                continue
            seen.add(ref)
            out.append({
                "instance": default_instance(kind, ref),
                "kind": kind,
                "ref": ref,
                "version": str(versions.get(ref, "") or "") if kind == "skill" else "",
                "params": {},
            })
    return out


def _secret_shaped(key: str, value: Any) -> bool:
    from ampuly.cli.distribution import _secret_shaped as shaped

    return shaped(key, value)


def _params_findings(prefix: str, params: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(params, dict):
        for key, value in params.items():
            if not isinstance(key, str) or not key.strip():
                found.append(f"{prefix}: parameter names must be non-empty text")
                continue
            here = f"{path}.{key}" if path else key
            if isinstance(value, (dict, list)):
                found.extend(_params_findings(prefix, value, here))
            elif _secret_shaped(key, value):
                found.append(f"{prefix}: parameter {here} looks like a secret; a step's parameters are stored in the package, "
                             "so a credential goes in through an approved environment reference instead")
    elif isinstance(params, list):
        for index, value in enumerate(params):
            found.extend(_params_findings(prefix, value, f"{path}[{index}]"))
    return found


def normalize_components(node_id: str, raw: Any) -> list[dict[str, Any]]:
    """The instances a save sent, in the stored shape; refuses (ValueError) what cannot be stored as sent."""
    if not isinstance(raw, list):
        raise ValueError(f"node {node_id}: components must be a list")
    out: list[dict[str, Any]] = []
    names: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    problems: list[str] = []
    for index, item in enumerate(raw):
        prefix = f"node {node_id} component {index + 1}"
        if not isinstance(item, dict):
            problems.append(f"{prefix}: must be an object")
            continue
        kind = str(item.get("kind", "") or "").strip()
        ref = str(item.get("ref", "") or "").strip()
        if kind not in COMPONENT_KINDS:
            problems.append(f"{prefix}: kind must be one of {', '.join(COMPONENT_KINDS)}")
            continue
        if not ref:
            problems.append(f"{prefix}: ref is required")
            continue
        instance = str(item.get("instance", "") or "").strip() or default_instance(kind, ref)
        if not _INSTANCE_RE.match(instance):
            problems.append(f"{prefix}: instance {instance!r} is not a usable name")
            continue
        if instance in names:
            problems.append(f"{prefix}: instance {instance!r} is used twice in this step")
            continue
        if (kind, ref) in pairs:
            # skills / tools are derived from the instances and name each component once; two instances of one
            # component in one step would have nowhere to go in the runtime's view.
            problems.append(f"{prefix}: {kind} {ref!r} is already in this step")
            continue
        params = item.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            problems.append(f"{prefix}: params must be an object")
            continue
        try:
            size = len(json.dumps(params, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            problems.append(f"{prefix}: params must be plain data")
            continue
        if size > MAX_PARAMS_BYTES:
            problems.append(f"{prefix}: params are {size} bytes, more than {MAX_PARAMS_BYTES}")
            continue
        secret = _params_findings(prefix, params)
        if secret:
            problems.extend(secret)
            continue
        names.add(instance)
        pairs.add((kind, ref))
        out.append({
            "instance": instance,
            "kind": kind,
            "ref": ref,
            "version": str(item.get("version", "") or "").strip() if kind in ("skill", "connector", "desktop") else "",
            "params": params,
        })
    if problems:
        raise ValueError("; ".join(problems))
    return out


def runtime_params(node: dict[str, Any]) -> list[dict[str, Any]]:
    """What a step's own skill / tool parameters give its run (``workflow_node.component_params``).

    Only instances that set a parameter; a connector's parameters travel as ``workflow_node.connector`` instead.
    A parameter that could be a credential never leaves the package this way (node_contract.param_carries_secret),
    and a parameter object too large to have been saved is not carried at all.
    """
    from ampuly.layer2_organization.node_contract import param_carries_secret

    out: list[dict[str, Any]] = []
    for item in list(node.get("components", []) or []):
        if not isinstance(item, dict) or str(item.get("kind", "") or "") not in _LISTED_KINDS:
            continue
        ref = str(item.get("ref", "") or "").strip()
        params = item.get("params")
        if not ref or not isinstance(params, dict):
            continue
        kept = {key: value for key, value in params.items()
                if isinstance(key, str) and key.strip() and not param_carries_secret(key, value)}
        try:
            size = len(json.dumps(kept, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            continue
        if kept and size <= MAX_PARAMS_BYTES:
            out.append({"kind": str(item["kind"]), "ref": ref, "params": kept})
    return out


def _refs(components: list[dict[str, Any]], kind: str) -> list[str]:
    return [item["ref"] for item in components if item["kind"] == kind]


def reconcile_components(existing: dict[str, Any] | None, draft: dict[str, Any], merged: dict[str, Any]) -> None:
    """Settle ``merged`` (existing overlaid with the draft) so its instances and its bare lists agree.

    Which side speaks: a draft whose ``components`` differ from what the node had (stored, or implied) is an edit
    of the instances, and ``skills`` / ``tools`` follow them. Otherwise the draft's ``skills`` / ``tools`` are the
    edit (the canvas that knows only those sends them), and stored instances follow: ones whose component left the
    step go, new components come in with default names, and the parameters of the rest are kept.
    """
    node_id = str(merged.get("id", "") or "")
    before = dict(existing or {})
    stored = normalize_components(node_id, before["components"]) if isinstance(before.get("components"), list) else None
    had = stored if stored is not None else derived_components(before)
    if "components" in draft and draft.get("components") is not None:
        sent = normalize_components(node_id, draft.get("components"))
        if _comparable(sent) != _comparable(had):
            merged["components"] = sent
            merged["skills"] = _refs(sent, "skill")
            merged["tools"] = _refs(sent, "tool")
            return
    if stored is None:
        merged.pop("components", None)
        return
    # Kept in their stored order (the order is part of what was saved), newcomers after them. A connector is not in
    # the bare lists at all, so a draft that sends only those says nothing about it: it stays.
    wanted = derived_components(merged)
    wanted_keys = {(item["kind"], item["ref"]) for item in wanted}
    kept = [item for item in stored if item["kind"] not in _LISTED_KINDS or (item["kind"], item["ref"]) in wanted_keys]
    kept_keys = {(item["kind"], item["ref"]) for item in kept}
    merged["components"] = [*kept, *[item for item in wanted if (item["kind"], item["ref"]) not in kept_keys]]
    # 4.7 A02: the builder reorders a step's skills (or its tools) through the bare list (上移 / 下移). When the draft's
    # order of a kind differs from the one the step had, that kind's instances take the draft's order, each kept whole;
    # the other kinds keep their places, so skills / tools stay what the instances say. A draft that sends the order the
    # step already had moves nothing here, and an echo save stays what it was.
    before_order = derived_components(before)
    for kind in _LISTED_KINDS:
        order = _refs(wanted, kind)
        if order == _refs(before_order, kind):
            continue
        slots = [index for index, item in enumerate(merged["components"]) if item["kind"] == kind]
        ranked = sorted((merged["components"][index] for index in slots), key=lambda item: order.index(item["ref"]))
        for index, item in zip(slots, ranked):
            merged["components"][index] = item


def _comparable(components: list[dict[str, Any]]) -> list[tuple[str, str, str, str]]:
    # A version left empty is the version the package locks: the same instance, not an edit.
    return [(item["instance"], item["kind"], item["ref"], json.dumps(item["params"], sort_keys=True, ensure_ascii=False))
            for item in components]


def settle_versions(manifest: dict[str, Any]) -> list[str]:
    """After the save has declared its library skills: pin each skill instance and drop what says nothing new.

    Returns the problems that refuse the save: a skill the package does not declare (an unknown component), or a
    pinned version the package cannot give that step.
    """
    declared = {str(skill).strip() for skill in list(manifest.get("skills", []) or []) if str(skill).strip()}
    versions = dict(manifest.get("skill_versions", {}) or {})
    problems: list[str] = []
    for node in list(manifest.get("nodes", []) or []):
        if not isinstance(node, dict) or not isinstance(node.get("components"), list):
            continue
        node_id = str(node.get("id", "") or "")
        for item in node["components"]:
            if item["kind"] == "connector":
                from ampuly.plugins.office_ui.browser_connector import CONNECTOR_VERSION

                # A built-in: pinned to the one version this console has (a different one is a finding, not a pin).
                if not item["version"]:
                    item["version"] = CONNECTOR_VERSION
                continue
            if item["kind"] == "desktop":
                from ampuly.plugins.office_ui.desktop_connector import DESKTOP_VERSION

                if not item["version"]:
                    item["version"] = DESKTOP_VERSION
                continue
            if item["kind"] != "skill":
                continue
            ref = item["ref"]
            if ref not in declared:
                problems.append(f"node {node_id}: component {ref!r} is not a skill this package declares or the library holds")
                continue
            locked = str(versions.get(ref, "") or "")
            if not item["version"]:
                item["version"] = locked
            elif item["version"] != locked:
                problems.append(
                    f"node {node_id}: component {ref!r} version {item['version']} is not available "
                    f"(the package locks {locked or 'no version'})"
                )
        # Nothing beyond the bare lists: default names, no parameters (versions are the locked ones by now).
        if not problems and all(
            item["kind"] in _LISTED_KINDS and item["instance"] == default_instance(item["kind"], item["ref"]) and not item["params"]
            for item in node["components"]
        ):
            node.pop("components", None)
    return problems
