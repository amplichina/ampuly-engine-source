"""Reading one JSON object a role prompt answered with -- shared by company mode and the gate judge.

Native Runtime V2 may append exactly one ``Verification: ...`` paragraph to otherwise bare or fenced JSON. That known
runtime annotation is accepted; narration, several JSON values, unterminated fences or other trailing text are not.
The gate judge used a plain ``json.loads`` that failed on the footer, so every native verdict carrying it was read as
"non-JSON" and passed unjudged (found by the model-routing phase 0 review).
"""
from __future__ import annotations

import json
import re
from typing import Any

_FOOTER = re.compile(r"\r?\n\r?\nVerification: [^\r\n]+\s*")


def parse_role_prompt_json_value(raw: Any) -> tuple[bool, Any]:
    """``(True, value)`` for one unambiguous JSON value (any JSON type), ``(False, None)`` otherwise.

    ``raw_decode`` is used before the remainder is inspected, so a string containing the word ``Verification`` inside
    the JSON cannot be mistaken for the footer.
    """
    text = str(raw or "").strip()
    if not text:
        return False, None
    if text.startswith("```"):
        opening = re.match(r"^```(?:json)?[ \t]*\r?\n", text, re.IGNORECASE)
        if opening is None:
            return False, None
        closing = re.search(r"(?m)^```[ \t]*\r?$", text[opening.end():])
        if closing is None:
            return False, None
        candidate = text[opening.end(): opening.end() + closing.start()]
        remainder = text[opening.end() + closing.end():]
    else:
        candidate = text
        remainder = ""
    try:
        leading = len(candidate) - len(candidate.lstrip())
        data, end = json.JSONDecoder().raw_decode(candidate, leading)
    except json.JSONDecodeError:
        return False, None
    after = candidate[end:]
    if text.startswith("```"):
        # The fence holds JSON only; the runtime's status annotation, when present, follows the closing fence.
        if after.strip():
            return False, None
        if remainder and not _FOOTER.fullmatch(remainder):
            return False, None
    elif after and not _FOOTER.fullmatch(after):
        return False, None
    return True, data


def parse_role_prompt_json(raw: Any) -> dict[str, Any] | None:
    """One JSON object, or ``None``."""
    ok, data = parse_role_prompt_json_value(raw)
    return dict(data) if ok and isinstance(data, dict) else None
