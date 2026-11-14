"""Workflow packages: a tested business line, stored as one directory, invoked as one order.

The rule the team settled on : a fixed business runs a
standardised workflow that was designed and tested first; placing an order
only *invokes* it. Nothing is recruited, configured or confirmed per order.
This module is the shell around that rule - layer ② of the extension ladder,
plugin-side, no kernel change:

* a **package** is a directory holding ``workflow.yaml`` (the manifest), a
  one-page ``README.md`` for the business owner, and ``tests/*.md`` records of
  full runs. The org config and the skills it depends on are referenced by
  path so the same files stay the single source of truth;
* a package has a **lifecycle**: ``draft`` → ``testing`` → ``published`` →
  ``retired``. The console lists ``published`` packages; ``testing`` ones are
  visible only to the owner (flagged), and publishing requires at least one
  ``passed`` test record;
* formal invocation uses RuntimeProjectionLaunchHost to pin one immutable
  input generation on the original Task. The old mutable installer below is
  an explicit administrative compatibility operation, not a launch transaction.
"""

from __future__ import annotations

import copy
import json
import hashlib
import math
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache, wraps
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any, Callable

import yaml
from ampuly.core.config import replace_with_retry
from ampuly.layer2_organization.employee_binding_guard import (
    assert_employee_binding_runtime_available,
    employee_binding_runtime_findings,
)

LIFECYCLE = ("draft", "testing", "published", "retired")
INVOKABLE = frozenset({"testing", "published"})
# Architecture step 2: a package is a node table. Four node types (who does
# the work), three gate settings (whether a person signs it off).
NODE_TYPES = ("ai_do", "human_do", "human_review", "invoke_workflow")
GATES = ("none", "human_required", "ai_first_human_spot_check")
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


def _derived_from(manifest: dict[str, Any]) -> dict[str, str] | None:
    """The template and version a project's draft was cut from, or None for a flow that was not derived."""
    raw = manifest.get("derived_from")
    if not isinstance(raw, dict) or not str(raw.get("id", "") or "").strip():
        return None
    return {"id": str(raw.get("id", "")).strip(), "version": str(raw.get("version", "") or "").strip()}


def derived_draft_id(workflow_id: str, project_id: str, taken: set[str]) -> str:
    """`<flow>__<project>` as a package id (lowercase, letters digits _ -), suffixed when taken."""
    slug = re.sub(r"[^a-z0-9_-]+", "_", str(project_id or "").strip().lower()).strip("_-") or "project"
    base = f"{workflow_id}__{slug}"[:60].rstrip("_-")
    if not base[:1].isalpha():
        base = f"p{base}"[:60]
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}-{number}", number + 1
    return candidate


# 从模板切草稿时要去掉什么 (4.7 Q01: 「采用模板生成项目独立草稿且不复制旧授权」). A draft cut from another flow -- a
# project's derived draft, the Builder's copy, a ProcessSpec candidate, a received distribution candidate -- is a new
# candidate that has to earn its own release and its own grants. What the source earned says nothing about the copy:
# its publication and release approvals, its rollback point, its rollout, its approval and redistribution records,
# its trial evidence.
DRAFT_DROPPED_KEYS = (
    "publication",
    "release_approval",
    "release_approval_history",
    "rollback",
    "rollout",
    "approval_records",
    "redistribution",
    "tests",
)
# What an intake field can hold from the customer it was last filled for. A draft starts with the fields, not the answers.
INTAKE_FILLED_KEYS = ("default", "value")


def draft_manifest_from(manifest: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``manifest`` fit to seed a new draft; the manifest passed in is not changed.

    Drops DRAFT_DROPPED_KEYS, empties ``tool_grants`` and clears INTAKE_FILLED_KEYS from every intake field. The
    grants belong to the source flow; a derived draft may contain different steps and must be authorized separately. With
    no grant a tool asks a person, which is the safe default (``tools_without_grant``); granting again is the owner's.
    """
    clean = copy.deepcopy(dict(manifest or {}))
    for node in list(clean.get("nodes", []) or []):
        if isinstance(node, dict) and "employee_binding" in node:
            node.update(_employee_binding_payload(node))
    for key in DRAFT_DROPPED_KEYS:
        clean.pop(key, None)
    clean["tool_grants"] = {}
    intake = clean.get("intake")
    if isinstance(intake, dict) and isinstance(intake.get("fields"), list):
        for item in intake["fields"]:
            if isinstance(item, dict):
                for key in INTAKE_FILLED_KEYS:
                    item.pop(key, None)
    return clean


def flow_project(package: WorkflowPackage) -> str:
    """The project a flow belongs to: '' for a global flow any project may adopt, else the one it was derived for."""
    return str(package.manifest.get("project_id", "") or "").strip()


def usable_by_project(package: WorkflowPackage, project_id: str | None) -> bool:
    """Whether ``project_id`` may list, adopt, order or run this flow.

    One project is one customer (owner's rule), so a flow that names a project is that customer's: no other project
    sees it or runs on it. A global flow (no project) stays open to every project, as before. An empty project id is
    the default project, as everywhere else in the business store.
    """
    owner = flow_project(package)
    return not owner or owner == (str(project_id or "").strip() or "default")


@dataclass
class TestRecord:
    path: Path
    date: str = ""
    outcome: str = ""  # passed | failed | stopped
    duration_minutes: float | None = None
    model_calls: int | None = None
    cost_cny_estimate: float | None = None
    notes: str = ""
    failure_reason: str = ""
    workflow_version: str = ""
    manifest_sha256: str = ""
    recorded_by: str = ""
    execution_mode: str = "manual"  # manual | simulated | provider

    def to_payload(self) -> dict[str, Any]:
        return {
            "file": self.path.name,
            "date": self.date,
            "outcome": self.outcome,
            "duration_minutes": self.duration_minutes,
            "model_calls": self.model_calls,
            "cost_cny_estimate": self.cost_cny_estimate,
            "notes": self.notes,
            "failure_reason": self.failure_reason,
            "workflow_version": self.workflow_version,
            "manifest_sha256": self.manifest_sha256,
            "recorded_by": self.recorded_by,
            "execution_mode": self.execution_mode,
        }


@dataclass
class SkillContract:
    """Machine-readable release contract for one installed SKILL.md.

    Skills remain ordinary Markdown for authors, but a workflow package needs
    a small amount of metadata before it can safely lock a production version.
    Missing fields are reported to the package payload and block testing or
    publication readiness once the package declares executable skills.
    """

    skill_id: str
    path: Path
    version: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)
    tests: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return bool(self.version and self.provenance and self.tests and self.success_criteria and not self.problems)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.skill_id,
            "version": self.version,
            "provenance": dict(self.provenance),
            "tests": list(self.tests),
            "success_criteria": list(self.success_criteria),
            "complete": self.complete,
            "problems": list(self.problems),
        }


def _canonical_bytes(raw: bytes) -> bytes:
    """What a repository would store for these bytes.

    Line endings are the only difference git introduces for the formats this
    product publishes, and it introduces it on every commit from a CRLF
    checkout. Folding CRLF to LF here makes a recorded hash mean the same thing
    on every machine, which is the only way a value written on one host can be
    checked on another.

    A lone CR is left alone: git's own normalisation only rewrites CRLF pairs,
    and rewriting more than git does would produce a hash matching nothing.
    """
    if b"\x00" in raw:
        # Binary. Git stores it verbatim; so does this.
        return raw
    return raw.replace(b"\r\n", b"\n")


@dataclass
class WorkflowPackage:
    root: Path
    manifest: dict[str, Any]
    tests: list[TestRecord] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    # Exact manifest bytes observed by load_package; never recomputed at save time.
    loaded_revision: str = ""

    @property
    def id(self) -> str:
        return str(self.manifest.get("id", "") or "").strip()

    @property
    def name(self) -> str:
        return str(self.manifest.get("name", "") or self.id).strip()

    @property
    def status(self) -> str:
        return str(self.manifest.get("status", "draft") or "draft").strip().lower()

    @property
    def org_id(self) -> str:
        return str(self.manifest.get("org_id", "") or self.id).strip()

    @property
    def org_config_path(self) -> Path | None:
        rel = str(self.manifest.get("org_config", "") or "").strip()
        return (self.root / rel).resolve() if rel else None

    @property
    def skills_root(self) -> Path:
        rel = str(self.manifest.get("skills_root", "../../skills") or "../../skills")
        return (self.root / rel).resolve()

    @property
    def skills(self) -> list[str]:
        return [str(s).strip() for s in list(self.manifest.get("skills", []) or []) if str(s).strip()]

    @property
    def skill_contracts(self) -> list[SkillContract]:
        return [load_skill_contract(self.skills_root, skill) for skill in self.skills]

    @property
    def locked_skill_versions(self) -> dict[str, str]:
        """Resolve the package's effective Skill versions for one run.

        Testing and published packages must declare matching
        ``skill_versions``. The invocation records the exact contracts it used
        so a run can be audited and compared with a later package revision.
        """
        declared = dict(self.manifest.get("skill_versions", {}) or {})
        return {
            contract.skill_id: str(declared.get(contract.skill_id) or contract.version or "")
            for contract in self.skill_contracts
        }

    @property
    def node_tools(self) -> list[str]:
        """Every tool the package's nodes ask for, in declaration order."""
        seen: list[str] = []
        for node in self.nodes:
            for tool in list(node.get("tools", []) or []):
                name = str(tool).strip()
                if name and name not in seen:
                    seen.append(name)
        return seen

    @property
    def tools_without_grant(self) -> list[str]:
        """Node tools with no project-level grant, so they stop for a person.

        A grant is what lets a tool run without asking. Its absence is not an
        error -- asking is the safe default -- but the operator has to know
        *before* launching, because the alternative is meeting the prompt in
        the middle of a customer's order. Never fill this in on someone's
        behalf: granting is widening, and widening is the owner's decision.
        """
        granted = {str(k).strip() for k in dict(self.manifest.get("tool_grants", {}) or {})}
        return [tool for tool in self.node_tools if tool not in granted]

    @property
    def reaching_tools_without_grant(self) -> list[str]:
        """The ungranted tools that reach outside the workspace.

        Reading a file and writing inside the workspace is what every node
        does; a package that does not grant those is ordinary. What an
        operator needs before launching is the tool that makes a network
        call, spends money or sends something -- and the tool that declares
        no effects at all, which is exactly the case that surprised us with
        ``image_generate`` on 2026-09-01.
        """
        effects = _tool_permission_effects()
        out: list[str] = []
        for name in self.tools_without_grant:
            if name not in effects:
                continue
            declared = effects[name]
            if not declared or (declared - ROUTINE_TOOL_EFFECTS):
                out.append(name)
        return out

    @property
    def tools_with_unknown_effects(self) -> list[str]:
        """Ungranted node tools whose effects this build cannot state.

        Some tools are built with runtime context (the acceptance reminder
        needs the effect gateway), so no zero-argument factory produces them
        and their declaration cannot be read here. Say so instead of dropping
        them: a tool nobody can classify is the one worth naming out loud.
        """
        effects = _tool_permission_effects()
        return [name for name in self.tools_without_grant if name not in effects]

    @property
    def nodes(self) -> list[dict[str, Any]]:
        return [dict(n) for n in list(self.manifest.get("nodes", []) or []) if isinstance(n, dict)]

    @property
    def stages(self) -> list[dict[str, Any]]:
        """The reading groups of this package's steps, from the ``stages.yaml`` beside it (see :func:`load_stages`)."""
        return load_stages(self)

    @property
    def gates(self) -> list[dict[str, Any]]:
        """Declared gates, or the ones implied by nodes that require a person."""
        declared = [dict(g) for g in list(self.manifest.get("gates", []) or []) if isinstance(g, dict)]
        if declared or not self.nodes:
            return declared
        implied: list[dict[str, Any]] = []
        for node in self.nodes:
            gate = str(node.get("gate", "none") or "none")
            kind = str(node.get("type", "") or "")
            if gate == "none" and kind not in {"human_do", "human_review"}:
                continue
            implied.append({
                "station": str(node.get("name", node.get("id", "")) or ""),
                "kind": kind if kind in {"human_do", "human_review"} else gate,
                "who": str(node.get("gate_to", "") or node.get("assignee", "") or "owner"),
                "decides": str(node.get("standard_summary", "") or "; ".join(str(x) for x in list(node.get("standard", []) or [])[:2])),
            })
        return implied

    @property
    def intake_fields(self) -> list[dict[str, Any]]:
        intake = dict(self.manifest.get("intake", {}) or {})
        return [dict(f) for f in list(intake.get("fields", []) or []) if isinstance(f, dict) and f.get("id")]

    @property
    def exec(self) -> dict[str, str]:
        raw = dict(self.manifest.get("exec", {}) or {})
        return {
            "mode": str(raw.get("mode", "org") or "org"),
            "company_profile": str(raw.get("company_profile", "custom") or "custom"),
            "preferred_agent": str(raw.get("preferred_agent", "native") or "native"),
        }

    @property
    def latest_test(self) -> TestRecord | None:
        return self.tests[-1] if self.tests else None

    @property
    def invokable(self) -> bool:
        return (
            not self.problems and self.status in INVOKABLE
            and not employee_binding_runtime_findings(self.nodes)
        )

    @property
    def release_readiness(self) -> dict[str, Any]:
        """Explain whether this package has enough evidence to publish.

        This is intentionally read-only: publishing remains a separate,
        auditable command.  The console can therefore show a truthful reason
        instead of presenting a button that silently cannot work.
        """
        blockers = list(self.problems)
        blockers.extend(
            f"{finding['code']}: {finding['message']}"
            for finding in employee_binding_runtime_findings(self.nodes)
        )
        if self.status in {"testing", "published"}:
            if not self.latest_test or self.latest_test.outcome != "passed":
                blockers.append("the latest test record must have outcome: passed")
            elif self.latest_test.execution_mode == "simulated":
                blockers.append("the latest passed test is simulated-only; provider or manual evidence is required")
            if self.skills:
                for contract in self.skill_contracts:
                    if not contract.complete:
                        blockers.append(
                            f"skill {contract.skill_id} has incomplete release contract"
                        )
                skill_tests = run_package_skill_tests(self)
                if skill_tests["status"] != "passed":
                    blockers.append(
                        "Skill contract tests are not fully executable: "
                        + str(skill_tests["status"])
                    )
                if "skill_versions" not in self.manifest:
                    blockers.append("skill_versions must lock production skill versions")
                else:
                    declared = dict(self.manifest.get("skill_versions", {}) or {})
                    for contract in self.skill_contracts:
                        locked = str(declared.get(contract.skill_id, "") or "").strip()
                        if locked and contract.version and locked != contract.version:
                            blockers.append(
                                f"skill_versions.{contract.skill_id} does not match SKILL.md version {contract.version}"
                            )
            approval = dict(self.manifest.get("release_approval", {}) or {})
            approval_status = str(approval.get("status", "") or "").strip().lower()
            if approval_status == "pending":
                blockers.append("release approval is pending")
            elif approval_status == "rejected":
                blockers.append("release approval was rejected")
        if self.status not in {"testing", "published"}:
            blockers.append(f"workflow status is {self.status}")
        # Validation and readiness inspect the same contract; de-duplicate
        # repeated diagnostics so the console presents one actionable reason.
        blockers = list(dict.fromkeys(str(item) for item in blockers if str(item).strip()))
        return {"ready": not blockers, "blockers": blockers}

    @staticmethod
    def _file_sha256(path: Path) -> str:
        """The hash of what a repository stores for this file.

        Not the hash of the bytes on this disk. `.gitattributes` declares the
        text formats `eol=lf`, so a CRLF working copy is committed as LF and a
        value taken from the working copy describes bytes that exist nowhere in
        the repository. A release recorded that way cannot be verified on a
        fresh clone: the retired snapshot `0.2.1-0174bbce17c5.yaml` is named
        after its own CRLF bytes, and the committed blob hashes to something
        else entirely.

        Binary content is returned untouched. A NUL byte is how git decides a
        file is binary and stores it verbatim, and folding line endings inside
        one would change what the hash means rather than stabilise it.
        """
        try:
            raw = path.read_bytes()
        except OSError:
            return ""
        return hashlib.sha256(_canonical_bytes(raw)).hexdigest()

    @property
    def release_evidence(self) -> dict[str, Any]:
        """A deterministic, read-only evidence bundle for a release decision."""
        manifest_path = self.root / "workflow.yaml"
        skill_hashes = {
            contract.skill_id: self._file_sha256(contract.path)
            for contract in self.skill_contracts
            if contract.path.is_file()
        }
        evidence = {
            "workflow_id": self.id,
            "version": str(self.manifest.get("version", "") or ""),
            "skill_versions": self.locked_skill_versions,
            "manifest_sha256": self._file_sha256(manifest_path),
            "skill_sha256": skill_hashes,
            "test_records": [test.to_payload() for test in self.tests],
            "approval_records": [
                *[
                    dict(item)
                    for item in list(self.manifest.get("approval_records", []) or [])
                    if isinstance(item, dict)
                ],
                *[
                    dict(item)
                    for item in list(self.manifest.get("release_approval_history", []) or [])
                    if isinstance(item, dict)
                ],
                *([dict(self.manifest["release_approval"])] if isinstance(self.manifest.get("release_approval"), dict) else []),
            ],
            "change_note": str(self.manifest.get("change_note", "") or ""),
            "rollback_point": self._file_sha256(manifest_path),
        }
        # The fingerprint covers the candidate while excluding volatile
        # approval/publication metadata.  Expose it in the read-only payload
        # so the console can show exactly what an approval refers to.
        evidence["candidate_fingerprint"] = _candidate_fingerprint(self)
        approval = self.manifest.get("release_approval")
        if isinstance(approval, dict):
            if approval.get("evidence_file"):
                evidence["evidence_file"] = str(approval["evidence_file"])
            if approval.get("evidence_file_sha256"):
                evidence["evidence_file_sha256"] = str(approval["evidence_file_sha256"])
        return evidence

    def to_payload(self) -> dict[str, Any]:
        latest = self.latest_test
        rollout = dict(self.manifest.get("rollout", {}) or {})
        try:
            rollout_evaluation = evaluate_rollout(self) if rollout else None
        except (TypeError, ValueError):
            rollout_evaluation = {"rollout_status": "unhealthy", "blockers": ["rollout policy is invalid"]}
        return {
            "id": self.id,
            "name": self.name,
            "version": str(self.manifest.get("version", "") or ""),
            # What a node save names as expected_revision: a layout-only save keeps the version, so the version
            # alone cannot tell two saves of the same draft apart (4.7 Q01).
            "draft_revision": self.loaded_revision,
            # Whose flow this is: '' for a global template projects adopt; a project for a draft derived for it.
            "project_id": str(self.manifest.get("project_id", "") or ""),
            "derived_from": _derived_from(self.manifest),
            "status": self.status,
            "summary": str(self.manifest.get("summary", "") or ""),
            "applies_to": [str(x) for x in list(self.manifest.get("applies_to", []) or [])],
            "org_id": self.org_id,
            # The builder offers these as a node's role choices; validate_nodes checks against the same list.
            "org_roles": _org_roles(self),
            "gates": self.gates,
            "gate_count": len(self.gates),
            "stations": [str(x) for x in list(self.manifest.get("stations", []) or [])] or [
                str(n.get("name", n.get("id", "")) or "") for n in self.nodes
            ],
            # S3-2A: the reading groups of the steps (确认需求 / 设计与选稿 / ...), from the stages.yaml beside the
            # package; [] when it has none. A projection for the project page, never an execution rule.
            "stages": self.stages,
            "nodes": [node_payload(n) for n in self.nodes],
            "intake_fields": self.intake_fields,
            # The flow's own acceptance criteria, as written: the builder's package view says when there are none
            # (an order then gets one generated from the deliverables at launch; ws_handler._ensure_order_acceptance).
            "acceptance": [str(item).strip() for item in list(self.manifest.get("acceptance", []) or []) if str(item).strip()],
            "latest_test": latest.to_payload() if latest else None,
            "test_count": len(self.tests),
            "problems": list(self.problems),
            "invokable": self.invokable,
            "skill_contracts": [contract.to_payload() for contract in self.skill_contracts],
            # 调用: whether the internal library's skills can go on this package's steps, and which of them a save
            # can declare and lock (see library_skills_for). Sent with the package so the builder knows without
            # the 组件库 page having been opened.
            **_library_payload(self),
            # 4.7-A06: whether the owner opened the desktop channel -- the builder says a desktop step will not run
            # while it is closed. Read when the package is sent; never stored in it.
            "desktop_channel_open": _desktop_channel_open(),
            "node_tools": self.node_tools,
            "tools_without_grant": self.tools_without_grant,
            "reaching_tools_without_grant": self.reaching_tools_without_grant,
            "tools_with_unknown_effects": self.tools_with_unknown_effects,
            "skill_test_report": run_package_skill_tests(self),
            "skill_versions": self.locked_skill_versions,
            # Computed once: it runs every skill contract check (the admission check re-hashes files).
            **_readiness_payload(self.release_readiness),
            "publication": dict(self.manifest.get("publication", {}) or {}),
            "rollback_preview": rollback_preview(self),
            "release_approval": dict(self.manifest.get("release_approval", {}) or {}),
            "release_approval_history": [
                dict(item)
                for item in list(self.manifest.get("release_approval_history", []) or [])
                if isinstance(item, dict)
            ],
            "release_evidence": self.release_evidence,
            "rollout": rollout,
            "rollout_evaluation": rollout_evaluation,
            "root": str(self.root),
        }


# ── discovery ─────────────────────────────────────────────────────────────


def _json_safe(value: Any) -> Any:
    """Return ``value`` with YAML-native dates/datetimes rendered as ISO strings.

    Frontmatter is a data contract that ends up projected over the console
    WebSocket as JSON.  ``yaml.safe_load`` turns a bare ``reviewed_at:
    2026-09-05`` into ``datetime.date``; every consumer here already treats
    such fields as text, so normalising at the parse boundary keeps
    ``json.dumps`` from failing deep inside ``_send_ack`` for any current or
    future field.  Strings, numbers, bools and ``None`` pass through untouched.
    """
    import datetime as _dt

    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    return value


def _parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    try:
        meta = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        meta = {}
    meta = _json_safe(meta)
    return (meta if isinstance(meta, dict) else {}), text[end + 4:]


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(item).strip() for item in list(value or []) if str(item).strip()]


def _frontmatter_field(meta: dict[str, Any], key: str) -> Any:
    if key in meta:
        return meta.get(key)
    nested = meta.get("metadata")
    return nested.get(key) if isinstance(nested, dict) else None


# Reading and writing inside the workspace is every node's ordinary work, and
# pausing to ask the operator a question reaches nothing at all.
ROUTINE_TOOL_EFFECTS = frozenset({"local_read", "workspace_write", "runtime_internal"})


@lru_cache(maxsize=1)
def _tool_permission_effects() -> dict[str, frozenset[str]]:
    """What each built-in tool declares it does, by tool name.

    Built once from the same zero-argument ``create_*_tools`` factories the
    permission-declaration invariant test uses. A module that needs an
    optional dependency is skipped rather than breaking a page render.
    """
    import importlib
    import inspect
    import pkgutil

    import ampuly.layer4_tools as tools_package

    effects: dict[str, frozenset[str]] = {}
    for module_info in pkgutil.iter_modules(tools_package.__path__):
        try:
            module = importlib.import_module(f"ampuly.layer4_tools.{module_info.name}")
        except Exception:
            continue
        for name, function in inspect.getmembers(module, inspect.isfunction):
            # Factories come in both shapes: ``create_file_tools()`` returning a
            # list and ``create_user_input_tool()`` returning one definition.
            if not (name.startswith("create_") and name.endswith(("_tool", "_tools"))):
                continue
            if inspect.signature(function).parameters:
                continue
            try:
                produced = function()
            except Exception:
                continue
            produced = produced if isinstance(produced, (list, tuple, set)) else [produced]
            for definition in list(produced or []):
                tool_name = str(getattr(definition, "name", "") or "")
                if tool_name:
                    effects[tool_name] = frozenset(
                        str(e) for e in (getattr(definition, "permission_effects", ()) or ())
                    )
    return effects


ADMISSION_RECORD_FILE = "ADMISSION.json"
ADMISSION_RECORD_CHECK = "admission_record"
# Files admission writes beside the market's own; everything else in the folder is the market's.
ADMISSION_OWN_FILES = frozenset({"admission.json", ".gitattributes"})
# The market adapter refuses a skill with more files than this (skill_market.MAX_FILES); a folder that grew past it
# is not what was admitted, and the check stops walking.
ADMISSION_MAX_FILES = 500


def admission_record(skill_root: Path) -> dict[str, Any] | None:
    """The owner's admission decision beside a market skill, when there is one that says admitted."""
    path = Path(skill_root) / ADMISSION_RECORD_FILE
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("decision") != "admitted" or not isinstance(record.get("files_sha256"), dict):
        return None
    return record


def verify_admission_record(skill_root: Path) -> list[str]:
    """What is wrong with an admitted skill's files against its admission record; empty when they are the admitted bytes.

    The record lists every file admission copied, by sha256. A file changed, added or removed since is named,
    so a skill someone edited after the owner admitted it cannot pass as the tested one.
    """
    root = Path(skill_root)
    record = admission_record(root)
    if record is None:
        return ["no admission record says this skill was admitted"]
    problems: list[str] = []
    if str(dict(record.get("test") or {}).get("outcome") or "") != "passed":
        problems.append("the admission record does not carry a passed test")
    from ampuly.plugins.office_ui.component_catalog import _is_link

    expected = {str(key): str(value) for key, value in dict(record["files_sha256"]).items()}
    present: dict[str, Path] = {}
    # os.walk with links pruned: rglob follows Windows junctions (is_symlink() is False for them) into any tree.
    for folder, dirs, files in os.walk(root):
        base = Path(folder)
        for name in list(dirs):
            if _is_link(base / name):
                problems.append(f"{(base / name).relative_to(root).as_posix()} is a link")
                dirs.remove(name)
        for name in files:
            path = base / name
            rel = path.relative_to(root).as_posix()
            if _is_link(path):
                problems.append(f"{rel} is a link")
                continue
            if rel.casefold() in ADMISSION_OWN_FILES:
                continue
            present[rel] = path
            if len(present) > ADMISSION_MAX_FILES:
                return [*problems, f"more than {ADMISSION_MAX_FILES} files: not the skill that was admitted"]
    for rel in sorted(set(expected) - set(present)):
        problems.append(f"{rel} is missing")
    for rel in sorted(set(present) - set(expected)):
        problems.append(f"{rel} was added after admission")
    for rel in sorted(set(expected) & set(present)):
        if _sha256_file(present[rel]) != expected[rel]:
            problems.append(f"{rel} changed after admission")
    return problems


def _desktop_channel_open() -> bool:
    from ampuly.computer_use.desktop_bridge import channel_open

    return channel_open()


def _readiness_payload(readiness: dict[str, Any]) -> dict[str, Any]:
    return {"release_ready": bool(readiness["ready"]), "release_blockers": list(readiness["blockers"])}


def _library_payload(package: WorkflowPackage) -> dict[str, Any]:
    lockable, unlockable = library_skills_for(package)
    return {
        "shared_skill_library": uses_shared_skill_library(package),
        # A runtime draft takes library skills by copying them in at the save (copies_library_skills).
        "library_by_copy": copies_library_skills(package),
        "library_skills": lockable,
        "library_skills_unlockable": unlockable,
    }


def load_skill_contract(skills_root: Path, skill_id: str) -> SkillContract:
    path = skills_root / skill_id / "SKILL.md"
    contract = SkillContract(skill_id=skill_id, path=path)
    if not path.is_file():
        contract.problems.append("SKILL.md not found")
        return contract
    try:
        meta, body = _parse_front_matter(path.read_text(encoding="utf-8"))
    except OSError as exc:
        contract.problems.append(f"SKILL.md unreadable: {exc}")
        return contract
    contract.version = str(_frontmatter_field(meta, "version") or "").strip()
    raw_provenance = _frontmatter_field(meta, "provenance")
    if isinstance(raw_provenance, dict):
        contract.provenance = {str(k): v for k, v in raw_provenance.items()}
    elif str(raw_provenance or "").strip():
        contract.provenance = {"source": str(raw_provenance).strip()}
    contract.tests = _string_list(_frontmatter_field(meta, "tests"))
    contract.success_criteria = _string_list(_frontmatter_field(meta, "success_criteria"))
    # Markdown headings are a friendly fallback for skills whose authors do
    # not want to duplicate the contract in YAML frontmatter.
    section = ""
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            section = "tests" if ("test" in heading or "测试" in heading) else "success" if ("success" in heading or "成功" in heading) else ""
            continue
        if line.startswith(("- ", "* ")):
            item = line[2:].strip()
            if section == "tests" and item not in contract.tests:
                contract.tests.append(item)
            elif section == "success" and item not in contract.success_criteria:
                contract.success_criteria.append(item)
    admission = admission_record(path.parent)
    if admission is not None:
        # A skill admitted from a market (component_admission) keeps its own files untouched, and its release
        # contract is the owner's admission, not whatever the author wrote: a "## Testing" heading in a
        # third-party SKILL.md is documentation, not a check we can run, and must not stand in for the byte
        # re-check or for the success criteria the owner set.
        source = dict(admission.get("source") or {})
        if not contract.version:
            contract.version = str(source.get("version") or "").strip()
        contract.provenance = {
            "source": f"{source.get('market') or 'market'}:{source.get('slug') or skill_id}@{source.get('version') or ''}",
            "zip_sha256": str(source.get("zip_sha256") or ""),
            "admitted_by": str(admission.get("admitted_by") or ""),
            "admitted_at": str(admission.get("admitted_at") or ""),
        }
        contract.tests = [ADMISSION_RECORD_CHECK]
        contract.success_criteria = _string_list(admission.get("success_criteria"))
    if str(_frontmatter_field(meta, "status") or "").strip().lower() == "draft":
        # A generated skill says so in its own frontmatter. Words nobody
        # has reviewed are not a release contract, however complete the
        # fields look; the person who reviews it removes the status.
        contract.problems.append(
            "skill is a draft: a person has to review it, record provenance.reviewed_at and drop status: draft"
        )
    if not contract.version:
        contract.problems.append("version is required")
    if not contract.provenance:
        contract.problems.append("provenance is required")
    if not contract.tests:
        contract.problems.append("tests must list at least one repeatable check")
    if not contract.success_criteria:
        contract.problems.append("success_criteria must list at least one criterion")
    # A path-like test reference is part of the Skill package contract. Keep
    # symbolic checks such as ``static_frontmatter_contract`` valid, but fail
    # closed when an author points at a missing local evidence file.
    for test_ref in contract.tests:
        if "/" in test_ref or "\\" in test_ref or test_ref.endswith((".md", ".py", ".json")):
            test_path = (path.parent / test_ref).resolve()
            try:
                test_path.relative_to(path.parent.resolve())
            except ValueError:
                contract.problems.append(f"test reference escapes Skill directory: {test_ref}")
            else:
                if not test_path.is_file():
                    contract.problems.append(f"test reference not found: {test_ref}")
    return contract


def _skill_test_is_path_reference(test_ref: str) -> bool:
    """Return whether a Skill test names a local evidence file.

    The contract deliberately supports symbolic checks (for example
    ``static_frontmatter_contract``) without ever treating arbitrary text in
    SKILL.md as a shell command.  Only an explicit path-like value is allowed
    to address a local file, and the caller still confines it to the Skill
    directory.
    """
    value = str(test_ref or "").strip()
    return bool("/" in value or "\\" in value or value.endswith((".md", ".py", ".json")))


def _sha256_file(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _recorded_outcome(path: Path) -> tuple[str, str]:
    """What a referenced evidence file says happened, and why that is the verdict.

    A file with no front matter at all is evidence of the plain kind -- a
    document somebody wrote -- and passes as it always did. A file that
    carries an ``outcome`` is a test record, and then the outcome is the
    answer: passed, failed, or unrun, which is neither.
    """
    try:
        meta, _body = _parse_front_matter(path.read_text(encoding="utf-8"))
    except OSError:
        return "failed", "the referenced file could not be read"
    if not isinstance(meta, dict) or "outcome" not in meta:
        return "passed", ""
    outcome = str(meta.get("outcome", "") or "").strip().lower()
    if outcome == "passed":
        return "passed", ""
    if outcome in {"failed", "stopped"}:
        reason = str(meta.get("failure_reason", "") or "").strip()
        return "failed", f"the record says {outcome}" + (f": {reason}" if reason else "")
    return (
        "unsupported",
        f"the record says {outcome or 'nothing'}; nobody has run this contract yet",
    )


def run_skill_contract_tests(contract: SkillContract) -> dict[str, Any]:
    """Run the deterministic, local checks declared by one Skill contract.

    This is intentionally *not* a general command runner.  A Skill's tests
    are metadata and evidence references, so the runner only executes built-in
    checks and verifies local files under the Skill directory.  Unknown
    symbolic checks are reported as ``unsupported`` rather than guessed or
    executed.  The result is safe to include in Builder/release evidence.
    """
    skill_root = contract.path.parent.resolve()
    checks: list[dict[str, Any]] = []
    for raw_ref in contract.tests:
        test_ref = str(raw_ref or "").strip()
        if test_ref == "static_frontmatter_contract":
            problems = list(contract.problems)
            # A path reference problem is reported by its own check below;
            # the built-in check focuses on the required machine fields.
            field_problems = [
                item for item in problems
                if any(item.startswith(prefix) for prefix in (
                    "version is required",
                    "provenance is required",
                    "tests must list",
                    "success_criteria must list",
                ))
            ]
            checks.append({
                "id": test_ref,
                "kind": "builtin",
                "status": "passed" if not field_problems else "failed",
                "details": field_problems,
            })
            continue
        if test_ref == ADMISSION_RECORD_CHECK:
            # A market skill admitted by the owner: the files are still the bytes that were tested and admitted.
            problems = verify_admission_record(skill_root)
            checks.append({
                "id": test_ref,
                "kind": "builtin",
                "status": "passed" if not problems else "failed",
                "details": problems[:20],
            })
            continue
        if _skill_test_is_path_reference(test_ref):
            candidate = (skill_root / test_ref).resolve()
            try:
                candidate.relative_to(skill_root)
            except ValueError:
                checks.append({
                    "id": test_ref,
                    "kind": "local_evidence",
                    "status": "failed",
                    "details": ["test reference escapes Skill directory"],
                })
                continue
            if not candidate.is_file():
                checks.append({
                    "id": test_ref,
                    "kind": "local_evidence",
                    "status": "failed",
                    "details": ["test reference not found"],
                })
                continue
            outcome, note = _recorded_outcome(candidate)
            checks.append({
                "id": test_ref,
                "kind": "local_evidence",
                # The file existing is not the result. What it says happened
                # is: a drafted skill ships a record whose outcome is
                # "unrun", and counting that as a pass turns "nobody has
                # tried this" into evidence that it works.
                "status": outcome,
                "file": test_ref,
                "sha256": _sha256_file(candidate),
                "bytes": candidate.stat().st_size,
                **({"details": [note]} if note else {}),
            })
            continue
        checks.append({
            "id": test_ref,
            "kind": "symbolic",
            "status": "unsupported",
            "details": ["no deterministic local executor is registered for this symbolic check"],
        })

    statuses = [str(item.get("status", "")) for item in checks]
    if not checks:
        status = "failed"
    elif "failed" in statuses:
        status = "failed"
    elif "unsupported" in statuses:
        status = "unsupported"
    else:
        status = "passed"
    return {
        "skill_id": contract.skill_id,
        "version": contract.version,
        "status": status,
        "tests": checks,
        "skill_file": contract.path.name,
        "skill_file_sha256": _sha256_file(contract.path),
        "side_effects": [],
    }


def run_package_skill_tests(package: WorkflowPackage) -> dict[str, Any]:
    """Run all Skill contract checks without changing the package or runtime."""
    reports = [run_skill_contract_tests(contract) for contract in package.skill_contracts]
    statuses = [str(report.get("status", "")) for report in reports]
    if any(status == "failed" for status in statuses):
        status = "failed"
    elif any(status == "unsupported" for status in statuses):
        status = "unsupported"
    else:
        status = "passed"
    return {
        "workflow_id": package.id,
        "workflow_version": str(package.manifest.get("version", "") or ""),
        "status": status,
        "skills": reports,
        "side_effects": [],
    }


def load_test_records(root: Path) -> list[TestRecord]:
    records: list[TestRecord] = []
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return records
    for path in sorted(tests_dir.glob("*.md")):
        try:
            meta, body = _parse_front_matter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        calls = _num(meta.get("model_calls"))
        records.append(
            TestRecord(
                path=path,
                date=str(meta.get("date", "") or path.stem[:10]),
                outcome=str(meta.get("outcome", "") or "").strip().lower(),
                duration_minutes=_num(meta.get("duration_minutes")),
                model_calls=int(calls) if calls is not None else None,
                cost_cny_estimate=_num(meta.get("cost_cny_estimate")),
                notes=str(meta.get("notes", "") or body.strip()[:400]),
                failure_reason=str(meta.get("failure_reason", "") or "").strip(),
                workflow_version=str(meta.get("workflow_version", "") or "").strip(),
                manifest_sha256=str(meta.get("manifest_sha256", "") or "").strip(),
                recorded_by=str(meta.get("recorded_by", "") or "").strip(),
                execution_mode=str(meta.get("execution_mode", "manual") or "manual").strip().lower(),
            )
        )
    return records


def validate_package(package: WorkflowPackage) -> list[str]:
    problems: list[str] = []
    manifest = package.manifest
    if int(manifest.get("schema_version", 0) or 0) != 1:
        problems.append("schema_version must be 1")
    if not _ID_RE.match(package.id):
        problems.append("id must be lowercase letters, digits, '-' or '_' (2-64 chars)")
    if package.status not in LIFECYCLE:
        problems.append(f"status must be one of {', '.join(LIFECYCLE)}")
    org_path = package.org_config_path
    if org_path is None:
        problems.append("org_config is required")
    elif not org_path.is_file():
        problems.append(f"org_config not found: {org_path}")
    else:
        try:
            org = yaml.safe_load(org_path.read_text(encoding="utf-8")) or {}
            if str(org.get("organization_id", "") or "").strip() != package.org_id:
                problems.append("org_config organization_id does not match the package org_id")
        except (OSError, yaml.YAMLError) as exc:
            problems.append(f"org_config unreadable: {exc}")
    for skill in package.skills:
        if not (package.skills_root / skill / "SKILL.md").is_file():
            problems.append(f"skill not found: {skill}")
    problems.extend(validate_nodes(package))
    if not package.intake_fields:
        problems.append("intake.fields must list at least one field")
    if not str(dict(manifest.get("intake", {}) or {}).get("template", "") or "").strip():
        problems.append("intake.template is required")
    if package.status == "published" and not any(t.outcome == "passed" for t in package.tests):
        problems.append("a published package needs at least one test record with outcome: passed")
    # A package with executable skills must carry an explicit version lock
    # before it can be testing-ready or published.  This prevents a runtime
    # invocation from resolving a moving SKILL.md and makes the task contract
    # deterministic across retries and replays.
    if package.status in {"testing", "published"} and package.skills:
        if "skill_versions" not in manifest:
            problems.append("skill_versions must lock production skill versions")
        skill_versions = dict(manifest.get("skill_versions", {}) or {})
        for contract in package.skill_contracts:
            if not contract.complete:
                problems.append(f"skill {contract.skill_id} has incomplete release contract: {'; '.join(contract.problems)}")
                continue
            locked = str(skill_versions.get(contract.skill_id, "") or "").strip()
            if not locked:
                problems.append(f"skill_versions must lock {contract.skill_id}")
            elif locked != contract.version:
                problems.append(f"skill_versions.{contract.skill_id} does not match SKILL.md version {contract.version}")
    return problems


def _org_role_entries(package: WorkflowPackage) -> list[dict[str, Any]]:
    path = package.org_config_path
    if path is None or not path.is_file():
        return []
    try:
        org = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    return [role for role in list(org.get("roles", []) or []) if isinstance(role, dict)]


def _org_role_ids(package: WorkflowPackage) -> set[str]:
    return {str(role.get("id", role.get("role_id", "")) or "").strip() for role in _org_role_entries(package)}


def _canvas_position(value: Any) -> dict[str, int]:
    """A node's hand-placed canvas position as whole pixels, or {} when absent or not a pair of finite numbers.

    Presentation only: it rides in workflow.yaml so the layout travels with the package, but the runtime
    projection never reads it.
    """
    if not isinstance(value, dict):
        return {}
    coords: dict[str, int] = {}
    for axis in ("x", "y"):
        number = value.get(axis)
        if isinstance(number, bool) or not isinstance(number, (int, float)) or number != number or number in (float("inf"), float("-inf")):
            return {}
        coords[axis] = int(round(number))
    return coords


def _org_roles(package: WorkflowPackage) -> list[dict[str, str]]:
    """The org's roles in file order -- what a node's role may name, with a display name for the builder."""
    roles: list[dict[str, str]] = []
    seen: set[str] = set()
    for role in _org_role_entries(package):
        role_id = str(role.get("id", role.get("role_id", "")) or "").strip()
        if not role_id or role_id in seen:
            continue
        seen.add(role_id)
        roles.append({"id": role_id, "name": str(role.get("name", "") or "").strip() or role_id})
    return roles


def node_findings(package: WorkflowPackage) -> list[dict[str, Any]]:
    """What is wrong with the nodes, each tied to the node and the field it is about.

    ``{"node_id", "field", "code", "message"}``; ``node_id`` is "" for a finding about the graph as a whole, and a
    cycle also carries ``nodes`` (the steps on the loop). ``validate_nodes`` is these same findings as sentences, so
    the page that points at a field and the save that refuses can never disagree about what is wrong.
    """
    nodes = package.nodes
    if not nodes:
        return []
    findings: list[dict[str, Any]] = []
    label = ""

    def found(node_id: str, field: str, code: str, message: str, **extra: Any) -> None:
        # `label` is how validate_nodes has always named the node in its sentence; graph-wide findings have none.
        findings.append({"node_id": node_id, "field": field, "code": code, "message": message,
                         "label": "" if code in _GRAPH_FINDINGS else label, **extra})

    role_ids = _org_role_ids(package)
    ids = [str(n.get("id", "") or "").strip() for n in nodes]
    if any(not i for i in ids):
        found("", "id", "id_missing", "every node needs an id")
    if len(set(ids)) != len(ids):
        found("", "id", "id_duplicate", "node ids must be unique")
    known = set(ids)
    for node in nodes:
        node_id = str(node.get("id", "") or "").strip()
        label = f"node {node.get('id', '?')}"
        kind = str(node.get("type", "") or "").strip()
        if kind not in NODE_TYPES:
            found(node_id, "type", "type_invalid", f"type must be one of {', '.join(NODE_TYPES)}")
            continue
        gate = str(node.get("gate", "none") or "none").strip()
        if gate not in GATES:
            found(node_id, "gate", "gate_invalid", f"gate must be one of {', '.join(GATES)}")
        if "employee_binding" in node:
            from ampuly.plugins.office_ui.ai_employee_instance_contract import validate_binding

            # Draft validation is structural only. Resolving an adopted package,
            # schema, instance or reviewer eligibility is a later server boundary.
            for problem in validate_binding(
                node["employee_binding"], nodes=nodes, node_id=node_id,
                requires_review=(kind == "human_review" or gate != "none"),
            ):
                found(
                    node_id, "employee_binding", problem["code"], problem["message"],
                    binding_path=problem["path"],
                )
        role = str(node.get("role", "") or "").strip()
        # Every node needs a role, a call to another workflow included: the runtime reads a package's nodes
        # only through the role they are projected onto (node_contract.role_nodes / all_nodes), so a
        # role-less node is saved, installed, and then silently never dispatched.
        if not role:
            found(node_id, "role", "role_missing", "role is required")
        elif role_ids and role not in role_ids:
            found(node_id, "role", "role_not_in_org", f"role '{role}' is not in the org config")
        if kind != "invoke_workflow":
            if not str(node.get("responsibility", "") or "").strip():
                found(node_id, "responsibility", "responsibility_missing", "responsibility is required")
            if not list(node.get("deliverables", []) or []):
                found(node_id, "deliverables", "deliverables_missing", "deliverables must name at least one path")
        if kind in {"human_do", "human_review"} and not str(node.get("assignee", "") or "").strip():
            found(node_id, "assignee", "assignee_missing",
                  f"a {kind} node must name the person (assignee), 'owner' is allowed")
        if kind == "invoke_workflow" and not str(dict(node.get("invoke", {}) or {}).get("workflow", "") or "").strip():
            found(node_id, "invoke", "invoke_target_missing", "invoke.workflow is required")
        for dep in list(node.get("depends_on", []) or []):
            if str(dep) not in known:
                found(node_id, "depends_on", "unknown_dependency", f"depends_on '{dep}' is not a node", dependency=str(dep))
        for tool in list(node.get("tools", []) or []):
            if not str(tool).strip():
                found(node_id, "tools", "tool_name_empty", "empty tool name")
        canvas = node.get("canvas")
        if canvas not in (None, {}) and not _canvas_position(canvas):
            found(node_id, "canvas", "canvas_invalid", "canvas must be {x, y} with finite numbers")
        model = node.get("model", "")
        if model not in (None, ""):
            from ampuly.plugins.office_ui.step_models import model_known

            if not isinstance(model, str) or not model.strip():
                found(node_id, "model", "model_invalid", "model must be a model id, or left empty for the default")
            elif kind != "ai_do":
                found(node_id, "model", "model_not_ai_step", "only an AI step runs on a model; leave it empty here")
            elif not model_known(model.strip()):
                found(node_id, "model", "model_unknown", f"model '{model.strip()}' is not one the console lists")
        tier = node.get("tier", "")
        if tier not in (None, ""):
            from ampuly.plugins.office_ui.model_tiers import normalize_tier

            if not isinstance(tier, str) or not normalize_tier(tier):
                found(node_id, "tier", "tier_invalid",
                      "tier must be one of light, standard, strong, or left empty for the default")
            elif kind != "ai_do":
                found(node_id, "tier", "tier_not_ai_step",
                      "only an AI step runs on a tier; leave it empty here")
        # 4.7-A07: where 打回 sends the work back. Only a person's review has a 打回 the runtime reads it on
        # (company_mode._run_human_node), and only a step this one comes after can take the work back.
        rework_to = node.get("rework_to", "")
        if rework_to not in (None, ""):
            target = rework_to.strip() if isinstance(rework_to, str) else ""
            if kind != "human_review":
                found(node_id, "rework_to", "rework_to_not_review",
                      "only a human_review step sends work back; leave rework_to empty here")
            elif not target or target not in _steps_before(nodes, node_id):
                found(node_id, "rework_to", "rework_to_invalid",
                      f"rework_to '{target or rework_to}' must be a step this one comes after",
                      rework_to=target or str(rework_to))
        # Q05: the browser universal connector's own parameters (sites, goal, postconditions, output, hand-offs).
        from ampuly.plugins.office_ui.browser_connector import connector_findings

        for field, code, message in connector_findings(node):
            found(node_id, field, code, message)
        # 4.7-A06: the desktop connector's own parameters (apps, goal, done-when conditions, output), in Chinese.
        from ampuly.plugins.office_ui.desktop_connector import desktop_findings

        for field, code, message in desktop_findings(node):
            found(node_id, field, code, message)
    # The same rule ProcessSpec applies when it compiles steps: dependencies must be acyclic, or the
    # nodes on the loop wait on each other forever. The console canvas refuses such a wire, but
    # update_package_nodes is the authority and must not persist one sent by any other client.
    graph = {
        node_id: [str(dep) for dep in list(node.get("depends_on", []) or []) if str(dep) in known]
        for node, node_id in zip(nodes, ids)
        if node_id
    }
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        loop = [str(item) for item in (exc.args[1] if len(exc.args) > 1 else [])]
        found("", "depends_on", "dependency_cycle",
              "node dependencies contain a cycle" + (f": {' -> '.join(loop)}" if loop else ""), nodes=loop)
    return findings


_GRAPH_FINDINGS = frozenset({"id_missing", "id_duplicate", "dependency_cycle"})


def _steps_before(nodes: list[dict[str, Any]], node_id: str) -> list[str]:
    """The steps ``node_id`` comes after -- its depends_on, followed back -- nearest first; a loop is walked once.

    What a review's rework_to may name (4.7-A07): 打回 sends the work back to a step whose output the review saw.
    """
    deps = {
        str(n.get("id", "") or "").strip(): [str(dep).strip() for dep in list(n.get("depends_on", []) or [])]
        for n in nodes
        if isinstance(n, dict)
    }
    before: list[str] = []
    queue = list(deps.get(node_id, []))
    while queue:
        current = queue.pop(0)
        if current == node_id or current in before or current not in deps:
            continue
        before.append(current)
        queue.extend(deps.get(current, []))
    return before


def _browser_fields(node: dict[str, Any], connector_role: bool, summarize: Any) -> dict[str, Any]:
    """A step's browser, as the runtime reads it: its connector, or "none" beside a connector step on its role."""
    summary = summarize(node)
    if summary is not None:
        return {"connector": summary}
    return {"browser": "none"} if connector_role else {}


def validate_nodes(package: WorkflowPackage) -> list[str]:
    """Every node names who does it, what it delivers, and whether a person signs it off."""
    problems: list[str] = []
    for finding in node_findings(package):
        label = str(finding.get("label", "") or "")
        problems.append(f"{label}: {finding['message']}" if label else str(finding["message"]))
    return problems


# The same findings in the owner's words. A "missing" code names the field that is still empty (缺什么); the rest
# say what is wrong with what was filled in. The canvas strip words the same codes (workflowHealth.ts LABELS), so
# the refusal and the strip cannot say two different things about one save. A code neither knows falls back to the
# finding's own sentence, never to nothing.
_FINDING_MISSING_ZH: dict[str, str] = {
    "role_missing": "角色",
    "responsibility_missing": "职责",
    "deliverables_missing": "交付物",
    "assignee_missing": "负责人",
    "invoke_target_missing": "调用哪条流程",
    "connector_domains_missing": "允许的网站",
    "connector_goal_missing": "要在网站上完成什么",
    "connector_postconditions_missing": "完成条件",
    "connector_output_missing": "输出文件",
    "connector_lookup_key_missing": "查询用的钥匙（订单里的哪一项）",
    "connector_lookup_answer_missing": "要从页面取回的答案字段",
    "connector_batch_items_missing": "批量清单来自订单里的哪一项",
    "connector_batch_item_key_missing": "每一行的身份字段",
    # 4.7-A06: the desktop connector's blanks.
    "connector_desktop_apps_missing": "允许的应用",
    "connector_desktop_goal_missing": "要在应用里做成什么",
    "connector_desktop_postconditions_missing": "完成条件",
    "connector_desktop_output_missing": "输出文件",
}
_FINDING_PROBLEM_ZH: dict[str, str] = {
    "id_missing": "有步骤没有 id",
    "id_duplicate": "步骤 id 重复",
    "type_invalid": "步骤类型无效",
    "gate_invalid": "审核方式无效",
    "role_not_in_org": "角色不在这个组织里",
    "unknown_dependency": "依赖了不存在的步骤",
    "dependency_cycle": "步骤依赖成环",
    "input_not_delivered": "需要的输入没有上游步骤交付",
    "tool_name_empty": "有空的工具名",
    "canvas_invalid": "画布位置无效",
    "model_invalid": "模型要填模型编号，或留空用默认",
    "model_not_ai_step": "只有 AI 步骤才选模型，这里请留空",
    "model_unknown": "选的模型不在控制台的列表里",
    "tier_invalid": "档位只能是轻量、标准、加强，或留空用默认",
    "tier_not_ai_step": "只有 AI 步骤才选档位，这里请留空",
    "rework_to_invalid": "「打回时回到」只能选这一步之前的步骤",
    "rework_to_not_review": "只有人审步骤才设「打回时回到」，这里请留空",
    "connector_twice": "一步只能带一个浏览器连接器，第二个网站的活请拆成单独一步",
    "connector_unknown": "这个连接器不是本控制台有的",
    "connector_version": "浏览器连接器的版本不是本控制台提供的",
    "connector_not_ai_step": "浏览器连接器只能放在 AI 步骤上；人做的步骤用自己的浏览器",
    "connector_domain_invalid": "允许的网站里有不是网址的写法",
    "connector_paths_invalid": "限定网址要写成「网站/路径」",
    "connector_paths_unlisted": "限定的网址不属于这一步列出的网站",
    "connector_controls_invalid": "可点的按钮要按页面上的字样一个个列",
    "connector_controls_empty": "可点的按钮列表是空的：列出按钮，或把这一项去掉",
    "connector_controls_long": "可点的按钮写的是按钮名，不是一句话",
    "connector_inputs_invalid": "输入项要写成「名字 → 订单里的哪一项」",
    "connector_input_not_reference": "输入项只能引用订单里的信息，不能把值写死在流程里",
    "connector_postcondition_invalid": "完成条件写得不对",
    "connector_file_type_invalid": "下载文件按类型写（pdf、xlsx、csv……或 * 表示任意）",
    "connector_output_not_deliverable": "输出文件不在这一步的交付物里",
    "connector_handoff_invalid": "交给人处理的设置不对：验证码、上传、登录只能交给人",
    "connector_max_steps_invalid": "最多步数要是 1 到上限之间的整数",
    "connector_login_not_enabled": "按客户保留登录还没启用，登录先交给人",
    "connector_login_invalid": "登录只能交给人处理",
    "connector_decider_invalid": "「由谁决定下一步」的设置写得不对",
    "connector_verify_invalid": "核对方式的设置写得不对",
    "connector_item_outside_batch": "只有批量步骤的条件里才能写 {item.…}",
    "connector_key_outside_lookup": "只有查询步骤的条件里才能写 {key}",
    "connector_lookup_key_not_shown": "查询步骤至少要有一条页面条件包含 {key}",
    "connector_lookup_no_downloads": "查询步骤不以下载为证据；要下载请用操作步骤",
    "connector_lookup_no_result_invalid": "「没查到」的提示语数量或长度超了",
    "connector_lookup_extract_invalid": "答案字段与页面上标签的对应关系写得不对",
    "connector_batch_item_not_shown": "批量步骤至少要有一条页面条件写出这一行自己的字",
    "connector_batch_lookup_keys": "查询用的钥匙、「没查到」的提示语、答案对应关系只属于查询步骤；批量步骤填清单和每一行的身份字段",
    # D1 (policy): the batch step's 「这一步做什么」.
    "connector_distribution_action_invalid": "「这一步做什么」只能选上传、发布或回查",
}


def refused_save_text(package: WorkflowPackage, nodes: list[dict[str, Any]], findings: list[dict[str, Any]]) -> str:
    """A refused save as the owner reads it: which step (by number and name) and what it still lacks, in Chinese.

    ``validate_nodes`` keeps its English sentences (the preflight's ``blockers``, tests and logs read them); this is
    the same findings worded by field for the console. Steps are numbered as the drafts sent them, merged onto the
    stored nodes the way the save judged them. '' when there is nothing to word, so the caller keeps the original
    sentence.
    """
    drafts = [dict(node) for node in nodes if isinstance(node, dict)]
    merged = merge_node_drafts(package.nodes, drafts) if drafts else list(package.nodes)
    place: dict[str, tuple[int, str]] = {}
    for index, node in enumerate(merged, 1):
        node_id = str(node.get("id", "") or "").strip()
        if node_id and node_id not in place:
            place[node_id] = (index, str(node.get("name", "") or "").strip())

    def _problem(finding: dict[str, Any]) -> str:
        code = str(finding.get("code", "") or "")
        text = _FINDING_PROBLEM_ZH.get(code) or str(finding.get("message", "") or "") or code
        if code == "unknown_dependency" and finding.get("dependency"):
            text += f"（{finding['dependency']}）"
        elif code == "input_not_delivered" and finding.get("input"):
            text = f"需要「{finding['input']}」，但没有上游步骤交付它"
        elif code == "dependency_cycle" and finding.get("nodes"):
            text += "：" + " → ".join(place.get(str(n), (0, ""))[1] or str(n) for n in list(finding.get("nodes") or []))
        return text

    steps: dict[str, dict[str, list[str]]] = {}
    graph: list[str] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        code = str(finding.get("code", "") or "")
        node_id = str(finding.get("node_id", "") or "").strip()
        if not node_id:
            graph.append(_problem(finding))
            continue
        bucket = steps.setdefault(node_id, {"missing": [], "wrong": [], "connector_missing": [], "connector_wrong": []})
        prefix = "connector_" if code.startswith("connector_") else ""
        word = _FINDING_MISSING_ZH.get(code)
        if word:
            bucket[prefix + "missing"].append(word)
        else:
            bucket[prefix + "wrong"].append(_problem(finding))

    parts: list[str] = []
    for node_id, bucket in steps.items():
        index, name = place.get(node_id, (0, ""))
        step = f"第 {index} 步「{name or node_id}」" if index else f"步骤「{name or node_id}」"
        for key, lead, sep in (
            ("missing", "还没填：", "、"),
            ("wrong", "有问题：", "；"),
            ("connector_missing", "的连接器还没填：", "、"),
            ("connector_wrong", "的连接器有问题：", "；"),
        ):
            items = list(dict.fromkeys(bucket[key]))
            if items:
                parts.append(step + lead + sep.join(items))
    parts.extend(dict.fromkeys(graph))
    return "这次保存没通过。" + "。".join(parts) + "。" if parts else ""


_PATH_LIKE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


_BLANKS = " \t\r\n\u3000"  # the canvas trims exactly these too (neededDeliverables.ts): the two sides must agree


def _looks_like_path(text: str) -> bool:
    """An input is a claim about a file only when it is written like one; anything else is a note to the reader."""
    value = str(text or "").strip(_BLANKS)
    return "/" in value or "*" in value or bool(_PATH_LIKE.search(value))


def _glob_match(pattern: str, text: str) -> bool:
    """``*`` is any run of characters (slashes included), ``?`` is one -- the same two rules the canvas applies."""
    source = "".join(".*" if char == "*" else "." if char == "?" else re.escape(char) for char in pattern)
    return re.fullmatch(source, text) is not None


def _delivers(deliverable: str, needed: str) -> bool:
    out, need = str(deliverable).strip(_BLANKS), str(needed).strip(_BLANKS)
    return out == need or _glob_match(out, need) or _glob_match(need, out)


def input_findings(package: WorkflowPackage) -> list[dict[str, Any]]:
    """Inputs a step names that no upstream step promises to deliver.

    Business steps hand each other named deliverables; a step may say which ones it needs (``inputs``). This checks
    the path-like ones against everything upstream, direct or further back. It is advice, not a rule: an input can
    also come from the order's intake or an upload, and packages written before this carry free-text inputs. So
    these never enter ``problems`` and never refuse a save -- the preflight and the canvas show them.
    """
    nodes = [node for node in package.nodes if isinstance(node, dict)]
    by_id = {str(node.get("id", "") or ""): node for node in nodes}
    findings: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(node.get("id", "") or "")
        needs = [str(item).strip(_BLANKS) for item in list(node.get("inputs", []) or []) if str(item).strip(_BLANKS) and _looks_like_path(str(item))]
        if not needs:
            continue
        upstream: list[str] = []
        seen: set[str] = set()
        queue = [str(dep) for dep in list(node.get("depends_on", []) or [])]
        while queue:
            current = queue.pop(0)
            if current in seen or current == node_id or current not in by_id:
                continue
            seen.add(current)
            upstream.extend(str(item) for item in list(by_id[current].get("deliverables", []) or []))
            queue.extend(str(dep) for dep in list(by_id[current].get("depends_on", []) or []))
        for need in needs:
            if not any(_delivers(deliverable, need) for deliverable in upstream):
                findings.append({
                    "node_id": node_id, "field": "inputs", "code": "input_not_delivered", "input": need,
                    "message": f"needs '{need}', which no upstream step delivers",
                })
    return findings


def node_findings_for(package: WorkflowPackage, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The findings these drafts would have in this package -- what a refused save can point at. Nothing is written.

    The save validates the drafts *merged onto* the stored nodes (a draft may carry only some fields), so this does
    too: a pointer at a field the save never objected to would be worse than no pointer. The builder's ``connector``
    field is not a key of the node -- ``merge_node_drafts`` drops it and the save turns it into the step's connector
    instance afterwards (``preview_package_nodes``) -- so the same turn happens here, or a connector the canvas just
    added or emptied would have no findings at all, and the refusal it caused would reach the owner unworded.
    """
    # Either channel (4.7-A06): `kind: desktop` in the field is the desktop connector, anything else the browser's.
    from ampuly.plugins.office_ui.desktop_connector import apply_any_connector_draft as apply_connector_draft

    drafts = [dict(n) for n in nodes if isinstance(n, dict)]
    merged = merge_node_drafts(package.nodes, drafts)
    for draft, node in zip(drafts, merged):
        apply_connector_draft(draft, node)
    candidate = WorkflowPackage(root=package.root, manifest={**package.manifest, "nodes": merged})
    return node_findings(candidate)


def _merge_node_tables(
    existing_meta: dict[str, Any], package_id: str, nodes: list[dict[str, Any]] | None,
    *, keep: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Keep one node table per package on the role; the union is what the runtime reads.

    ``keep`` says, by package id, which tables an earlier install left on the role may stay (P): the
    team configuration is shared by the whole machine, so without it a table another project's own flow projected
    would ride along into this project's run. The package being installed is always kept. ``nodes`` is None when
    the package puts no step on this role: only the tables already there are filtered and the union recomputed.
    """
    tables = {
        str(pid): list(table or [])
        for pid, table in dict(existing_meta.get("workflow_node_tables", {}) or {}).items()
        if keep is None or str(pid) == package_id or keep(str(pid))
    }
    if nodes is not None:
        tables[package_id] = list(nodes)
    union: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pkg_nodes in tables.values():
        for node in list(pkg_nodes or []):
            node_id = str(node.get("id", "") or "")
            if node_id and node_id not in seen:
                seen.add(node_id)
                union.append(dict(node))
    merged = dict(existing_meta)
    merged["workflow_node_tables"] = tables
    merged["workflow_nodes"] = union
    return merged


def project_nodes_onto_org(
    org: dict[str, Any], package: WorkflowPackage, *, installed: dict[str, Any] | None = None,
    project_id: str | None = None, packages: list[WorkflowPackage] | None = None,
    skill_bodies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The runtime org for this package: base org + what each role's nodes grant.

    The org directory says who exists; the package says what each node may
    use and must deliver. Installing composes the two so the running org has
    the node's tools (the hard whitelist), skills and standards on the role,
    without the base org ever carrying business rules itself.

    With ``project_id`` (P, 2026-09-25), what earlier installs left on the roles is kept only when it belongs to
    this project or to no project (``usable_by_project``): the node table and the dispatch line of another
    project's own flow are dropped, and a table whose package ``packages`` no longer lists is dropped too, since
    nothing vouches for it. ``None`` keeps everything, as before, for callers that answer no project.
    """
    nodes = package.nodes
    if not nodes:
        return org
    keep: Callable[[str], bool] | None = None
    foreign_dispatch: tuple[str, ...] = ()
    if project_id is not None:
        known = {p.id: p for p in list(packages or [])}
        known[package.id] = package
        keep = lambda pid: pid in known and usable_by_project(known[pid], project_id)  # noqa: E731
        # The dispatch line is keyed by package name; a name a usable package shares stays (its own install
        # replaces the line by that same prefix anyway).
        usable_names = {p.name for p in known.values() if usable_by_project(p, project_id)}
        foreign_dispatch = tuple(
            f"派单纪律（节点表·{p.name}）：" for p in known.values()
            if not usable_by_project(p, project_id) and p.name not in usable_names
        )
    by_role: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        role = str(node.get("role", "") or "").strip()
        # An invoke_workflow node is projected too: the delegator must be
        # able to dispatch it, and the runtime runs it without a model.
        if role:
            by_role.setdefault(role, []).append(node)
    composed = dict(org)
    installed_roles: dict[str, dict[str, Any]] = {}
    for raw in list((installed or {}).get("roles", []) or []):
        if isinstance(raw, dict):
            installed_roles[str(raw.get("id", raw.get("role_id", "")) or "").strip()] = raw
    roles_out: list[dict[str, Any]] = []
    for raw_role in list(org.get("roles", []) or []):
        role = dict(raw_role) if isinstance(raw_role, dict) else raw_role
        if not isinstance(role, dict):
            roles_out.append(role)
            continue
        role_id = str(role.get("id", role.get("role_id", "")) or "").strip()
        role_nodes = by_role.get(role_id, [])
        previous_meta = dict((installed_roles.get(role_id) or {}).get("metadata", {}) or {})
        previous_prompts = [str(x) for x in list((installed_roles.get(role_id) or {}).get("prompt_refs", []) or [])]
        if foreign_dispatch:
            previous_prompts = [line for line in previous_prompts if not line.startswith(foreign_dispatch)]
        if not role_nodes:
            if previous_meta.get("workflow_node_tables"):
                # Another package already projected nodes onto this role; keep them (those this project may see).
                carried = _merge_node_tables(previous_meta, package.id, None, keep=keep)
                if carried.get("workflow_node_tables"):
                    role_meta = dict(role.get("metadata", {}) or {})
                    role_meta.update({k: carried[k] for k in ("workflow_node_tables", "workflow_nodes")})
                    role["metadata"] = role_meta
            role["prompt_refs"] = _with_skill_bodies(
                package,
                _carry_prompt_lines([str(x) for x in list(role.get("prompt_refs", []) or [])], previous_prompts),
                [str(x) for x in list(role.get("skill_refs", []) or [])], bodies=skill_bodies,
            )
            roles_out.append(role)
            continue
        tools = [str(t) for t in list(role.get("tools", []) or [])]
        skills = [str(x) for x in list(role.get("skill_refs", []) or [])]
        prompts = _carry_prompt_lines([str(x) for x in list(role.get("prompt_refs", []) or [])], previous_prompts)
        from ampuly.plugins.office_ui.browser_connector import connector_of, connector_summary, connector_tools
        from ampuly.plugins.office_ui.desktop_connector import desktop_fields, desktop_tools
        from ampuly.plugins.office_ui.node_components import runtime_params

        for node in role_nodes:
            # Q05: a browser connector's tools reach the role here and never through node.tools (a canvas save would
            # otherwise read them back as tool instances of their own). 4.7-A06: a desktop connector's the same way.
            for tool in [*list(node.get("tools", []) or []), *connector_tools(node), *desktop_tools(node)]:
                if str(tool) not in tools:
                    tools.append(str(tool))
            for skill in list(node.get("skills", []) or []):
                if str(skill) not in skills:
                    skills.append(str(skill))
            line = _node_prompt_line(node)
            if line not in prompts:
                prompts.append(line)
        role["tools"] = tools
        role["skill_refs"] = skills
        # Company-mode workers cannot read outside their task workspace, so
        # the skill index's "read SKILL.md with file_read" never works for
        # them (presales stalled on exactly that). The body rides
        # on the role instead; one line per skill, replaced when it changes.
        role["prompt_refs"] = _with_skill_bodies(package, prompts, skills, bodies=skill_bodies)
        role_meta = dict(role.get("metadata", {}) or {})
        role_meta.update({k: previous_meta[k] for k in ("workflow_node_tables",) if k in previous_meta})
        # Q05: once one step of a role works in the browser, the role holds the browser tools; its other steps are
        # marked so the Gateway refuses them the browser (browser_not_in_this_step). A step that works through an API
        # or MCP component must never move to a website after its own outcome was unknown (owner decision 2).
        connector_role = any(connector_of(n) is not None for n in role_nodes)
        # The dispatcher reads this to recognise a human node and to know
        # who signs a gate off; it is data the runtime needs, not a prompt.
        projected_nodes = [
            {
                "id": str(n.get("id", "") or ""),
                "name": str(n.get("name", n.get("id", "")) or ""),
                "type": str(n.get("type", "ai_do") or "ai_do"),
                "assignee": str(n.get("assignee", "") or ""),
                "gate": str(n.get("gate", "none") or "none"),
                "gate_to": str(n.get("gate_to", "") or ""),
                "deliverables": [str(x) for x in list(n.get("deliverables", []) or [])],
                "standard": [str(x) for x in list(n.get("standard", []) or [])],
                "responsibility": str(n.get("responsibility", "") or ""),
                "writes": [str(x) for x in list(n.get("writes", []) or [])],
                "revision_path": str(n.get("revision_path", "") or ""),
                "invoke": dict(n.get("invoke", {}) or {}),
                **_employee_binding_payload(n),
                # A branch and its edges: without these the runtime cannot
                # tell a branch from an ordinary step, and the person at the
                # human node has nothing to choose between.
                "branch_when": str(n.get("branch_when", "") or ""),
                "depends_on": [str(x) for x in list(n.get("depends_on", []) or [])],
                # The step's own model, read by the engine when it builds the step's agent (step_models.py).
                **({"model": str(n.get("model", "")).strip()} if str(n.get("model", "") or "").strip() else {}),
                # The step's tier (轻/标准/强), read by the engine when it resolves the step's model
                # (engine._task_step_tier via node_contract.node_summary). Left out Previously,, so the
                # builder's choice never reached a real run.
                **({"tier": str(n.get("tier", "")).strip()} if str(n.get("tier", "") or "").strip() else {}),
                # 4.7-A07: where 打回 on a person's review sends the work back (read by company_mode._run_human_node).
                **({"rework_to": str(n.get("rework_to", "")).strip()} if str(n.get("rework_to", "") or "").strip() else {}),
                # 4.7-A16: the step's own skill / tool parameters (credential-shaped ones left out), which its run
                # states to the model (node_contract.step_settings_lines). Until now they stopped at workflow.yaml.
                **({"component_params": runtime_params(n)} if runtime_params(n) else {}),
                **_browser_fields(n, connector_role, connector_summary),
                # 4.7-A06: the step's desktop connector as its desktop tools read it (workflow_node.desktop).
                **desktop_fields(n),
            }
            for n in role_nodes
        ]
        role_meta = _merge_node_tables(role_meta, package.id, projected_nodes, keep=keep)
        role["metadata"] = role_meta
        roles_out.append(role)
    # Whoever delegates must name work items after the nodes, so the runtime
    # can match a work item to its node (scope_key == node id).
    node_index = "；".join(
        f"{str(n.get('id', '') or '')} = {str(n.get('name', n.get('id', '')) or '')}（{str(n.get('role', '') or '')}"
        + ("，自动节点：派给该角色即可，不跑模型" if str(n.get("type", "") or "") == "invoke_workflow" else "")
        + "）"
        for n in nodes
    )
    # One dispatch line per package, keyed by the package name the order
    # message quotes; an org serving several packages carries one line each.
    dispatch_prefix = f"派单纪律（节点表·{package.name}）："
    for role in roles_out:
        if isinstance(role, dict) and list(role.get("can_spawn", []) or []):
            prompts = [str(x) for x in list(role.get("prompt_refs", []) or [])]
            prompts = [p for p in prompts if not p.startswith(dispatch_prefix) and not p.startswith("派单纪律（节点表）：")]
            prompts.append(
                dispatch_prefix
                + f"接到「{package.name}」的单时，每个工作项的 scope_key 必须用节点 id，标题用节点名，"
                + f"一个节点一个工作项，不多不少，只派这个包的节点：{node_index}。"
            )
            role["prompt_refs"] = prompts
    composed["roles"] = roles_out
    meta = dict(composed.get("metadata", {}) or {})
    meta["workflow_package"] = {"id": package.id, "version": str(package.manifest.get("version", "") or ""), "nodes": [str(n.get("id", "")) for n in nodes]}
    composed["metadata"] = meta
    return composed


# Prompt lines earlier installs put on a role; they belong to the org as a
# whole (one per package / per skill), so a later package must not drop them.
_CARRIED_PROMPT_PREFIXES = ("派单纪律（节点表·", "技能「")


def _carry_prompt_lines(base_lines: list[str], previous_lines: list[str]) -> list[str]:
    out = [str(x) for x in base_lines]
    for line in previous_lines:
        text = str(line)
        if text.startswith(_CARRIED_PROMPT_PREFIXES) and text not in out:
            out.append(text)
    return out


def _with_skill_bodies(package: WorkflowPackage, prompts: list[str], skills: list[str], *, bodies: dict[str, str] | None = None) -> list[str]:
    out = list(prompts)
    for skill in skills:
        body = _skill_body(package, skill) if bodies is None else bodies.get(skill, "")
        if not body:
            continue
        prefix = f"技能「{skill}」正文（已给全文，不要再读文件）：\n"
        out = [p for p in out if not str(p).startswith(prefix)]
        out.append(prefix + body)
    return out


def _skill_body(package: WorkflowPackage, skill: str, *, limit: int = 6000) -> str:
    """The SKILL.md text without its frontmatter, or empty if the package lacks it."""
    path = package.skills_root / skill / "SKILL.md"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.match(r"^---\s*\n.*?\n---\s*\n", text, re.DOTALL)
    if match:
        text = text[match.end():]
    return text.strip()[:limit]


def _node_prompt_line(node: dict[str, Any]) -> str:
    name = str(node.get("name", node.get("id", "")) or "")
    parts = [f"节点「{name}」职责：{str(node.get('responsibility', '') or '').strip()}"]
    standard = [str(x) for x in list(node.get("standard", []) or []) if str(x).strip()]
    if standard:
        parts.append("交付标准：" + "；".join(standard))
    deliverables = [str(x) for x in list(node.get("deliverables", []) or []) if str(x).strip()]
    if deliverables:
        parts.append("交付路径：" + "、".join(deliverables))
    writes = [str(x) for x in list(node.get("writes", []) or []) if str(x).strip()]
    if writes:
        parts.append("只能写入：" + "、".join(writes))
    browser = _connector_prompt(node)
    if browser:
        parts.append(browser)
    # 4.7-A06: a step carrying the desktop connector is told where, towards what, and that code decides done.
    from ampuly.plugins.office_ui.desktop_connector import desktop_prompt

    desktop = desktop_prompt(node)
    if desktop:
        parts.append(desktop)
    gate = str(node.get("gate", "none") or "none")
    gate_to = str(node.get("gate_to", "") or "owner")
    if gate == "human_required":
        parts.append(f"完成后必须用 request_user_input 请 {gate_to} 审核，放行前不得进入下一步")
    elif gate == "ai_first_human_spot_check":
        parts.append(f"完成后自检交付标准，{gate_to} 抽查；发现不符即打回")
    return "。".join(parts) + "。"


def _connector_prompt(node: dict[str, Any]) -> str:
    """Q05 B5: what a step carrying the browser connector is told -- where, towards what, and that code decides done.
    Empty for every other step (their prompt line is unchanged)."""
    from ampuly.core.order_layout import step_segment
    from ampuly.plugins.office_ui.browser_connector import connector_summary

    summary = connector_summary(node)
    if summary is None:
        return ""
    downloads = (f"浏览器为这一步下载的文件在 <订单文件夹>/steps/{step_segment(node.get('id', ''))}/downloads/"
                 "（只读；订单文件夹的完整路径见运行说明里的 Order folder 一行，读时写完整路径），用 list_dir / file_read 读；"
                 "只能读文本类（csv、txt、md、json、xml），pdf、表格等读不出内容时写明并交给人")
    sites = "、".join(summary["allowed_domains"]) or "（未指定）"
    inputs = "、".join(summary["inputs"])
    output = summary["output"]
    fields = "、".join(output["fields"])
    decider = summary["decider"]
    parts = [f"这一步在内置浏览器里做，只在这些网站上：{sites}；要做成：{summary['goal'] or '（未写明）'}"]
    if inputs:
        parts.append(f"用到的订单资料：{inputs}（从订单的输入里取）")
    if decider["jev"]:
        parts.append("先 browser_navigate 到网站、browser_observe 看页面，可用 browser_decide_act 让 PULY（JEV）走低风险的几步"
                     + ("（它只给建议，由你决定）" if decider["mode"] == "suggest" else ""))
    if summary.get("verify", {}).get("mode") == "batch":
        # 批量自动化 P4: the list is the ORDER'S. The model is told which rows exist and may say only which one it
        # is on; it cannot add one, drop one or invent a key, and the runtime refuses a key the list does not carry.
        item_key = str(summary["verify"].get("item_key", "") or "条目")
        parts.append(f"这是批量步骤：订单里有一份清单，每一条单独做、单独核验。每次动手前先调用 "
                     f"browser_decide_act(item='<这一条的{item_key}>')，说明现在做的是哪一条；"
                     "清单是订单的，你不能增加、删除或改写任何一行，写一个清单里没有的钥匙会被拒绝")
        parts.append("做完一条就调用 browser_decide_act(check_only=true, item='<同一条>')，由代码核对这一条；"
                     "一条没过不影响已经过的那些，接着做下一条")
        parts.append(f"**不要自己写 {output['path'] or '交付文件'}**：批量步骤的结果台账由代码统一写，"
                     "你去写会把已经记下的条目整份盖掉")
        parts.append(downloads)
        return "；".join(parts)
    if summary.get("verify", {}).get("mode") == "lookup":
        key_name = str(summary["verify"].get("key", {}).get("from", "")).rsplit(".", 1)[-1]
        parts.append(f"这是查询步骤：只读网站，不提交、不写入（页面若要写入会被拦下）；用本单的 {key_name or '钥匙'} 查到结果即可，"
                     "直接打开结果网址也可以")
        parts.append("查到后调用 browser_decide_act(check_only=true)：代码核对页面同一块里有本单的钥匙和答案、且不是「查无结果」，"
                     f"并从页面摘出答案{('（' + fields + '）') if fields else ''}；done_verified 后把返回的 values 原样写到 "
                     f"{output['path'] or '指定的交付'}，不要自己改写；done_unverified 就写明原因并交给人，不要自称完成")
        parts.append(downloads)
        return "；".join(parts)
    parts.append("字由你来填；提交、发送、支付、删除都要带预期提案等审批；验证码、上传、登录用 browser_request_human 交给人")
    parts.append("做完先调用 browser_decide_act(check_only=true) 由代码核对：done_verified 才算完成，"
                 f"再把结果{('（' + fields + '）') if fields else ''}写到 {output['path'] or '指定的交付'}；"
                 "done_unverified 就写明原因并交给人，不要自称完成")
    parts.append(downloads)
    return "；".join(parts)


def load_package(root: Path) -> WorkflowPackage | None:
    manifest_path = root / "workflow.yaml"
    if not manifest_path.is_file():
        return None
    try:
        raw = manifest_path.read_bytes()
        manifest = yaml.safe_load(raw.decode("utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return WorkflowPackage(root=root, manifest={"id": root.name}, problems=[f"workflow.yaml unreadable: {exc}"])
    if not isinstance(manifest, dict):
        return WorkflowPackage(root=root, manifest={"id": root.name}, problems=["workflow.yaml must be a mapping"])
    package = WorkflowPackage(root=root, manifest=manifest, tests=load_test_records(root),
                              loaded_revision=hashlib.sha256(raw).hexdigest())
    package.problems = validate_package(package)
    return package


# Stages are presentation groups stored beside the package.
#
# ``<package>/stages.yaml`` groups a package's steps into the stages a project page reads them by, with the words that
# explain each group (what comes in, what is done, what comes out, what happens next, where a 打回 goes). The package's
# manifest, README and organisation config are not modified when loading stages. A
# package with no such file simply has no stages. The runtime never reads this file: a stage is not a dependency, not a
# gate and not an order of execution; the steps' ``depends_on`` is still the only thing that decides what runs when.
STAGES_FILE = "stages.yaml"
_STAGE_TEXT_FIELDS = ("input", "work", "output", "next", "back")
_STAGES_WARNED: set[str] = set()


def _stages_warning(package: WorkflowPackage, reason: str) -> None:
    """Say once per process why a package's stages file was set aside; the package itself still loads."""
    from loguru import logger

    key = f"{package.root / STAGES_FILE}::{reason}"
    if key in _STAGES_WARNED:
        return
    _STAGES_WARNED.add(key)
    logger.warning("stages file set aside for {} ({}): {}", package.id or package.root.name, package.root / STAGES_FILE, reason)


def load_stages(package: WorkflowPackage) -> list[dict[str, Any]]:
    """The stages of one package: ``[{id, name, nodes, input, work, output, next, back, parallel}]``, or ``[]``.

    Read-only. The file must be a mapping with a ``stages`` list; every stage needs a unique ``id``, a ``name`` and a
    ``nodes`` list naming steps that exist in the package, and a step may be in at most one stage. Anything else sets
    the whole file aside (``[]`` plus one warning) rather than showing half a grouping as if it were the design.
    """
    path = package.root / STAGES_FILE
    if not path.is_file():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        _stages_warning(package, f"unreadable: {exc}")
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("stages", []), list):
        _stages_warning(package, "must be a mapping with a `stages` list")
        return []
    known = {str(node.get("id", "") or "") for node in package.nodes}
    placed: dict[str, str] = {}
    ids: set[str] = set()
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(list(raw.get("stages", []) or [])):
        if not isinstance(entry, dict):
            _stages_warning(package, f"stages[{index}] must be a mapping")
            return []
        group_id = str(entry.get("id", "") or "").strip()
        name = str(entry.get("name", "") or "").strip()
        nodes_raw = entry.get("nodes", [])
        if not group_id or not name or not isinstance(nodes_raw, list):
            _stages_warning(package, f"stages[{index}] needs id, name and a nodes list")
            return []
        if group_id in ids:
            _stages_warning(package, f"stage id {group_id!r} appears twice")
            return []
        ids.add(group_id)
        nodes: list[str] = []
        for node_id in nodes_raw:
            node = str(node_id or "").strip()
            if node not in known:
                _stages_warning(package, f"stage {group_id!r} names a step the package does not have: {node!r}")
                return []
            if node in placed:
                _stages_warning(package, f"step {node!r} is in two stages: {placed[node]!r} and {group_id!r}")
                return []
            placed[node] = group_id
            nodes.append(node)
        out.append({
            "id": group_id,
            "name": name,
            "nodes": nodes,
            **{field: str(entry.get(field, "") or "").strip() for field in _STAGE_TEXT_FIELDS},
            "parallel": bool(entry.get("parallel", False)),
        })
    return out


def _guard_manifest_writer(writer):
    """Keep package-owned asset edits and their manifest CAS in one lock."""
    @wraps(writer)
    def guarded(*args, **kwargs):
        package = args[0] if args else kwargs.get("candidate", kwargs.get("package"))
        with _manifest_lock(package.root / "workflow.yaml"):
            _assert_loaded_revision(package)
            return writer(*args, **kwargs)
    return guarded


@contextmanager
def observed_package_revision(package: WorkflowPackage, expected_revision: str):
    """Bind an external caller's observation to the same lock as the write.

    A freshly loaded server package is not a substitute for the caller's
    token. Direct package writers instead use their supplied object's exact
    loaded_revision, which is checked by _guard_manifest_writer.
    """
    if not isinstance(expected_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
        raise ValueError("workflow write requires expected_revision from the observed package")
    with _manifest_lock(package.root / "workflow.yaml"):
        _assert_loaded_revision(package)
        if expected_revision != package.loaded_revision:
            raise ValueError("workflow draft revision conflict: re-read the exact manifest before saving")
        yield


@_guard_manifest_writer
def publish_package(package: WorkflowPackage, *, actor: str, employee_runtime_host=None, composition_gate=None) -> WorkflowPackage:
    """Promote a tested package without rewriting its test history.

    Publication is an explicit, owner-facing command.  It only changes the
    manifest status and appends publication metadata; existing pass/fail test
    records remain immutable files.  The replacement is same-directory and
    atomic so a crash cannot leave a half-written workflow manifest.
    """
    employee_runtime_readiness(package, employee_runtime_host)
    if package.status != "testing":
        raise ValueError(f"workflow {package.id!r} is {package.status}, not testing")
    if not str(actor or "").strip():
        raise ValueError("publication actor is required")
    approval = dict(package.manifest.get("release_approval", {}) or {})
    if str(approval.get("status", "") or "").strip().lower() != "approved":
        raise ValueError("workflow release approval is not approved")
    if approval.get("candidate_fingerprint") != _candidate_fingerprint(package):
        raise ValueError("release approval is stale; request approval again")
    _verify_release_evidence_file(package, approval)
    readiness = employee_runtime_readiness(package, employee_runtime_host)
    if readiness["blockers"]:
        raise ValueError("workflow is not ready to publish: " + "; ".join(readiness["blockers"]))
    # D thin wiring: formal composition gate (default off / only when caller supplies gate).
    from .employee_verifiable_template import apply_formal_publish_gate
    apply_formal_publish_gate(composition_gate)
    manifest_path = package.root / "workflow.yaml"
    manifest = dict(package.manifest)
    manifest["status"] = "published"
    publication = dict(manifest.get("publication", {}) or {})
    evidence = package.release_evidence
    # Keep the exact pre-publication manifest as a local, immutable rollback
    # point.  The publication hash alone proves identity but cannot restore a
    # package after an accidental edit or a failed upgrade.
    rollback_dir = package.root / ".releases"
    rollback_hash = str(evidence.get("manifest_sha256", "") or "")
    rollback_version = str(evidence.get("version", "") or "unversioned").strip() or "unversioned"
    safe_version = re.sub(r"[^A-Za-z0-9_.-]+", "_", rollback_version)
    snapshot_name = f"{safe_version}-{rollback_hash[:12] or uuid.uuid4().hex[:12]}.yaml"
    snapshot_path = rollback_dir / snapshot_name
    snapshot_bytes = manifest_path.read_bytes()
    if not package.loaded_revision or hashlib.sha256(snapshot_bytes).hexdigest() != package.loaded_revision:
        raise ValueError("workflow draft revision conflict before publication snapshot")
    try:
        rollback_dir.mkdir(parents=True, exist_ok=True)
        if not snapshot_path.exists():
            with snapshot_path.open("xb") as snapshot:
                snapshot.write(snapshot_bytes)
    except OSError as exc:
        raise ValueError(f"cannot create rollback snapshot: {exc}") from exc
    publication.update({
        "published_at": datetime.now().isoformat(timespec="seconds"),
        "published_by": str(actor).strip(),
        "evidence_test": package.latest_test.path.name if package.latest_test else "",
        # Keep the pre-publication evidence fingerprint in the manifest.  The
        # publication metadata itself changes the manifest hash, so retaining
        # this value is what lets an auditor identify exactly what was signed.
        "evidence_workflow_version": str(evidence.get("version", "") or ""),
        "evidence_manifest_sha256": str(evidence.get("manifest_sha256", "") or ""),
        "evidence_skill_sha256": dict(evidence.get("skill_sha256", {}) or {}),
        "rollback_snapshot": str(snapshot_path.relative_to(package.root)).replace("\\", "/"),
        "rollback_manifest_sha256": rollback_hash,
        "rollback_workflow_version": rollback_version,
        "evidence_file": str(approval.get("evidence_file", "") or ""),
        "evidence_file_sha256": str(approval.get("evidence_file_sha256", "") or ""),
    })
    manifest["publication"] = publication
    return _write_manifest_atomic(package, manifest, suffix="publish")


def _write_manifest_atomic(package: WorkflowPackage, manifest: dict[str, Any], *, suffix: str) -> WorkflowPackage:
    manifest_path = package.root / "workflow.yaml"
    with _manifest_lock(manifest_path):
        _assert_loaded_revision(package)
        temporary = manifest_path.with_name(f".workflow.yaml.{suffix}-{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
            replace_with_retry(temporary, manifest_path)
        finally:
            if temporary.exists():
                temporary.unlink()
    return load_package(package.root) or WorkflowPackage(root=package.root, manifest=manifest, tests=package.tests)


_RELEASE_VOLATILE_KEYS = frozenset({
    "release_approval",
    "release_approval_history",
    "publication",
    # Rollout observations are post-publication operational evidence. They
    # must never invalidate the immutable release candidate fingerprint.
    "rollout",
})

ROLLOUT_OUTCOMES = frozenset({"passed", "failed", "unknown"})
ROLLOUT_STATES = frozenset({"canary", "healthy", "unhealthy", "insufficient", "rolled_back"})


def _rollout_root(package: WorkflowPackage) -> Path:
    """Return the package-local rollout ledger directory."""
    return package.root / ".releases" / "rollout"


def _rollout_key(package: WorkflowPackage) -> str:
    version = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(package.manifest.get("version", "") or "unversioned"))
    return f"{version}-{_candidate_fingerprint(package)[:16]}"


def _rollout_observation_dir(package: WorkflowPackage) -> Path:
    return _rollout_root(package) / _rollout_key(package)


def _load_rollout_observations(package: WorkflowPackage) -> list[dict[str, Any]]:
    """Read only evidence committed by the manifest, never directory orphans.

    Legacy counts do not identify which files committed. Such evidence must
    remain unverified until an explicit, separately authorized migration;
    scanning the directory cannot reconstruct a lost commit boundary.
    """
    rollout = dict(package.manifest.get("rollout", {}) or {})
    directory = _rollout_observation_dir(package)
    if "observation_schema" not in rollout:
        count = rollout.get("observation_count", 0)
        if type(count) is not int or count != 0 or (directory.exists() and any(directory.iterdir())):
            raise ValueError("blocked_legacy_observation_provenance: observation files have no manifest commit references")
        return []
    refs = rollout.get("committed_observations")
    if (type(rollout.get("observation_schema")) is not int or rollout["observation_schema"] != 1
            or not isinstance(refs, list) or type(rollout.get("observation_count")) is not int
            or rollout["observation_count"] != len(refs)):
        raise ValueError("blocked_rollout_observation_integrity: invalid manifest observation index")
    observations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        if not isinstance(ref, dict):
            raise ValueError("blocked_rollout_observation_integrity: invalid observation reference")
        observation_id = ref.get("observation_id")
        digest = ref.get("evidence_sha256")
        if (not isinstance(observation_id, str) or not re.fullmatch(r"obs-[0-9a-f]{32}", observation_id)
                or observation_id in seen or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("blocked_rollout_observation_integrity: invalid or duplicate observation identity")
        seen.add(observation_id)
        path = directory / f"{observation_id}.json"
        relative = path.relative_to(package.root).as_posix()
        if ref.get("evidence_file") != relative:
            raise ValueError("blocked_rollout_observation_integrity: observation path does not match its identity")
        try:
            for node in (path, *path.parents):
                if node.is_symlink() or getattr(node, "is_junction", lambda: False)():
                    raise ValueError("observation path contains a symlink or junction")
                if node == package.root:
                    break
            path.resolve().relative_to(package.root.resolve())
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("observation hash differs from manifest reference")
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ValueError("blocked_rollout_observation_integrity: committed observation cannot be verified") from exc
        if (not isinstance(value, dict) or value.get("observation_id") != observation_id
                or value.get("candidate_fingerprint") != rollout.get("candidate_fingerprint")
                or not isinstance(value.get("outcome"), str) or value["outcome"] not in ROLLOUT_OUTCOMES):
            raise ValueError("blocked_rollout_observation_integrity: observation content does not match the committed identity")
        item = dict(value)
        item["evidence_file"] = relative
        item["evidence_sha256"] = digest
        observations.append(item)
    return observations


@_guard_manifest_writer
def start_rollout(
    package: WorkflowPackage,
    *,
    actor: str,
    canary_percent: int = 10,
    min_observations: int = 1,
    max_failure_rate: float = 0.05,
) -> WorkflowPackage:
    """Start a local, deterministic canary rollout ledger for a published package.

    This records policy and intent only; it never routes customer traffic or
    invokes a provider.  Production traffic remains behind the existing
    explicit invocation boundary.
    """
    if package.status != "published":
        raise ValueError(f"workflow {package.id!r} is {package.status}, not published")
    clean_actor = str(actor or "").strip()
    if not clean_actor:
        raise ValueError("rollout actor is required")
    try:
        percent = int(canary_percent)
        minimum = int(min_observations)
        failure_rate = float(max_failure_rate)
    except (TypeError, ValueError) as exc:
        raise ValueError("rollout policy values are invalid") from exc
    if not 1 <= percent <= 100:
        raise ValueError("canary_percent must be between 1 and 100")
    if minimum < 1:
        raise ValueError("min_observations must be at least 1")
    if not 0 <= failure_rate <= 1:
        raise ValueError("max_failure_rate must be between 0 and 1")
    current = dict(package.manifest.get("rollout", {}) or {})
    if str(current.get("status", "") or "").strip().lower() in {"canary", "healthy"}:
        raise ValueError("workflow already has an active rollout")
    rollout = {
        "status": "canary",
        "candidate_fingerprint": _candidate_fingerprint(package),
        "version": str(package.manifest.get("version", "") or ""),
        "canary_percent": percent,
        "min_observations": minimum,
        "max_failure_rate": failure_rate,
        "started_by": clean_actor,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "observation_schema": 1,
        "committed_observations": [],
        "observation_count": 0,
    }
    _rollout_observation_dir(package).mkdir(parents=True, exist_ok=True)
    manifest = dict(package.manifest)
    manifest["rollout"] = rollout
    return _write_manifest_atomic(package, manifest, suffix="rollout-start")


@_guard_manifest_writer
def record_rollout_observation(
    package: WorkflowPackage,
    *,
    actor: str,
    outcome: str,
    note: str = "",
    run_id: str = "",
) -> WorkflowPackage:
    """Append one immutable canary observation without executing anything."""
    if package.status != "published":
        raise ValueError(f"workflow {package.id!r} is {package.status}, not published")
    rollout = dict(package.manifest.get("rollout", {}) or {})
    if str(rollout.get("status", "") or "").strip().lower() != "canary":
        raise ValueError("workflow has no active canary rollout")
    clean_actor = str(actor or "").strip()
    if not clean_actor:
        raise ValueError("rollout observation actor is required")
    clean_outcome = str(outcome or "").strip().lower()
    if clean_outcome not in ROLLOUT_OUTCOMES:
        raise ValueError("rollout outcome must be passed, failed, or unknown")
    clean_run_id = str(run_id or "").strip()
    existing_observations = _load_rollout_observations(package)
    directory = _rollout_observation_dir(package)
    if clean_run_id:
        for existing in existing_observations:
            if str(existing.get("run_id", "")) == clean_run_id:
                if (existing.get("outcome") != clean_outcome or existing.get("note", "") != str(note or "").strip()[:2000]
                        or existing.get("recorded_by") != clean_actor):
                    raise ValueError("rollout run_id was already committed with different observation content")
                return package
    payload = {
        "observation_id": f"obs-{uuid.uuid4().hex}",
        "run_id": clean_run_id,
        "candidate_fingerprint": str(rollout.get("candidate_fingerprint", "") or ""),
        "outcome": clean_outcome,
        "note": str(note or "").strip()[:2000],
        "recorded_by": clean_actor,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{payload['observation_id']}.json"
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        with path.open("xb") as handle:
            handle.write(raw)
    except OSError as exc:
        raise ValueError(f"cannot write rollout observation: {exc}") from exc
    refreshed_rollout = dict(rollout)
    refs = [dict(ref) for ref in rollout.get("committed_observations", [])]
    refs.append({"observation_id": payload["observation_id"],
                 "evidence_file": path.relative_to(package.root).as_posix(),
                 "evidence_sha256": hashlib.sha256(raw).hexdigest()})
    refreshed_rollout["observation_schema"] = 1
    refreshed_rollout["committed_observations"] = refs
    refreshed_rollout["observation_count"] = len(refs)
    manifest = dict(package.manifest)
    manifest["rollout"] = refreshed_rollout
    return _write_manifest_atomic(package, manifest, suffix="rollout-observation")


def evaluate_rollout(package: WorkflowPackage) -> dict[str, Any]:
    """Evaluate canary evidence using a stable, explainable local policy."""
    rollout = dict(package.manifest.get("rollout", {}) or {})
    evidence_error = ""
    try:
        observations = _load_rollout_observations(package)
    except (OSError, ValueError) as exc:
        evidence_error = str(exc)
        observations = []
    current_fingerprint = _candidate_fingerprint(package)
    recorded_fingerprint = str(rollout.get("candidate_fingerprint", "") or current_fingerprint)
    raw_minimum = rollout.get("min_observations", 1)
    raw_failure_rate = rollout.get("max_failure_rate", 0.05)
    minimum_valid = type(raw_minimum) is int and raw_minimum >= 1
    rate_valid = (type(raw_failure_rate) in (int, float)
                  and 0 <= raw_failure_rate <= 1 and math.isfinite(raw_failure_rate))
    minimum = raw_minimum if minimum_valid else None
    max_failure_rate = float(raw_failure_rate) if rate_valid else None
    failed = sum(1 for item in observations if item.get("outcome") == "failed")
    unknown = sum(1 for item in observations if item.get("outcome") == "unknown")
    count = len(observations)
    failure_rate = (failed / count) if count else 0.0
    if evidence_error:
        status = "blocked_legacy_observation_provenance" if evidence_error.startswith("blocked_legacy_observation_provenance") else "blocked_rollout_observation_integrity"
        blockers = [evidence_error]
    elif not minimum_valid or not rate_valid:
        status = "blocked_invalid_rollout_policy"
        blockers = ["rollout policy must have a positive integer minimum and a finite failure rate between 0 and 1"]
    elif recorded_fingerprint != current_fingerprint:
        status = "unhealthy"
        blockers = ["rollout candidate fingerprint drifted; start a fresh rollout"]
    elif count < minimum:
        status = "insufficient"
        blockers = [f"need at least {minimum} rollout observations (have {count})"]
    elif unknown:
        status = "unhealthy"
        blockers = [f"{unknown} rollout observation(s) are unknown and require review"]
    elif failure_rate > max_failure_rate:
        status = "unhealthy"
        blockers = [f"failure rate {failure_rate:.3f} exceeds policy {max_failure_rate:.3f}"]
    else:
        status = "healthy"
        blockers = []
    return {
        "workflow_id": package.id,
        "candidate_fingerprint": recorded_fingerprint,
        "rollout_status": status,
        "observed_count": count,
        "failed_count": failed,
        "unknown_count": unknown,
        "failure_rate": failure_rate,
        "policy": {"min_observations": minimum, "max_failure_rate": max_failure_rate},
        "blockers": blockers,
        "observations": observations,
    }


@_guard_manifest_writer
def finalize_rollout(package: WorkflowPackage, *, actor: str) -> WorkflowPackage:
    """Close a healthy canary as the package's recorded rollout outcome."""
    if package.status != "published":
        raise ValueError(f"workflow {package.id!r} is {package.status}, not published")
    clean_actor = str(actor or "").strip()
    if not clean_actor:
        raise ValueError("rollout finalization actor is required")
    evaluation = evaluate_rollout(package)
    if evaluation["rollout_status"] != "healthy":
        raise ValueError("rollout finalization requires a healthy evaluation")
    rollout = dict(package.manifest.get("rollout", {}) or {})
    rollout.update({
        "status": "healthy",
        "finalized_by": clean_actor,
        "finalized_at": datetime.now().isoformat(timespec="seconds"),
        "evaluation": evaluation,
    })
    manifest = dict(package.manifest)
    manifest["rollout"] = rollout
    return _write_manifest_atomic(package, manifest, suffix="rollout-finalize")


@_guard_manifest_writer
def auto_rollback_package(package: WorkflowPackage, *, actor: str) -> WorkflowPackage:
    """Perform an explicit owner-triggered rollback after an unhealthy canary."""
    evaluation = evaluate_rollout(package)
    if evaluation["rollout_status"] != "unhealthy":
        raise ValueError("automatic rollback requires an unhealthy rollout evaluation")
    manifest = _prepare_rollback_manifest(package, actor=actor)
    rollback = dict(manifest.get("rollback", {}) or {})
    rollback["reason"] = "automatic rollback gate: " + "; ".join(evaluation["blockers"])
    rollback["rollout_evaluation"] = evaluation
    manifest["rollback"] = rollback
    manifest["rollout"] = {**dict(package.manifest.get("rollout", {}) or {}), "status": "rolled_back", "evaluated_at": datetime.now().isoformat(timespec="seconds")}
    return _write_manifest_atomic(package, manifest, suffix="rollout-rollback")


def _candidate_fingerprint(package: WorkflowPackage) -> str:
    """Fingerprint the candidate while excluding approval bookkeeping itself."""
    manifest = {
        str(key): value
        for key, value in package.manifest.items()
        if str(key) not in _RELEASE_VOLATILE_KEYS
    }
    payload = {
        "manifest": manifest,
        "skill_sha256": {
            contract.skill_id: package._file_sha256(contract.path)
            for contract in package.skill_contracts
            if contract.path.is_file()
        },
        "test_records": [test.to_payload() for test in package.tests],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def package_candidate_fingerprint(package: WorkflowPackage) -> str:
    """Public stable digest used by owner decisions outside release handling."""

    return _candidate_fingerprint(package)


def _write_release_evidence_file(package: WorkflowPackage, *, fingerprint: str) -> tuple[str, str]:
    """Persist one immutable, local evidence bundle for a release request."""
    evidence_dir = package.root / ".releases" / "evidence"
    version = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(package.manifest.get("version", "") or "unversioned"))
    relative = Path(".releases") / "evidence" / f"{version}-{fingerprint[:16]}.json"
    path = package.root / relative
    evidence = dict(package.release_evidence)
    evidence["candidate_fingerprint"] = fingerprint
    evidence["generated_at"] = datetime.now().isoformat(timespec="seconds")
    content = json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing_bytes = path.read_bytes()
            try:
                existing = json.loads(existing_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("release evidence path contains invalid JSON") from exc
            if not isinstance(existing, dict) or existing.get("candidate_fingerprint") != fingerprint:
                raise ValueError("release evidence path already contains different content")
            return str(relative).replace("\\", "/"), hashlib.sha256(existing_bytes).hexdigest()
        else:
            with path.open("x", encoding="utf-8", newline="") as handle:
                handle.write(content)
    except OSError as exc:
        raise ValueError(f"cannot write release evidence: {exc}") from exc
    return str(relative).replace("\\", "/"), hashlib.sha256(content.encode("utf-8")).hexdigest()


def _verify_release_evidence_file(package: WorkflowPackage, approval: dict[str, Any]) -> None:
    relative = str(approval.get("evidence_file", "") or "").strip()
    expected_hash = str(approval.get("evidence_file_sha256", "") or "").strip()
    if not relative or not expected_hash:
        raise ValueError("release approval is missing immutable evidence file")
    root = package.root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("release evidence escapes workflow package") from exc
    if not path.is_file() or WorkflowPackage._file_sha256(path) != expected_hash:
        raise ValueError("release evidence file is missing or has changed")


def rollback_preview(package: WorkflowPackage) -> dict[str, Any]:
    """Inspect a publication rollback point without changing package state."""
    publication = dict(package.manifest.get("publication", {}) or {})
    relative = str(publication.get("rollback_snapshot", "") or "").strip()
    expected = str(publication.get("rollback_manifest_sha256", "") or "").strip()
    result: dict[str, Any] = {
        "available": False,
        "status": "not_published" if package.status != "published" else "missing",
        "snapshot": relative.replace("\\", "/"),
        "expected_sha256": expected,
        "actual_sha256": "",
        "workflow_version": str(publication.get("rollback_workflow_version", "") or ""),
        "blockers": [],
    }
    if package.status != "published":
        result["blockers"] = ["workflow is not published"]
        return result
    if not relative:
        result["blockers"] = ["publication has no rollback snapshot"]
        return result
    root = package.root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        result["status"] = "invalid"
        result["blockers"] = ["rollback snapshot escapes workflow package"]
        return result
    if not path.is_file():
        result["blockers"] = ["rollback snapshot is missing"]
        return result
    actual = WorkflowPackage._file_sha256(path)
    result["actual_sha256"] = actual
    if not expected:
        result["status"] = "unverified"
        result["blockers"] = ["rollback snapshot has no recorded hash"]
        return result
    if actual != expected:
        result["status"] = "tampered"
        result["blockers"] = ["rollback snapshot hash does not match publication record"]
        return result
    result["available"] = True
    result["status"] = "ready"
    return result


@_guard_manifest_writer
def request_release_approval(package: WorkflowPackage, *, actor: str, note: str = "", employee_runtime_host=None) -> WorkflowPackage:
    """Create an explicit release-approval record without publishing."""
    if package.status != "testing":
        raise ValueError(f"workflow {package.id!r} is {package.status}, not testing")
    if not str(actor or "").strip():
        raise ValueError("release approval requester is required")
    existing = dict(package.manifest.get("release_approval", {}) or {})
    approval_status = str(existing.get("status", "") or "").strip().lower()
    if approval_status == "pending":
        if existing.get("candidate_fingerprint") != _candidate_fingerprint(package):
            raise ValueError("pending release approval is stale; request approval again")
        return package
    readiness_package = package
    if approval_status == "rejected":
        retry_manifest = dict(package.manifest)
        retry_manifest.pop("release_approval", None)
        readiness_package = WorkflowPackage(root=package.root, manifest=retry_manifest, tests=package.tests)
        readiness_package.problems = validate_package(readiness_package)
    readiness = employee_runtime_readiness(readiness_package, employee_runtime_host)
    if readiness["blockers"]:
        raise ValueError("workflow is not ready for approval: " + "; ".join(readiness["blockers"]))
    fingerprint = _candidate_fingerprint(package)
    evidence_file, evidence_file_sha256 = _write_release_evidence_file(package, fingerprint=fingerprint)
    manifest = dict(package.manifest)
    history = [
        dict(item)
        for item in list(manifest.get("release_approval_history", []) or [])
        if isinstance(item, dict)
    ]
    if existing and approval_status != "pending":
        history.append(existing)
    manifest["release_approval_history"] = history[-20:]
    manifest["release_approval"] = {
        "approval_id": f"rel-{uuid.uuid4().hex}",
        "status": "pending",
        "requested_by": str(actor).strip(),
        "requested_at": datetime.now().isoformat(timespec="seconds"),
        "note": str(note or "").strip()[:2000],
        "evidence_manifest_sha256": package.release_evidence.get("manifest_sha256", ""),
        "evidence_workflow_version": package.release_evidence.get("version", ""),
        "candidate_fingerprint": fingerprint,
        "evidence_file": evidence_file,
        "evidence_file_sha256": evidence_file_sha256,
    }
    return _write_manifest_atomic(package, manifest, suffix="release-request")


@_guard_manifest_writer
def decide_release_approval(
    package: WorkflowPackage,
    *,
    actor: str,
    decision: str,
    note: str = "",
) -> WorkflowPackage:
    """Approve or reject a pending release; publishing remains separate."""
    if package.status != "testing":
        raise ValueError(f"workflow {package.id!r} is {package.status}, not testing")
    if not str(actor or "").strip():
        raise ValueError("release approval actor is required")
    clean_decision = str(decision or "").strip().lower()
    if clean_decision not in {"approved", "rejected"}:
        raise ValueError("release approval decision must be approved or rejected")
    approval = dict(package.manifest.get("release_approval", {}) or {})
    if str(approval.get("status", "") or "").strip().lower() != "pending":
        raise ValueError("workflow has no pending release approval")
    if approval.get("candidate_fingerprint") != _candidate_fingerprint(package):
        raise ValueError("release approval is stale; request approval again")
    _verify_release_evidence_file(package, approval)
    approval.update({
        "status": clean_decision,
        "decided_by": str(actor).strip(),
        "decided_at": datetime.now().isoformat(timespec="seconds"),
        "decision_note": str(note or "").strip()[:2000],
    })
    manifest = dict(package.manifest)
    manifest["release_approval"] = approval
    return _write_manifest_atomic(package, manifest, suffix="release-decision")


def _prepare_rollback_manifest(package: WorkflowPackage, *, actor: str) -> dict[str, Any]:
    """Validate the immutable rollback evidence and prepare, without writes."""
    if package.status != "published":
        raise ValueError(f"workflow {package.id!r} is {package.status}, not published")
    if not str(actor or "").strip():
        raise ValueError("rollback actor is required")
    publication = dict(package.manifest.get("publication", {}) or {})
    relative_snapshot = str(publication.get("rollback_snapshot", "") or "").strip()
    if not relative_snapshot:
        raise ValueError("workflow has no rollback snapshot")
    root = package.root.resolve()
    snapshot_path = (root / relative_snapshot).resolve()
    try:
        snapshot_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("rollback snapshot escapes workflow package") from exc
    if not snapshot_path.is_file():
        raise ValueError(f"rollback snapshot is missing: {relative_snapshot}")
    expected_snapshot_hash = str(publication.get("rollback_manifest_sha256", "") or "").strip()
    actual_snapshot_hash = WorkflowPackage._file_sha256(snapshot_path)
    if not expected_snapshot_hash:
        raise ValueError("rollback snapshot has no recorded hash")
    if actual_snapshot_hash != expected_snapshot_hash:
        raise ValueError("rollback snapshot hash does not match publication record")
    try:
        restored = yaml.safe_load(snapshot_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read rollback snapshot: {exc}") from exc
    if not isinstance(restored, dict):
        raise ValueError("rollback snapshot must contain a workflow mapping")
    if str(restored.get("id", "") or package.id).strip() != package.id:
        raise ValueError("rollback snapshot workflow id does not match package")
    restored["status"] = "testing"
    # A rollback is a new release candidate.  Never carry an old approval
    # across it, even when the snapshot was captured after approval.
    restored["release_approval"] = {
        "approval_id": f"rel-{uuid.uuid4().hex}",
        "status": "rejected",
        "decided_by": str(actor).strip(),
        "decided_at": datetime.now().isoformat(timespec="seconds"),
        "decision_note": "rollback requires a fresh release approval",
    }
    restored["rollback"] = {
        "rolled_back_at": datetime.now().isoformat(timespec="seconds"),
        "rolled_back_by": str(actor).strip(),
        "from_version": str(package.manifest.get("version", "") or ""),
        "from_manifest_sha256": package.release_evidence.get("manifest_sha256", ""),
        "snapshot": relative_snapshot.replace("\\", "/"),
    }
    candidate = WorkflowPackage(root=package.root, manifest=restored, tests=load_test_records(package.root))
    candidate.problems = validate_package(candidate)
    if candidate.problems:
        raise ValueError("rollback snapshot is invalid: " + "; ".join(candidate.problems))
    return restored


@_guard_manifest_writer
def rollback_package(package: WorkflowPackage, *, actor: str) -> WorkflowPackage:
    """Restore the validated snapshot with one manifest commit.

    This never installs runtime assets or invokes a tool. Approval is
    invalidated in the same commit that restores the testing candidate.
    """
    restored = _prepare_rollback_manifest(package, actor=actor)
    return _write_manifest_atomic(package, restored, suffix="rollback")


@_guard_manifest_writer
def record_test_run(
    package: WorkflowPackage,
    *,
    actor: str,
    outcome: str,
    notes: str = "",
    failure_reason: str = "",
    duration_minutes: float | None = None,
    model_calls: int | None = None,
    cost_cny_estimate: float | None = None,
    execution_mode: str = "manual",
) -> WorkflowPackage:
    """Append one immutable Builder/acceptance trial record to a package."""
    if package.status not in {"draft", "testing"}:
        raise ValueError(f"workflow {package.id!r} is {package.status}, test records are immutable")
    clean_actor = str(actor or "").strip()
    if not clean_actor:
        raise ValueError("test record actor is required")
    clean_outcome = str(outcome or "").strip().lower()
    if clean_outcome not in {"passed", "failed", "stopped"}:
        raise ValueError("test outcome must be passed, failed, or stopped")
    if clean_outcome == "failed" and not str(failure_reason or notes or "").strip():
        raise ValueError("failed test records require a failure reason or note")
    clean_mode = str(execution_mode or "manual").strip().lower()
    if clean_mode not in {"manual", "simulated", "provider"}:
        raise ValueError("test execution_mode must be manual, simulated, or provider")
    tests_dir = package.root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    path = tests_dir / f"{timestamp}-{uuid.uuid4().hex[:8]}.md"
    metadata: dict[str, Any] = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "outcome": clean_outcome,
        "recorded_by": clean_actor,
        "workflow_version": str(package.manifest.get("version", "") or ""),
        "manifest_sha256": package.release_evidence.get("manifest_sha256", ""),
        "execution_mode": clean_mode,
    }
    if duration_minutes is not None:
        metadata["duration_minutes"] = float(duration_minutes)
    if model_calls is not None:
        metadata["model_calls"] = int(model_calls)
    if cost_cny_estimate is not None:
        metadata["cost_cny_estimate"] = float(cost_cny_estimate)
    if str(notes or "").strip():
        metadata["notes"] = str(notes).strip()[:4000]
    if str(failure_reason or "").strip():
        metadata["failure_reason"] = str(failure_reason).strip()[:2000]
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write("---\n")
            handle.write(yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip() + "\n")
            handle.write("---\n")
            handle.write(str(notes or failure_reason or "").strip() + "\n")
    except OSError as exc:
        raise ValueError(f"cannot write test record: {exc}") from exc
    return load_package(package.root) or package


@_guard_manifest_writer
def submit_for_testing(package: WorkflowPackage, *, actor: str, change_note: str = "") -> WorkflowPackage:
    """Promote a draft after a passing, immutable trial record.

    This is the Builder's local candidate boundary: it does not install the
    package or launch a customer run, but it makes the candidate eligible for
    release approval. A draft cannot be promoted on a claimed preflight alone;
    it must have a recorded ``passed`` result and a clean package contract.
    """
    if package.status != "draft":
        raise ValueError(f"workflow {package.id!r} is {package.status}, not draft")
    clean_actor = str(actor or "").strip()
    if not clean_actor:
        raise ValueError("testing submission actor is required")
    if package.problems:
        raise ValueError("workflow is invalid: " + "; ".join(package.problems))
    latest = package.latest_test
    if latest is None or latest.outcome != "passed":
        raise ValueError("workflow needs a passed test record before testing submission")
    if latest.execution_mode == "simulated":
        raise ValueError("simulated-only test evidence cannot submit a workflow for testing")
    current_version = str(package.manifest.get("version", "") or "").strip()
    current_manifest_hash = str(package.release_evidence.get("manifest_sha256", "") or "").strip()
    if not latest.workflow_version or not latest.manifest_sha256:
        raise ValueError("latest passed test lacks workflow version or manifest evidence")
    if latest.workflow_version and latest.workflow_version != current_version:
        raise ValueError("latest passed test targets a different workflow version")
    if latest.manifest_sha256 and current_manifest_hash and latest.manifest_sha256 != current_manifest_hash:
        raise ValueError("latest passed test targets a different manifest")
    manifest_path = package.root / "workflow.yaml"
    manifest = dict(package.manifest)
    manifest["status"] = "testing"
    manifest["testing_submitted_at"] = datetime.now().isoformat(timespec="seconds")
    manifest["testing_submitted_by"] = clean_actor
    if str(change_note or "").strip():
        manifest["change_note"] = str(change_note).strip()[:2000]
    return _write_manifest_atomic(package, manifest, suffix="testing")


def create_workflow_draft(
    package: WorkflowPackage,
    *,
    ampuly_home: Path,
    draft_id: str,
    actor: str,
    name: str = "",
    summary: str = "",
    nodes: list[dict[str, Any]] | None = None,
    intake_fields: list[dict[str, Any]] | None = None,
    acceptance: list[str] | None = None,
    change_note: str = "",
    project_id: str = "",
) -> WorkflowPackage:
    """Clone a workflow template into an isolated, draft-only candidate.

    Every draft starts from ``draft_manifest_from`` -- no tool grants, no rollout or approval records, no intake
    values from the template's last customer (4.7 Q01: 不复制旧授权). That includes the Builder's plain "copy as a new
    draft": policy, 「新草稿必须自己证明，放行重新批」. With ``project_id`` the draft is that project's own
    (派生为本项目草稿, policy): it records the project and the template and version it was cut from.
    Without it, a draft keeps whatever ownership its source had; a copy of a derived draft records that draft as
    what it was cut from.

    Builder v1 starts from a known package instead of fabricating a production
    workflow from an unbounded prompt. The candidate owns its copied org and
    Skill assets under assets/ so it can later be installed without ever
    deleting or overwriting the source package. This operation is local and
    deterministic: it never launches a run, calls a model, changes an
    allowlist, or sends an external message.
    """
    if package is None or not package.id:
        raise ValueError("workflow template is required")
    if not str(actor or "").strip():
        raise ValueError("draft actor is required")
    clean_id = str(draft_id or "").strip().lower()
    if not _ID_RE.match(clean_id):
        raise ValueError("draft_id must be lowercase letters, digits, '-' or '_' (2-64 chars)")
    clean_home = Path(ampuly_home).resolve()
    workflows_root = clean_home / "workflows"
    destination = (workflows_root / clean_id).resolve()
    try:
        destination.relative_to(workflows_root.resolve())
    except ValueError as exc:
        raise ValueError("draft destination escapes workflow home") from exc
    if destination.exists():
        raise ValueError(f"workflow draft {clean_id!r} already exists")

    clean_project = str(project_id or "").strip()
    source_manifest = dict(package.manifest)
    candidate_manifest = draft_manifest_from(source_manifest)
    candidate_manifest.update({
        "id": clean_id,
        "name": str(name or source_manifest.get("name", "") or clean_id).strip()[:160],
        "summary": str(summary or source_manifest.get("summary", "") or "").strip()[:2000],
        "status": "draft",
        "version": "0.1.0",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "created_by": str(actor).strip(),
        "change_note": str(change_note or "").strip()[:2000] or "draft created from workflow template",
    })
    candidate_manifest.pop("publication", None)
    candidate_manifest.pop("release_approval", None)
    candidate_manifest.pop("release_approval_history", None)
    candidate_manifest.pop("rollback", None)
    # A new candidate must prove itself; never copy an old run's evidence.
    candidate_manifest["status"] = "draft"
    candidate_manifest.pop("tests", None)
    if clean_project:
        candidate_manifest["project_id"] = clean_project[:128]
        candidate_manifest["derived_from"] = {"id": package.id, "version": str(source_manifest.get("version", "") or "")}
    elif "derived_from" in candidate_manifest:
        # A copy of a project's own draft stays that project's, but it was cut from that draft, not from the template
        # the draft was derived from. Left pointing at the template, 派生为本项目草稿 (derive_workflow_for_project looks
        # for the project's draft by derived_from) could open this copy in place of the draft it made.
        candidate_manifest["derived_from"] = {"id": package.id, "version": str(source_manifest.get("version", "") or "")}
    if nodes is not None:
        if not isinstance(nodes, list) or any(not isinstance(item, dict) for item in nodes):
            raise ValueError("nodes must be a list of objects")
        candidate_manifest["nodes"] = [dict(item) for item in nodes]
    if intake_fields is not None:
        if not isinstance(intake_fields, list) or any(not isinstance(item, dict) for item in intake_fields):
            raise ValueError("intake_fields must be a list of objects")
        intake = dict(candidate_manifest.get("intake", {}) or {})
        intake["fields"] = [dict(item) for item in intake_fields]
        candidate_manifest["intake"] = intake
    if acceptance is not None:
        if not isinstance(acceptance, list):
            raise ValueError("acceptance must be a list")
        candidate_manifest["acceptance"] = [str(item).strip() for item in acceptance if str(item).strip()]

    org_source = package.org_config_path
    if org_source is None or not org_source.is_file():
        raise ValueError("workflow template org_config is missing")
    skills_root = package.skills_root.resolve()
    source_skills: list[tuple[str, Path]] = []
    for skill in package.skills:
        source = (skills_root / skill).resolve()
        try:
            source.relative_to(skills_root)
        except ValueError as exc:
            raise ValueError(f"workflow template skill escapes skills_root: {skill}") from exc
        source_skills.append((skill, source))
    missing_skills = [skill for skill, path in source_skills if not path.is_dir()]
    if missing_skills:
        raise ValueError("workflow template skills are missing: " + ", ".join(missing_skills))

    assets = destination / "assets"
    asset_org = assets / "org_config.yaml"
    asset_skills = assets / "skills"
    candidate_manifest["org_config"] = "assets/org_config.yaml"
    candidate_manifest["skills_root"] = "assets/skills"
    owns_destination = False
    try:
        destination.mkdir(parents=True)
        owns_destination = True
        assets.mkdir()
        shutil.copy2(org_source, asset_org)
        for skill, source in source_skills:
            shutil.copytree(source, asset_skills / skill)
        manifest_path = destination / "workflow.yaml"
        manifest_path.write_text(
            yaml.safe_dump(candidate_manifest, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        readme = package.root / "README.md"
        if readme.is_file():
            shutil.copy2(readme, destination / "README.md")
    except OSError as exc:
        # The destination did not exist before this call; remove only this
        # candidate so a failed clone cannot be discovered as a partial pack.
        if owns_destination:
            try:
                shutil.rmtree(destination)
            except OSError:
                pass
        raise ValueError(f"cannot create workflow draft: {exc}") from exc
    created = load_package(destination)
    if created is None:
        raise ValueError("workflow draft could not be reloaded")
    return created


def create_blank_workflow_draft(
    *,
    ampuly_home: Path,
    draft_id: str,
    actor: str,
    org_source: Path,
    name: str = "",
    summary: str = "",
    intake_fields: list[dict[str, Any]] | None = None,
    created_from: str = "",
    project_id: str = "",
) -> WorkflowPackage:
    """A new, empty draft: no steps, one intake field, run by the team in ``org_source`` (4.7 Q01, A01).

    ``created_from`` names what the draft was made from (a 安安 flow draft: ``flow_draft:<project>:<message>``), so
    opening the same card again finds this draft instead of making a second one.

    ``intake_fields`` (id, label, required) replace the single default field when a flow draft names its own
    (4.7 Q04-B); the template then lists each of them.

    ``create_workflow_draft`` can only clone a template, so nothing could be built from nothing. This writes the
    least a package must have to load and validate -- an org to run on (copied, so later edits to that team do not
    change a draft silently), one intake field and its template -- and nothing else: no steps, no skills, no test
    record, no approval. Like a cloned draft it lives in the runtime home, so it carries its own ``assets/skills``
    and cannot declare skills from the shared internal library (a known gap, see the Q00 cards).

    An optional project uses the existing manifest ownership field; omission keeps a global draft.
    """
    if not str(actor or "").strip():
        raise ValueError("draft actor is required")
    from ampuly.project_id import is_valid_project_id
    if project_id != "" and not is_valid_project_id(project_id):
        raise ValueError("invalid project_id")
    clean_id = str(draft_id or "").strip().lower()
    if not _ID_RE.match(clean_id):
        raise ValueError("draft_id must be lowercase letters, digits, '-' or '_' (2-64 chars)")
    workflows_root = (Path(ampuly_home).resolve() / "workflows").resolve()
    destination = (workflows_root / clean_id).resolve()
    try:
        destination.relative_to(workflows_root)
    except ValueError as exc:
        raise ValueError("draft destination escapes workflow home") from exc
    if destination.exists():
        raise ValueError(f"workflow draft {clean_id!r} already exists")
    source = Path(org_source)
    try:
        org = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"the team to run the draft on cannot be read: {exc}") from exc
    org_id = str(org.get("organization_id", "") or "").strip() if isinstance(org, dict) else ""
    if not org_id:
        raise ValueError("the team to run the draft on has no organization_id")
    zh_name = str(name or "").strip()[:160] or clean_id
    fields = [
        {"id": str(field["id"]), "label": str(field.get("label") or field["id"])[:60], "required": bool(field.get("required"))}
        for field in list(intake_fields or [])
        if isinstance(field, dict) and _ID_RE.match(str(field.get("id", "") or ""))
    ][:10] or [{"id": "request", "label": "需求描述", "required": True, "multiline": True}]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "id": clean_id,
        "name": zh_name,
        "summary": str(summary or "").strip()[:2000],
        "status": "draft",
        "version": "0.1.0",
        "org_id": org_id,
        "org_config": "assets/org_config.yaml",
        "skills_root": "assets/skills",
        "skills": [],
        "nodes": [],
        "intake": {
            "title": f"{zh_name} · {{{fields[0]['id']}}}",
            "fields": fields,
            "template": "\n".join(f"{field['label']}：{{{field['id']}}}" for field in fields) + "\n按节点依赖派单。",
        },
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "created_by": str(actor).strip(),
        "change_note": "blank draft",
        **({"created_from": str(created_from).strip()[:200]} if str(created_from or "").strip() else {}),
        **({"project_id": project_id} if project_id else {}),
    }
    owns_destination = False
    try:
        destination.mkdir(parents=True)
        owns_destination = True
        (destination / "assets" / "skills").mkdir(parents=True)
        shutil.copy2(source, destination / "assets" / "org_config.yaml")
        (destination / "workflow.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except OSError as exc:
        # The destination did not exist before this call; remove only what this call made.
        if owns_destination:
            try:
                shutil.rmtree(destination)
            except OSError:
                pass
        raise ValueError(f"cannot create workflow draft: {exc}") from exc
    created = load_package(destination)
    if created is None:
        raise ValueError("workflow draft could not be reloaded")
    return created


# The node fields the builder canvas reads and edits: what a draft carries, and all a proposal may change.
CANVAS_NODE_FIELDS = (
    "id", "name", "type", "role", "assignee", "gate", "gate_to", "depends_on", "deliverables", "inputs",
    "skills", "tools", "responsibility", "invoke", "canvas",
    # An AI step's own model (step_models.py); "" / absent = the configured default.
    "model",
    # An AI step's tier (model_tiers.py: light / standard / strong); "" / absent = the work default. Edited by the
    # canvas dropdown, and shown and applied by a 总控 proposal card too (frontend lib/nodeProposal.ts
    # ``nodeFrom``, workspace/workflowGraph.ts ``DIFF_FIELDS``), so a proposal may change it like any canvas field.
    "tier",
    # Q05 B4: the step's browser universal connector -- its parameters, or null -- stored as a component instance.
    "connector",
    # 4.7-A07: where a human review's 打回 sends the work back -- an upstream step's id; "" / absent = this step.
    # Checked by node_findings (rework_to_invalid / rework_to_not_review), projected for the runtime.
    "rework_to",
)

# Portable binding data survives an ordinary owner/CAS node save, but is not
# added to CANVAS_NODE_FIELDS: master-control proposals must not gain authority
# to add, remove or change an employee binding merely through this transport.
PRESERVED_NODE_FIELDS = ("employee_binding",)

# Shown by the console, never edited there: the quality standard a human gate judges by, and the step's write fence.
# They are part of what the package promises, and a person standing at a gate needs to read them.
#
# They are NOT dropped by ``merge_node_drafts``: a node save that deliberately carries a new value still sets one --
# that is how a package's own edit reaches the runtime projection, and a test has pinned it since before the console
# could show these at all. The console cannot change them because it has no editor for either, so the only value it
# can send is the one it was handed. What must not happen is 总控 *proposing* a different value and having it quietly
# dropped: the proposal path refuses that by name (``read_only_fields`` in master_loop), loudly, instead.
#
# Kept out of CANVAS_NODE_FIELDS so that anything listing "what the canvas edits" keeps telling the truth.
#
# ``tier`` sat here for one day while the proposal card could not show or apply it; it is a canvas field
# now (see CANVAS_NODE_FIELDS), and a proposal may change it.
#
# ``branch_when`` (4.7 A02): a branch step's condition, the words that choose it at the human decision before it. The
# canvas shows it on the step's card and on the wire into it, and has no editor for it; the runtime reads it
# (node_contract, trial_runner). A draft that echoes an empty one writes no key (``merge_node_drafts``).
#
# ``channel`` / ``sla_hours`` / ``confirm_mode`` (S3-2A): a human step's channel, its deadline in hours and
# whether the confirmation is an employee's (员工确认) or the customer's (客户确认). Accepted in a package manifest and
# shown on the step for the project page; **display only -- the runtime ignores all three** (a gate still goes to
# ``gate_to`` / ``assignee`` through whatever channel the console has, the SLA still lives on the order, and a customer
# confirmation is still the acceptance round). Making them act -- sending by channel, escalating at the deadline, a
# customer gate -- is kernel work, not this. Read-only here so a canvas save or a 总控 proposal echoes them unchanged.
READ_ONLY_NODE_FIELDS = ("standard", "writes", "branch_when", "channel", "sla_hours", "confirm_mode")
# The two words a step's confirm_mode may say; anything else is shown as "" (not set).
CONFIRM_MODES = ("员工确认", "客户确认")


def _sla_hours(value: Any) -> int | float | None:
    """A deadline in hours as the manifest wrote it, or None: not a number, missing, or not positive."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    if hours != hours or hours <= 0 or hours == float("inf"):
        return None
    return int(hours) if hours.is_integer() else hours


def _employee_binding_payload(node: dict[str, Any]) -> dict[str, Any]:
    """Validate and copy portable binding data without resolving any instance."""
    if "employee_binding" not in node:
        return {}
    from ampuly.plugins.office_ui.ai_employee_instance_contract import validate_binding

    problems = validate_binding(node["employee_binding"])
    if problems:
        # Never stringify the submitted object: it may contain an attempted
        # credential or fabricated authorization value.
        raise ValueError(f"invalid employee_binding: {problems[0]['code']}")
    return {"employee_binding": copy.deepcopy(node["employee_binding"])}


def node_payload(n: dict[str, Any]) -> dict[str, Any]:
    """One node as the console shows and edits it (the CANVAS_NODE_FIELDS, normalised), plus the read-only fields.

    ``channel`` / ``sla_hours`` / ``confirm_mode`` are display only: shown as the manifest wrote them (absent -> "" /
    None), and ignored by the runtime -- see READ_ONLY_NODE_FIELDS.
    """
    confirm_mode = str(n.get("confirm_mode", "") or "").strip()
    return {
        "id": str(n.get("id", "") or ""),
        "name": str(n.get("name", n.get("id", "")) or ""),
        "type": str(n.get("type", "") or ""),
        "role": str(n.get("role", "") or ""),
        "assignee": str(n.get("assignee", "") or ""),
        "gate": str(n.get("gate", "none") or "none"),
        "gate_to": str(n.get("gate_to", "") or ""),
        "depends_on": [str(x) for x in list(n.get("depends_on", []) or [])],
        "deliverables": [str(x) for x in list(n.get("deliverables", []) or [])],
        # which deliverables the step needs, by name (may be free text in packages written before the canvas knew it)
        "inputs": [str(x) for x in list(n.get("inputs", []) or [])],
        "skills": [str(x) for x in list(n.get("skills", []) or [])],
        "tools": [str(x) for x in list(n.get("tools", []) or [])],
        "responsibility": str(n.get("responsibility", "") or ""),
        "invoke": dict(n.get("invoke", {}) or {}),
        **_employee_binding_payload(n),
        # the step's own model; "" = the configured default
        "model": str(n.get("model", "") or ""),
        # the step's own tier as stored, not normalised. "" = none of its own: the run then takes the default
        # (model_tiers.resolve_model -- 「标准」 for an AI work step); the builder's 「建议」 is only shown, never applied.
        # Echoed back unchanged it stays what it was, and a value the save would refuse is shown as it is, so the
        # finding that points at it points at what is there. The canvas edits it, and a 总控 proposal may change it.
        "tier": str(n.get("tier", "") or ""),
        # the step's browser universal connector (its parameters), or None: stored as nodes[].components (Q05)
        "connector": _connector_params(n),
        # where 打回 on this human review sends the work back (an upstream step id); "" = back to this step
        "rework_to": str(n.get("rework_to", "") or ""),
        # where a person placed the step on the builder canvas ({} = let the layout decide)
        "canvas": _canvas_position(n.get("canvas")),
        # Read-only (READ_ONLY_NODE_FIELDS): shown so a person at a gate can see what this step promised, and what it
        # is allowed to write. Never editable, and dropped from any draft that echoes them back.
        "standard": [str(x) for x in list(n.get("standard", []) or [])],
        "writes": [str(x) for x in list(n.get("writes", []) or [])],
        # Read-only too (4.7 A02): a branch step's condition, as stored; "" = not a branch. Shown on the card.
        "branch_when": str(n.get("branch_when", "") or ""),
        # Read-only, display only (S3-2A): the step's channel, deadline in hours and confirmation mode as the manifest
        # wrote them. The runtime ignores all three (see READ_ONLY_NODE_FIELDS).
        "channel": str(n.get("channel", "") or "").strip(),
        "sla_hours": _sla_hours(n.get("sla_hours")),
        "confirm_mode": confirm_mode if confirm_mode in CONFIRM_MODES else "",
    }


def _connector_params(node: dict[str, Any]) -> dict[str, Any] | None:
    from ampuly.plugins.office_ui.browser_connector import connector_params
    from ampuly.plugins.office_ui.desktop_connector import desktop_params

    # 4.7-A06: the one field shows either channel; a desktop connector's parameters come marked `kind: desktop`.
    browser = connector_params(node)
    return browser if browser is not None else desktop_params(node)


def merge_node_drafts(existing: list[dict[str, Any]], drafts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overlay an editor's node drafts onto the nodes already in the package.

    The console editor only knows a handful of fields (role, assignee, gate,
    deliverables, skills, tools). Replacing whole nodes with what it sends
    silently dropped ``standard``, ``writes``, ``responsibility`` and the
    rest -- the gate's checklist and the write fence vanished from a package
    after one draft save. Fields the draft does not carry stay
    as they were; a draft for an unknown id is taken as a new node; nodes
    the draft omits are removed (the editor always sends the full table).
    """
    by_id = {str(node.get("id", "") or ""): dict(node) for node in existing if isinstance(node, dict)}
    merged: list[dict[str, Any]] = []
    for draft in drafts:
        node_id = str(draft.get("id", "") or "")
        base = dict(by_id.get(node_id, {}))
        if "employee_binding" in base:
            # An omitted edit preserves content, not a mutable alias into the
            # original package. Leave unrelated legacy field-copy semantics alone.
            base["employee_binding"] = copy.deepcopy(base["employee_binding"])
        for key, value in dict(draft).items():
            if key == "employee_binding":
                if value is None:
                    # Explicit CAS edit removal. An old client omitting the
                    # field preserves the binding; null is never stored.
                    base.pop(key, None)
                else:
                    base.update(_employee_binding_payload({key: value}))
                continue
            if key == "connector":
                continue  # never a key of the node: turned into its connector instance after the lists settle (Q05 B4)
            if key == "invoke" and not value and "invoke" not in base:
                continue  # the editor sends an empty invoke for every node
            if key in ("inputs", "model", "tier", "rework_to", "branch_when", "channel", "sla_hours", "confirm_mode") and not value:
                # ...and an empty inputs, model "" (the default) and tier "" (the step's own default). A step that
                # names nothing it needs, or runs on the default model or tier, has no such key: writing
                # `inputs: []`, `model: ''` or `tier: ''` into every node would make a layout-only save a
                # material change. Clearing the field removes the key. rework_to "" (打回 back to this step) too,
                # and branch_when "" (not a branch), which the console is handed for every step since 4.7 A02.
                # The display-only channel "" / sla_hours None / confirm_mode "" (S3-2A) likewise: a package that
                # never wrote them must not gain three empty keys on every step at the next canvas save.
                base.pop(key, None)
                continue
            base[key] = value
        # The builder clears a placed position (e.g. after a role change moves the step to another lane) by
        # sending canvas: null; drop the key rather than write "canvas: null" into the package.
        if base.get("canvas") in (None, {}):
            base.pop("canvas", None)
        merged.append(base)
    return merged


def manifest_text_preserving_header(manifest_path: Path, manifest: dict[str, Any]) -> str:
    """Serialize a manifest, keeping the comment block that heads the current file.

    PyYAML drops comments; the leading block is where a package explains its
    own design decisions, so it is carried over verbatim.  Comments inside
    the document are still lost -- keep design notes at the top.
    """
    header: list[str] = []
    try:
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                header.append(line)
            elif line.strip():
                break
    except OSError:
        header = []
    body = yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False)
    return ("\n".join(header) + "\n" if header else "") + body


# skill_factory.SKILL_ID's rule (it imports this module, so the pattern is repeated): a name that is also a
# plain folder name, never a path.
LIBRARY_SKILL_ID = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


# What the builder's saves declared on their own, so a later save can take back one no step names any more.
# Skills an author declared by hand are never taken back.
BUILDER_DECLARED_KEY = "skills_declared_by_builder"


def uses_shared_skill_library(package: WorkflowPackage) -> bool:
    """Whether the package's skills come from the shared internal library (``distribution/skills``).

    A package in the distribution tree does (``skills_root`` defaults to ``../../skills``). A draft cut into the
    runtime home carries its own ``assets/skills`` and holds only the skills it was cut with, so the internal
    library's skills cannot be declared there.
    """
    root = package.root
    return (
        root.parent.name == "workflows" and root.parent.parent.name == "distribution"
        and package.skills_root == (root.parent.parent / "skills").resolve()
    )


# The shared internal library a draft in the runtime home copies from (the 内部库; distribution/skills).
SHARED_SKILL_LIBRARY = Path(__file__).resolve().parents[3] / "distribution" / "skills"


def copies_library_skills(package: WorkflowPackage) -> bool:
    """A draft or testing package in the runtime home, which takes an internal-library skill by copying it.

    policy: such a draft carries its own ``assets/skills``; 调用 of a library skill copies that
    skill in and locks its version, so a later change to the library never changes the draft underneath it -- the
    same way a draft cut from a template already holds copies of the template's skills.
    """
    root = package.root
    return (
        not uses_shared_skill_library(package)
        and root.parent.name == "workflows" and root.parent.parent.name != "distribution"
        and package.skills_root == (root / "assets" / "skills").resolve()
        and package.status in {"draft", "testing"}
    )


def _plain_skill_folder(root: Path, skill_id: str) -> bool:
    """A usable id, a SKILL.md, no link in the way, nothing resolving outside ``root``."""
    from ampuly.plugins.office_ui.component_catalog import _is_link

    if not LIBRARY_SKILL_ID.match(str(skill_id or "")):
        return False
    folder = root / skill_id
    skill_md = folder / "SKILL.md"
    if _is_link(folder) or _is_link(skill_md) or not skill_md.is_file():
        return False
    try:
        skill_md.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def library_holds_skill(package: WorkflowPackage, skill_id: str) -> bool:
    """A plain skill folder in the package's shared library: a usable id, a SKILL.md, no link out of it.

    The same rule the 内部库 listing applies (component_catalog.internal_skills skips linked folders), so the
    save never declares a skill the library page does not show. For a draft that copies library skills, it is the
    draft's own copy that is declared, so the question is whether that copy is there.
    """
    if not (uses_shared_skill_library(package) or copies_library_skills(package)):
        return False
    return _plain_skill_folder(package.skills_root, skill_id)


def library_skills_for(package: WorkflowPackage) -> tuple[list[str], list[str]]:
    """(lockable, not lockable): the shared library's skills a step of this package can take.

    A draft can hold any of them; a testing or published package locks what it declares, so a skill without a
    complete release contract is listed apart -- the builder says the save would refuse it.
    """
    root = package.skills_root if uses_shared_skill_library(package) else SHARED_SKILL_LIBRARY if copies_library_skills(package) else None
    if root is None or not root.is_dir():
        return [], []
    lockable: list[str] = []
    unlockable: list[str] = []
    for folder in sorted(root.iterdir(), key=lambda item: item.name):
        if not folder.is_dir() or not _plain_skill_folder(root, folder.name):
            continue
        if package.status == "draft" or load_skill_contract(root, folder.name).complete:
            lockable.append(folder.name)
        else:
            unlockable.append(folder.name)
    return lockable, unlockable


def _copy_library_skills(package: WorkflowPackage, nodes: list[dict[str, Any]]) -> list[Path]:
    """Copy into a runtime draft the library skills its steps now name and it does not hold yet; returns what was made.

    Links inside a library skill are left out rather than followed: a copy must not carry anything from outside
    the library. The caller removes the returned folders if the save that needed them does not go through.
    """
    from ampuly.plugins.office_ui.component_catalog import _is_link

    if not copies_library_skills(package):
        return []
    named: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        named.extend(str(skill or "").strip() for skill in list(node.get("skills", []) or []))
        named.extend(
            str(item.get("ref", "") or "").strip()
            for item in list(node.get("components", []) or [])
            if isinstance(item, dict) and item.get("kind") == "skill"
        )
    created: list[Path] = []
    try:
        for skill_id in dict.fromkeys(named):
            if not skill_id or _plain_skill_folder(package.skills_root, skill_id) or not _plain_skill_folder(SHARED_SKILL_LIBRARY, skill_id):
                continue
            destination = package.skills_root / skill_id
            if destination.exists():
                continue  # something that is not a plain skill folder is in the way: never overwrite it
            package.skills_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                SHARED_SKILL_LIBRARY / skill_id, destination,
                ignore=lambda directory, names: [name for name in names if _is_link(Path(directory) / name)],
            )
            created.append(destination)
    except OSError:
        _remove_copies(created)
        raise
    return created


def _remove_copies(created: list[Path]) -> None:
    for folder in created:
        shutil.rmtree(folder, ignore_errors=True)


def declare_library_skills(package: WorkflowPackage, manifest: dict[str, Any]) -> list[str]:
    """Declare, in ``manifest``, the skills its steps name that the shared internal library holds.

    The builder's 调用 takes a skill from the internal library (``distribution/skills``, the package's
    ``skills_root``) to a step. A step naming a skill the package does not declare is accepted but never
    installed or version-locked, so the save declares such a skill and locks the version its SKILL.md states,
    and remembers it declared it: once no step names it, a later save takes it back out. Names the library
    does not hold stay undeclared, as before: the process factory names skills on steps before it drafts them.
    Whether the package may lock the skill -- a testing package needs a complete release contract -- is then
    the ordinary package validation's to say.
    """
    declared = [str(skill).strip() for skill in list(manifest.get("skills", []) or []) if str(skill).strip()]
    by_builder = [str(skill).strip() for skill in list(manifest.get(BUILDER_DECLARED_KEY, []) or []) if str(skill).strip()]
    named: list[str] = []
    for node in list(manifest.get("nodes", []) or []):
        if isinstance(node, dict):
            named.extend(str(skill or "").strip() for skill in list(node.get("skills", []) or []))
    versions = dict(manifest.get("skill_versions", {}) or {})
    had_versions = "skill_versions" in manifest
    # Taken back: declared by an earlier builder save, named by no step now.
    dropped = [skill for skill in by_builder if skill in declared and skill not in named]
    declared = [skill for skill in declared if skill not in dropped]
    by_builder = [skill for skill in by_builder if skill not in dropped and skill in declared]
    for skill in dropped:
        versions.pop(skill, None)
    added: list[str] = []
    for skill_id in named:
        if skill_id and skill_id not in declared and skill_id not in added and library_holds_skill(package, skill_id):
            added.append(skill_id)
    if not added and not dropped:
        return []
    manifest["skills"] = [*declared, *added]
    for skill_id in added:
        version = load_skill_contract(package.skills_root, skill_id).version
        if version:
            versions[skill_id] = version
    if versions or had_versions:
        manifest["skill_versions"] = versions
    by_builder = [*by_builder, *added]
    if by_builder:
        manifest[BUILDER_DECLARED_KEY] = by_builder
    else:
        manifest.pop(BUILDER_DECLARED_KEY, None)
    return added


def preview_package_nodes(
    package: WorkflowPackage,
    nodes: list[dict[str, Any]],
    *,
    actor: str,
    expected_version: str = "",
    new_version: str = "",
    change_note: str = "",
) -> WorkflowPackage:
    """The package a node save would produce, checked exactly as the save checks it, and not written.

    ``update_package_nodes`` is this plus the atomic replace, so anything that
    shows a person a change before they save it (a master-control proposal on
    the builder canvas) is held to the same rules as the save itself: status,
    acknowledged version, merge, full package validation.
    """
    if package.status not in {"draft", "testing"}:
        raise ValueError(f"workflow {package.id!r} is {package.status}, nodes are immutable")
    if not str(actor or "").strip():
        raise ValueError("workflow node update actor is required")
    current_version = str(package.manifest.get("version", "") or "").strip()
    if expected_version and expected_version.strip() != current_version:
        raise ValueError(
            f"workflow version conflict: expected {expected_version.strip()}, current {current_version or 'unversioned'}"
        )
    next_version = str(new_version or current_version).strip()
    if not next_version:
        raise ValueError("new workflow version is required")
    if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
        raise ValueError("nodes must be a list of objects")
    from ampuly.plugins.office_ui.node_components import reconcile_components, settle_versions

    candidate_manifest = dict(package.manifest)
    candidate_manifest["nodes"] = merge_node_drafts(package.nodes, nodes)
    # Component instances (nodes[].components) and the bare skills/tools lists must agree before anything is compared.
    existing_by_id = {str(node.get("id", "") or ""): node for node in package.nodes if isinstance(node, dict)}
    # Either channel (4.7-A06): `kind: desktop` in the field is the desktop connector, anything else the browser's.
    from ampuly.plugins.office_ui.desktop_connector import apply_any_connector_draft as apply_connector_draft

    for draft, merged in zip(nodes, candidate_manifest["nodes"]):
        reconcile_components(existing_by_id.get(str(draft.get("id", "") or "")), draft, merged)
        apply_connector_draft(draft, merged)
    declare_library_skills(package, candidate_manifest)
    # ...then each skill instance is pinned to what the package locks, and one that says nothing beyond the bare
    # lists is dropped, so a layout-only save of an old package stays exactly what it was.
    component_problems = settle_versions(candidate_manifest)
    if component_problems:
        raise ValueError("workflow node update is invalid: " + "; ".join(component_problems))
    # Canvas coordinates are presentation and may be saved without creating a
    # new business contract. Every other node-table change alters what the
    # workflow does and therefore needs a distinct, forward version: orders,
    # test evidence and rollback points all bind to that version.
    semantic_before = [{key: value for key, value in node.items() if key != "canvas"} for node in package.nodes]
    semantic_after = [
        {key: value for key, value in node.items() if key != "canvas"}
        for node in list(candidate_manifest["nodes"] or [])
    ]
    if semantic_before != semantic_after:
        current_match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", current_version)
        next_match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", next_version)
        if next_version == current_version or (
            current_match and next_match
            and tuple(int(value) for value in next_match.groups()) <= tuple(int(value) for value in current_match.groups())
        ):
            raise ValueError(
                f"material workflow node changes require a newer workflow version (current {current_version or 'unversioned'})"
            )
    candidate_manifest["version"] = next_version
    candidate_manifest["change_note"] = str(change_note or "").strip() or "workflow nodes updated"
    candidate_manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
    candidate_manifest["updated_by"] = str(actor).strip()
    candidate = WorkflowPackage(root=package.root, manifest=candidate_manifest, tests=list(package.tests),
                                loaded_revision=package.loaded_revision)
    candidate.problems = validate_package(candidate)
    if candidate.problems:
        raise ValueError("workflow node update is invalid: " + "; ".join(candidate.problems))
    return candidate


def draft_revision(root: Path) -> str:
    """The exact bytes revision of a package's mutable workflow.yaml.

    The version cannot serve: a layout-only save keeps it, so two saves of one
    draft at one version would both be accepted and the second would silently
    drop the first. Empty when the manifest cannot be read.
    """
    try:
        return hashlib.sha256((Path(root) / "workflow.yaml").read_bytes()).hexdigest()
    except OSError:
        return ""


_MANIFEST_LOCKS: dict[str, threading.RLock] = {}
_MANIFEST_LOCKS_GUARD = threading.Lock()
_MANIFEST_HELD = threading.local()


@contextmanager
def _manifest_lock(manifest_path: Path):
    """Serialize the byte CAS and replace across threads and processes."""
    from ampuly.core.file_lock import exclusive_file_lock
    key = os.path.normcase(str(manifest_path.resolve()))
    with _MANIFEST_LOCKS_GUARD:
        lock = _MANIFEST_LOCKS.get(key)
        if lock is None:
            lock = _MANIFEST_LOCKS[key] = threading.RLock()
    with lock:
        held = getattr(_MANIFEST_HELD, "paths", None)
        if held is None:
            held = _MANIFEST_HELD.paths = set()
        if key in held:
            yield
            return
        lock_path = manifest_path.with_name(".workflow.write.lock")
        if manifest_path.is_symlink() or lock_path.is_symlink():
            raise ValueError("workflow write target or lock cannot be a symbolic link")
        with lock_path.open("a+b") as stream, exclusive_file_lock(stream.fileno()):
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)


def _assert_loaded_revision(package: WorkflowPackage) -> None:
    expected = str(package.loaded_revision or "").strip()
    current = draft_revision(package.root)
    if not expected or not current or expected != current:
        raise ValueError("workflow draft revision conflict: re-read the exact manifest before saving")


def _version_on_disk(manifest_path: Path) -> str:
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"workflow.yaml unreadable: {exc}") from exc
    return str(manifest.get("version", "") or "").strip() if isinstance(manifest, dict) else ""


def update_package_nodes(
    package: WorkflowPackage,
    nodes: list[dict[str, Any]],
    *,
    actor: str,
    expected_version: str = "",
    new_version: str = "",
    change_note: str = "",
    expected_revision: str = "",
) -> WorkflowPackage:
    """Persist an owner-authored workflow-node draft with a version boundary.

    This is deliberately narrower than a general manifest editor: only draft
    or testing packages may be changed, the caller must acknowledge the
    version it read, and the candidate is validated before an atomic replace.
    Published/retired packages stay immutable so existing runtime contracts
    cannot be silently changed underneath an active order.

    The version is required, and it is compared with the file on disk, not
    with the caller's copy, inside a per-manifest lock that also covers the
    replace: two saves can no longer both pass the check and the later one
    overwrite the earlier. ``expected_revision`` (the ``draft_revision`` the
    caller read) also catches a save that kept the version, such as a layout
    change; it is required. Legacy writers must re-read and present the token
    they actually observed, rather than minting a current token at write time.
    """
    manifest_path = package.root / "workflow.yaml"
    with _manifest_lock(manifest_path):
        if not str(expected_revision or "").strip():
            raise ValueError("workflow node save requires expected_revision; re-read the draft")
        read_version = str(expected_version or "").strip()
        current_version = _version_on_disk(manifest_path)
        if read_version and read_version != current_version:
            raise ValueError(f"workflow version conflict: expected {read_version}, current {current_version or 'unversioned'}")
        _assert_loaded_revision(package)
        # A runtime draft takes a library skill by copying it in (copies_library_skills); the copies this save needs
        # are made first so the save can declare and lock them, and removed again if the save does not go through.
        copied = _copy_library_skills(package, nodes)
        try:
            written = _checked_node_save(
                package, nodes, manifest_path, actor=actor, expected_version=expected_version,
                new_version=new_version, change_note=change_note, expected_revision=expected_revision,
            )
        except BaseException:
            _remove_copies(copied)
            raise
    return load_package(package.root) or written


def _checked_node_save(
    package: WorkflowPackage,
    nodes: list[dict[str, Any]],
    manifest_path: Path,
    *,
    actor: str,
    expected_version: str,
    new_version: str,
    change_note: str,
    expected_revision: str,
) -> WorkflowPackage:
    """The body of update_package_nodes, run under its manifest lock: check against the file, then replace it."""
    candidate = preview_package_nodes(
        package, nodes, actor=actor, expected_version=expected_version, new_version=new_version, change_note=change_note,
    )
    read_version = str(expected_version or "").strip()
    if not read_version:
        raise ValueError("workflow node save must name the version it was read at (expected_version)")
    current_version = _version_on_disk(manifest_path)
    if read_version != current_version:
        raise ValueError(
            f"workflow version conflict: expected {read_version}, current {current_version or 'unversioned'}"
        )
    read_revision = str(expected_revision or "").strip()
    if not read_revision:
        raise ValueError("workflow node save requires expected_revision; re-read the draft")
    _assert_loaded_revision(package)
    current_revision = draft_revision(package.root)
    if read_revision != current_revision:
        raise ValueError(
            "workflow draft revision conflict: the draft was saved since it was read "
            f"(expected {read_revision[:12]}, current {current_revision[:12] or 'unreadable'})"
        )
    temporary = manifest_path.with_name(f".workflow.yaml.nodes-{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            manifest_text_preserving_header(manifest_path, candidate.manifest),
            encoding="utf-8",
        )
        replace_with_retry(temporary, manifest_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return candidate


def package_roots(repo_root: Path | None, ampuly_home: Path | None) -> list[Path]:
    roots: list[Path] = []
    if repo_root is not None:
        roots.append(repo_root / "distribution" / "workflows")
    if ampuly_home is not None:
        roots.append(Path(ampuly_home) / "workflows")
    return roots


def discover_packages(repo_root: Path | None, ampuly_home: Path | None) -> list[WorkflowPackage]:
    """Packages from the distribution first, then the runtime home; later ids override."""
    by_id: dict[str, WorkflowPackage] = {}
    for base in package_roots(repo_root, ampuly_home):
        if not base.is_dir():
            continue
        for child in sorted(p for p in base.iterdir() if p.is_dir()):
            package = load_package(child)
            if package is not None:
                by_id[package.id or child.name] = package
    return sorted(by_id.values(), key=lambda p: (p.status != "published", p.name))


def sibling_packages(package: WorkflowPackage, ampuly_home: Path | None) -> list[WorkflowPackage]:
    """Other invokable packages that run on the same org (from the same distribution tree)."""
    repo_root = package.root.parents[2] if len(package.root.parents) > 2 else None
    out: list[WorkflowPackage] = []
    for candidate in discover_packages(repo_root, ampuly_home):
        if candidate.id == package.id or candidate.org_id != package.org_id:
            continue
        if candidate.problems or not candidate.nodes:
            continue
        out.append(candidate)
    return out


def visible_packages(
    packages: list[WorkflowPackage], *, include_testing: bool = True, include_drafts: bool = False,
    project_id: str | None = None,
) -> list[WorkflowPackage]:
    """What the console lists. Drafts only when asked for (the owner's builder, 4.7 Q01): a draft is built there --
    ＋ 新建业务流 and 安安's flow drafts start as one -- and is never orderable (INVOKABLE excludes it).

    With ``project_id``, a flow another project owns is left out (``usable_by_project``): a project's derived flow is
    that customer's. ``None`` keeps every owner, for callers that are not answering one project."""
    allowed = {"published", "testing"} if include_testing else {"published"}
    if include_drafts:
        allowed = allowed | {"draft"}
    return [
        p for p in packages
        if p.status in allowed and (project_id is None or usable_by_project(p, project_id))
    ]


# ── invocation ────────────────────────────────────────────────────────────


def install_package(ampuly_home: Path, package: WorkflowPackage, *, project_id: str | None = None) -> dict[str, Any]:
    """Copy the org config and skills into the runtime home (package wins).

    The org config is one file for the whole machine, so with ``project_id`` the node tables earlier installs left
    on its roles are kept only when this project may run their package (``project_nodes_onto_org``); every flow
    this project may run is installed again right before its run (``prepare_invocation``), so nothing is migrated.
    ``None`` keeps them all, as before.
    """
    from ampuly.core.org_config import org_config_path

    if package.problems:
        raise ValueError("; ".join(package.problems))
    home = Path(ampuly_home)
    known: list[WorkflowPackage] | None = None
    if project_id is not None:
        # Every package this installation knows, from the same distribution tree as sibling_packages reads it,
        # so a table another package left on a role is judged by that package's owner.
        repo_root = package.root.parents[2] if len(package.root.parents) > 2 else None
        known = discover_packages(repo_root, home)
    org_src = package.org_config_path
    assert org_src is not None
    org_dst = org_config_path(home / "config", package.org_id)
    org_dst.parent.mkdir(parents=True, exist_ok=True)
    base_org = yaml.safe_load(org_src.read_text(encoding="utf-8")) or {}
    installed_org: dict[str, Any] | None = None
    if org_dst.is_file():
        try:
            loaded = yaml.safe_load(org_dst.read_text(encoding="utf-8")) or {}
            installed_org = loaded if isinstance(loaded, dict) else None
        except (OSError, yaml.YAMLError):
            installed_org = None
    composed = (
        project_nodes_onto_org(base_org, package, installed=installed_org, project_id=project_id, packages=known)
        if isinstance(base_org, dict) else base_org
    )
    org_dst.write_text(
        f"# 由工作流包 {package.id} 安装生成：基础组织 + 节点表投影；改节点请改包再重新发起。{chr(10)}"
        + yaml.safe_dump(composed, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    installed_skills: list[str] = []
    for skill in package.skills:
        src = package.skills_root / skill
        dst = home / "skills" / skill
        # Orders already running read the body they were launched with (skill_library, by fingerprint): keep the one
        # being replaced and the one being installed before the shared folder is overwritten (BA).
        if dst.exists():
            _keep_skill_version(home / "skills", skill, dst)
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        _keep_skill_version(home / "skills", skill, dst)
        installed_skills.append(skill)
    return {"org_config": str(org_dst), "skills": installed_skills}


def reproject_installed_tiers(ampuly_home: Path, packages: list[WorkflowPackage]) -> dict[str, Any]:
    """LP-B P-e : give a step its tier in team configs installed before tiers were projected.

    ``project_nodes_onto_org`` carries a step's tier onto its role's node table, and every launch
    installs its flow again, so a new order reads the tier. A team config last installed before that keeps tables
    without it -- and ``node_contract.role_nodes`` reads those tables, a run resumed after a restart included -- so those
    steps ran on 标准 whatever the builder chose.

    This pass fills ONLY the ``tier`` key: for each installed team's ``workflow_node_tables[package id]``, a node with
    no tier gets the tier the current package's node of the same id names; then the union the runtime reads is
    recomputed with ``_merge_node_tables``. Nothing else on a node, role or file changes; a tier a table already has
    is left for the next launch's install; a package with problems vouches for nothing. A file with nothing to fill
    is not written at all, so it stays byte for byte as it was; a file that is written is replaced atomically, with
    its header comment and line endings kept. Idempotent: a second pass finds nothing to fill. It never calls
    ``install_package``.

    A task already sent out from the old table keeps no tier and finishes on 标准; only work sent out after this
    pass reads the tier.

    Returns ``{"files": [written paths], "steps": tiers filled, "problems": [paths that could not be read or written]}``.
    """
    from loguru import logger

    from ampuly.core.org_config import list_org_config_paths

    report: dict[str, Any] = {"files": [], "steps": 0, "problems": []}
    wanted: dict[str, dict[str, str]] = {}
    for package in list(packages or []):
        if package is None or package.problems:
            continue
        named = {
            str(node.get("id", "") or ""): str(node.get("tier", "") or "").strip()
            for node in package.nodes if isinstance(node, dict)
        }
        named = {node_id: tier for node_id, tier in named.items() if node_id and tier}
        if named:
            wanted[str(package.id)] = named
    if not wanted:
        return report
    for path in list_org_config_paths(Path(ampuly_home) / "config"):
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
            org = yaml.safe_load(text)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            report["problems"].append(str(path))
            continue
        if not isinstance(org, dict):
            continue
        filled = 0
        for role in list(org.get("roles", []) or []):
            meta = role.get("metadata") if isinstance(role, dict) else None
            tables = meta.get("workflow_node_tables") if isinstance(meta, dict) else None
            if not isinstance(tables, dict):
                continue
            if not all(isinstance(table, list) and all(isinstance(node, dict) for node in table) for table in tables.values()):
                continue  # not what an install writes: left alone
            before = filled
            for package_id, table in tables.items():
                tiers = wanted.get(str(package_id))
                if not tiers:
                    continue
                for node in table:
                    tier = tiers.get(str(node.get("id", "") or ""))
                    if tier and not str(node.get("tier", "") or "").strip():
                        node["tier"] = tier
                        filled += 1
            if filled != before:
                role["metadata"] = _merge_node_tables(meta, "", None)
        if not filled:
            continue
        # The installer's header comment stays on top; the body is dumped as the installer dumps it.
        header = []
        for line in text.splitlines():
            if not line.startswith("#"):
                break
            header.append(line)
        body = "".join(line + "\n" for line in header) + yaml.safe_dump(org, allow_unicode=True, sort_keys=False, width=120)
        newline = "\r\n" if b"\r\n" in raw else "\n"
        temporary = path.with_name(f".{path.name}.tiers-{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(body.replace("\n", newline).encode("utf-8"))
            replace_with_retry(temporary, path)
        except OSError:
            logger.opt(exception=True).warning("a team config could not be given its steps' tiers: {}", path.name)
            report["problems"].append(str(path))
            continue
        finally:
            try:
                temporary.unlink(missing_ok=True)  # only this pass's own temporary file
            except OSError:
                pass
        report["files"].append(str(path))
        report["steps"] += filled
    return report


def _keep_skill_version(skills_dir: Path, skill: str, source: Path) -> Path | None:
    """Keep one copy of the skill folder ``source`` under its SKILL.md fingerprint; never overwritten, never removed.

    Content-addressed and write-once: a fingerprint already kept is left as it is, so the same body is copied once
    however many orders are launched on it. The folder only grows; cleaning it up is the owner's decision. A copy that
    cannot be made is logged and the launch goes on -- a running order then reads the shared body (with a warning).
    """
    from loguru import logger

    from ampuly.layer5_memory.skill_library import skill_version_dir

    skill_md = source / "SKILL.md"
    if not skill_md.is_file():
        return None
    target = skill_version_dir(skills_dir, skill, WorkflowPackage._file_sha256(skill_md))
    if target is None or target.exists():
        return target
    staging = target.parent / f".staging-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, staging)
        # Named after what was copied, so a folder always holds the body its name says.
        target = skill_version_dir(skills_dir, skill, WorkflowPackage._file_sha256(staging / "SKILL.md"))
        if target is None or target.exists():
            return target
        staging.rename(target)
        return target
    except OSError as exc:
        if target is not None and target.is_dir():
            return target  # another launch kept the same body first
        logger.warning(f"skill {skill}: could not keep this version for orders in flight: {exc}")
        return None
    finally:
        # Only this call's own staging copy, and only while it still exists (a successful rename moved it away).
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def staffing_scope_keys(package: WorkflowPackage) -> list[str]:
    # The runtime keys the plan by the profile the router resolved, which has
    # been observed as both ``custom`` and ``corporate`` for the same custom
    # org; writing both keeps the order zero-config whichever it picks.
    return [f"profile:{profile}|org:{package.org_id}" for profile in ("custom", "corporate")]


def write_staffing_defaults(ampuly_home: Path, project_id: str, package: WorkflowPackage) -> list[str]:
    staffing = dict(package.manifest.get("staffing", {}) or {})
    if not staffing:
        return []
    path = Path(ampuly_home) / "projects" / (project_id or "default") / "company_staffing_defaults.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    scopes = payload.setdefault("scopes", {})
    if not isinstance(scopes, dict):
        scopes = payload["scopes"] = {}
    written: list[str] = []
    for key in staffing_scope_keys(package):
        profile = key.split("|", 1)[0].split(":", 1)[1]
        scopes[key] = {
            "company_profile": profile,
            "org_id": package.org_id if profile == "custom" else "",
            "recruitment_role_agents": dict(staffing.get("recruitment_role_agents", {}) or {}),
            "staffing_selections": dict(staffing.get("staffing_selections", {}) or {}),
            "updated_at": datetime.now().isoformat(),
            "source": f"workflow:{package.id}",
        }
        written.append(key)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return written


def existing_tool_grants(ampuly_home: Path, project_id: str, package: WorkflowPackage) -> dict[str, list[str]]:
    """Verify existing exact grant patterns without installing or extending permission."""
    from ampuly.layer5_memory.approval_allowlist import ApprovalAllowlistManager

    grants = package.manifest.get("tool_grants", {}) or {}
    if type(grants) is not dict:
        raise ValueError("existing_tool_grants_required")
    manager = ApprovalAllowlistManager(ampuly_home)
    checked: dict[str, list[str]] = {}
    for tool, raw in grants.items():
        patterns = raw if type(raw) is list else [raw]
        if (type(tool) is not str or not tool.strip() or not patterns
                or any(type(value) is not str or not value.strip() for value in patterns)):
            raise ValueError("existing_tool_grants_required")
        available = manager.list_patterns("tool", tool, project_id=project_id)
        if any(value not in available for value in patterns):
            raise ValueError("existing_tool_grants_required")
        checked[tool] = list(patterns)
    return checked


def write_tool_grants(ampuly_home: Path, project_id: str, package: WorkflowPackage) -> dict[str, list[str]]:
    from ampuly.layer5_memory.approval_allowlist import ApprovalAllowlistManager

    grants = dict(package.manifest.get("tool_grants", {}) or {})
    if not grants:
        return {}
    manager = ApprovalAllowlistManager(ampuly_home)
    added: dict[str, list[str]] = {}
    for tool_name, patterns in grants.items():
        values = [str(p) for p in (patterns if isinstance(patterns, list) else [patterns]) if str(p).strip()]
        if not values:
            continue
        added[str(tool_name)] = manager.add_patterns("tool", str(tool_name), values, project_id=project_id or "default")
    return added


def compose_intake(package: WorkflowPackage, fields: dict[str, Any]) -> str:
    """Render the order message; missing required fields raise."""
    intake = dict(package.manifest.get("intake", {}) or {})
    values: dict[str, str] = {}
    missing: list[str] = []
    for spec in package.intake_fields:
        key = str(spec["id"])
        value = str(fields.get(key, "") or "").strip()
        if not value and spec.get("required"):
            missing.append(str(spec.get("label", key)))
        values[key] = value
    if missing:
        raise ValueError("缺少必填项：" + "、".join(missing))
    template = str(intake.get("template", "") or "")
    try:
        return template.format(**values).strip()
    except (KeyError, IndexError) as exc:
        raise ValueError(f"intake.template references an unknown field: {exc}") from exc


def model_findings(package: WorkflowPackage, *, default_model: str = "") -> list[dict[str, Any]]:
    """Steps whose own model the catalogue will refuse when they run (model-routing phase 0): advice, never a blocker.

    A step naming the current default is left out: the default is never asked, so that step runs.

    A retired model names its successor; an overseas one is off by default; an unroutable one cannot be reached by this
    engine yet; a new one waits for phase-1 parameters. An unknown id is already a blocker (``node_findings``). A
    vendor-routed alias (deepseek-chat, deepseek-reasoner) gets no finding: the owner says DeepSeek serves those names.
    """
    from ampuly.plugins.office_ui.model_catalogue import refusal

    messages = {
        "model_retired": "is retired",
        "model_overseas_off": "is an overseas model, off by default",
        "model_not_routable": "cannot be reached by this engine yet",
        "model_needs_params": "is selectable once phase 1 adds its parameters",
    }
    found: list[dict[str, Any]] = []
    for node in package.nodes:
        model = str(node.get("model", "") or "").strip()
        if model and str(node.get("tier", "") or "").strip() and str(node.get("type", "") or "") == "ai_do":
            # Advice, not a blocker: the step runs on the model it names, and the tier does not choose it. It is
            # still worth saying, because 「我设了档位」 and 「这一步按档位跑」 are not the same thing here.
            # Gatekeeping is unaffected: the floor is derived from the model this step is pinned to.
            found.append({
                "node_id": str(node.get("id", "") or ""), "field": "tier", "code": "tier_with_model",
                "model": model, "successor": "",
                "message": "这一步钉了模型，档位不生效（把关仍按这个模型的档位来）",
            })
        if not model or str(node.get("type", "") or "") != "ai_do" or model == str(default_model or "").strip():
            continue
        refused = refusal(model)
        if refused is None or refused["code"] == "model_unknown":
            continue
        successor = str(refused.get("successor", "") or "")
        message = f"model '{model}' {messages.get(refused['code'], refused['code'])}; the step will be refused when it runs"
        if successor:
            message += f"; use {successor}"
        found.append({"node_id": str(node.get("id", "") or ""), "field": "model", "code": refused["code"],
                      "model": model, "successor": successor, "message": message})
    return found


def preflight_invocation(
    package: WorkflowPackage,
    fields: dict[str, Any],
    *,
    focus_node_id: str = "",
    default_model: str = "",
) -> dict[str, Any]:
    """Run a read-only Builder preflight for a workflow draft.

    The Builder must be able to explain a failed candidate before it is
    installed into an org or launched.  This function intentionally performs
    no filesystem writes, staffing changes, allowlist updates, model calls or
    external sends.  It validates the same intake and node contracts used by
    invocation, then returns an explicit simulation plan for the UI.
    """
    values = dict(fields or {})
    blockers = list(package.problems)
    binding_runtime_findings = employee_binding_runtime_findings(package.nodes)
    blockers.extend(
        f"{finding['code']}: {finding['message']}"
        for finding in binding_runtime_findings
    )
    # The loader has already put these sentences in `problems`; a package built any other way has not, and a
    # preflight that points at a broken step must not also call the draft ready.
    for sentence in validate_nodes(package):
        if sentence not in blockers:
            blockers.append(sentence)
    warnings: list[str] = []
    missing: list[str] = []
    for spec in package.intake_fields:
        value = str(values.get(str(spec["id"]), "") or "").strip()
        if spec.get("required") and not value:
            missing.append(str(spec.get("label", spec["id"])))
    if missing:
        blockers.append("缺少必填项：" + "、".join(missing))
    if package.status == "retired":
        blockers.append("workflow status is retired")
    if not package.nodes:
        blockers.append("workflow must declare at least one node for a Builder run")

    # Drafts may be incomplete for release, but an explicitly referenced Skill
    # still needs a readable contract before the simulated plan is useful.
    skill_states: list[dict[str, Any]] = []
    skill_test_reports: list[dict[str, Any]] = []
    for contract in package.skill_contracts:
        test_report = run_skill_contract_tests(contract)
        skill_test_reports.append(test_report)
        skill_states.append({
            "id": contract.skill_id,
            "version": contract.version,
            "complete": contract.complete,
            "problems": list(contract.problems),
            "test_status": test_report["status"],
        })
        if contract.problems:
            blockers.append(f"skill {contract.skill_id}: " + "; ".join(contract.problems))
        if test_report["status"] != "passed":
            blockers.append(f"skill {contract.skill_id}: contract tests {test_report['status']}")

    node_warnings = input_findings(package)
    for finding in node_warnings:
        warnings.append(f"node {finding['node_id']}: {finding['message']}")
    model_warnings = model_findings(package, default_model=default_model)
    for finding in model_warnings:
        warnings.append(f"node {finding['node_id']}: {finding['message']}")

    focus_node_id = str(focus_node_id or "").strip()
    known_node_ids = {
        str(node.get("id", "") or "").strip()
        for node in package.nodes
        if str(node.get("id", "") or "").strip()
    }
    if focus_node_id and focus_node_id not in known_node_ids:
        blockers.append(f"workflow node {focus_node_id!r} not found")

    node_by_id = {
        str(node.get("id", "") or ""): node
        for node in package.nodes
        if str(node.get("id", "") or "")
    }
    steps: list[dict[str, Any]] = []
    simulated_effects: list[dict[str, str]] = []
    for node in package.nodes:
        node_id = str(node.get("id", "") or "")
        if focus_node_id and node_id != focus_node_id:
            continue
        tools = [str(item).strip() for item in list(node.get("tools", []) or []) if str(item).strip()]
        depends_on = [str(item).strip() for item in list(node.get("depends_on", []) or []) if str(item).strip()]
        inputs: list[str] = []
        for dependency_id in depends_on:
            dependency = node_by_id.get(dependency_id, {})
            deliverables = [
                str(item).strip()
                for item in list(dependency.get("deliverables", []) or [])
                if str(item).strip()
            ]
            inputs.extend(deliverables or [str(dependency.get("name", dependency_id) or dependency_id)])
        declared = [str(item).strip() for item in list(node.get("inputs", []) or []) if str(item).strip()]
        if declared:
            # The step said what it needs: that is the plan, not everything the wires could bring.
            inputs = declared
        steps.append({
            "node_id": node_id,
            "name": str(node.get("name", node_id) or node_id),
            "type": str(node.get("type", "") or ""),
            "status": "simulated",
            "skills": [str(item) for item in list(node.get("skills", []) or []) if str(item).strip()],
            "tools": tools,
            "depends_on": depends_on,
            "inputs": inputs,
            "deliverables": [str(item) for item in list(node.get("deliverables", []) or []) if str(item).strip()],
            "gate": str(node.get("gate", "none") or "none"),
        })
        for tool in tools:
            if tool not in dict(package.manifest.get("tool_grants", {}) or {}):
                warnings.append(f"node {node_id}: tool {tool} has no package grant; runtime policy may deny it")
            simulated_effects.append({
                "tool": tool,
                "mode": "simulated",
                "reason": "Builder preflight never executes tools or external side effects",
            })

    # Exercise template rendering without returning customer data to the
    # caller.  Unknown template fields are a deterministic candidate error.
    if not blockers:
        try:
            compose_intake(package, values)
        except ValueError as exc:
            blockers.append(str(exc))

    failure_explanation: dict[str, Any] | None = None
    if blockers:
        first = str(blockers[0])
        if first.startswith("缺少必填项："):
            category = "input"
            actions = ["补齐必填字段后重新预检"]
        elif first.startswith("employee_binding_runtime_unavailable:"):
            category = "runtime_unsupported"
            actions = ["保留绑定草稿，待服务端员工运行装配接入；不能按旧角色替代执行"]
        elif first.startswith("skill "):
            category = "skill_contract"
            actions = ["修复对应 SKILL.md 合同字段或移除未完成 Skill 后重新预检"]
        elif "node " in first or first.startswith("workflow node"):
            category = "node_contract"
            actions = ["修正节点角色、责任、交付路径、依赖或工具白名单后重新预检"]
        elif "workflow status" in first:
            category = "lifecycle"
            actions = ["将候选置于 draft/testing 生命周期后重新预检"]
        else:
            category = "intake_contract"
            actions = ["修正工作流清单或输入模板后重新预检"]
        failure_explanation = {
            "category": category,
            "root_cause": first,
            "blocker_count": len(blockers),
            "actions": actions,
            "basis": "deterministic_contract_check",
        }

    return {
        "workflow_id": package.id,
        "workflow_version": str(package.manifest.get("version", "") or ""),
        "focus_node_id": focus_node_id,
        "status": package.status,
        "ready": not blockers,
        "mode": "simulated",
        "blockers": list(dict.fromkeys(str(item) for item in blockers if str(item).strip())),
        # The node-shaped part of the blockers above, each tied to its step and field, for a page that points at it.
        "node_blockers": node_findings(package) + binding_runtime_findings,
        # Advice tied to a step and field, never a blocker: an input nobody upstream promises to deliver.
        "node_warnings": node_warnings,
        # Advice about a step's own model (model-routing phase 0): the catalogue will refuse it when the step runs.
        "model_warnings": model_warnings,
        "warnings": list(dict.fromkeys(str(item) for item in warnings if str(item).strip())),
        "missing_fields": missing,
        "skills": skill_states,
        "skill_tests": skill_test_reports,
        "steps": steps,
        "simulated_effects": simulated_effects,
        "input_fields": [str(spec["id"]) for spec in package.intake_fields],
        "failure_explanation": failure_explanation,
    }


def simulate_test_run(
    package: WorkflowPackage,
    fields: dict[str, Any],
    *,
    actor: str,
) -> tuple[WorkflowPackage, dict[str, Any]]:
    """Record a deterministic, side-effect-free Builder simulation.

    This is deliberately weaker than a provider-backed trial: it exercises
    the intake/node/Skill contract and records *simulated* evidence, but it
    cannot unlock testing submission or release.  Keeping that distinction in
    the immutable record prevents a green preflight from being presented as a
    production execution proof.
    """
    result = preflight_invocation(package, fields)
    if not result["ready"]:
        raise ValueError("workflow simulation blocked: " + "; ".join(result["blockers"]))
    simulated = record_test_run(
        package,
        actor=actor,
        outcome="passed",
        execution_mode="simulated",
        notes="Deterministic Builder simulation; no tools, writes, model calls, or external messages were executed.",
        duration_minutes=0,
        model_calls=0,
        cost_cny_estimate=0,
    )
    result["recorded"] = True
    result["execution_mode"] = "simulated"
    return simulated, result


def provider_trial_preflight(
    package: WorkflowPackage,
    fields: dict[str, Any],
    *,
    adapter: Any,
    project_id: str,
) -> dict[str, Any]:
    """Build a Provider-backed trial plan without starting the Provider.

    The plan joins the deterministic workflow contract check with the
    fail-closed Provider Conformance report.  It is intentionally a read-only
    boundary: no session, process, model call, file write, or external effect
    is created here.  A later real trial may proceed only after this plan is
    ready and the caller explicitly opts into execution.
    """
    if adapter is None:
        raise ValueError("provider adapter is required")
    from ampuly.layer3_agent.provider_conformance import evaluate_provider

    preflight = preflight_invocation(package, fields)
    report = evaluate_provider(adapter)
    provider_payload = report.to_payload()
    blockers = list(preflight.get("blockers", []) or [])
    if not report.production_ready:
        blockers.extend(f"provider {report.provider}: {name}" for name in report.blockers)
    return {
        "workflow_id": package.id,
        "workflow_version": str(package.manifest.get("version", "") or ""),
        "project_id": str(project_id or "default").strip() or "default",
        "provider": provider_payload,
        "preflight": preflight,
        "ready": not blockers,
        "mode": "provider_preflight",
        "blockers": list(dict.fromkeys(str(item) for item in blockers if str(item).strip())),
        "execution_started": False,
    }


def employee_runtime_readiness(package, host=None):
    readiness = package.release_readiness
    findings = employee_binding_runtime_findings(package.nodes)
    if not findings:
        return readiness
    from .employee_runtime_host import EmployeeRuntimeHost
    if type(host) is not EmployeeRuntimeHost:
        assert_employee_binding_runtime_available(package.nodes)
    host.validate_for_publication(package)
    # Remove only the blanket "not wired" finding after a real host proves
    # this exact bound configuration. All original test/approval gates remain.
    blanket = {f"{row['code']}: {row['message']}" for row in findings}
    blockers = [item for item in readiness["blockers"] if item not in blanket]
    return {"ready": not blockers, "blockers": blockers}


def validate_invocation(ampuly_home: Path, project_id: str, package: WorkflowPackage, fields: dict[str, Any], *, employee_launch_source=None, employee_runtime_host=None, existing_grants_only: bool = False) -> dict[str, Any]:
    """Read-only invocation checks and derived inputs; never install or grant."""
    if type(existing_grants_only) is not bool:
        raise ValueError("invalid_grant_mode")
    if existing_grants_only:
        existing_tool_grants(ampuly_home, project_id, package)
    if employee_launch_source is None or employee_runtime_host is None:
        assert_employee_binding_runtime_available(package.nodes)
    else:
        from .employee_runtime_host import EmployeeRuntimeHost
        if type(employee_runtime_host) is not EmployeeRuntimeHost:
            raise ValueError("employee runtime host is unavailable")
        employee_runtime_host.validate_launch(project_id, package, employee_launch_source)
    if (not package.invokable if employee_launch_source is None else
            bool(package.problems) or package.status not in INVOKABLE):
        raise ValueError(
            "; ".join(package.problems) if package.problems else f"workflow status is {package.status}, not invokable"
        )
    # Before anything is installed or granted: a flow another project owns never runs here, whoever asks.
    if not usable_by_project(package, project_id):
        raise ValueError("这条业务流是另一个项目专用的，不能在本项目运行")
    message = compose_intake(package, fields)
    return {
        "message": message,
        "exec": package.exec,
        "org_id": package.org_id,
        "workflow_contract": {
            "project_id": str(project_id or "default").strip() or "default",
            "workflow_id": package.id,
            "workflow_version": str(package.manifest.get("version", "") or ""),
            "skill_versions": package.locked_skill_versions,
            "skill_sha256": dict(package.release_evidence.get("skill_sha256", {}) or {}),
            "manifest_sha256": str(package.release_evidence.get("manifest_sha256", "") or ""),
        },
        "title": str(dict(package.manifest.get("intake", {}) or {}).get("title", "") or "{name}").format(
            name=package.name, **{k: str(fields.get(k, "") or "") for k in (f["id"] for f in package.intake_fields)}
        ).strip()[:80],
    }


def prepare_invocation(ampuly_home: Path, project_id: str, package: WorkflowPackage, fields: dict[str, Any], *, employee_launch_source=None, employee_runtime_host=None, existing_grants_only: bool = False, legacy_mutable_install: bool = False) -> dict[str, Any]:
    """Explicit legacy admin installation, not the supported formal launch.

    Unclaimed direct calls refuse before any read/install/grant. The real
    console supplies RuntimeProjectionLaunchHost and the original outbox claim.
    Only trusted compatibility embeddings can opt into this old installer;
    the flag is never accepted from a client message or invocation fields.
    """
    if legacy_mutable_install is not True:
        from .ai_employee_contract import AIEmployeeContractError
        raise AIEmployeeContractError('runtime_publication_required','请通过正式发起入口固定运行版本；旧安装器需要显式管理员兼容调用。')
    prepared = validate_invocation(ampuly_home, project_id, package, fields,
        employee_launch_source=employee_launch_source, employee_runtime_host=employee_runtime_host,
        existing_grants_only=existing_grants_only)
    # Every package that runs on this org is installed first, so the role
    # prompts carry all node tables (the delegator sees each package's plan
    # and picks the one the order names); the launched package goes last.
    # Another project's own flow is not one of them: its steps are that customer's -- and the tables its earlier
    # runs left in the shared org config are cleared on the way (install_package with the project).
    for sibling in sibling_packages(package, ampuly_home):
        if not usable_by_project(sibling, project_id):
            continue
        try:
            install_package(ampuly_home, sibling, project_id=project_id)
        except ValueError:
            continue
    installed = install_package(ampuly_home, package, project_id=project_id)
    staffing_keys = write_staffing_defaults(ampuly_home, project_id, package)
    grants = (existing_tool_grants(ampuly_home, project_id, package) if existing_grants_only
              else write_tool_grants(ampuly_home, project_id, package))
    return {**prepared, "installed": installed, "staffing_scopes": staffing_keys, "tool_grants": grants}
