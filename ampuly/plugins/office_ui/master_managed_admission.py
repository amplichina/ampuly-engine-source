"""Fail-closed admission for the composed master runtime, not a CLI adapter.

The deployment bootstrap must supply a synchronous, read-only identity resolver
for the installed runtime AND its gateway/tool policy. No resolver is registered
here; a declaration or a cached production_ready boolean cannot authorize it.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ampuly.layer3_agent.conformance_suite import EvidenceIdentity
from ampuly.layer3_agent.provider_admission import decide_from_home
from ampuly.plugins.office_ui.master_runtime import MasterSafetyStop

SUBJECT = 'superones_managed_master'


def _check(engine: Any, business: Any) -> EvidenceIdentity:
    resolver = getattr(engine, '_managed_master_identity', None)
    if not callable(resolver):
        raise MasterSafetyStop('managed master identity resolver is unavailable')
    home = Path(engine.ampuly_home).resolve()
    if home != business.path.parent.resolve():
        raise MasterSafetyStop('managed master evidence root mismatch')
    identity = resolver()
    if not isinstance(identity, EvidenceIdentity):
        raise MasterSafetyStop('managed master identity is invalid')
    identity = EvidenceIdentity(**asdict(identity))  # snapshot mutable resolver result
    if identity.provider != SUBJECT or not identity.complete:
        raise MasterSafetyStop('managed master identity is incomplete or belongs to another runtime')
    decision = decide_from_home(home, provider=SUBJECT, current_identity=identity)
    if not decision.admitted:
        # Reasons may contain evidence text; do not leak them into model output.
        raise MasterSafetyStop('managed master lacks current live composition evidence')
    return identity


async def require_managed_master_admission(engine: Any, business: Any) -> EvidenceIdentity:
    # Reading a report and fingerprinting files must not block the control loop.
    try:
        return await asyncio.to_thread(_check, engine, business)
    except MasterSafetyStop:
        raise
    except Exception as exc:
        raise MasterSafetyStop('managed master admission check failed') from exc
