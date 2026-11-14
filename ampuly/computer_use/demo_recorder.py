"""A demonstration is recorded from the Gateway's own sequence, redacted as it is taken (4.3 X07, second slice).

The owner starts a recording on one bound session; from then on every action
the Gateway admitted whose input was accepted (succeeded, or not yet verified;
never cancelled, or contradicted by the page afterwards) becomes one DemoStep: the
observation it was decided on, its contract kind, a target reduced to refs,
selectors, urls and lengths (never typed text), and the observation the page
showed afterwards. A refused or failed action is not a step. Stopping seals
the demo (C19) and writes it to the product's own directory; the compiler
takes it from there. Nothing here replays anything.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ampuly.computer_use.contracts import ContractRefused
from ampuly.computer_use.workflow_contracts import DemoStep, SealedDemo, WorkflowRefused
from ampuly.core.config import replace_with_retry

# What a recorded target may keep. Text, values and anything else stay out.
TARGET_KEYS = frozenset({"ref", "selector", "url", "length", "direction", "amount", "index", "state", "click", "hotkey", "seconds"})


def redact_target(target: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(target or {}).items():
        if key not in TARGET_KEYS:
            continue
        out[key] = int(value) if isinstance(value, bool) is False and isinstance(value, (int, float)) and key in {"length", "amount", "index", "seconds"} else str(value)[:256]
    return out


@dataclass
class _Recording:
    demo_id: str
    scope_digest: str
    recorded_by: str
    label: str = ""
    steps: list[DemoStep] = field(default_factory=list)


@dataclass
class DemoRecorder:
    _active: dict[str, _Recording] = field(default_factory=dict)

    def start(self, binding: Any, *, demo_id: str, recorded_by: str, scope_digest: str, label: str = "") -> dict[str, Any]:
        key = binding.key()
        if key in self._active:
            raise WorkflowRefused("already_recording", self._active[key].demo_id)
        self._active[key] = _Recording(demo_id=demo_id, scope_digest=scope_digest, recorded_by=recorded_by, label=str(label or "")[:200])
        return self.status(binding)

    def recording(self, binding: Any) -> bool:
        return binding.key() in self._active

    def status(self, binding: Any) -> dict[str, Any]:
        rec = self._active.get(binding.key())
        if rec is None:
            return {"recording": False, "demo_id": "", "steps": 0}
        return {"recording": True, "demo_id": rec.demo_id, "steps": len(rec.steps), "label": rec.label}

    def note(self, binding: Any, *, action: Any, after_hash: str, before_hash: str = "", intent: str = "") -> DemoStep | None:
        """One accepted action becomes one step. Called by the bridge after its receipt is recorded.
        `before_hash` is the page's structure digest the action was decided on; it is what a replay
        can compare against in another session. The Gateway observation hash is the fallback."""
        rec = self._active.get(binding.key())
        if rec is None:
            return None
        try:
            step = DemoStep(sequence=len(rec.steps) + 1, observation_hash=str(before_hash or getattr(action, "observation_hash", "") or ""),
                            kind=str(getattr(action, "kind", "") or ""), target=redact_target(getattr(action, "target", None)),
                            expected_after_hash=str(after_hash or ""), intent=str(intent or "")[:200])
        except ContractRefused:
            return None  # a step the contract refuses is not recorded; the action itself already happened
        rec.steps.append(step)
        return step

    def stop(self, binding: Any) -> SealedDemo:
        rec = self._active.pop(binding.key(), None)
        if rec is None:
            raise WorkflowRefused("not_recording", binding.key())
        return SealedDemo.seal(demo_id=rec.demo_id, scope_digest=rec.scope_digest, steps=rec.steps, recorded_by=rec.recorded_by)

    def discard(self, binding: Any) -> bool:
        return self._active.pop(binding.key(), None) is not None


def demo_to_payload(demo: SealedDemo) -> dict[str, Any]:
    return {"demo_id": demo.demo_id, "scope_digest": demo.scope_digest, "recorded_by": demo.recorded_by, "seal": demo.seal,
            "steps": [dict(step.__dict__) for step in demo.steps]}


def demo_from_payload(payload: dict[str, Any]) -> SealedDemo:
    steps = tuple(DemoStep(**{k: v for k, v in dict(s).items() if k in DemoStep.__dataclass_fields__})
                  for s in list(payload.get("steps", []) or []))
    return SealedDemo(demo_id=str(payload.get("demo_id", "") or ""), scope_digest=str(payload.get("scope_digest", "") or ""),
                      steps=steps, recorded_by=str(payload.get("recorded_by", "") or ""), seal=str(payload.get("seal", "") or ""))


def demos_dir() -> Path:
    from ampuly.core.config import get_ampuly_home

    return get_ampuly_home() / "computer_use" / "demos"


def save_demo(demo: SealedDemo, directory: Path | None = None) -> Path:
    """The sealed demo as a file the compiler reads later. The seal travels with it and is checked on load."""
    if not demo.sealed:
        raise WorkflowRefused("demo_not_sealed_or_tampered", demo.demo_id)
    target = Path(directory) if directory is not None else demos_dir()
    target.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in demo.demo_id)[:120] or "demo"
    path = target / f"{safe}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(demo_to_payload(demo), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    replace_with_retry(tmp, path)
    return path


def load_demo(path: Path | str) -> SealedDemo:
    demo = demo_from_payload(json.loads(Path(path).read_text(encoding="utf-8")))
    if not demo.sealed:
        raise WorkflowRefused("demo_not_sealed_or_tampered", str(path))
    return demo
