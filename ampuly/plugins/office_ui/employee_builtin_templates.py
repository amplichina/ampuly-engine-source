"""Owner-selected shipped templates, saved through the existing durable journal.

No installation on startup. The wire supplies an observed source revision and
new asset identity; file paths and bytes always come from the installed source.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
from pathlib import Path
import re

from .ai_employee_contract import AIEmployeeContractError, canonical_json, package_fingerprint
from .ai_employee_storage import AIEmployeeFileStore

TEMPLATE_ID = "customer-service"
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_MONEY = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?\Z")
_RESOURCES = frozenset({"conversation-system.md", "conversation-playbook.md",
    "conversation-response.schema.json", "conversation-input.schema.json",
    "conversation-output.schema.json", "knowledge-contract.json", "taskset.json",
    "skills/customer-service/SKILL.md"})


def _bundle(actor: str, at: str, budget: str):
    from ampuly.components.customer_service.assets import build_shipped_customer_service_package
    return build_shipped_customer_service_package(actor=actor, created_at=at, max_cost_cny=budget)


def builtin_template_list() -> list[dict]:
    # Zero is only a pure authoring argument, not a pricing claim or a default
    # copied to a user's draft. Creation requires the owner's explicit limit.
    bundle = _bundle("template-catalog", "2026-10-16T00:00:00+00:00", "0")
    manifest = bundle.manifest
    return [{"template_id": TEMPLATE_ID, "source_revision": manifest["provenance"]["source_revision"],
        "name": manifest["name"], "version": manifest["version"],
        "responsibility": manifest["responsibility"], "scope_in": manifest["scope_in"],
        "scope_out": manifest["scope_out"], "human_slots": manifest["human_slots"],
        "input_schema": manifest["input_schema"], "output_schema": manifest["output_schema"],
        "artifact_contract": manifest["artifact_contract"], "resource_count": len(bundle.files),
        "persisted": False, "evaluation_state": "not_run", "publication_state": "not_requested"}]


class _TemplateStore(AIEmployeeFileStore):
    max_changes = 16

    def __init__(self, root: Path, package_id: str):
        self.resource_paths = {f"employee_packages/{package_id}/{name}".casefold(): f"employee_packages/{package_id}/{name}" for name in _RESOURCES}
        super().__init__(root)

    def object_path(self, relative: str) -> Path:
        if type(relative) is str and relative.casefold() in self.resource_paths:
            path = self.root.joinpath(*self.resource_paths[relative.casefold()].split("/"))
            self._check(path)
            return path
        return super().object_path(relative)


def create_builtin_template(repository, manifest: dict, *, template_id: str,
                            expected_template_revision: str, project_id: str,
                            actor: str, request_id: str) -> dict:
    if (template_id != TEMPLATE_ID or type(manifest) is not dict
            or set(manifest) != {"package_id", "name", "max_cost_cny"}
            or type(manifest["package_id"]) is not str or not _PACKAGE.fullmatch(manifest["package_id"])
            or type(manifest["name"]) is not str or not 0 < len(manifest["name"].strip()) <= 128
            or type(manifest["max_cost_cny"]) is not str or not _MONEY.fullmatch(manifest["max_cost_cny"])
            or type(expected_template_revision) is not str or not re.fullmatch(r"[a-f0-9]{64}", expected_template_revision)):
        raise AIEmployeeContractError("request_invalid", "请选择内置模板，填写岗位编号、名称与本次金额上限。")
    package_id = manifest["package_id"]
    store = _TemplateStore(repository.store.root, package_id)
    action = "employee_package_draft_create"
    payload = {"manifest": manifest, "template_id": template_id,
               "expected_template_revision": expected_template_revision}
    previous = store.request_status(action=action, project_id=project_id, actor=actor,
                                    request_id=request_id, payload=payload)
    if previous is not None and previous.get("result") != "outcome_unknown":
        return previous
    if previous is not None:
        # The frozen intent, not a newer installed template or wall clock,
        # supplies the original bytes and receipt during explicit recovery.
        return store.commit(action=action, project_id=project_id, actor=actor,
                            request_id=request_id, payload=payload, changes=[], response={})
    if previous is None and store.read(repository._relative(package_id)) is not None:
        raise AIEmployeeContractError("not_allowed_in_status", "这个岗位编号已有草稿，请另选编号。")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    bundle = _bundle(actor, now, manifest["max_cost_cny"])
    if bundle.manifest["provenance"]["source_revision"] != expected_template_revision:
        raise AIEmployeeContractError("revision_conflict", "模板已有新版本，请重新查看后创建。")
    if set(bundle.files) != _RESOURCES:
        raise AIEmployeeContractError("validation_failed", "模板资源清单与当前保存合同不符。")
    draft = deepcopy(bundle.manifest)
    draft.update(package_id=package_id, name=manifest["name"].strip())
    repository._validate_draft(draft)
    # Skill bytes are saved below too, but their declared tree fingerprint
    # owns that namespace. Ordinary resource hashing must not double-declare it.
    resource_names = {entry["path"] for entry in draft["provenance"]["files"]}
    draft["package_fingerprint"] = package_fingerprint(draft,
        {name: raw for name, raw in bundle.files.items() if name in resource_names})
    text = canonical_json(draft).decode("utf-8") + "\n"
    revision = hashlib.sha256(text.encode("utf-8")).hexdigest()
    changes = [{"path": f"employee_packages/{package_id}/{name}", "before_sha256": None,
                "after_text": raw.decode("utf-8")} for name, raw in sorted(bundle.files.items())]
    # Manifest last: a package does not become discoverable while resources
    # are only partly saved. The original request journal owns crash recovery.
    changes.append({"path": repository._relative(package_id), "before_sha256": None, "after_text": text})
    response = {"ok": True, "action": action, "package_id": package_id, "request_id": request_id,
        "revision": revision, "fingerprint": draft["package_fingerprint"], "result": "success",
        "effects_produced": True, "problems": [], "allowed_actions": ["employee_package_detail", "employee_package_draft_save"]}
    return store.commit(action=action, project_id=project_id, actor=actor, request_id=request_id,
                        payload=payload, changes=changes, response=response)
