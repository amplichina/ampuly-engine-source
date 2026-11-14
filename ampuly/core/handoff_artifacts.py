"""Validate fixed inline handoff artifacts without trusting stored hash labels."""
from __future__ import annotations

import hashlib
from typing import Any


def verify_inline_artifact(row: Any, ref: dict[str, Any], *, project_id: str, task_id: str) -> str:
    if row is None or row.project_id != project_id or row.task_id != task_id:
        raise ValueError('handoff_artifact_missing')
    details = dict(row.details or {})
    content = details.get('content')
    if type(content) is not str:
        raise ValueError('handoff_artifact_content_missing')
    raw = content.encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    if (row.artifact_id != ref.get('artifact_id')
            or digest != ref.get('sha256') or digest != details.get('content_sha256')
            or type(ref.get('bytes')) is not int or ref['bytes'] != len(raw)
            or type(details.get('bytes')) is not int or details['bytes'] != len(raw)
            or type(ref.get('revision')) is not int or ref['revision'] < 1
            or ref['revision'] != details.get('revision')
            or row.name != ref.get('name')):
        raise ValueError('handoff_artifact_mismatch')
    return content
