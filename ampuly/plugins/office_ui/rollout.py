"""Whether a new version may keep going, decided by evidence.

An upgrade that has not fallen over is not an upgrade that works. The usual
way this goes wrong is time: ten minutes pass, nothing screams, somebody
promotes it everywhere, and the thing that was broken was the part nobody
exercised in those ten minutes.

So health here is a set of *named* checks, each of which passed, failed or is
unknown, and the verdict is the weakest of them. Two consequences, both
deliberate:

- **No checks means unknown, never healthy.** "Nothing failed" is what you get
  from looking at nothing.
- **Unknown is not a pass.** It is the state where somebody has to decide,
  which is exactly what an automatic promotion would have taken away.

A failure outranks an unknown, because a failed check is a decision that has
already been made.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

CHECK_RESULTS = ("passed", "failed", "unknown")
# Ordered worst first: the verdict is the first one present.
_VERDICT_ORDER = ("failed", "unknown", "passed")

ROLLOUT_STATES = ("planned", "in_progress", "succeeded", "rolled_back", "abandoned")
TARGET_STATES = ("pending", "deployed", "healthy", "unhealthy", "rolled_back")


class RolloutError(ValueError):
    """A rollout was asked to mean something it does not."""


@dataclass(frozen=True)
class HealthCheck:
    """One thing somebody actually looked at, and what they saw."""

    name: str
    result: str = "unknown"
    detail: str = ""

    def __post_init__(self) -> None:
        if not str(self.name or "").strip():
            raise RolloutError("a health check without a name says nothing")
        if self.result not in CHECK_RESULTS:
            raise RolloutError(f"unknown check result {self.result!r}")

    def to_payload(self) -> dict[str, Any]:
        return {"name": self.name, "result": self.result, "detail": self.detail}


def parse_checks(raw: Iterable[Any] | None) -> list[HealthCheck]:
    checks: list[HealthCheck] = []
    for item in list(raw or []):
        if isinstance(item, HealthCheck):
            checks.append(item)
            continue
        entry = dict(item or {})
        checks.append(HealthCheck(
            name=str(entry.get("name", "") or ""),
            result=str(entry.get("result", "unknown") or "unknown"),
            detail=str(entry.get("detail", "") or ""),
        ))
    names = [c.name for c in checks]
    if len(set(names)) != len(names):
        raise RolloutError("two checks with the same name: one of them is silently ignored")
    return checks


def verdict(checks: Iterable[HealthCheck]) -> str:
    """The weakest result present, and ``unknown`` when nothing was checked."""
    results = {check.result for check in checks}
    if not results:
        return "unknown"
    for candidate in _VERDICT_ORDER:
        if candidate in results:
            return candidate
    return "unknown"


def target_state_for(health: str) -> str:
    """What a deployed target becomes once its health is known.

    An unknown verdict leaves it ``deployed``: it is running and nobody has
    established whether that is good news.
    """
    return {"passed": "healthy", "failed": "unhealthy"}.get(health, "deployed")


def blocking_targets(targets: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Which targets stand between this rollout and being finished.

    Anything not ``healthy`` blocks, including targets nobody has looked at --
    promoting past those is how a rollout comes to mean less than it says.
    """
    return [t for t in targets if str(t.get("state", "") or "") != "healthy"]


def describe_block(target: dict[str, Any]) -> str:
    state = str(target.get("state", "") or "pending")
    where = str(target.get("customer_id", "") or "?")
    environment = str(target.get("environment_id", "") or "")
    if environment:
        where = f"{where}/{environment}"
    reason = {
        "pending": "has not been deployed yet",
        "deployed": "is running but nobody has established whether it is healthy",
        "unhealthy": "failed its health checks",
        "rolled_back": "was rolled back",
    }.get(state, f"is {state}")
    return f"{where} {reason}"
