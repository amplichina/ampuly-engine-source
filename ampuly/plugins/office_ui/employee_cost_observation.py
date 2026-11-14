"""Pure projection of loaded legacy project cost rows for display only.

USD is a legacy field label, not a verified billing currency. This module
does not read a store, price tokens, reconcile invoices, or grant execution.
Decimal(str(value)) describes the current legacy number; it cannot restore
the precision or pricing status discarded by the historical float record.
"""

from __future__ import annotations

import math
import re
from decimal import (
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    localcontext,
)


_MAX_TASKS = 5000
_MAX_ROW_FIELDS = 32
_PROJECT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_MAX_CALLS = 2**53 - 1
_MAX_AMOUNT_INT = 10**12
_MAX_AMOUNT = Decimal("1000000000000")
_MAX_DECIMAL_PLACES = 18
_BASE_GAPS = (
    "pricing_status_not_persisted",
    "call_coverage_unverified",
    "not_supplier_reconciled",
    "actual_currency_unverified",
)


def _observation(
    project_id: str,
    *,
    state: str,
    subtotal_text: str | None = None,
    calls: int | None = None,
    task_count: int | None = None,
    gap: str | None = None,
    runs: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    gaps = list(_BASE_GAPS)
    if gap is not None:
        gaps.append(gap)
    return {
        "project_id": project_id,
        "state": state,
        "recorded_subtotal_text": subtotal_text,
        "currency_label": "USD",
        "currency_basis": "legacy_field_label_unverified",
        "basis": "legacy_cost_record",
        "actual_currency": None,
        "actual_total": None,
        "calls": calls,
        "task_count": task_count,
        "gaps": gaps,
        # Per-row amounts from this same walk. Task keys stay off the DTO.
        "runs": None if state != "legacy_recorded" else list(runs or []),
    }


def _cost_decimal(value: object) -> Decimal | None:
    if type(value) not in (int, float):
        return None
    if type(value) is float and not math.isfinite(value):
        return None
    # Check the bound before str(): a huge integer must not be converted to
    # an unbounded decimal string, even though it is a built-in int.
    if value < 0 or value > _MAX_AMOUNT_INT:
        return None
    amount = Decimal(str(value))
    if not amount.is_finite():
        return None
    if amount.normalize().as_tuple().exponent < -_MAX_DECIMAL_PLACES:
        return None
    return amount


def _canonical_decimal_text(amount: Decimal) -> str:
    if amount == 0:
        return "0"
    text = format(amount, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def project_recorded_cost_observation(
    project_id: str,
    by_task: dict[str, dict[str, object]] | None,
    readable: bool,
    *,
    source_quality_required: bool = False,
) -> dict[str, object]:
    """Return a detached display DTO; every unknown clears all aggregates.

    Only exact built-in dict rows and exact built-in numeric values are
    accepted. No task key, row, exception text, or source metadata is exposed.
    Ordinary conversion failures become a fixed unknown result. Cancellation
    and other BaseException subclasses are intentionally not intercepted.
    """
    if (
        type(project_id) is not str
        or not 1 <= len(project_id) <= 256
        or _PROJECT_ID.fullmatch(project_id) is None
    ):
        raise ValueError("invalid_project_id") from None
    if type(readable) is not bool or type(source_quality_required) is not bool:
        return _observation(project_id, state="unknown", gap="invalid_payload")
    if not readable:
        return _observation(project_id, state="unknown", gap="unreadable")
    if type(by_task) is not dict:
        return _observation(project_id, state="unknown", gap="invalid_payload")
    if len(by_task) > _MAX_TASKS:
        return _observation(project_id, state="unknown", gap="record_limit_exceeded")
    if not by_task:
        return _observation(project_id, state="unknown", gap="no_records")

    try:
        # A fully specified local context prevents host precision, rounding,
        # traps, flags, or exponent limits from affecting the projection.
        with localcontext(
            Context(
                prec=64,
                rounding=ROUND_HALF_EVEN,
                Emin=-999,
                Emax=999,
                capitals=1,
                clamp=0,
                flags=[],
                traps=[InvalidOperation, DivisionByZero, Overflow],
            )
        ):
            subtotal = Decimal(0)
            total_calls = 0
            has_legacy_zero = False
            task_count = len(by_task)
            runs: list[dict[str, object]] = []
            missing = object()
            for row in by_task.values():
                if type(row) is not dict or len(row) > _MAX_ROW_FIELDS:
                    return _observation(project_id, state="unknown", gap="invalid_record")
                cost = missing
                calls = missing
                invalid_cost_records = missing
                # Do not perform lookups against exotic key objects that may
                # run their own __eq__. Unknown fields are not source proof.
                for field, value in row.items():
                    if type(field) is str:
                        if field == "cost":
                            cost = value
                        elif field == "calls":
                            calls = value
                        elif field == "invalid_cost_records":
                            invalid_cost_records = value
                if source_quality_required and invalid_cost_records is missing:
                    return _observation(project_id, state="unknown", gap="invalid_payload")
                amount = _cost_decimal(cost)
                if amount is None or type(calls) is not int or not 0 <= calls <= _MAX_CALLS:
                    return _observation(project_id, state="unknown", gap="invalid_record")
                if source_quality_required and (
                    type(invalid_cost_records) is not int
                    or not 0 <= invalid_cost_records <= calls
                    or invalid_cost_records != 0
                ):
                    return _observation(project_id, state="unknown", gap="invalid_record")
                subtotal += amount
                has_legacy_zero = has_legacy_zero or amount == 0
                total_calls += calls
                runs.append({
                    "run_index": len(runs) + 1,
                    "recorded_subtotal_text": _canonical_decimal_text(amount),
                    "calls": calls,
                })
                if subtotal > _MAX_AMOUNT or total_calls > _MAX_CALLS:
                    return _observation(project_id, state="unknown", gap="invalid_record")
            return _observation(
                project_id,
                state="legacy_recorded",
                subtotal_text=_canonical_decimal_text(subtotal),
                calls=total_calls,
                task_count=task_count,
                gap="legacy_zero_ambiguous" if has_legacy_zero else None,
                runs=runs,
            )
    except Exception:
        # Never expose private input or conversion exception text. Do not
        # catch BaseException: a cancelled host operation must stay cancelled.
        return _observation(project_id, state="unknown", gap="invalid_record")
