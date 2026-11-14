"""Exact retained flow draft references for the existing master conversation.

Pure codec/selection only. The caller authenticates the owner and loads ONE
stored project message by its exact ID. No latest-session lookup, fuzzy memory,
model, file, storage, execution, migration or implicit save exists here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

_ID=re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA=re.compile(r"[0-9a-f]{64}\Z")
_FIELDS={"schema_version","project_id","message_id","draft_sha256"}


class DraftReferenceError(ValueError):
    def __init__(self,code="invalid_reference"):
        self.code=code
        super().__init__("流程草稿引用无法核对，请重新打开原草稿。")


def _id(value):
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise DraftReferenceError()
    return value


def _plain(value):
    remaining=[12000]
    def walk(node,depth,seen):
        remaining[0]-=1
        if remaining[0]<0 or depth>24: raise DraftReferenceError()
        if type(node) in (dict,list):
            if id(node) in seen or len(node)>1000: raise DraftReferenceError()
            parents=seen|{id(node)}
            if type(node) is dict:
                if any(type(key) is not str or len(key)>128 for key in node): raise DraftReferenceError()
                for child in node.values(): walk(child,depth+1,parents)
            else:
                for child in node: walk(child,depth+1,parents)
        elif type(node) is str:
            if len(node)>16384: raise DraftReferenceError()
        elif type(node) is float:
            if not math.isfinite(node): raise DraftReferenceError()
        elif node is not None and type(node) not in (bool,int):
            raise DraftReferenceError()
        elif type(node) is int and abs(node)>2**63-1: raise DraftReferenceError()
    walk(value,0,set())
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")
    if len(raw)>512*1024: raise DraftReferenceError()
    return raw


def draft_digest(draft):
    if type(draft) is not dict or draft.get("kind")!="new_flow" or type(draft.get("intent")) is not dict:
        raise DraftReferenceError("draft_not_found")
    # Reference is associated identity, not recursive material in its own hash.
    body={key:value for key,value in draft.items() if key!="reference"}
    return hashlib.sha256(b"ampuly-flow-draft:v1\0"+_plain(body)).hexdigest()


def reference(project_id,message_id,draft):
    return {"schema_version":1,"project_id":_id(project_id),"message_id":_id(message_id),
        "draft_sha256":draft_digest(draft)}


def validate_reference(value,*,project_id):
    if (type(value) is not dict or set(value)!=_FIELDS or type(value.get("schema_version")) is not int
            or value["schema_version"]!=1 or value.get("project_id")!=_id(project_id)
            or type(value.get("draft_sha256")) is not str or _SHA.fullmatch(value["draft_sha256"]) is None):
        raise DraftReferenceError()
    _id(value["message_id"])
    return copy.deepcopy(value)


def resolve_exact_draft(stored,requested,*,project_id):
    expected=validate_reference(requested,project_id=project_id)
    if (type(stored) is not dict or stored.get("project_id")!=project_id
            or stored.get("message_id")!=expected["message_id"] or stored.get("sender")!="assistant"
            or type(stored.get("metadata")) is not dict):
        raise DraftReferenceError("draft_not_found")
    draft=stored["metadata"].get("master_flow_draft")
    if draft_digest(draft)!=expected["draft_sha256"]:
        raise DraftReferenceError("draft_revision_changed")
    result=copy.deepcopy(draft)
    result["reference"]=expected
    return result


def stamp_flow_metadata(metadata,*,project_id,message_id):
    if type(metadata) is not dict or "master_flow_draft" not in metadata: return metadata
    result=copy.deepcopy(metadata)
    draft=result["master_flow_draft"]
    existing=draft.get("reference") if type(draft) is dict else None
    if existing is not None:
        # A resumed card keeps its ORIGINAL source ID. Opening it continues the
        # exact existing created_from draft instead of creating another one.
        checked=validate_reference(existing,project_id=project_id)
        if checked["draft_sha256"]!=draft_digest(draft): raise DraftReferenceError("draft_revision_changed")
        draft["reference"]=checked
    else:
        draft["reference"]=reference(project_id,message_id,draft)
    return result


def resolve_materialized_draft(packages,requested,*,project_id,expected=None):
    ref=validate_reference(requested,project_id=project_id)
    origin=f"flow_draft:{project_id}:{ref['message_id']}"
    matches=[package for package in packages if type(getattr(package,"manifest",None)) is dict
        and package.manifest.get("created_from")==origin]
    if len(matches)>1: raise DraftReferenceError("ambiguous_materialized_draft")
    if not matches:
        if expected is not None: raise DraftReferenceError("materialized_draft_missing")
        return None
    package=matches[0]
    from .workflow_packages import usable_by_project
    if not usable_by_project(package,project_id) or package.status not in {"draft","testing"}:
        raise DraftReferenceError("draft_uneditable")
    payload=package.to_payload()
    current={"package_id":package.id,"version":payload.get("version"),"draft_revision":payload.get("draft_revision")}
    if (type(current["version"]) is not str or not current["version"] or
            type(current["draft_revision"]) is not str or _SHA.fullmatch(current["draft_revision"]) is None):
        raise DraftReferenceError("draft_revision_unavailable")
    if expected is not None and (type(expected) is not dict or expected!=current):
        raise DraftReferenceError("draft_revision_changed")
    return {"reference":ref,"materialized":current,"workflow":payload,"effects_produced":False}
