"""Structured missing Skill/subflow drafts on the original master path.

Pure preparation only: the existing skill_factory and flow_compiler build the
same contracts their ordinary editors use. The owner must materialize through
the original package writers; this module grants no executable capability.
"""
from __future__ import annotations

import copy
import hashlib
import re

from .flow_draft_resume import DraftReferenceError, _plain, resolve_exact_draft

_SKILL=re.compile(r"[a-z][a-z0-9_-]{1,63}\Z")
_STAMP=re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})?\Z")


class ComponentCandidateError(ValueError):
    def __init__(self,code="invalid_candidate"):
        self.code=code
        super().__init__("组件草稿无法生成，请核对原流程步骤与草稿内容。")


def _text(value,limit,*,required=True):
    if type(value) is not str or len(value)>limit or "\x00" in value or required and not value.strip():
        raise ComponentCandidateError()
    return value


def _texts(value,*,count=20,limit=500,required=True):
    if type(value) is not list or len(value)>count or required and not value:
        raise ComponentCandidateError()
    return [_text(item,limit) for item in value]


def _target(stored,source,*,project_id,step_id):
    try:
        draft=resolve_exact_draft(stored,source,project_id=project_id)
    except DraftReferenceError as error:
        raise ComponentCandidateError(error.code) from None
    _text(step_id,64)
    nodes=draft.get("nodes")
    missing=draft.get("missing_components")
    if type(nodes) is not list or type(missing) is not list:
        raise ComponentCandidateError()
    selected=[node for node in nodes if type(node) is dict and node.get("id")==step_id]
    gaps=[gap for gap in missing if type(gap) is dict and gap.get("step")==step_id]
    if len(selected)!=1 or len(gaps)!=1:
        raise ComponentCandidateError("target_not_missing")
    if selected[0].get("type")!="ai_do":
        raise ComponentCandidateError("target_not_ai_step")
    return draft,copy.deepcopy(selected[0]),copy.deepcopy(gaps[0])


def build_component_candidate(stored,source,*,project_id,step_id,kind,details,generated_at,roles,library):
    """One untested preview tied to one exact missing node and source revision.

    ``roles`` and ``library`` come from the same authenticated project loaders
    as master_propose_flow_draft, never model arguments or frontend tables.
    ``generated_at`` is supplied by the server. No runtime/file access occurs.
    """
    draft,node,gap=_target(stored,source,project_id=project_id,step_id=step_id)
    if type(generated_at) is not str or _STAMP.fullmatch(generated_at) is None:
        raise ComponentCandidateError()
    if type(details) is not dict or kind not in {"skill","flow"}:
        raise ComponentCandidateError()
    _plain(details)
    if kind=="skill":
        if set(details)!={"skill_id","method","acceptance"}:
            raise ComponentCandidateError()
        skill_id=_text(details["skill_id"],64)
        if _SKILL.fullmatch(skill_id) is None: raise ComponentCandidateError()
        method=_text(details["method"],8000)
        acceptance=_texts(details["acceptance"])
        from .process_spec import ProcessSpec,Step
        from .skill_factory import draft_skill_files
        inputs=_texts(node.get("inputs",[]),required=False,limit=120)
        outputs=_texts(node.get("deliverables",[]),limit=120)
        role=_text(node.get("role",""),64)
        if type(roles) is not list or role not in {row.get("id") for row in roles if type(row) is dict}:
            raise ComponentCandidateError("current_role_unavailable")
        responsibility=re.sub(r"（待补组件：[^）]*）$","",_text(node.get("responsibility",""),1000))
        step=Step(id="component_step",name=_text(node.get("name"),80),kind="ai_do",role=role,
            inputs=inputs,outputs=outputs,skills=[skill_id],tools=[],standard=acceptance,
            responsibility=responsibility+"\n\n方法草稿（待人工确认）：\n"+method,
            human_gate=True,gate_to="owner")
        spec_id="component_"+hashlib.sha256(_plain({"source":source,"step_id":step_id,"skill_id":skill_id})).hexdigest()[:16]
        spec=ProcessSpec(spec_id=spec_id,name=step.name+"组件草稿",version="0.1.0",
            trigger="收到原步骤声明的输入后",finishes_when="原步骤交付物经人工确认",
            steps=[step],acceptance_criteria=acceptance,data_scope=project_id,author="master-candidate")
        files=draft_skill_files(spec,step,skill_id,generated_at=generated_at)
        payload={"skill_id":skill_id,"process_spec":spec.to_payload(),"files":files}
    else:
        if set(details)!={"intent"}: raise ComponentCandidateError()
        from .flow_compiler import normalize_intent,compile_intent
        intent=normalize_intent(details["intent"])
        # The parent team's identity stays explicit. A model cannot select a
        # different customer's team by including one in a proposed child.
        if intent["team"] not in {"",draft.get("team","")}:
            raise ComponentCandidateError("target_team_changed")
        intent["team"]=draft.get("team","")
        payload={"flow_draft":compile_intent(intent,roles=roles,library=library)}
    body={"schema_version":1,"kind":kind,"source":copy.deepcopy(source),"step_id":step_id,
        "source_node_sha256":hashlib.sha256(_plain(node)).hexdigest(),"capability":_text(gap.get("capability"),160),
        "payload":payload,"status":"draft","tested":False,"admitted":False,"published":False,
        "effects_produced":False,"generated_at":generated_at}
    body["candidate_id"]="component-"+hashlib.sha256(_plain(body)).hexdigest()[:24]
    return body


def validate_candidate(value,*,project_id,stored):
    """Revalidate retained content and original source before any user adoption.

    This returns data for the existing editor/writer. It never flips readiness,
    copies files, inserts a node, opens a model, or silently rebases a draft.
    """
    if type(value) is not dict or set(value)!={"schema_version","kind","source","step_id",
        "source_node_sha256","capability","payload","status","tested","admitted","published",
        "effects_produced","generated_at","candidate_id"}:
        raise ComponentCandidateError()
    if (type(value["schema_version"]) is not int or value["schema_version"]!=1
            or value["kind"] not in {"skill","flow"} or value["status"]!="draft"
            or any(value[key] is not False for key in ("tested","admitted","published","effects_produced"))):
        raise ComponentCandidateError()
    _plain(value)
    detached=copy.deepcopy(value)
    candidate_id=detached.pop("candidate_id")
    if candidate_id!="component-"+hashlib.sha256(_plain(detached)).hexdigest()[:24]:
        raise ComponentCandidateError("candidate_changed")
    _,node,_=_target(stored,value["source"],project_id=project_id,step_id=value["step_id"])
    if value["source_node_sha256"]!=hashlib.sha256(_plain(node)).hexdigest():
        raise ComponentCandidateError("source_node_changed")
    payload=value["payload"]
    if type(payload) is not dict: raise ComponentCandidateError()
    if value["kind"]=="skill":
        if (set(payload)!={"skill_id","process_spec","files"} or type(payload["skill_id"]) is not str
                or _SKILL.fullmatch(payload["skill_id"]) is None or type(payload["files"]) is not dict
                or set(payload["files"])!={"SKILL.md","tests/contract.md"}
                or any(type(text) is not str or len(text.encode("utf-8"))>65536 for text in payload["files"].values())):
            raise ComponentCandidateError()
        from .process_spec import from_payload
        from .skill_factory import draft_skill_files
        spec=from_payload(payload["process_spec"])
        if len(spec.steps)!=1 or spec.steps[0].skills!=[payload["skill_id"]] or spec.data_scope!=project_id:
            raise ComponentCandidateError()
        if payload["files"]!=draft_skill_files(spec,spec.steps[0],payload["skill_id"],generated_at=value["generated_at"]):
            raise ComponentCandidateError("candidate_changed")
    elif set(payload)!={"flow_draft"}:
        raise ComponentCandidateError()
    return copy.deepcopy(value)
