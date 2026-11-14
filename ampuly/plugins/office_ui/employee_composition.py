"""C-package config composition: three layers, typed overlays, one identity.

Pure contract, no storage and no execution. Inputs are the frozen employee
pin (published package or an explicit isolated-trial draft snapshot), the
workflow pin, the project instance revision and typed overlays. Output is a
composition record whose ``composition_id`` binds every input, the effective
config digest and the canonicalisation rule version.

Security rules (V2 spec C, T5/T6/T8):

* Overlays may only set whitelisted context keys (datasets, language, output
  format, approved destinations) and add restrictions. Unknown, ambiguous or
  conflicting keys refuse assembly; there is no last-writer-wins.
* Capability = declared requirement AND current host capability AND every
  overlay restriction. An overlay never grants, widens or touches the veto.
* An empty or missing overlay never means full rights.
* Removing an overlay yields a new ``pending_review`` composition; widened
  actions stay disabled until an approval binds that exact composition.
* A fixed composition is not frozen authorization: ``authorize_action``
  re-checks the live host capability, live restrictions and the veto.
* Secrets exist only as controlled refs. Literal secret values refuse
  assembly and never enter hash input, snapshots, logs or diffs.
"""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Callable, Iterable, Mapping

from .ai_employee_contract import AIEmployeeContractError, _secret_shaped, canonical_json
from .ai_employee_instance_contract import effective_limits

RULES_VERSION = "c-composition-v1"
SCHEMA_VERSION = 1
LAYERS = ("workflow", "project")
CONTEXT_KEYS = frozenset({"knowledge_refs", "language", "output_format", "destinations"})
RESTRICTION_KEYS = frozenset({"limits", "allow_capabilities", "deny_capabilities", "block_effect_kinds"})
OVERLAY_KEYS = frozenset({"schema_version", "overlay_id", "layer", "scope", "revision", "source",
                          "context", "restrictions", "note"})
SOURCE_KINDS = frozenset({"owner_form", "template", "conversation_draft", "legacy_import"})
LANGUAGES = frozenset({"zh-CN", "zh-TW", "en-US", "ja-JP"})
EFFECT_KINDS = frozenset({"read", "write", "external_send", "paid"})
# Keys that try to grant, relax or switch off a guard get a precise refusal.
_GRANT_HINT = re.compile(r"(grant|allow_all|full_access|unrestricted|capabilit(y|ies)_requirements|"
                         r"capability_impls|connector_refs|credential|secret|token|password|api_key)", re.I)
_VETO_HINT = re.compile(r"(veto|hard_fail|approval|review_required|human_review|skip_review)", re.I)
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CAP = re.compile(r"[A-Za-z0-9_.-]{1,128}(:[A-Za-z0-9_.-]{1,128})?\Z")


def _err(code: str, message: str, location: str = "", **details: Any) -> AIEmployeeContractError:
    return AIEmployeeContractError(code, message, location, details=details or None)


def sha256_canonical(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _secret_paths(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            here = f"{path}.{key}" if path else str(key)
            if isinstance(child, (dict, list)):
                found.extend(_secret_paths(child, here))
            elif key != "credential_ref" and _secret_shaped(str(key), child):
                found.append(here)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            here = f"{path}[{index}]"
            if isinstance(child, (dict, list)):
                found.extend(_secret_paths(child, here))
            elif _secret_shaped("", child):
                found.append(here)
    return found


def _reject_secrets(value: Any, where: str) -> None:
    paths = _secret_paths(value)
    if paths:
        # Only the paths are reported; never the value.
        raise _err("composition_secret_literal", "配置里出现了疑似密钥原文，只允许受控引用。", where, paths=paths)


# ---------------------------------------------------------------- overlays

def _strings(value: Any, where: str, pattern: re.Pattern[str] = _ID) -> list[str]:
    if type(value) is not list or len(value) > 200 or any(type(v) is not str or not pattern.fullmatch(v) for v in value):
        raise _err("overlay_value_invalid", "覆盖值必须是有限的标识符列表。", where)
    if len(set(value)) != len(value):
        raise _err("overlay_value_ambiguous", "覆盖值重复，含义不明确。", where)
    return list(value)


def validate_overlay(overlay: Any, *, project_id: str, workflow_id: str) -> dict[str, Any]:
    """Return a canonical copy of a typed overlay or raise; never coerces."""
    if type(overlay) is not dict:
        raise _err("overlay_invalid", "覆盖必须是对象。")
    unknown = set(overlay) - OVERLAY_KEYS
    if unknown:
        raise _err("overlay_key_unknown", "覆盖含未列入白名单的字段。", "overlay", keys=sorted(unknown))
    _reject_secrets(overlay, "overlay")
    if overlay.get("schema_version") != SCHEMA_VERSION:
        raise _err("overlay_schema_unsupported", "覆盖结构版本不受支持。", "overlay.schema_version")
    if type(overlay.get("overlay_id")) is not str or not _ID.fullmatch(overlay["overlay_id"]):
        raise _err("overlay_invalid", "覆盖缺少有效标识。", "overlay.overlay_id")
    layer = overlay.get("layer")
    if layer not in LAYERS:
        raise _err("overlay_layer_invalid", "覆盖只能来自业务流或项目上下文层。", "overlay.layer")
    revision = overlay.get("revision")
    if type(revision) is not int or revision < 1:
        raise _err("overlay_invalid", "覆盖修订号必须是正整数。", "overlay.revision")
    scope = overlay.get("scope")
    if type(scope) is not dict or set(scope) - {"project_id", "workflow_id"} or scope.get("project_id") != project_id:
        raise _err("overlay_scope_denied", "覆盖不属于当前项目，不能跨项目装配。", "overlay.scope")
    if layer == "workflow" and scope.get("workflow_id") != workflow_id:
        raise _err("overlay_scope_denied", "业务流覆盖不属于当前业务流。", "overlay.scope.workflow_id")
    source = overlay.get("source")
    if (type(source) is not dict or set(source) - {"kind", "actor", "ref"} or source.get("kind") not in SOURCE_KINDS
            or type(source.get("actor")) is not str or not source["actor"].strip()):
        raise _err("overlay_source_missing", "覆盖必须写明来源与修改人。", "overlay.source")
    context = overlay.get("context", {})
    restrictions = overlay.get("restrictions", {})
    for name, block, allowed in (("context", context, CONTEXT_KEYS), ("restrictions", restrictions, RESTRICTION_KEYS)):
        if type(block) is not dict:
            raise _err("overlay_invalid", "覆盖分组必须是对象。", f"overlay.{name}")
        for key in block:
            if key in allowed:
                continue
            if _VETO_HINT.search(str(key)):
                raise _err("overlay_cannot_relax_veto", "覆盖不能关闭硬否决、审批或复核。", f"overlay.{name}.{key}")
            if _GRANT_HINT.search(str(key)):
                raise _err("overlay_cannot_grant", "覆盖不能授予能力、凭据或连接器。", f"overlay.{name}.{key}")
            raise _err("overlay_key_unknown", "覆盖含未列入白名单的字段。", f"overlay.{name}.{key}")
    if "knowledge_refs" in context:
        refs = context["knowledge_refs"]
        if type(refs) is not list or len(refs) > 200:
            raise _err("overlay_value_invalid", "资料集必须是列表。", "overlay.context.knowledge_refs")
        seen = set()
        for i, ref in enumerate(refs):
            if (type(ref) is not dict or set(ref) != {"kind", "ref", "version", "access_scope"}
                    or any(type(ref[k]) is not str or not ref[k] for k in ref)):
                raise _err("overlay_value_invalid", "资料引用结构无效。", f"overlay.context.knowledge_refs[{i}]")
            key = (ref["ref"], ref["version"])
            if key in seen:
                raise _err("overlay_value_ambiguous", "资料引用重复。", f"overlay.context.knowledge_refs[{i}]")
            seen.add(key)
    if "language" in context and context["language"] not in LANGUAGES:
        raise _err("overlay_value_invalid", "语言不在允许范围。", "overlay.context.language")
    if "output_format" in context and (type(context["output_format"]) is not str or not _ID.fullmatch(context["output_format"])):
        raise _err("overlay_value_invalid", "输出格式无效。", "overlay.context.output_format")
    if "destinations" in context:
        _strings(context["destinations"], "overlay.context.destinations")
    if "limits" in restrictions:
        effective_limits(restrictions["limits"])  # structure only
    for key in ("allow_capabilities", "deny_capabilities"):
        if key in restrictions:
            _strings(restrictions[key], f"overlay.restrictions.{key}", _CAP)
    if "block_effect_kinds" in restrictions:
        kinds = _strings(restrictions["block_effect_kinds"], "overlay.restrictions.block_effect_kinds")
        if set(kinds) - EFFECT_KINDS:
            raise _err("overlay_value_invalid", "效果类型不在允许范围。", "overlay.restrictions.block_effect_kinds")
    clean = copy.deepcopy(overlay)
    clean.setdefault("context", {})
    clean.setdefault("restrictions", {})
    return clean


def overlay_hash(overlay: dict[str, Any]) -> str:
    return sha256_canonical({"rules_version": RULES_VERSION, "overlay": overlay})


# ---------------------------------------------------------------- pins

def _employee_pin(pin: Any, purpose: str) -> dict[str, Any]:
    if type(pin) is not dict or not pin:
        raise _err("composition_pin_missing", "缺少员工固定版本，拒绝装配，不会回退到最新。", "employee_pin")
    status = pin.get("status")
    if purpose == "formal":
        if status != "published" or pin.get("freeze_source") != "employee_packages_published" or pin.get("draft_digest"):
            raise _err("composition_draft_pin_refused", "正式装配只接受企业已发布的员工固定版本。", "employee_pin")
    elif status not in {"published", "draft_snapshot"}:
        raise _err("composition_pin_invalid", "隔离试运行只接受已发布版本或明确的草稿快照。", "employee_pin")
    if type(pin.get("package_id")) is not str or type(pin.get("version")) is not str or not pin["version"]:
        raise _err("composition_pin_missing", "员工固定版本缺少包标识或版本。", "employee_pin")
    if type(pin.get("package_fingerprint")) is not str or not _SHA.fullmatch(pin["package_fingerprint"]):
        raise _err("composition_pin_missing", "员工固定版本缺少指纹。", "employee_pin.package_fingerprint")
    keys = ("freeze_source", "package_id", "version", "package_fingerprint", "status", "snapshot_fingerprint")
    return {k: pin[k] for k in keys if k in pin}


def _workflow_pin(pin: Any, purpose: str, workflow_id: str) -> dict[str, Any]:
    if type(pin) is not dict or not pin:
        raise _err("composition_pin_missing", "缺少业务流固定版本，拒绝装配，不会回退到最新。", "workflow_pin")
    if pin.get("workflow_id") != workflow_id:
        raise _err("composition_pin_invalid", "业务流固定版本不属于当前业务流。", "workflow_pin.workflow_id")
    if purpose == "formal" and pin.get("status") != "published":
        raise _err("composition_draft_pin_refused", "正式装配只接受已发布的业务流版本。", "workflow_pin")
    if purpose != "formal" and pin.get("status") not in {"published", "draft_snapshot"}:
        raise _err("composition_pin_invalid", "隔离试运行只接受已发布版本或明确的草稿快照。", "workflow_pin")
    if type(pin.get("version")) is not str or not pin["version"] or type(pin.get("release_fingerprint")) is not str \
            or not _SHA.fullmatch(pin["release_fingerprint"]):
        raise _err("composition_pin_missing", "业务流固定版本缺少版本或发布指纹。", "workflow_pin")
    return {k: pin[k] for k in ("workflow_id", "version", "release_fingerprint", "status") if k in pin}


# ---------------------------------------------------------------- assembly

def _host_caps(host_capabilities: Any) -> dict[str, frozenset[str]]:
    if not isinstance(host_capabilities, Mapping):
        raise _err("composition_host_unknown", "宿主能力清单缺失，按无权限处理。", "host_capabilities")
    return {str(k): frozenset(v) for k, v in host_capabilities.items()}


def _cap_matches(entry: str, tool: str, action: str) -> bool:
    return entry == tool or entry == f"{tool}:{action}"


def _capabilities(manifest: dict[str, Any], host: dict[str, frozenset[str]], overlays: list[dict[str, Any]]):
    effective: dict[str, list[str]] = {}
    gaps: list[str] = []
    effect_of: dict[str, str] = {}
    for req in manifest.get("capability_requirements") or []:
        tool = req.get("tool")
        kind = req.get("effect_kind", "write")
        for action in req.get("actions") or []:
            name = f"{tool}:{action}"
            effect_of[name] = kind
            if action not in host.get(tool, frozenset()):
                gaps.append(name)  # need, not grant
                continue
            allowed = True
            for ov in overlays:
                r = ov["restrictions"]
                if "allow_capabilities" in r and not any(_cap_matches(e, tool, action) for e in r["allow_capabilities"]):
                    allowed = False
                if any(_cap_matches(e, tool, action) for e in r.get("deny_capabilities", [])):
                    allowed = False
                if kind in r.get("block_effect_kinds", []):
                    allowed = False
            if allowed:
                effective.setdefault(tool, []).append(action)
    return {t: sorted(a) for t, a in sorted(effective.items())}, sorted(gaps), effect_of


def _single_context(overlays: list[dict[str, Any]], key: str) -> tuple[Any, str | None]:
    values = [(ov["context"][key], ov["layer"]) for ov in overlays if key in ov["context"]]
    if not values:
        return None, None
    first = values[0][0]
    if any(canonical_json(v) != canonical_json(first) for v, _ in values[1:]):
        raise _err("overlay_conflict", "多层覆盖给出不同取值，拒绝装配（不按最后写入）。", f"context.{key}",
                   layers=[layer for _, layer in values])
    return copy.deepcopy(first), values[0][1]


def assemble_composition(*, project_id: str, workflow_id: str, node_id: str, purpose: str,
                         employee_pin: Any, workflow_pin: Any, manifest: dict[str, Any],
                         instance: dict[str, Any] | None, overlays: Iterable[Any],
                         host_capabilities: Any, approved_knowledge: Iterable[dict[str, Any]] = (),
                         approved_destinations: Iterable[str] = (), output_formats: Iterable[str] = (),
                         required_layers: Iterable[str] = (), runtime: dict[str, Any] | None = None,
                         trial_authorization: dict[str, Any] | None = None,
                         previous: dict[str, Any] | None = None) -> dict[str, Any]:
    if purpose not in {"formal", "isolated_trial"}:
        raise _err("request_invalid", "装配用途只能是正式运行或隔离试运行。", "purpose")
    if purpose == "isolated_trial" and (type(trial_authorization) is not dict or trial_authorization.get("scope") != "isolated"):
        raise _err("composition_trial_unauthorized", "隔离试运行需要单独的试运行授权。", "trial_authorization")
    for value, name in ((project_id, "project_id"), (workflow_id, "workflow_id"), (node_id, "node_id")):
        if type(value) is not str or not _ID.fullmatch(value):
            raise _err("request_invalid", "装配范围无效。", name)
    emp = _employee_pin(employee_pin, purpose)
    wf = _workflow_pin(workflow_pin, purpose, workflow_id)
    if type(manifest) is not dict or manifest.get("package_fingerprint") != emp["package_fingerprint"]:
        raise _err("composition_pin_mismatch", "员工配置内容与固定指纹不一致。", "manifest")
    profile = manifest.get("evaluation_profile") or {}
    if profile.get("hard_fail_veto") is not True:
        raise _err("composition_veto_missing", "员工配置缺少硬否决，拒绝装配。", "manifest.evaluation_profile.hard_fail_veto")
    host = _host_caps(host_capabilities)
    clean = [validate_overlay(o, project_id=project_id, workflow_id=workflow_id) for o in overlays]
    seen_layers = [o["layer"] for o in clean]
    if len(set(seen_layers)) != len(seen_layers):
        raise _err("overlay_conflict", "同一层只能有一个生效覆盖。", "overlays")
    missing = sorted(set(required_layers) - set(seen_layers))
    if missing:
        raise _err("composition_overlay_missing", "缺少必需的覆盖层，拒绝装配（不回退到宽松配置）。", "overlays", layers=missing)
    inst = None
    if instance is not None:
        if instance.get("project_id") != project_id:
            raise _err("composition_scope_denied", "员工在本项目的上下文不属于当前项目。", "instance.project_id")
        if instance.get("package_fingerprint") != emp["package_fingerprint"]:
            raise _err("composition_pin_mismatch", "项目上下文绑定的员工版本与固定版本不一致。", "instance")
        _reject_secrets({k: v for k, v in instance.items() if k != "revision_fingerprint"}, "instance")
        inst = instance
    # Context: whitelisted, scoped to approved sources of this project.
    approved_k = {(r.get("ref"), r.get("version")) for r in approved_knowledge}
    knowledge, k_layer = _single_context(clean, "knowledge_refs")
    if knowledge is None:
        knowledge = copy.deepcopy((inst or {}).get("knowledge_refs") or [])
        k_layer = "project_instance" if knowledge else None
    else:
        for i, ref in enumerate(knowledge):
            if ref["access_scope"] not in {project_id, "project", "enterprise_shared"} or (ref["ref"], ref["version"]) not in approved_k:
                raise _err("overlay_scope_denied", "资料集不在本项目批准范围内。", f"context.knowledge_refs[{i}]")
    language, l_layer = _single_context(clean, "language")
    fmt, f_layer = _single_context(clean, "output_format")
    if fmt is not None and fmt not in set(output_formats):
        raise _err("overlay_scope_denied", "输出格式不在员工允许范围内。", "context.output_format")
    dests, d_layer = _single_context(clean, "destinations")
    approved_d = set(approved_destinations)
    if dests is not None and set(dests) - approved_d:
        raise _err("overlay_scope_denied", "交付目的地不在本项目已批准列表中。", "context.destinations",
                   refused=sorted(set(dests) - approved_d))
    caps, gaps, effect_of = _capabilities(manifest, host, clean)
    limit_layers = [manifest.get("loop_policy") or {}]
    if inst and inst.get("limits"):
        limit_layers.append(inst["limits"])
    limit_layers.extend(o["restrictions"]["limits"] for o in clean if "limits" in o["restrictions"])
    base_limits = {k: v for k, v in (manifest.get("loop_policy") or {}).items()}
    limits = effective_limits(*[{k: v for k, v in layer.items() if k in base_limits or k == "max_cost_cny"} for layer in limit_layers])
    secret_refs = sorted(
        ({"connector_id": c.get("connector_id"), "credential_ref": c.get("credential_ref")}
         for c in ((inst or {}).get("connector_refs") or []) if c.get("credential_ref")),
        key=lambda r: (str(r["connector_id"]), str(r["credential_ref"])))
    effective = {
        "knowledge_refs": knowledge,
        "language": language,
        "output_format": fmt,
        "destinations": sorted(dests) if dests is not None else [],
        "capabilities": caps,
        "capability_gaps": gaps,
        "limits": limits,
        "hard_fail_veto": True,
        "secret_refs": secret_refs,
    }
    sources = {"knowledge_refs": k_layer, "language": l_layer, "output_format": f_layer, "destinations": d_layer,
               "capabilities": "intersection(employee,host,overlays)", "limits": "minimum(employee,project,overlays)",
               "hard_fail_veto": "employee_locked"}
    effective_digest = sha256_canonical({"rules_version": RULES_VERSION, "project_id": project_id, "effective": effective})
    identity = {
        "rules_version": RULES_VERSION,
        "schema_version": SCHEMA_VERSION,
        "purpose": purpose,
        "project_id": project_id,
        "workflow_id": workflow_id,
        "node_id": node_id,
        "employee_pin": emp,
        "workflow_pin": wf,
        "instance": None if inst is None else {k: inst.get(k) for k in ("instance_id", "instance_revision", "revision_fingerprint")},
        "overlays": [{"layer": o["layer"], "overlay_id": o["overlay_id"], "revision": o["revision"], "hash": overlay_hash(o)}
                     for o in sorted(clean, key=lambda o: o["layer"])],
        "effective_config_digest": effective_digest,
        "runtime": copy.deepcopy(runtime or {"engine_schema": 1}),
    }
    record = {
        **identity,
        "composition_id": sha256_canonical(identity),
        "effective": effective,
        "sources": sources,
        "effect_kinds": dict(sorted(effect_of.items())),
        "review_state": "pending_review",
        "disabled_actions": [],
        "lost_restrictions": [],
        "approved": False,
    }
    if previous is not None:
        widened = _widened(previous, record)
        record["disabled_actions"] = widened
    return record


# ---------------------------------------------------------------- diffs / removal

def _flat_caps(record: dict[str, Any]) -> set[str]:
    return {f"{t}:{a}" for t, acts in record["effective"]["capabilities"].items() for a in acts}


def _widened(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    out = set(_flat_caps(new) - _flat_caps(old))
    od, nd = set(old["effective"]["destinations"]), set(new["effective"]["destinations"])
    out |= {f"destination:{d}" for d in nd - od}
    ok = {(r["ref"], r["version"]) for r in old["effective"]["knowledge_refs"]}
    out |= {f"knowledge:{r['ref']}@{r['version']}" for r in new["effective"]["knowledge_refs"] if (r["ref"], r["version"]) not in ok}
    for key, value in new["effective"]["limits"].items():
        before = old["effective"]["limits"].get(key)
        if before is None or (str(value) != str(before) and (float(value) > float(before))):
            out.add(f"limit:{key}")
    for key in old["effective"]["limits"]:
        if key not in new["effective"]["limits"]:
            out.add(f"limit:{key}")
    return sorted(out)


def composition_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Raw (what layers changed) and effective (what actually changes) diff.

    Secret refs appear only as refs; values never exist here.
    """
    old_ov = {o["layer"]: o for o in old["overlays"]}
    new_ov = {o["layer"]: o for o in new["overlays"]}
    raw = {
        "employee_pin": None if old["employee_pin"] == new["employee_pin"] else {"before": old["employee_pin"], "after": new["employee_pin"]},
        "workflow_pin": None if old["workflow_pin"] == new["workflow_pin"] else {"before": old["workflow_pin"], "after": new["workflow_pin"]},
        "instance": None if old["instance"] == new["instance"] else {"before": old["instance"], "after": new["instance"]},
        "overlays_added": sorted(set(new_ov) - set(old_ov)),
        "overlays_removed": sorted(set(old_ov) - set(new_ov)),
        "overlays_changed": sorted(k for k in set(old_ov) & set(new_ov) if old_ov[k] != new_ov[k]),
    }
    effective: dict[str, Any] = {}
    for key in ("knowledge_refs", "language", "output_format", "destinations", "limits", "secret_refs", "capability_gaps"):
        if canonical_json(old["effective"][key]) != canonical_json(new["effective"][key]):
            effective[key] = {"before": old["effective"][key], "after": new["effective"][key]}
    oc, nc = _flat_caps(old), _flat_caps(new)
    if oc != nc:
        effective["capabilities"] = {"added": sorted(nc - oc), "removed": sorted(oc - nc)}
    return {"from": old["composition_id"], "to": new["composition_id"], "raw": raw, "effective": effective,
            "widened": _widened(old, new), "same_identity": old["composition_id"] == new["composition_id"]}


def lost_restrictions(old_overlays: Iterable[dict[str, Any]], removed_layer: str) -> list[dict[str, Any]]:
    out = []
    for ov in old_overlays:
        if ov["layer"] != removed_layer:
            continue
        for key, value in sorted(ov.get("restrictions", {}).items()):
            out.append({"layer": removed_layer, "key": key, "value": copy.deepcopy(value)})
        for key, value in sorted(ov.get("context", {}).items()):
            out.append({"layer": removed_layer, "key": f"context.{key}", "value": copy.deepcopy(value)})
    return out


def remove_overlay(previous: dict[str, Any], inputs: dict[str, Any], layer: str) -> dict[str, Any]:
    """Assemble a new pending composition without ``layer``; widened actions stay disabled."""
    if layer not in LAYERS:
        raise _err("overlay_layer_invalid", "只能移除业务流或项目上下文覆盖。", "layer")
    old_overlays = list(inputs.get("overlays") or [])
    if not any(o.get("layer") == layer for o in old_overlays):
        raise _err("overlay_not_found", "该层没有可移除的覆盖。", "layer")
    kept = [o for o in old_overlays if o.get("layer") != layer]
    required = [r for r in inputs.get("required_layers", ()) if r != layer]
    new = assemble_composition(**{**inputs, "overlays": kept, "required_layers": required, "previous": previous})
    new["lost_restrictions"] = lost_restrictions(old_overlays, layer)
    new["review_state"] = "pending_review"
    new["removal_of"] = {"layer": layer, "from_composition": previous["composition_id"]}
    return new


# ---------------------------------------------------------------- approval / runtime

def approve_composition(record: dict[str, Any], *, actor: str, evidence_ref: str,
                        enable_actions: Iterable[str] = ()) -> dict[str, Any]:
    """Approval binds this exact composition; it is not a grant."""
    if type(actor) is not str or not actor.strip() or type(evidence_ref) is not str or not evidence_ref.strip():
        raise _err("composition_approval_invalid", "批准必须写明批准人和审阅依据。")
    enable = sorted(set(enable_actions))
    if set(enable) - set(record.get("disabled_actions", [])):
        raise _err("composition_approval_invalid", "只能启用本次组合里待批准的放宽项。")
    return {"composition_id": record["composition_id"], "project_id": record["project_id"],
            "effective_config_digest": record["effective_config_digest"], "rules_version": record["rules_version"],
            "actor": actor, "evidence_ref": evidence_ref, "enabled_actions": enable, "grants": False}


def check_approval(approval: Any, record: dict[str, Any]) -> None:
    if type(approval) is not dict:
        raise _err("composition_approval_missing", "这个组合还没有批准。")
    for key in ("composition_id", "project_id", "effective_config_digest", "rules_version"):
        if approval.get(key) != record.get(key):
            raise _err("composition_approval_stale", "批准对应的是另一组配置，需要重新审阅。", key)


def authorize_action(record: dict[str, Any], tool: str, action: str, *, current_host_capabilities: Any,
                     current_restrictions: Iterable[dict[str, Any]] = (), veto_failed: bool = False,
                     approval: dict[str, Any] | None = None) -> dict[str, Any]:
    """Re-check every action at run time; a fixed config is not frozen authorization."""
    name = f"{tool}:{action}"
    if veto_failed:
        raise _err("hard_fail_veto", "硬否决已触发，动作被拦下。", name)
    if action not in record["effective"]["capabilities"].get(tool, []):
        raise _err("capability_not_in_composition", "这个动作不在本次已审阅的组合里。", name)
    if name in record.get("disabled_actions", []):
        if approval is None or name not in approval.get("enabled_actions", []):
            raise _err("composition_widening_unapproved", "放宽项尚未针对本组合审批，保持禁用。", name)
    if approval is not None:
        check_approval(approval, record)
    live = _host_caps(current_host_capabilities)
    if action not in live.get(tool, frozenset()):
        raise _err("capability_revoked", "宿主授权已收回，动作在执行前被拦下。", name)
    for r in current_restrictions:
        if any(_cap_matches(e, tool, action) for e in r.get("deny_capabilities", [])) or \
                record.get("effect_kinds", {}).get(name) in r.get("block_effect_kinds", []):
            raise _err("capability_restricted", "当前限制禁止该动作。", name)
    return {"allowed": True, "action": name, "composition_id": record["composition_id"]}


def resolve_secret(record: dict[str, Any], credential_ref: str, *, resolver: Callable[[str], str],
                   ref_grant_current: Callable[[str], bool]) -> str:
    """Resolve a controlled ref at use time only; the value is never stored back."""
    refs = {r["credential_ref"] for r in record["effective"]["secret_refs"]}
    if credential_ref not in refs:
        raise _err("secret_ref_not_in_composition", "凭据引用不在本次组合里。")
    if not ref_grant_current(credential_ref):
        raise _err("secret_unresolvable", "凭据引用当前不可解析或权限已变化，动作被拦下。")
    try:
        value = resolver(credential_ref)
    except Exception:  # resolver details may carry secret text
        raise _err("secret_unresolvable", "凭据引用当前不可解析，动作被拦下。") from None
    if type(value) is not str or not value:
        raise _err("secret_unresolvable", "凭据引用当前不可解析，动作被拦下。")
    return value


def host_capabilities_from(tool_inventory: Any, tool_grants: Any) -> dict[str, frozenset[str]]:
    """Current host capability = installed AND granted (readiness context shape)."""
    if not isinstance(tool_inventory, Mapping) or not isinstance(tool_grants, Mapping):
        return {}
    out = {}
    for tool, actions in tool_inventory.items():
        granted = tool_grants.get(tool)
        if isinstance(actions, (list, set, frozenset, tuple)) and isinstance(granted, (list, set, frozenset, tuple)):
            both = frozenset(a for a in actions if isinstance(a, str)) & frozenset(a for a in granted if isinstance(a, str))
            if both:
                out[str(tool)] = both
    return out
