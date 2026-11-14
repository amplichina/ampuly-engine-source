"""Comparison-only transcript helpers shared by persistence and UI snapshots.

Never write these lossy comparison keys back as delivered content.
"""
from __future__ import annotations

import re


def strip_narrative_title_prefix(content: str) -> str:
    """Unwrap explicit long narrative titles; preserve ordinary Markdown colons."""
    trimmed = str(content or "").strip()
    # Resolve nested wrappers to a fixed point so repeated syncs are idempotent.
    while True:
        markdown_title = re.match(r"^\*\*(.{8,160}?)\*\*:\s+([\s\S]+)$", trimmed)
        if not markdown_title:
            return trimmed
        body = markdown_title.group(2).strip()
        if len(body) < 80 or body == trimmed:
            return trimmed
        trimmed = body
