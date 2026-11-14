"""Vision action grammar (4.3 X03): the UI-TARS action string, parsed strictly into our contract.

Ported from bytedance/UI-TARS-desktop, packages/ui-tars/action-parser/src/actionParser.ts
and packages/ui-tars/shared/src/constants/vlm.ts at commit
c2ad42e3eb9b27830db41a3e6f51ca7179d9b168 (Copyright (c) 2025 Bytedance, Inc. and its
affiliates; SPDX-License-Identifier: Apache-2.0). Re-implemented in Python; the
grammar, the coordinate normalisation (model space -> unit square -> screen
pixels, with the 1.5 "smart resize") and the accepted spellings are theirs.
Recorded in THIRD_PARTY_NOTICES.md.

What is ours, and stricter than the upstream (G06, C31, C30):

- an unknown function name is refused, never passed on as "some action";
- ``finished`` is not a success: it is a request for verification;
- ``call_user`` is a hand-off to a person;
- a coordinate action is only produced against a Geometry, and the resulting
  payload is the strict ``ComputerAction`` the Gateway parses -- so a changed
  window, display or DPI invalidates it before any executor sees it;
- typed text never enters the Gateway's target, only its length.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from ampuly.computer_use.contracts import ACTION_KINDS, ContractRefused, Geometry

# UI-TARS constants (shared/src/constants/vlm.ts)
IMAGE_FACTOR = 28
DEFAULT_FACTOR = 1000
MIN_PIXELS = 100 * IMAGE_FACTOR * IMAGE_FACTOR
MAX_PIXELS_V1_5 = 16384 * IMAGE_FACTOR * IMAGE_FACTOR
MAX_RATIO = 200

# The upstream function names we accept, and what each is in our contract.
VISION_FUNCTIONS: dict[str, str] = {
    "click": "select_candidate",
    "left_click": "select_candidate",
    "left_single": "select_candidate",
    "left_double": "select_candidate",
    "double_click": "select_candidate",
    "right_single": "select_candidate",
    "right_click": "select_candidate",
    "drag": "select_candidate",
    "type": "type_draft",
    "hotkey": "type_draft",
    "scroll": "scroll",
    "wait": "wait",
    "call_user": "hand_to_human",
    "finished": "verify",  # not an action: a verification trigger (C31)
}
_CLICK_VARIANTS = {"left_double": "double", "double_click": "double", "right_single": "right", "right_click": "right"}

# UI-TARS spells a chord as space-separated words (`ctrl c`); Playwright and the desktop helper want `Control+c`.
HOTKEY_NAMES = {
    "ctrl": "Control", "control": "Control", "alt": "Alt", "shift": "Shift", "cmd": "Meta", "command": "Meta", "win": "Meta",
    "meta": "Meta", "enter": "Enter", "return": "Enter", "esc": "Escape", "escape": "Escape", "tab": "Tab", "space": "Space",
    "backspace": "Backspace", "delete": "Delete", "del": "Delete", "home": "Home", "end": "End", "pageup": "PageUp",
    "pagedown": "PageDown", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
}
# C08 / 12.6: keys that move focus or the view commit nothing; keys that edit text commit nothing while the focus is a
# text field. Any other key -- Enter, Space, Control+Enter, Control+s, a function key -- can submit a form, send a
# message, press a focused button or fire a page shortcut, so it is a submit, which needs an approved Effect.
NAV_HOTKEYS = frozenset(key.lower() for key in (
    "Tab", "Shift+Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End", "Escape"))
# Clipboard chords are not edits: a paste puts whatever the owner last copied (a password, a key, a file) into the page,
# a copy or cut hands page content to the host clipboard. They are refused like any committing key.
EDIT_HOTKEYS = frozenset(key.lower() for key in (
    "Backspace", "Delete", "Control+a", "Control+z", "Control+y", "Control+Shift+z", "Meta+a", "Meta+z", "Meta+Shift+z"))


def playwright_hotkey(spec: str) -> str:
    """`ctrl c` / `ctrl+c` as Playwright's `Control+c`."""
    parts = [p for p in str(spec or "").replace("+", " ").split() if p]
    return "+".join(HOTKEY_NAMES.get(p.lower(), p if len(p) > 1 else p.lower()) for p in parts)


def keystroke_kind(function: str, inputs: dict[str, Any]) -> str:
    """The contract kind of a keystroke by sight: a draft, or a submit when the keys can commit."""
    if function == "hotkey":
        combo = playwright_hotkey(str(inputs.get("key", "") or "")).lower()
        return "type_draft" if combo in NAV_HOTKEYS or combo in EDIT_HOTKEYS else "submit"
    content = str(inputs.get("content", "") or "")
    # A typed line break is an Enter; UI-TARS also writes Enter as a literal backslash-n.
    return "submit" if ("\r" in content or "\n" in content or "\\n" in content) else "type_draft"


def keystroke_needs_text_focus(function: str, inputs: dict[str, Any]) -> bool:
    """Typing and editing keys commit nothing only while the focus is a text field (a Delete on a list deletes)."""
    if function == "type":
        return True
    return function == "hotkey" and playwright_hotkey(str(inputs.get("key", "") or "")).lower() in EDIT_HOTKEYS


@dataclass(frozen=True)
class VisionAction:
    """One parsed upstream action, coordinates already in screen pixels when a screen was given."""

    function: str
    kind: str
    inputs: dict[str, str] = field(default_factory=dict)
    thought: str = ""
    reflection: str = ""
    start: tuple[float, float] | None = None
    end: tuple[float, float] | None = None
    start_unit: tuple[float, float, float, float] | None = None
    end_unit: tuple[float, float, float, float] | None = None

    @property
    def is_finished(self) -> bool:
        return self.function == "finished"


# ── coordinate helpers (upstream) ──────────────────────────────────────────

def _round_by(num: float, factor: int) -> int:
    return int(round(num / factor)) * factor


def _floor_by(num: float, factor: int) -> int:
    return int(math.floor(num / factor)) * factor


def _ceil_by(num: float, factor: int) -> int:
    return int(math.ceil(num / factor)) * factor


def smart_resize_v15(height: int, width: int, *, max_ratio: int = MAX_RATIO, factor: int = IMAGE_FACTOR,
                     min_pixels: int = MIN_PIXELS, max_pixels: int = MAX_PIXELS_V1_5) -> tuple[int, int] | None:
    """(width, height) the 1.5 model saw for this screen, or None when the ratio is absurd."""
    if max(height, width) / min(height, width) > max_ratio:
        return None
    w_bar = max(factor, _round_by(width, factor))
    h_bar = max(factor, _round_by(height, factor))
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = _floor_by(height / beta, factor)
        w_bar = _floor_by(width / beta, factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = _ceil_by(height * beta, factor)
        w_bar = _ceil_by(width * beta, factor)
    return w_bar, h_bar


# ── the grammar (upstream) ─────────────────────────────────────────────────

_FUNCTION = re.compile(r"^(\w+)\((.*)\)$", re.S)
_ARG_PAIRS = re.compile(r"(?:[^,']|'[^']*')+")


def _parse_call(action_str: str) -> tuple[str, dict[str, str]] | None:
    text = re.sub(r"<\|box_start\|>|<\|box_end\|>", "", action_str)
    text = re.sub(r"(?<!start_)(?<!end_)point=", "start_box=", text)
    text = text.replace("start_point=", "start_box=").replace("end_point=", "end_box=")
    match = _FUNCTION.match(text.strip())
    if not match:
        return None
    name, args = match.group(1), match.group(2)
    kwargs: dict[str, str] = {}
    if args.strip():
        for pair in _ARG_PAIRS.findall(args):
            key, sep, value = pair.partition("=")
            if not sep or not key.strip():
                continue
            value = value.strip()
            value = re.sub(r"^['\"]|['\"]$", "", value)
            if "<bbox>" in value:
                value = "(" + re.sub(r"\s+", ",", value.replace("<bbox>", "").replace("</bbox>", "")) + ")"
            if "<point>" in value:
                value = "(" + re.sub(r"\s+", ",", value.replace("<point>", "").replace("</point>", "")) + ")"
            kwargs[key.strip()] = value
    return name, kwargs


def _split_thought(text: str, mode: str) -> tuple[str, str, str]:
    """(thought, reflection, action text) in the upstream 'bc' or 'o1' layouts."""
    thought, reflection = "", ""
    text = text.strip()
    if mode == "o1":
        t = re.search(r"<Thought>\s*(.*?)\s*</Thought>", text, re.S)
        s = re.search(r"\nAction_Summary:\s*(.*?)\s*Action:", text, re.S)
        a = re.search(r"\nAction:\s*(.*?)\s*</Output>", text, re.S)
        thought = f"{t.group(1) if t else ''}\n<Action_Summary>\n{s.group(1) if s else ''}"
        return thought, reflection, (a.group(1) if a else "")
    if "Thought:" in text:
        m = re.search(r"Thought: ([\s\S]+?)(?=\s*Action[:：]|$)", text)
        thought = m.group(1).strip() if m else ""
    elif text.startswith("Reflection:"):
        m = re.search(r"Reflection: ([\s\S]+?)Action_Summary: ([\s\S]+?)(?=\s*Action[:：]|$)", text)
        if m:
            reflection, thought = m.group(1).strip(), m.group(2).strip()
    elif text.startswith("Action_Summary:"):
        m = re.search(r"Action_Summary: (.+?)(?=\s*Action[:：]|$)", text, re.S)
        thought = m.group(1).strip() if m else ""
    if "Action:" in text or "Action：" in text:
        action_text = re.split(r"Action[:：]", text)[-1]
    else:
        action_text = text
    return thought, reflection, action_text


def _box_to_unit(raw: str, factors: tuple[float, float]) -> tuple[float, float, float, float]:
    numbers = [n for n in re.sub(r"[()\[\]]", "", raw).split(",") if n != ""]
    values = []
    for index, num in enumerate(numbers):
        try:
            values.append(float(num) / factors[index % 2])
        except ValueError:
            raise ContractRefused("vision_box_not_numeric", raw[:40]) from None
    if len(values) == 2:
        values += values[:2]
    if len(values) != 4:
        raise ContractRefused("vision_box_shape", raw[:40])
    return values[0], values[1], values[2], values[3]


def _unit_to_screen(box: tuple[float, float, float, float], screen: tuple[int, int], factors: tuple[float, float],
                    scale: float) -> tuple[float, float]:
    x1, y1, x2, y2 = box
    wf, hf = factors
    x = round(((x1 + x2) / 2) * screen[0] * wf) / wf * scale
    y = round(((y1 + y2) / 2) * screen[1] * hf) / hf * scale
    return x, y


def parse_prediction(text: str, *, factors: tuple[float, float] | float = (DEFAULT_FACTOR, DEFAULT_FACTOR),
                     screen: tuple[int, int] | None = None, scale_factor: float = 1.0, mode: str = "bc",
                     model_version: str = "1.0") -> list[VisionAction]:
    """Every action in a model reply, in order. Unknown functions are refused, not skipped."""
    if not isinstance(factors, tuple):
        factors = (float(factors), float(factors))
    resize = smart_resize_v15(screen[1], screen[0]) if (model_version == "1.5" and screen) else None
    box_factors = (float(resize[0]), float(resize[1])) if resize else factors
    thought, reflection, action_text = _split_thought(str(text or ""), mode)
    actions: list[VisionAction] = []
    for raw in action_text.split("\n\n"):
        raw = raw.replace("\n", "\\n").lstrip()
        if not raw.strip():
            continue
        call = _parse_call(raw)
        if call is None:
            raise ContractRefused("vision_not_a_function_call", raw[:60])
        name, args = call
        if name not in VISION_FUNCTIONS:
            raise ContractRefused("unknown_action_kind", name[:40])
        inputs: dict[str, str] = {}
        start = end = None
        start_unit = end_unit = None
        for key, value in args.items():
            if not value:
                continue
            if "start_box" in key or "end_box" in key:
                box = _box_to_unit(value.strip(), box_factors)
                if "start_box" in key:
                    start_unit = box
                    if screen:
                        start = _unit_to_screen(box, screen, factors, scale_factor)
                else:
                    end_unit = box
                    if screen:
                        end = _unit_to_screen(box, screen, factors, scale_factor)
            else:
                inputs[key] = value.strip()
        actions.append(VisionAction(function=name, kind=VISION_FUNCTIONS[name], inputs=inputs, thought=thought,
                                    reflection=reflection, start=start, end=end, start_unit=start_unit, end_unit=end_unit))
    return actions


# ── into the contract (ours) ───────────────────────────────────────────────

def to_computer_action(action: VisionAction, *, action_id: str, attempt_id: str, scope_digest: str,
                       control_lease_id: str, observation_hash: str, geometry: Geometry | None) -> dict[str, Any]:
    """The strict payload the Gateway parses. A finished/verify request is not an action."""
    if action.is_finished:
        raise ContractRefused("finished_is_not_an_action", "finished only triggers verification (C31)")
    kind = action.kind
    if kind not in ACTION_KINDS:
        raise ContractRefused("unknown_action_kind", kind)
    target: dict[str, Any] = {}
    needs_geometry = False
    if action.function in VISION_FUNCTIONS and kind in {"select_candidate", "scroll"}:
        if action.start is None:
            raise ContractRefused("vision_action_without_screen_coordinates", action.function)
        target = {"x": action.start[0], "y": action.start[1]}
        needs_geometry = True
        variant = _CLICK_VARIANTS.get(action.function)
        if variant:
            target["click"] = variant
        if action.function == "drag":
            if action.end is None:
                raise ContractRefused("drag_without_end", "")
            target["to"] = {"x": action.end[0], "y": action.end[1]}
        if kind == "scroll":
            target["direction"] = str(action.inputs.get("direction", "down") or "down").lower()
    elif kind == "type_draft":
        kind = keystroke_kind(action.function, dict(action.inputs))
        if action.function == "hotkey":
            target = {"hotkey": str(action.inputs.get("key", "") or "")[:64]}
        else:
            target = {"length": len(str(action.inputs.get("content", "") or ""))}  # the text stays out (C27)
            if kind == "submit":
                target["enter"] = True
    elif kind == "wait":
        target = {"seconds": 1}
    elif kind == "hand_to_human":
        target = {"reason": "the model asked for the user"}
    if needs_geometry and geometry is None:
        raise ContractRefused("coordinate_without_geometry", action.function)
    payload: dict[str, Any] = {
        "action_id": action_id, "attempt_id": attempt_id, "kind": kind, "scope_digest": scope_digest,
        "control_lease_id": control_lease_id, "observation_hash": observation_hash, "target": target,
        "expected_effect": f"{action.function} via vision grounding", "risk": ACTION_KINDS[kind].value,
        "timeout_sec": 30, "idempotent": kind in {"wait", "scroll"}, "retryable": False,
    }
    if geometry is not None:
        # Keystrokes go to a window too: any helper-bound action carries the
        # geometry it was decided on, and the Gateway refuses it if that moved.
        payload["geometry"] = geometry
    return payload
