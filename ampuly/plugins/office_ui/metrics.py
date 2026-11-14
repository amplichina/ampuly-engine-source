"""Numbers that say where they came from, or say they were not calculated.

The temptation in this part of a product is a dashboard: an ROI tile, an SLA
badge, a KPI trend, all of them showing something. The trouble is that the
honest answer is usually "we do not know yet", and a tile has nowhere to put
that, so a default appears -- zero, or last month's, or a plausible estimate --
and from then on the number is repeated by people who have no way to tell it
apart from a measured one.

So nothing here returns a bare number.

- A result is ``calculated`` or ``not_calculated``, and the second one names
  every input it was missing. There is no third state where a default stands
  in.
- Every input says how it was obtained: ``measured`` (we recorded it),
  ``stated`` (a person said so), or ``derived`` (computed from others). A
  result that rests on a stated input says which ones, forever -- the
  arithmetic does not launder an assumption into a fact.
- A cost we only partly know is a *lower bound* on cost, which makes a saving
  computed from it an *upper* bound on saving. The direction is the whole
  point: "saved at most ¥800" and "saved ¥800" are different claims, and only
  one of them is true.

Money is in integer cents. Floating point money is a bug waiting for an
invoice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

SOURCES = ("measured", "stated", "derived")
BOUNDS = ("exact", "at_most", "at_least")


class MetricError(ValueError):
    """An input that cannot be trusted is not allowed to become a number."""


@dataclass(frozen=True)
class MetricInput:
    """One number, and how we come to have it."""

    name: str
    value: int | None = None
    unit: str = ""
    source: str = "stated"
    evidence: str = ""

    def __post_init__(self) -> None:
        if not str(self.name or "").strip():
            raise MetricError("an input without a name cannot be reported as missing")
        if self.source not in SOURCES:
            raise MetricError(f"unknown source {self.source!r}; say how this number was obtained")
        if self.value is not None:
            if isinstance(self.value, bool) or not isinstance(self.value, int):
                raise MetricError(f"{self.name} must be a whole number of {self.unit or 'units'}")
            if self.value < 0:
                raise MetricError(f"{self.name} cannot be negative")

    @property
    def present(self) -> bool:
        return self.value is not None

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name, "value": self.value, "unit": self.unit,
            "source": self.source, "evidence": self.evidence, "present": self.present,
        }


@dataclass
class MetricResult:
    """What we can say, including when the answer is that we cannot say."""

    metric: str
    rule: str
    state: str = "not_calculated"
    value: int | None = None
    unit: str = ""
    bound: str = "exact"
    missing: list[str] = field(default_factory=list)
    rests_on: list[str] = field(default_factory=list)
    inputs: list[MetricInput] = field(default_factory=list)
    detail: str = ""

    @property
    def calculated(self) -> bool:
        return self.state == "calculated"

    def to_payload(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "rule": self.rule,
            "state": self.state,
            "value": self.value,
            "unit": self.unit,
            "bound": self.bound,
            "missing": list(self.missing),
            "rests_on": list(self.rests_on),
            "inputs": [i.to_payload() for i in self.inputs],
            "detail": self.detail,
        }


def _missing(inputs: Iterable[MetricInput]) -> list[str]:
    return [i.name for i in inputs if not i.present]


def _stated(inputs: Iterable[MetricInput]) -> list[str]:
    return [i.name for i in inputs if i.present and i.source == "stated"]


def saving(
    baseline: MetricInput,
    costs: list[MetricInput],
    *,
    metric: str = "roi.saving",
    costs_are_complete: bool = True,
) -> MetricResult:
    """What this run saved against what doing it the old way costs.

    Without a baseline there is no saving to report -- not zero, not an
    estimate. "We do not have a number for what this used to cost" is the
    finding, and a useful one: somebody can go and measure it. A named cost
    with no value is the same situation, and answers the same way.

    ``costs_are_complete`` is a different question from a missing value: it
    asks whether the *list* of costs is exhaustive. When it is not, the total
    is a lower bound, which makes the saving an upper bound, reported as *at
    most*. Rounding that to a plain number is how a saving nobody can defend
    ends up in a proposal.
    """
    inputs = [baseline, *costs]
    missing = _missing(inputs)
    if not baseline.present:
        return MetricResult(
            metric=metric,
            rule="saving = baseline cost - actual cost",
            state="not_calculated", unit=baseline.unit, missing=missing, inputs=inputs,
            detail="no baseline: what this used to cost has not been established",
        )
    if missing:
        return MetricResult(
            metric=metric,
            rule="saving = baseline cost - actual cost",
            state="not_calculated", unit=baseline.unit, missing=missing, inputs=inputs,
            detail="some costs of this run are not known, so what it saved is not either",
        )
    spent = sum(int(cost.value or 0) for cost in costs)
    return MetricResult(
        metric=metric,
        rule="saving = baseline cost - actual cost",
        state="calculated",
        value=int(baseline.value or 0) - spent,
        unit=baseline.unit,
        bound="exact" if costs_are_complete else "at_most",
        missing=[],
        rests_on=_stated(inputs),
        inputs=inputs,
        detail="" if costs_are_complete else "not every kind of cost is on this list, so the real saving is no larger than this",
    )


@dataclass
class SLAVerdict:
    """Whether a promise was kept, is still running, or cannot be judged."""

    promise_hours: int
    state: str = "not_calculated"
    elapsed_hours: int | None = None
    remaining_hours: int | None = None
    missing: list[str] = field(default_factory=list)
    detail: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "promise_hours": self.promise_hours,
            "state": self.state,
            "elapsed_hours": self.elapsed_hours,
            "remaining_hours": self.remaining_hours,
            "missing": list(self.missing),
            "detail": self.detail,
        }


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None


def sla_verdict(
    *,
    promise_hours: int,
    started_at: str,
    delivered_at: str = "",
    now: datetime | None = None,
) -> SLAVerdict:
    """Judged against the clock, and only when there is a clock to judge by.

    An order still inside its promise is ``open``, not ``met``: it has not
    been kept yet, and a badge that says otherwise is making a claim about
    the future. Past the deadline with nothing delivered is already
    ``breached`` -- waiting for the delivery to arrive before admitting it
    only delays the same conclusion.
    """
    if not isinstance(promise_hours, int) or isinstance(promise_hours, bool) or promise_hours <= 0:
        raise MetricError("a promise of no time is not a promise")
    start = _parse(started_at)
    if start is None:
        return SLAVerdict(
            promise_hours=promise_hours, state="not_calculated", missing=["started_at"],
            detail="nothing says when the clock started",
        )
    deadline = start + timedelta(hours=promise_hours)
    delivered = _parse(delivered_at) if delivered_at else None
    if delivered is not None:
        elapsed = int((delivered - start).total_seconds() // 3600)
        return SLAVerdict(
            promise_hours=promise_hours,
            state="met" if delivered <= deadline else "breached",
            elapsed_hours=elapsed,
            detail="" if delivered <= deadline else f"delivered {elapsed}h after the clock started",
        )
    moment = now or datetime.now()
    elapsed = int((moment - start).total_seconds() // 3600)
    if moment > deadline:
        return SLAVerdict(
            promise_hours=promise_hours, state="breached", elapsed_hours=elapsed,
            detail="the deadline passed and nothing has been delivered",
        )
    return SLAVerdict(
        promise_hours=promise_hours, state="open", elapsed_hours=elapsed,
        remaining_hours=int((deadline - moment).total_seconds() // 3600),
        detail="still inside the promise; not kept yet",
    )


def summarize(results: Iterable[MetricResult]) -> dict[str, Any]:
    """A report that leads with what is missing, because that is the finding.

    A summary listing only the numbers it managed to produce reads as a
    complete picture of a partial one.
    """
    items = list(results)
    gaps: list[str] = []
    for result in items:
        gaps.extend(f"{result.metric}: {name}" for name in result.missing)
    return {
        "calculated": [r.to_payload() for r in items if r.calculated],
        "not_calculated": [r.to_payload() for r in items if not r.calculated],
        "missing_inputs": gaps,
        "rests_on_stated": sorted({
            f"{r.metric}: {name}" for r in items if r.calculated for name in r.rests_on
        }),
    }


@dataclass
class TrendPoint:
    """One period, and how much of it could actually be computed."""

    period: str
    total: int = 0
    calculated: int = 0
    value: int | None = None
    unit: str = "cents"
    bound: str = "exact"
    excluded: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.calculated == self.total

    def to_payload(self) -> dict[str, Any]:
        return {
            "period": self.period, "total": self.total, "calculated": self.calculated,
            "value": self.value, "unit": self.unit, "bound": self.bound,
            "excluded": list(self.excluded), "complete": self.complete,
            # Said outright so a chart cannot render a partial period as a
            # whole one: "the average of the two we could compute" is a
            # different number from "the average".
            "covers": (
                "nothing to compute" if self.total == 0
                else "every order in this period" if self.complete
                else f"{self.calculated} of {self.total} orders; the rest could not be computed"
            ),
        }


def trend(results_by_period: dict[str, list[MetricResult]]) -> list[TrendPoint]:
    """A metric over time, carrying what each period could not account for.

    The failure this exists to prevent is a line going up. Averaging only the
    orders that happened to be computable, and drawing that as the period's
    figure, makes a month with two measurable orders out of ten look like a
    month. Every point says how many it is built from, and a period with
    nothing computable has no value at all rather than a zero.

    A point is an upper bound if any result it contains is one: mixing an
    exact number with an at-most and calling the total exact is how a bound
    quietly disappears.
    """
    points: list[TrendPoint] = []
    for period in sorted(results_by_period):
        results = list(results_by_period[period] or [])
        usable = [r for r in results if r.calculated and r.value is not None]
        excluded = sorted({
            name for r in results if not r.calculated for name in (r.missing or ["未说明"])
        })
        if not usable:
            points.append(TrendPoint(
                period=period, total=len(results), calculated=0, value=None, excluded=excluded,
            ))
            continue
        points.append(TrendPoint(
            period=period,
            total=len(results),
            calculated=len(usable),
            value=sum(int(r.value or 0) for r in usable),
            unit=usable[0].unit or "cents",
            bound="at_most" if any(r.bound == "at_most" for r in usable) else "exact",
            excluded=excluded,
        ))
    return points
