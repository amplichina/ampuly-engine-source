"""Private fresh-claim production, never launch or runtime acceptance authority."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from .order_launch_claim_source import OriginalOrderLaunchClaim


_MESSAGES = {
    "unavailable": "新发起来源暂不可用。",
    "invalid_source": "新发起来源无法核验。",
    "outcome_unknown": "新发起结果尚未核实；请勿再次发起。",
}


class FreshOrderLaunchError(ValueError):
    """Only a fixed code/message; never the source, path, or original exception."""

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _MESSAGES else "invalid_source"
        super().__init__(_MESSAGES[self.code])


@dataclass(frozen=True, repr=False, slots=True)
class _FreshLaunchDeliveryRequest:
    """Select the private producer mode, not proof of freshness or permission."""


_FRESH_LAUNCH_DELIVERY = _FreshLaunchDeliveryRequest()


@dataclass(frozen=True, repr=False, slots=True)
class _FreshOrderLaunchClaimResult:
    source: OriginalOrderLaunchClaim
    sequence: int
    created_at: str


def validate_fresh_launch_delivery(request: Any) -> None:
    if request is not _FRESH_LAUNCH_DELIVERY:
        raise FreshOrderLaunchError("invalid_source")


@contextmanager
def fresh_launch_connection(path: Any) -> Iterator[sqlite3.Connection]:
    """One owned existing-file transaction, with no result before commit/close.

    Cleanup is bounded and never replaces a primary cancellation or failure.
    Commit/close uncertainty is not evidence of a rollback or permission to retry.
    """

    conn = None
    failure = None
    primary = None
    committed = False
    try:
        try:
            fixed_path = Path(path)
            if not fixed_path.is_absolute():
                raise ValueError
            conn = sqlite3.connect(
                fixed_path.as_uri() + "?mode=rw", timeout=5.0, uri=True,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE").close()
        except Exception:
            failure = "unavailable"
        except BaseException as exc:
            primary = exc

        if failure is None and primary is None:
            try:
                yield conn
            except Exception as exc:
                failure = exc.code if type(exc) is FreshOrderLaunchError else "invalid_source"
            except BaseException as exc:
                primary = exc
            else:
                try:
                    conn.commit()
                except Exception:
                    failure = "outcome_unknown"
                except BaseException as exc:
                    primary = exc
                else:
                    committed = True
    finally:
        if conn is not None:
            if not committed:
                try:
                    conn.rollback()
                except BaseException:
                    pass
            try:
                conn.close()
            except Exception:
                if failure is None and primary is None:
                    failure = "outcome_unknown" if committed else "unavailable"
            except BaseException as exc:
                if failure is None and primary is None:
                    primary = exc

    if primary is not None:
        raise primary
    if failure is not None:
        raise FreshOrderLaunchError(failure) from None


def freeze_fresh_launch_result(
    conn: sqlite3.Connection, *, project_id: str, order_id: str,
    outbox_id: str, workflow_id: str, payload_raw: str,
    claim_event_id: str, sequence: int, created_at: str,
) -> _FreshOrderLaunchClaimResult:
    """Freeze only the current CAS winner's raw claim and complete bounded chain.

    The owner must first create and reserve this exact outbox in this transaction.
    This function neither connects nor commits, and a retained local result does
    not turn a failed transaction into a successful producer observation.
    """

    import_failed = False
    try:
        from .employee_order_launch_anchor import (
            _ReadBudget, _identity, _json_dict, _MAX_JSON_BYTES,
        )
        from .order_launch_claim_source import OriginalOrderLaunchClaim, _legacy_payload
        from .employee_order_claim_pins import _read_claim_events
    except Exception:
        import_failed = True
    if import_failed:
        raise FreshOrderLaunchError("unavailable") from None

    failure = False
    result = None
    try:
        if (not isinstance(conn, sqlite3.Connection) or not conn.in_transaction
                or conn.row_factory is not sqlite3.Row or conn.text_factory is not str):
            raise ValueError
        project_id = _identity(project_id, scope=True)
        order_id = _identity(order_id, scope=True)
        outbox_id = _identity(outbox_id, scope=True)
        workflow_id = _identity(workflow_id, scope=True)
        claim_event_id = _identity(claim_event_id)
        if type(sequence) is not int or not 0 < sequence <= 2**63 - 1:
            raise ValueError
        if (type(created_at) is not str or len(created_at) > 128
                or len(created_at.encode("utf-8")) > 128):
            raise ValueError
        if type(payload_raw) is not str or not payload_raw or len(payload_raw) > _MAX_JSON_BYTES:
            raise ValueError
        raw_bytes = payload_raw.encode("utf-8")
        if len(raw_bytes) > _MAX_JSON_BYTES:
            raise ValueError
        budget = _ReadBudget()
        budget.charge_bytes(len(raw_bytes))
        payload = _json_dict(payload_raw, budget)
        version, _, _ = _legacy_payload(
            payload, project_id=project_id, order_id=order_id, workflow_id=workflow_id,
        )
        payload_sha = hashlib.sha256(raw_bytes).hexdigest()
        observed_claim, pins_event, head_sha = _read_claim_events(
            conn, order_id=order_id, outbox_id=outbox_id, payload_sha=payload_sha,
            budget=budget, pins_source=None, project_id=project_id,
            workflow_id=workflow_id, workflow_version=version,
        )
        if observed_claim != claim_event_id or pins_event != "":
            raise ValueError
        source = OriginalOrderLaunchClaim(
            project_id=project_id, order_id=order_id, outbox_id=outbox_id,
            workflow_id=workflow_id, workflow_version=version,
            launch_payload_bytes=raw_bytes, launch_payload_sha256=payload_sha,
            claim_event_id=observed_claim, event_chain_head_sha256=head_sha,
        )
        result = _FreshOrderLaunchClaimResult(
            source=source, sequence=sequence, created_at=created_at,
        )
    except Exception:
        failure = True
    if failure:
        raise FreshOrderLaunchError("invalid_source") from None
    return result
