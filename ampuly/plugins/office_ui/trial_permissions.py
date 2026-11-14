"""The permission policy of an isolated trial.

A trial runs the native runtime with nobody at the console to ask. Until now that was safe for one reason only: the
trial's tool set held nothing that reaches outside (tests/test_policyless_runtimes_cannot_reach.py wrote that reason
down as a check). A paid image generator reaches the network, so that reason no longer covers every trial -- and the
answer is a policy, not an exception to the rule:

- a tool that reaches nothing (local read, workspace write, runtime-internal) runs, as before;
- a tool that reaches outside runs only when the caller that built this trial admitted it by name, and only with
  arguments that stay inside the attempt's workspace;
- everything else is denied. There is no "ask": a question with nobody to answer it is dropped by the runtime, and a
  dropped question is an allow.

Spending is not decided here. The allowance is: an admitted paid tool still goes through ``spend_guard`` and stops
the turn when the trial's ceiling is reached.
"""
from __future__ import annotations

from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from ampuly.core.models import PermissionResolution, PermissionScope, RiskLevel, RuntimePermissionDecision

# The same set the guard test uses: a call with one of these leaves the product.
REACHING_EFFECTS = frozenset({"process_execute", "network_access", "external_side_effect", "unknown"})

ArgumentCheck = Callable[[dict[str, Any], Any], str]


def _decision(resolution: PermissionResolution, rationale: str, risk: RiskLevel) -> RuntimePermissionDecision:
    return RuntimePermissionDecision(
        resolution=resolution, scope=PermissionScope.ONCE, risk_level=risk, rationale=rationale, source="trial_policy",
    )


def workspace_of(task: Any) -> Path | None:
    metadata = dict(getattr(task, "metadata", {}) or {})
    raw = str(metadata.get("output_root", "") or metadata.get("workspace_root", "") or "").strip()
    return Path(raw) if raw else None


def path_stays_inside(raw: Any, workspace: Path | None) -> str:
    """'' when ``raw`` names a place inside the workspace; otherwise why not. Lexical first, then resolved: a name
    that only becomes an escape through a link is an escape too."""
    text = str(raw or "").strip()
    if not text:
        return "an empty path"
    parts = text.replace("\\", "/").split("/")
    if Path(text).is_absolute() or PureWindowsPath(text).drive or text.startswith(("/", "\\")):
        return f"{text!r} is an absolute path; a trial writes inside its own workspace only"
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        return f"{text!r} is not a plain workspace-relative path"
    if workspace is None:
        return "the trial's workspace is not known, so no path can be checked against it"
    try:
        if not (workspace / text).resolve().is_relative_to(workspace.resolve()):
            return f"{text!r} leads outside the trial's workspace"
    except (OSError, RuntimeError, ValueError):
        return f"{text!r} could not be resolved inside the trial's workspace"
    return ""


def image_arguments_stay_inside(arguments: dict[str, Any], task: Any) -> str:
    """The image generator resolves an absolute ``file_path`` as it is given. In production the permission engine and
    the workspace fence stand in front of it; in a trial this check does."""
    workspace = workspace_of(task)
    refusal = path_stays_inside(arguments.get("file_path"), workspace)
    if refusal:
        return f"file_path: {refusal}"
    references = arguments.get("reference_images")
    if references is not None:
        if not isinstance(references, list):
            return "reference_images must be a list of workspace-relative paths"
        for item in references:
            refusal = path_stays_inside(item, workspace)
            if refusal:
                return f"reference_images: {refusal}"
    return ""


def image_call_check(allowed_arguments: Any, time_left: Callable[[], float], worst_case_sec: Callable[[dict[str, Any]], float]) -> ArgumentCheck:
    """The whole check of one image call in a trial: only the arguments the tool declares (the registry renames
    ``filename``/``filepath``/``file`` to ``file_path`` *after* this decision, so an undeclared name is a way round the
    path check), paths inside the workspace, and enough time left to finish. A paid call is never started when the
    trial's deadline could cancel it half way: the image would be billed and never saved."""
    allowed = frozenset(str(name) for name in allowed_arguments)

    def check(arguments: dict[str, Any], task: Any) -> str:
        extra = sorted(str(name) for name in arguments if str(name) not in allowed)
        if extra:
            return f"arguments this tool does not declare: {', '.join(extra)}"
        refusal = image_arguments_stay_inside(arguments, task)
        if refusal:
            return refusal
        needed, left = float(worst_case_sec(arguments)), float(time_left())
        if left < needed:
            return (f"one image call can take {int(needed)} s and this trial has {max(0, int(left))} s left; "
                    "nothing was sent and nothing was billed -- report this instead of retrying")
        return ""

    return check


class TrialPermissionPolicy:
    """What a trial's runtime may call. ``admitted`` names the reaching tools the caller took responsibility for, each
    with an optional check of its arguments (returning why not, or '')."""

    def __init__(self, admitted: dict[str, ArgumentCheck | None] | None = None) -> None:
        self.admitted: dict[str, ArgumentCheck | None] = dict(admitted or {})

    def predict(self, tool: Any, arguments: dict[str, Any] | None = None, *, task: Any = None) -> RuntimePermissionDecision:
        if tool is None:
            return _decision(PermissionResolution.DENY, "An unknown tool is not part of this trial.", RiskLevel.HIGH)
        name = str(getattr(tool, "name", "") or "")
        effects = set(getattr(tool, "permission_effects", None) or ("unknown",))
        reaching = bool(effects & REACHING_EFFECTS)
        if bool(getattr(tool, "requires_confirmation", False)) and name not in self.admitted:
            return _decision(PermissionResolution.DENY, f"{name} needs a person's confirmation, and a trial has nobody to give it.", RiskLevel.MEDIUM)
        if not reaching:
            return _decision(PermissionResolution.ALLOW, "Reaches nothing outside the trial's workspace.", RiskLevel.LOW)
        if name not in self.admitted:
            return _decision(PermissionResolution.DENY, f"{name} reaches outside the trial and was not admitted to it.", RiskLevel.HIGH)
        check = self.admitted[name]
        refusal = check(dict(arguments or {}), task) if callable(check) else ""
        if refusal:
            return _decision(PermissionResolution.DENY, f"{name}: {refusal}", RiskLevel.HIGH)
        return _decision(PermissionResolution.ALLOW, f"{name} was admitted to this trial by the person who started it.", RiskLevel.MEDIUM)
