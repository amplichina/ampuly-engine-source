"""Owner adopts a retained component draft through ORIGINAL package writers.

No execution, publish, admission or test evidence is created. One original
manifest lock and node-save CAS owns the parent change. Asset/child preparation
is not a cross-file transaction: failures after preparation return unknown and
retain those drafts for inspection; check_only never retries preparation.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import re

from .flow_component_candidates import ComponentCandidateError, validate_candidate
from .flow_draft_resume import _plain, resolve_materialized_draft

_ID=re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class ComponentAdoptionError(ValueError):
    def __init__(self,code="component_adoption_refused"):
        self.code=code
        self.effects_produced=None if code=="adoption_outcome_unknown" else False
        super().__init__("组件草稿未能确认采用，请核对原草稿或查询本次结果。")


def retained_candidate(message,*,project_id,message_id,candidate_id):
    if (type(message) is not dict or message.get("project_id")!=project_id
            or message.get("message_id")!=message_id or message.get("sender")!="assistant"
            or type(message.get("metadata")) is not dict):
        raise ComponentAdoptionError("candidate_not_found")
    values=message["metadata"].get("master_component_candidates")
    if type(values) is not list or len(values)>4: raise ComponentAdoptionError("candidate_not_found")
    selected=[item for item in values if type(item) is dict and item.get("candidate_id")==candidate_id]
    if len(selected)!=1: raise ComponentAdoptionError("candidate_not_found")
    return copy.deepcopy(selected[0])


def _authorize(callback,actor):
    if type(actor) is not str or not actor or len(actor)>128 or not callable(callback) or callback() is not True:
        raise ComponentAdoptionError("owner_required")


def _node_fingerprint(node):
    from .workflow_packages import node_payload
    return hashlib.sha256(_plain({key:value for key,value in node_payload(node).items() if key!="canvas"})).hexdigest()


def _target_nodes(package,candidate,source):
    from .workflow_packages import node_payload
    original=[node for node in source["metadata"]["master_flow_draft"]["nodes"] if node.get("id")==candidate["step_id"]]
    if len(original)!=1: raise ComponentAdoptionError("source_node_changed")
    nodes=copy.deepcopy(package.nodes or source["metadata"]["master_flow_draft"]["nodes"])
    matches=[node for node in nodes if node.get("id")==candidate["step_id"]]
    if len(matches)!=1: raise ComponentAdoptionError("source_node_changed")
    # Layout is the owner's current layout and survives. Every original
    # semantic field of the targeted node must still be what was proposed.
    current=node_payload(matches[0]); previous=node_payload(original[0])
    if {key:value for key,value in current.items() if key!="canvas"}!={key:value for key,value in previous.items() if key!="canvas"}:
        raise ComponentAdoptionError("source_node_changed")
    return nodes,matches[0]


def inspect_component_candidate(*,stored_candidate,stored_source,project_id,candidate_message_id,candidate_id,
        source_reference,packages,actor,authorize_owner):
    _authorize(authorize_owner,actor)
    value=retained_candidate(stored_candidate,project_id=project_id,message_id=candidate_message_id,candidate_id=candidate_id)
    if source_reference!=value.get("source"): raise ComponentAdoptionError("source_reference_changed")
    try:
        value=validate_candidate(value,project_id=project_id,stored=stored_source)
        target=resolve_materialized_draft(packages,value["source"],project_id=project_id)
    except ValueError as error:
        raise ComponentAdoptionError(getattr(error,"code","invalid_candidate")) from None
    if target is None: raise ComponentAdoptionError("open_original_draft_first")
    package=next(item for item in packages if item.id==target["materialized"]["package_id"])
    if package.status!="draft": raise ComponentAdoptionError("draft_required")
    _target_nodes(package,value,stored_source)
    _authorize(authorize_owner,actor)
    return {"ok":True,"materialized":target["materialized"],"workflow_name":target["workflow"]["name"],
        "candidate_id":candidate_id,"source_reference":copy.deepcopy(source_reference),
        "effects_produced":False,"external_effects_produced":False,"ready":False}


def adopt_component_candidate(*,stored_candidate,stored_source,project_id,candidate_message_id,candidate_id,
        package,expected_materialized,new_version,request_id,actor,authorize_owner,ampuly_home,check_only=False):
    from .workflow_packages import (_manifest_lock,_checked_node_save,preview_package_nodes,
        load_package,node_payload,create_workflow_draft,_write_manifest_atomic,draft_revision,usable_by_project,
        update_package_nodes,WorkflowPackage,SHARED_SKILL_LIBRARY,_plain_skill_folder)
    from .process_spec import from_payload
    from .skill_factory import add_draft_skills
    from .process_candidate import _bounded_assets

    _authorize(authorize_owner,actor)
    if type(check_only) is not bool or type(request_id) is not str or _ID.fullmatch(request_id) is None:
        raise ComponentAdoptionError("invalid_request")
    candidate=retained_candidate(stored_candidate,project_id=project_id,message_id=candidate_message_id,candidate_id=candidate_id)
    try:
        candidate=validate_candidate(candidate,project_id=project_id,stored=stored_source)
    except ComponentCandidateError as error:
        raise ComponentAdoptionError(error.code) from None
    intent={"candidate_message_id":candidate_message_id,"candidate_id":candidate_id,"source":candidate["source"],
        "expected_materialized":expected_materialized,"new_version":new_version,"project_id":project_id}
    intent_sha=hashlib.sha256(_plain(intent)).hexdigest()
    if not isinstance(package.root,Path): raise ComponentAdoptionError("invalid_package")
    root=package.root
    home=Path(ampuly_home).resolve()
    if root.is_symlink() or getattr(root,"is_junction",lambda:False)() or root.resolve().parent!=(home/"workflows").resolve():
        raise ComponentAdoptionError("invalid_package")
    manifest_path=root/"workflow.yaml"
    started=False
    with _manifest_lock(manifest_path):
        current=load_package(root)
        if current is None or current.id!=package.id or not usable_by_project(current,project_id):
            raise ComponentAdoptionError("invalid_package")
        provenance=copy.deepcopy(current.manifest.get("provenance") or {})
        receipts=provenance.get("component_adoptions",{})
        if type(receipts) is not dict or len(receipts)>64: raise ComponentAdoptionError("adoption_history_unavailable")
        receipt=receipts.get(request_id)
        if receipt is not None:
            if type(receipt) is not dict or receipt.get("intent_sha256")!=intent_sha:
                raise ComponentAdoptionError("request_id_conflict")
            attached=next((node for node in current.nodes if node.get("id")==receipt["step_id"]),None)
            attached_hash=_node_fingerprint(attached) if attached else None
            _authorize(authorize_owner,actor)
            return {"ok":True,"outcome":"adopted","existing":True,"currently_attached":attached_hash==receipt["target_sha256"],
                "request_id":request_id,"candidate_id":candidate_id,"workflow":current.to_payload(),
                "effects_produced":False,"external_effects_produced":False,"ready":False}
        if check_only:
            _authorize(authorize_owner,actor)
            return {"ok":False,"outcome":"not_recorded","request_id":request_id,"effects_produced":False,
                "external_effects_produced":False,"ready":False}
        if len(receipts)>=64: raise ComponentAdoptionError("adoption_history_full")
        if current.status!="draft": raise ComponentAdoptionError("draft_required")
        try:
            resolve_materialized_draft([current],candidate["source"],project_id=project_id,expected=expected_materialized)
        except ValueError as error:
            raise ComponentAdoptionError(getattr(error,"code","draft_revision_changed")) from None
        nodes,target=_target_nodes(current,candidate,stored_source)
        target["responsibility"]=re.sub(r"（待补组件：[^）]*）$","",str(target.get("responsibility", "")))
        if candidate["kind"]=="skill":
            target["skills"]=[*target.get("skills",[]),candidate["payload"]["skill_id"]]
        else:
            child_id="component_flow_"+candidate_id.removeprefix("component-")[:24]
            child_nodes=candidate["payload"]["flow_draft"]["nodes"]
            target.update(type="invoke_workflow",invoke={"workflow":child_id},skills=[],tools=[])
        # The original preview rejects a bad graph/version/role before asset
        # preparation. The actual writer validates again after preparation.
        preview_package_nodes(current,nodes,actor=actor,expected_version=expected_materialized["version"],
            new_version=new_version,change_note="采用组件草稿："+candidate["capability"])
        _bounded_assets(current)
        if candidate["kind"]=="flow":
            # The clone writer copies only skills already declared by its
            # source. Newly selected library skills must go through the normal
            # node-save writer, not merely appear as dangling node references.
            child_skills=[]
            for node in child_nodes:
                child_skills.extend(node.get("skills",[]))
                child_skills.extend(component.get("ref") for component in node.get("components",[])
                    if type(component) is dict and component.get("kind")=="skill")
            child_skills=list(dict.fromkeys(child_skills))
            if len(child_skills)>64: raise ComponentAdoptionError("child_skill_unavailable")
            missing=[]
            for skill in child_skills:
                if not _plain_skill_folder(current.skills_root,skill):
                    if not _plain_skill_folder(SHARED_SKILL_LIBRARY,skill):
                        raise ComponentAdoptionError("child_skill_unavailable")
                    missing.append(skill)
            if missing:
                if SHARED_SKILL_LIBRARY.is_symlink() or getattr(SHARED_SKILL_LIBRARY,"is_junction",lambda:False)():
                    raise ComponentAdoptionError("child_skill_unavailable")
                library_source=WorkflowPackage(root=current.root,manifest={**copy.deepcopy(current.manifest),
                    "skills_root":str(SHARED_SKILL_LIBRARY),"skills":missing})
                _bounded_assets(library_source)
        _authorize(authorize_owner,actor)
        if draft_revision(root)!=expected_materialized["draft_revision"]:
            raise ComponentAdoptionError("draft_revision_changed")
        prepared=current
        try:
            started=True
            if candidate["kind"]=="skill":
                skill_id=candidate["payload"]["skill_id"]
                destination=current.skills_root/skill_id
                if destination.exists() or destination.is_symlink():
                    raise ComponentAdoptionError("skill_destination_exists")
                prepared=add_draft_skills(current,from_payload(candidate["payload"]["process_spec"]),[skill_id],
                    generated_at=candidate["generated_at"],persist_manifest=False)
            else:
                child= candidate["payload"]["flow_draft"]
                # Identity is isolated BEFORE the first filesystem write. A
                # retained half-prepared child must never claim the parent's
                # exact source-message anchor or inherit its adoption receipts.
                child_source_manifest=copy.deepcopy(current.manifest)
                child_source_manifest["created_from"]=f"component_draft:{project_id}:{candidate_id}"
                child_source_provenance=copy.deepcopy(child_source_manifest.get("provenance") or {})
                child_source_provenance.pop("component_adoptions",None)
                child_source_manifest["provenance"]=child_source_provenance
                child_source_manifest["component_origin"]={"candidate_id":candidate_id,"source":candidate["source"],"step_id":candidate["step_id"]}
                child_source=WorkflowPackage(root=current.root,manifest=child_source_manifest)
                created=create_workflow_draft(child_source,ampuly_home=home,draft_id=child_id,actor=actor,
                    name=child["name"],summary=child["summary"],nodes=[],
                    intake_fields=child["intake"] or current.intake_fields,project_id=project_id,
                    change_note="父流程组件候选 "+candidate_id)
                _authorize(authorize_owner,actor)
                created=update_package_nodes(created,child_nodes,actor=actor,
                    expected_version="0.1.0",new_version="0.1.1",expected_revision=draft_revision(created.root),
                    change_note="采用子流程节点及包内技能："+candidate_id)
                if any(skill not in created.skills or not _plain_skill_folder(created.skills_root,skill) for skill in child_skills):
                    raise ComponentAdoptionError("child_skill_unavailable")
                child_manifest=copy.deepcopy(created.manifest)
                child_manifest["created_from"]=f"component_draft:{project_id}:{candidate_id}"
                child_provenance=copy.deepcopy(child_manifest.get("provenance") or {})
                child_provenance.pop("component_adoptions",None)
                child_manifest["provenance"]=child_provenance
                child_manifest["component_origin"]={"candidate_id":candidate_id,"source":candidate["source"],"step_id":candidate["step_id"]}
                _write_manifest_atomic(created,child_manifest,suffix="component-origin")
            provenance=copy.deepcopy(prepared.manifest.get("provenance") or {})
            receipts[request_id]={"intent_sha256":intent_sha,"candidate_id":candidate_id,"step_id":candidate["step_id"],
                "source":candidate["source"],"before":copy.deepcopy(expected_materialized),"version":new_version,
                "target_sha256":_node_fingerprint(target),"actor":actor}
            provenance["component_adoptions"]=receipts
            prepared.manifest["provenance"]=provenance
            prepared.manifest["project_id"]=project_id
            _authorize(authorize_owner,actor)
            _checked_node_save(prepared,nodes,manifest_path,actor=actor,
                expected_version=expected_materialized["version"],new_version=new_version,
                expected_revision=expected_materialized["draft_revision"],change_note="采用组件草稿："+candidate["capability"])
            saved=load_package(root)
            if saved is None or saved.manifest.get("provenance",{}).get("component_adoptions",{}).get(request_id)!=receipts[request_id]:
                raise ComponentAdoptionError("adoption_outcome_unknown")
            _authorize(authorize_owner,actor)
        except BaseException as error:
            if isinstance(error,Exception) and started:
                raise ComponentAdoptionError("adoption_outcome_unknown") from None
            raise
        return {"ok":True,"outcome":"adopted","existing":False,"currently_attached":True,
            "request_id":request_id,"candidate_id":candidate_id,"workflow":saved.to_payload(),
            "effects_produced":True,"external_effects_produced":False,"ready":False}
