"""浏览器后端契约与批量执行（对齐 ego lite 的“一次提交多步”）。

后端只需实现 snapshot / act / capture / close；批量执行器负责：
- 动作白名单：任一步不在白名单，整批在执行前拒绝；
- 顺序执行、失败即停（默认），保留已完成回执；
- 上限与逐步结果，便于审计与回放。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

ALLOWED_KINDS = ("navigate", "click", "fill", "press", "wait", "scroll", "select", "capture")


class BatchRefused(ValueError):
    pass


@dataclass(frozen=True)
class Action:
    kind: str
    target: str = ""
    value: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionResult:
    action: Action
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class BatchResult:
    results: list[ActionResult]
    stopped_at: int | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.stopped_at is None and not self.error and all(r.ok for r in self.results)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "stopped_at": self.stopped_at, "error": self.error,
                "results": [{"kind": r.action.kind, "target": r.action.target,
                             "ok": r.ok, "error": r.error} for r in self.results]}


class BrowserBackend(Protocol):
    def snapshot(self) -> dict[str, Any]: ...
    def act(self, action: Action) -> dict[str, Any]: ...
    def capture(self, path: str) -> str: ...
    def close(self) -> None: ...


def validate_actions(actions: Sequence[Action], *, allowed: set[str] | None = None,
                     max_actions: int = 20) -> None:
    if not actions:
        raise BatchRefused("empty batch")
    if len(actions) > max_actions:
        raise BatchRefused(f"batch exceeds {max_actions} actions")
    permit = set(ALLOWED_KINDS) if allowed is None else set(allowed)
    for index, action in enumerate(actions):
        if not isinstance(action, Action):
            raise BatchRefused(f"action {index} is not an Action")
        if action.kind not in permit:
            raise BatchRefused(f"action {index} kind not allowed: {action.kind}")
        if action.kind in {"click", "fill", "select", "navigate"} and not action.target:
            raise BatchRefused(f"action {index} ({action.kind}) needs a target")


def run_batch(backend: BrowserBackend, actions: Sequence[Action], *,
              allowed: set[str] | None = None, max_actions: int = 20,
              stop_on_error: bool = True) -> BatchResult:
    """整批先校验，再逐步执行；任何拒绝都发生在第一个动作之前。"""
    validate_actions(actions, allowed=allowed, max_actions=max_actions)
    results: list[ActionResult] = []
    for index, action in enumerate(actions):
        try:
            detail = backend.act(action) or {}
            ok = bool(detail.get("ok", True))
            error = "" if ok else str(detail.get("error", "action_failed"))
        except Exception as exc:  # noqa: BLE001 —— 单步异常不应炸掉整批
            ok, error, detail = False, f"{type(exc).__name__}: {exc}", {}
        results.append(ActionResult(action, ok, detail, error))
        if not ok and stop_on_error:
            return BatchResult(results, stopped_at=index, error=error)
    return BatchResult(results)
