"""Prompt discipline for image generation, enforced by the tool rather than the model.

The prompt guard applies deterministic text normalization before generation:

* forbidden words are stripped from every prompt and ``no text`` is
  appended;
* a baseline negative prompt is always merged in;
* a **style template** (``<ampuly_home>/skills/<style>/template.yaml``) can be
  filled deterministically from named slots, so the designer supplies the
  imagery and colours and never hand-writes the composition rules;
* every generated image gets a sidecar ``<file>.prompt.json`` recording the
  exact prompt, negative prompt, provider, size, seed and template, so a
  picture can be reproduced or compared later.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# Words that turn a mark into a poster with fake lettering. Latin words are
# matched on word boundaries (so "logotype" and "wordmark" go too, but
# "typography" is listed explicitly); CJK terms are plain substrings.
FORBIDDEN_LATIN = (
    "logo", "logotype", "logos", "wordmark", "lettermark", "lettering", "typography",
    "typeface", "font", "fonts", "text", "texts", "caption", "slogan", "tagline",
    "brand name", "brandname", "trademark", "signage",
)
FORBIDDEN_CJK = (
    "标志", "商标", "品牌名", "品牌", "字样", "字体", "文字", "字母", "标语", "口号", "招牌", "题字", "书法字",
)
DEFAULT_NEGATIVE = ("文字", "字母", "数字", "汉字", "拼音", "水印", "签名")
NO_TEXT_SUFFIX = "no text"

_LATIN_RE = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(re.escape(w) for w in sorted(FORBIDDEN_LATIN, key=len, reverse=True)) + r")(?![A-Za-z])",
    re.IGNORECASE,
)
_SLOT_RE = re.compile(r"\{([^{}]+)\}")


def sanitize_prompt(prompt: str) -> tuple[str, list[str]]:
    """Strip forbidden words, tidy separators, and end with ``no text``."""
    text = str(prompt or "")
    removed: list[str] = []
    # The one phrase that legitimately contains a forbidden word is the
    # suffix itself; it is re-added once at the end, so take it out first.
    text = re.sub(r"(?i)(?<![A-Za-z])no\s+text(?![A-Za-z])", " ", text)

    def _drop_latin(match: re.Match[str]) -> str:
        removed.append(match.group(0))
        return " "

    text = _LATIN_RE.sub(_drop_latin, text)
    for term in sorted(FORBIDDEN_CJK, key=len, reverse=True):
        if term in text:
            removed.append(term)
            text = text.replace(term, " ")
    # Tidy: collapse blank items between commas, repeated spaces, dangling commas.
    parts = [p.strip(" \t\r\n,;，；") for p in re.split(r"[,，]", text)]
    parts = [re.sub(r"\s{2,}", " ", p) for p in parts if p and re.sub(r"\s", "", p)]
    if not any(p.lower() == NO_TEXT_SUFFIX for p in parts):
        parts.append(NO_TEXT_SUFFIX)
    return ", ".join(parts), removed


def merge_negative(negative_prompt: str) -> str:
    """Union of the caller's negative list and the baseline, order kept."""
    items: list[str] = []
    seen: set[str] = set()
    for raw in list(re.split(r"[,，;；\n]", str(negative_prompt or ""))) + list(DEFAULT_NEGATIVE):
        item = raw.strip()
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            items.append(item)
    return ", ".join(items)


def style_template_path(ampuly_home: Path | str, style: str) -> Path:
    return Path(ampuly_home) / "skills" / str(style).strip() / "template.yaml"


def load_style_template(ampuly_home: Path | str, style: str) -> dict[str, Any]:
    path = style_template_path(ampuly_home, style)
    if not path.is_file():
        raise FileNotFoundError(f"style template not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or not str(data.get("prompt", "") or "").strip():
        raise ValueError(f"style template {path} has no prompt")
    return data


def render_style_prompt(template: dict[str, Any], slots: dict[str, Any] | None) -> tuple[str, str, dict[str, str]]:
    """Fill ``{slot}`` placeholders deterministically.

    Slot values come from the caller, then from the template's ``slots``
    defaults (colours have their own ``colors`` block), and a placeholder
    with no value at all is dropped rather than left in the prompt. Returns
    the prompt, the negative prompt and the values actually used.
    """
    provided = {str(k).strip(): str(v).strip() for k, v in dict(slots or {}).items() if str(v or "").strip()}
    defaults = {str(k).strip(): str(v).strip() for k, v in dict(template.get("slots", {}) or {}).items()}
    colors = {str(k).strip(): str(v).strip() for k, v in dict(template.get("colors", {}) or {}).items()}
    color_aliases = {"主色": "main", "辅色": "accent", "底色": "background", "背景色": "background"}
    used: dict[str, str] = {}

    def _value(name: str) -> str:
        if name in provided:
            return provided[name]
        if name in color_aliases and color_aliases[name] in colors:
            return colors[color_aliases[name]]
        if name in colors:
            return colors[name]
        return defaults.get(name, "")

    def _fill(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        value = _value(name)
        if value:
            used[name] = value
        return value

    prompt = _SLOT_RE.sub(_fill, str(template.get("prompt", "") or ""))
    negative = _SLOT_RE.sub(_fill, str(template.get("negative", "") or ""))
    return prompt, negative, used


def sidecar_path(image_path: Path | str) -> Path:
    path = Path(image_path)
    return path.with_name(path.name + ".prompt.json")


def record_generation(image_path: Path | str, payload: dict[str, Any]) -> Path:
    path = sidecar_path(image_path)
    body = {"recorded_at": datetime.now().isoformat(), **payload}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
