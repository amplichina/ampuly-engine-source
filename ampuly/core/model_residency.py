"""出境: whether a model call, or an external CLI agent, may be used under the owner's residency setting.

The implementation lives here. ``ampuly.plugins.office_ui.model_residency`` re-exports it.

Pure. It is handed a catalogue entry and a setting and answers with a refusal code or ``""`` -- it reads no
config, no environment and no disk, so an order can be judged a month later by the setting it was launched under
rather than by today's.

Two halves of one rule, because 「只用境内」 and 「境内且不用于训练」 are different questions:

* where the service is (``hosting``), which the catalogue already records; and
* what the vendor does with what is sent (``trains_on_inputs``), which the catalogue also records -- but for the
  two vendors whose answer is 「opt out」 or 「anonymised」, the fact that somebody actually opted out is not
  something this product can know. So it is recorded by the owner, per vendor, and an unrecorded vendor is
  refused rather than assumed. Without that half, 「境内且不用于训练」 would change nothing at all today.
"""
from __future__ import annotations

from typing import Any

RESIDENCY: tuple[str, ...] = ("cn_only", "cn_no_training", "allow_overseas")

# What this module answers with. Never a fourth code: the console has a sentence for each of these three.
REFUSALS: tuple[str, ...] = ("residency_overseas", "residency_trains", "residency_optout_unrecorded")

AGENT_REFUSAL = "agent_residency_unmarked"


def normalize(value: Any) -> str:
    """One of :data:`RESIDENCY`. Anything unrecognised -- including ``""`` -- is the strictest, on purpose."""
    name = str(value or "").strip().lower()
    return name if name in RESIDENCY else "cn_only"


def model_refusal(entry: dict[str, Any] | None, residency: str,
                  optout: dict[str, str] | None = None) -> str:
    """``""`` when this catalogue entry may be called under this setting, else why not.

    An entry this module does not recognise answers ``""``: the catalogue's own ``model_unknown`` already covers
    a model nobody has heard of, and inventing a second kind of unknown here would mean two places to look.
    """
    item = dict(entry or {})
    if not item:
        return ""
    setting = normalize(residency)
    if setting == "allow_overseas":
        return ""
    if str(item.get("hosting", "") or "") != "cn_mainland":
        return "residency_overseas"
    if setting != "cn_no_training":
        return ""
    trains = str(item.get("trains_on_inputs", "") or "")
    if trains == "yes":
        return "residency_trains"
    if trains == "no":
        return ""
    # 「opt out」 or 「anonymised」: possible, but only once somebody has actually done it. The owner records who
    # and when, per vendor; until then this product does not assert on their behalf that it was done.
    vendor = str(item.get("id", "") or "").split("/", 1)[0]
    recorded = str(dict(optout or {}).get(vendor, "") or "").strip()
    return "" if recorded else "residency_optout_unrecorded"


def agent_refusal(reaches: str, residency: str) -> str:
    """``""`` when an external CLI agent that reaches ``reaches`` may be offered under this setting.

    An unmarked agent is not offered under anything but 允许出境. The product cannot see what a CLI on the
    owner's machine connects to, and guessing 「it is probably mainland」 is exactly the judgement it is not
    allowed to make for them.
    """
    if normalize(residency) == "allow_overseas":
        return ""
    return "" if str(reaches or "").strip() == "cn_mainland" else AGENT_REFUSAL
