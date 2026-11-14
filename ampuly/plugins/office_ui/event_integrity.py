"""Which records can be proven unaltered, and which are only records.

Two of this system's event streams carry a hash chain: each entry commits to
the one before it, so a row edited or removed after the fact shows up. Most of
the streams do not. They are append-only tables written by our own code, which
makes them evidence of what happened and not proof that nothing else did.

Both are useful and they are not the same thing. The failure this file exists
to prevent is a timeline that presents them as one: somebody reads a merged
history, sees "verified" somewhere on the page, and carries that word over to
the parts nobody can verify. In a dispute with a customer about what was
approved, that difference is the whole argument.

So each stream declares its level:

  chained    a hash chain exists and was checked just now;
  broken     a hash chain exists and did **not** check out -- the loudest
             thing this module can say;
  recorded   append-only, no chain: what we wrote down, not proof;
  derived    assembled from other sources at read time, with no record of its
             own -- it is only as good as what it was built from.

Nothing here writes. Verifying is reading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LEVELS = ("chained", "broken", "recorded", "derived")


@dataclass(frozen=True)
class StreamIntegrity:
    """One event stream and what may honestly be claimed about it."""

    stream: str
    level: str
    note: str = ""
    checked: int = 0

    @property
    def tamper_evident(self) -> bool:
        return self.level == "chained"

    def to_payload(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "level": self.level,
            "note": self.note,
            "checked": self.checked,
            "tamper_evident": self.tamper_evident,
        }


# The streams as they stand. Declared rather than discovered, because a stream
# added without a line here should show up as missing from the report rather
# than silently inherit somebody else's guarantee.
DECLARED: tuple[tuple[str, str, str], ...] = (
    ("order_events", "chained", "每条工单事件都指向前一条，改一条后面全对不上"),
    ("gate_events", "chained", "人工闸门的答复同样成链"),
    ("project_lifecycle_events", "recorded", "项目生死记录是追加表，没有链"),
    ("deployment_events", "recorded", "谁在跑哪一版的变更记录，追加表"),
    ("rollout_targets", "recorded", "发布逐目标的状态与健康检查，追加/就地更新"),
    ("effects", "recorded", "副作用日志：发了什么的证据，不是没发别的的证明"),
    ("delegation_events", "recorded", "运行时事件，追加表"),
    ("harness_session_events", "recorded", "Provider 会话事件，序列无空洞但无链"),
    ("order_timeline", "derived", "读的时候现拼的，本身没有记录，只跟它拼自哪几条一样可靠"),
)


@dataclass
class IntegrityReport:
    streams: list[StreamIntegrity] = field(default_factory=list)

    @property
    def broken(self) -> list[StreamIntegrity]:
        return [s for s in self.streams if s.level == "broken"]

    @property
    def tamper_evident_streams(self) -> list[str]:
        return [s.stream for s in self.streams if s.tamper_evident]

    def to_payload(self) -> dict[str, Any]:
        return {
            "streams": [s.to_payload() for s in self.streams],
            "broken": [s.stream for s in self.broken],
            "tamper_evident": self.tamper_evident_streams,
            # Said in one place so no caller has to infer it: most of this
            # history is recorded, not chained.
            "summary": (
                f"{len(self.tamper_evident_streams)} of {len(self.streams)} streams are tamper-evident; "
                "the rest are append-only records"
            ),
        }


def verify_streams(business: Any, *, order_ids: tuple[str, ...] = (), checkpoint_ids: tuple[str, ...] = ()) -> IntegrityReport:
    """Check the chained streams over the ids given, and declare the rest.

    Chains are verified per aggregate, so an empty list means nothing was
    checked -- and that reports as ``recorded``, not ``chained``. A chain
    nobody ran is not a chain that passed.
    """
    checks: dict[str, tuple[str, int]] = {}
    for stream, ids, verify in (
        ("order_events", order_ids, getattr(business, "verify_order_event_chain", None)),
        ("gate_events", checkpoint_ids, getattr(business, "verify_gate_event_chain", None)),
    ):
        clean = tuple(str(i).strip() for i in ids if str(i).strip())
        if not clean or not callable(verify):
            checks[stream] = ("unchecked", 0)
            continue
        failures = 0
        for identifier in clean:
            try:
                if not verify(identifier):
                    failures += 1
            except Exception:
                failures += 1
        checks[stream] = ("broken" if failures else "chained", len(clean))

    streams: list[StreamIntegrity] = []
    for stream, level, note in DECLARED:
        if stream in checks:
            outcome, count = checks[stream]
            if outcome == "unchecked":
                streams.append(StreamIntegrity(
                    stream, "recorded",
                    note=f"{note}；本次没有给出要核对的 id，未核对的链不算核对过", checked=0,
                ))
            else:
                streams.append(StreamIntegrity(
                    stream, outcome,
                    note=note if outcome == "chained" else f"{note}；有 id 没通过核对",
                    checked=count,
                ))
            continue
        streams.append(StreamIntegrity(stream, level, note=note))
    return IntegrityReport(streams)
