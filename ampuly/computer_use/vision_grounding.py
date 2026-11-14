"""Vision grounding (4.3 X03, third slice): a redacted screenshot goes to a model, and a strict action comes back.

The only way a picture of the page reaches a model:

- the screenshot must be one the runtime produced with its masks applied
  (``redacted``), never a raw capture (§12.3, C27);
- the call runs inside the task's model budget scope; a budget stop is a
  refusal, not a retry (C23);
- the reply is untrusted text parsed by the strict UI-TARS grammar port
  (G06): an unknown function name is refused, ``finished`` is a request for
  verification, ``call_user`` is a hand-off;
- the result is a ``ComputerAction`` payload bound to the observation hash
  and the geometry, for the Gateway to admit or refuse (C05, C30). Nothing
  here executes.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ampuly.computer_use.contracts import ContractRefused, Geometry
from ampuly.computer_use.vision_actions import VisionAction, parse_prediction, to_computer_action

GROUNDING_SYSTEM_PROMPT = (
    "You are a GUI grounding model. You see one screenshot of a page and one instruction. "
    "Reply with exactly one action in the form Thought: ... Action: <function>(...), using only these functions: "
    "click(start_box='(x,y)'), left_double(start_box='(x,y)'), right_single(start_box='(x,y)'), "
    "drag(start_box='(x,y)', end_box='(x,y)'), type(content='...'), hotkey(key='...'), scroll(start_box='(x,y)', direction='down'), "
    "wait(), call_user(), finished(content='...'). Coordinates are in a 0-1000 space over the screenshot. "
    "Never invent a function. If the instruction cannot be done from what you see, reply call_user()."
)


class GroundingRefused(PermissionError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class GroundingResult:
    action: dict[str, Any] | None   # a strict ComputerAction payload, or None
    vision: VisionAction
    finished: bool = False
    hand_off: bool = False
    thought: str = ""
    model: str = ""


def _data_url(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


# Vendors whose models never see an image through this engine: litellm's DeepSeek transform flattens list content to
# text, so the screenshot is dropped before the request leaves and the model answers blind (model-routing phase 0).
_BLIND_VENDORS = ("deepseek/",)


def _vision_capability(provider: Any) -> tuple[str, bool, str]:
    """``(model, can_see, basis)`` for the model a grounding call would reach -- decided before anything is sent.

    The vendor rule comes first (what actually reaches the model through this engine), then the capability the
    provider reports. litellm's own map is not used: it is fetched from the network and differs between machines.
    """
    capabilities = getattr(provider, "get_capabilities", None)
    if not callable(capabilities):
        return "", False, "model_capability_unknown"
    try:
        found = capabilities(task_type="vision_grounding")
    except Exception:  # noqa: BLE001 - a capability nobody can state is not assumed
        return "", False, "model_capability_unknown"
    model = str(getattr(found, "model", "") or "")
    if model.lower().startswith(_BLIND_VENDORS):
        return model, False, "the image is dropped before it reaches this vendor's models"
    if bool(getattr(found, "supports_multimodal", False)):
        return model, True, "reported multimodal"
    return model, False, "not a model that reads images"


async def ground(provider: Any, *, screenshot: dict[str, Any], instruction: str, screen: tuple[int, int], geometry: Geometry,
                 task: Any, action_id: str, attempt_id: str, scope_digest: str, control_lease_id: str, observation_hash: str,
                 model_version: str = "1.0") -> GroundingResult:
    """One grounding call. Refusals come before the model is asked; the reply is parsed strictly after."""
    from ampuly.llm.budget import ModelBudgetStop, RouteRefused, model_budget_scope

    if not screenshot or not screenshot.get("redacted"):
        raise GroundingRefused("screenshot_not_redacted", "only a runtime screenshot with its masks applied may reach a model")
    path = Path(str(screenshot.get("path", "") or ""))
    if not path.is_file():
        raise GroundingRefused("screenshot_missing", path.name)
    if len(str(instruction or "").strip()) == 0:
        raise GroundingRefused("instruction_empty")
    # Before the image is read or anything is reserved: a model that cannot see would answer blind.
    model_name, can_see, basis = _vision_capability(provider)
    if not can_see:
        raise GroundingRefused(
            "model_capability_unknown" if basis == "model_capability_unknown" else "model_not_multimodal",
            f"{model_name or 'this model'} ({basis})"[:160],
        )
    messages = [
        {"role": "system", "content": GROUNDING_SYSTEM_PROMPT},
        {"role": "user", "content": [
            {"type": "text", "text": str(instruction)[:2000]},
            {"type": "image_url", "image_url": {"url": _data_url(path)}},
        ]},
    ]
    try:
        with model_budget_scope(task):
            reply = await provider.chat(messages, task_type="vision_grounding")
    except RouteRefused as exc:
        raise GroundingRefused("model_route_refused", str(getattr(exc, "code", "") or exc)[:120]) from None
    except ModelBudgetStop as exc:
        raise GroundingRefused("model_budget_exhausted", str(exc)[:120]) from None
    text = str((reply or {}).get("content", "") or "")
    model = str((reply or {}).get("model", "") or "")
    try:
        actions = parse_prediction(text, screen=screen, model_version=model_version)
    except ContractRefused as exc:
        raise GroundingRefused(exc.reason, str(exc)[:160]) from None
    if not actions:
        raise GroundingRefused("no_action_in_reply")
    vision = actions[0]
    if vision.is_finished:
        return GroundingResult(action=None, vision=vision, finished=True, thought=vision.thought, model=model)
    if vision.function == "call_user":
        return GroundingResult(action=None, vision=vision, hand_off=True, thought=vision.thought, model=model)
    try:
        payload = to_computer_action(vision, action_id=action_id, attempt_id=attempt_id, scope_digest=scope_digest,
                                     control_lease_id=control_lease_id, observation_hash=observation_hash, geometry=geometry)
    except ContractRefused as exc:
        raise GroundingRefused(exc.reason, str(exc)[:160]) from None
    return GroundingResult(action=payload, vision=vision, thought=vision.thought, model=model)
