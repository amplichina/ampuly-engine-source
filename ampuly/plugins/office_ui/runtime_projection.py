"""Immutable runtime inputs published behind one versioned head.

This is artifact storage, not another Task/Run/Effect ledger. It reuses the AE00
file transaction and native path boundary. It grants no tool permission and does
not replace the live approval allowlist. Runtime consumers must carry the exact
snapshot reference; separately reading the legacy mutable folders is not safe.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Callable, Mapping

from .ai_employee_contract import AIEmployeeContractError, canonical_json
from .ai_employee_storage import AIEmployeeFileStore, MAX_RECORD_BYTES

ACTION = "runtime_projection_publish_v1"
MAX_FILES = 128
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_RESOURCE = re.compile(r"projects/[a-f0-9]{64}\.(?:head|generation)\.json\Z")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in "123456789¹²³"), *(f"lpt{i}" for i in "123456789¹²³")}


def publication_storage_contract(platform: str) -> dict:
    """The required native write protocol, not hardware/power-loss evidence.

    Returned only as part of a committed publication receipt. A restored old
    receipt without this field remains old evidence and is never upgraded.
    """
    if platform not in {"nt", "posix"}:
        raise ValueError("unsupported_storage_platform")
    return {
        "schema_version": 1,
        "scope": "publication_api_protocol",
        "platform": platform,
        "file_flush": "required_before_and_after_publication",
        "namespace_publication": "native_parent_scoped_with_identity_readback",
        "write_through": "file_mutation_handles_requested" if platform == "nt" else "not_applicable",
        "directory_flush": "not_separately_issued" if platform == "nt" else "parent_directory_fsync_required",
        "power_loss_validation": "not_attested",
    }


def _invalid(code: str = "projection_invalid") -> AIEmployeeContractError:
    return AIEmployeeContractError(code, "运行时发布输入或快照无效，请核对原版本。")


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _identifier(value: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise _invalid()
    return value


def _scope(project: str, org: str) -> str:
    return _hash(canonical_json([_identifier(project), _identifier(org)]))


def _relative(value: str) -> str:
    if type(value) is not str or not value or len(value) > 240 or "\\" in value or ":" in value:
        raise _invalid()
    parts = PurePosixPath(value).parts
    if (value != "/".join(parts) or len(parts) > 12 or value.startswith("/")
            or any(p in {"", ".", ".."} or p.endswith((".", " "))
                   or p.split(".", 1)[0].casefold() in _RESERVED
                   or any(ord(c) < 32 or ord(c) == 127 or c in '?*"<>|' for c in p) for p in parts)):
        raise _invalid()
    return value


def _grants(value: dict[str, list[str]]) -> dict[str, list[str]]:
    if type(value) is not dict or len(value) > 64:
        raise _invalid()
    if any(type(tool) is not str for tool in value):
        raise _invalid()
    result = {}
    for tool, patterns in sorted(value.items()):
        if (type(tool) is not str or not tool or len(tool) > 128
                or type(patterns) is not list or not 0 < len(patterns) <= 64
                or any(type(p) is not str or not p or len(p) > 512 for p in patterns)):
            raise _invalid()
        result[tool] = sorted(set(patterns))
    return result


class _ProjectionHeads(AIEmployeeFileStore):
    """Private instance of the existing file transaction, with fixed resources.

    Its root is home/runtime_projections. AE00 lock/request/pending metadata is
    therefore private to projection artifacts; ordinary employee assets and their
    pending index are neither read nor modified.
    """

    def object_path(self, relative: str) -> Path:
        if type(relative) is not str or not _RESOURCE.fullmatch(relative):
            raise _invalid("storage_path_invalid")
        path = self.root.joinpath(*relative.split("/"))
        self._check(path)
        return path

    def assert_targets_available(self, paths: list[str], project_id: str, request_id: str) -> None:
        """Called by commit's final callback while its original native lock is held.

        The generic transaction also checks reservations later. Prechecking here
        rejects a replacement request before creating another prepared intent.
        It does not release, reconcile or rewrite any prior request.
        """
        _, pending = self._pending_index()
        request = self._request_path(ACTION, project_id, request_id)
        for path in paths:
            previous = pending.get(path.casefold())
            if previous is None or previous == request.stem:
                continue
            raw = self._read(request.parent / f"{previous}.json")
            if raw is None or self._decode_intent(raw)["state"] != "committed":
                raise _invalid("outcome_unknown")


@dataclass(frozen=True)
class RuntimeProjection:
    reference: Mapping[str, str | int]
    head_revision: str | None
    files: Mapping[str, bytes]
    grant_intents: Mapping[str, tuple[str, ...]]


class RuntimeProjectionRepository:
    """An inert reader and guarded, CAS-protected publication boundary.

    The final guard is mandatory on writes/replays. It must revalidate the real
    caller's authority and source observation; the repository is not an authority
    source. A reference is a data identity, never a permission or execution grant.
    """

    def __init__(self, ampuly_home: Path):
        self.store = _ProjectionHeads(Path(ampuly_home) / "runtime_projections")

    @staticmethod
    def _input(project_id, organization_id, actor, request_id, source_revision,
               expected_revision, files, grant_intents):
        _identifier(project_id); _identifier(organization_id); _identifier(request_id)
        if (type(actor) is not str or not actor.strip() or len(actor) > 128
                or type(source_revision) is not str or not _SHA.fullmatch(source_revision)
                or (expected_revision is not None and
                    (type(expected_revision) is not str or not _SHA.fullmatch(expected_revision)))
                or type(files) is not dict or not 0 < len(files) <= MAX_FILES):
            raise _invalid()
        if any(type(name) is not str for name in files):
            raise _invalid()
        rows = {}; total = 0; names = set()
        for name, data in sorted(files.items()):
            _relative(name)
            if name.casefold() in names or type(data) is not bytes or len(data) > MAX_FILE_BYTES:
                raise _invalid()
            names.add(name.casefold()); total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise _invalid("projection_too_large")
            rows[name] = {"sha256": _hash(data), "base64": base64.b64encode(data).decode("ascii")}
        grants = _grants(grant_intents)
        payload = {"schema_version": 1, "project_id": project_id, "organization_id": organization_id,
                   "source_revision": source_revision, "expected_revision": expected_revision,
                   "file_sha256": {k: v["sha256"] for k, v in rows.items()}, "grant_intents": grants}
        generation = _hash(canonical_json([payload, actor, request_id]))
        document = {"schema_version": 1, "generation": generation, "project_id": project_id,
                    "organization_id": organization_id, "source_revision": source_revision,
                    "files": rows, "grant_intents": grants,
                    "publication": {"actor": actor, "request_id": request_id, "payload": payload}}
        raw = canonical_json(document)
        if len(raw) > MAX_RECORD_BYTES:
            raise _invalid("projection_too_large")
        reference = {"schema_version": 1, "project_id": project_id, "organization_id": organization_id,
                     "generation": generation, "snapshot_sha256": _hash(raw), "source_revision": source_revision}
        return payload, reference, raw

    def publish(self, *, project_id: str, organization_id: str, actor: str, request_id: str,
                source_revision: str, expected_revision: str | None, files: dict[str, bytes],
                grant_intents: dict[str, list[str]], assert_authorized_and_fresh: Callable[[], None]) -> dict:
        if not callable(assert_authorized_and_fresh):
            raise _invalid("projection_guard_required")
        payload, reference, snapshot = self._input(project_id, organization_id, actor, request_id,
                                                  source_revision, expected_revision, files, grant_intents)
        scope = _scope(project_id, organization_id)
        head = canonical_json(reference)
        generation_path = f"projects/{reference['generation']}.generation.json"
        head_path = f"projects/{scope}.head.json"

        def final_guard():
            self.store._before_commit(assert_authorized_and_fresh)
            self.store.assert_targets_available([generation_path, head_path], project_id, request_id)

        # One existing transaction: immutable generation first, then the only
        # active head. Readers require its committed receipt, not just its name.
        return self.store.commit(
            action=ACTION, project_id=project_id, request_id=request_id, actor=actor, payload=payload,
            changes=[{"path": generation_path, "before_sha256": None,
                      "after_text": snapshot.decode("utf-8")},
                     {"path": head_path, "before_sha256": expected_revision,
                      "after_text": head.decode("utf-8")}],
            response={"ok": True, "reference": reference, "head_revision": _hash(head),
                      "permissions_granted": False, "runtime_activated": False,
                      "storage_contract": publication_storage_contract(os.name)},
            before_commit=final_guard,
        )

    def inspect_request(self, *, project_id: str, organization_id: str, actor: str, request_id: str,
                        source_revision: str, expected_revision: str | None, files: dict[str, bytes],
                        grant_intents: dict[str, list[str]]) -> dict | None:
        payload, _, _ = self._input(project_id, organization_id, actor, request_id, source_revision,
                                    expected_revision, files, grant_intents)
        return self.store.request_status(action=ACTION, project_id=project_id, request_id=request_id,
                                         actor=actor, payload=payload, require_committed=True)

    def inspect_committed_publication(self, *, project_id, organization_id, actor, request_id,
                                      assert_authorized):
        """Read the ORIGINAL request, without guessing/replacing its head CAS.

        A committed launch resumes its original generation even if source assets
        later changed. Prepared/unknown requests never roll forward here.
        """
        _scope(project_id, organization_id); _identifier(request_id)
        if not callable(assert_authorized):
            raise _invalid('projection_guard_required')
        self.store._before_commit(assert_authorized)
        request = self.store._request_path(ACTION, project_id, request_id)
        from .ai_employee_storage import MAX_INTENT_BYTES
        raw = self.store._read(request, MAX_INTENT_BYTES)
        if raw is None:
            return None
        intent = self.store._decode_intent(raw)
        if intent['actor'] != actor:
            raise _invalid('request_id_reused')
        if intent['state'] != 'committed':
            raise _invalid('outcome_unknown')
        response = intent['response']
        if response.get('ok') is not True:
            raise _invalid('outcome_unknown')
        snapshot = self.read_pinned(response.get('reference'), project_id=project_id,
                                    organization_id=organization_id)
        # The generation's receipt must name this exact original request too.
        raw = self.store.read(f"projects/{snapshot.reference['generation']}.generation.json")
        if raw is None or _hash(raw) != snapshot.reference['snapshot_sha256']:
            raise _invalid('projection_corrupt')
        generation = json.loads(raw)
        if generation['publication']['request_id'] != request_id or generation['publication']['actor'] != actor:
            raise _invalid('request_id_reused')
        self.store._verify_request(intent, actor, generation['publication']['payload'])
        return {**response, 'idempotent': True}

    def read_active(self, project_id: str, organization_id: str) -> RuntimeProjection | None:
        raw = self.store.read(f"projects/{_scope(project_id, organization_id)}.head.json")
        if raw is None:
            return None
        try:
            reference = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise _invalid("projection_corrupt") from None
        result = self.read_pinned(reference, project_id=project_id, organization_id=organization_id)
        return RuntimeProjection(result.reference, _hash(raw), result.files, result.grant_intents)

    def read_pinned(self, reference: dict, *, project_id: str, organization_id: str) -> RuntimeProjection:
        scope = _scope(project_id, organization_id)
        if isinstance(reference, Mapping):
            reference = dict(reference)
        keys = {"schema_version", "project_id", "organization_id", "generation", "snapshot_sha256", "source_revision"}
        if (type(reference) is not dict or set(reference) != keys or type(reference["schema_version"]) is not int
                or reference["schema_version"] != 1 or reference["project_id"] != project_id
                or reference["organization_id"] != organization_id
                or any(type(reference[k]) is not str or not _SHA.fullmatch(reference[k])
                       for k in ["generation", "snapshot_sha256", "source_revision"])):
            raise _invalid("projection_scope_mismatch")
        raw = self.store.read(f"projects/{reference['generation']}.generation.json")
        if raw is None or _hash(raw) != reference["snapshot_sha256"]:
            raise _invalid("projection_corrupt")
        try:
            document = json.loads(raw)
            publication = document["publication"]
            encoded = document["files"]
            if type(encoded) is not dict or not 0 < len(encoded) <= MAX_FILES:
                raise ValueError
            files = {name: base64.b64decode(row["base64"], validate=True) for name, row in encoded.items()}
            payload, derived_reference, derived_raw = self._input(
                project_id, organization_id, publication["actor"], publication["request_id"],
                reference["source_revision"], publication["payload"]["expected_revision"],
                files, document["grant_intents"])
            if derived_reference != reference or derived_raw != raw or payload != publication["payload"]:
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, binascii.Error):
            raise _invalid("projection_corrupt") from None
        receipt = self.store.request_status(action=ACTION, project_id=project_id,
            request_id=publication["request_id"], actor=publication["actor"], payload=payload, require_committed=True)
        if receipt is None or receipt.get("ok") is not True or receipt.get("reference") != reference:
            raise _invalid("outcome_unknown")
        return RuntimeProjection(MappingProxyType(dict(reference)), None, MappingProxyType(files),
                                 MappingProxyType({k: tuple(v) for k, v in document["grant_intents"].items()}))
