"""验收这一轮 -- an order's acceptance round in one module (C5-2, policy Q14/Q16).

An order's contractual acceptance runs one round at a time: its criteria, the evidence for them, the submission, a
decision -- and then the order's close (确认收货并结案) or the next round (返工). The console's buttons come here, and a
Feishu button can too: every call that records something names who acts (``actor``). The websocket handlers only read
the request and answer it.

What this module keeps true:

* **Evidence by content.** Evidence that names a file is bound to its bytes (``artifact_versions.bind_artifact_evidence``,
  never through the preview, so a picture over the preview's 8 MB is bound like any other file). The owner's receipt
  names the deliverable files of the order's last step, found by the one order-folder rule (``order_folder.resolve``);
  a deliverable that cannot be bound is said, not skipped.
* **Path-backed evidence => a verified snapshot before the decision is recorded.** The round's files are frozen
  (``artifact_versions.freeze_delivery_round``), every frozen copy is verified against the digest its evidence was bound
  to, and the decision is recorded only when the frozen files are exactly the evidence's -- checked once more inside the
  decision's own transaction (``BusinessStore.settle_acceptance``). A rejection is frozen the same way when it can be;
  one whose files are no longer as evidenced is recorded without, as before, and its rework says what it cannot freeze.
* **确认收货并结案 is all or nothing** from the owner's view: ``confirm_receipt_and_close`` lists every state a failure
  can leave and why one more click finishes it.
* **The owner's words.** A refusal is an ``AcceptanceRefused`` in plain Chinese, without internal ids.

One order's round is handled one call at a time (``_order_lock``): the console and a Feishu button share one process.

Plugin layer: no kernel, no frontend.
"""
from __future__ import annotations

import asyncio
import json
import os
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from ampuly.core.order_layout import ORDERS
from ampuly.plugins.office_ui import artifact_versions, order_folder
from ampuly.plugins.office_ui.artifact_versions import SnapshotError
from ampuly.plugins.office_ui.artifacts_service import workspace_root_for_project
from ampuly.plugins.office_ui.business_store import (
    ACCEPTANCE_DECISIONS,
    ACCEPTANCE_PASSING,
    AcceptanceRecord,
    OrderTransitionError,
    acceptance_evidence_path,
)

CLOSE_NOTE = "确认收货并结案"
RECEIPT_NOTE = "负责人确认收货"
RECEIPT_NOTE_NO_FILE = "负责人确认收货（没有可指向的交付文件）"
# Who wrote a criterion the order's flow did not: the launch's own name for it (ws_handler._GENERATED_ACCEPTANCE_ACTOR).
GENERATED_ACTOR = "系统：按交付物生成"

_CLOSE_TAIL = "这次什么都没有记下，处理好后再点一次「确认收货并结案」就行"
_DECIDE_TAIL = "这次没有记下验收决定"
_REWORK_TAIL = "这次没有开始返工"
_EVIDENCE_TAIL = "这条证据没有记下"
_EXPORT_TAIL = "这次没有导出"


class AcceptanceRefused(OrderTransitionError):
    """A refusal in the owner's words."""


@dataclass
class Outcome:
    """What a decision or a close recorded: the round's record, and ``"closed"`` when the order is closed now."""

    record: AcceptanceRecord
    order_status: str = ""


@dataclass(frozen=True)
class _Frozen:
    reference: dict[str, Any]   # what the decision records (manifest path and digest, counts)
    created: bool               # this attempt wrote the round, so it removes it if nothing records it
    files: dict[str, str]       # workplace path -> sha256 of the verified frozen copy


# ── one call per order at a time ────────────────────────────────────────────

_LOCKS: "weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock]" = weakref.WeakValueDictionary()


def _order_lock(project_id: str, order_id: str) -> asyncio.Lock:
    """The lock of one order's round. Held only while a call runs, so it goes away with it."""
    key = (str(project_id or "default"), str(order_id or ""))
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


# ── the owner's words ───────────────────────────────────────────────────────


def _shown(path: str, order_id: str) -> str:
    """A file as the owner knows it: below the order's delivery tree, without the order folder in front."""
    text = str(path or "").replace("\\", "/")
    if order_id:
        for prefix in (f"{ORDERS}/{order_id}/output/", f"{ORDERS}/{order_id}/"):
            if text.startswith(prefix):
                return text[len(prefix):]
    return text


def _file(path: str, order_id: str) -> str:
    shown = _shown(path, order_id)
    return f"交付文件「{shown}」" if shown else "有一个交付文件"


def _megabytes(value: int) -> int:
    return max(1, round(int(value or 0) / (1024 * 1024)))


def plain_reason(exc: BaseException, order_id: str = "") -> str:
    """What went wrong, in the owner's words, without a closing full stop."""
    if isinstance(exc, SnapshotError):
        name = _file(exc.path, order_id)
        words = {
            artifact_versions.TOO_MANY: f"这一轮的交付文件有 {exc.size} 个，一轮最多只能冻结 {exc.limit} 个",
            artifact_versions.FILE_TOO_LARGE:
                f"{name}有 {_megabytes(exc.size)} MB，超过了单个文件 {_megabytes(exc.limit)} MB 的冻结上限",
            artifact_versions.ROUND_TOO_LARGE: f"这一轮的交付文件合计超过了 {_megabytes(exc.limit)} MB 的冻结上限",
            artifact_versions.CHANGED: f"{name}在记下验收证据之后被改过，已经不是当时确认的那一份",
            artifact_versions.CHANGING: f"{name}正在被改动，前后读到的内容不一样",
            artifact_versions.MISSING: f"{name}找不到了",
            artifact_versions.UNREADABLE: f"{name}不能作为交付物冻结（它是链接、系统内部文件，或者在别处还有第二个名字）",
            artifact_versions.CONFLICT: "这一轮已经冻结过另一组交付文件",
            artifact_versions.MISMATCH: "冻结下来的交付文件和记录核对不上",
            artifact_versions.STUCK: "上一次没做完时留下的冻结目录删不掉（可能正被别的程序打开）",
        }
        return words.get(exc.reason, "交付文件没能冻结")
    if isinstance(exc, OrderTransitionError):
        return str(exc).rstrip("。")
    if isinstance(exc, OSError):
        return "读写交付文件时出错了"
    return "处理验收时出错了"


def _refused(exc: BaseException, order_id: str, tail: str) -> AcceptanceRefused:
    if not isinstance(exc, (SnapshotError, OrderTransitionError, OSError)):
        logger.opt(exception=exc).warning("acceptance: order {} stopped on an unexpected error", order_id)
    return AcceptanceRefused(f"{plain_reason(exc, order_id)}。{tail}。")


# ── what the order is ───────────────────────────────────────────────────────


def order_steps(api: Any, engine: Any, project_id: str, order: Any) -> tuple[list[dict[str, Any]], str]:
    """The order's steps and the flow version they came with: the table frozen at launch (what the order started with),
    or -- when that table cannot be read (missing, not this order's, not a table, too large, behind a link) -- the
    package's steps today and no version."""
    order_id = str(getattr(order, "order_id", "") or "")
    order_root = workspace_root_for_project(engine, project_id) / ORDERS / order_id
    table = order_folder.read_step_table(order_root, order_id)
    if table.ok:
        return table.steps, str(table.data.get("workflow_version", "") or "")
    package = api.package(str(getattr(order, "workflow_id", "") or ""))
    return (list(getattr(package, "nodes", None) or []) if package is not None else []), ""


def generated_criterion(steps: list[dict[str, Any]] | None) -> str:
    """The one criterion an order gets when its flow wrote none: 「<最后一步的交付物> 已交付并经 owner 接受」 -- the
    launch's rule (ws_handler._generated_acceptance_criterion), so an order closed without one gets the same."""
    name, deliverables = order_folder.last_deliverables(steps)
    subject = "、".join(deliverables) if deliverables else (f"「{name}」的交付物" if name else "本单的交付物")
    return f"{subject} 已交付并经 owner 接受"


async def receipt_evidence(
    api: Any, engine: Any, project_id: str, order: Any, *, steps: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """What the owner's receipt points at: the last step's deliverable files by content, else the delivering event.

    The deliverables are the order's steps' (``order_steps``). Which files a deliverable names is the order-folder rule
    (``order_folder.resolve``): the output tree first, the order folder for an older order that delivered there (never
    the runtime's areas or the launch's own files), a name written with ``output/`` from the order folder; hidden files
    and links never count. A pattern that matches nothing is skipped. A file that cannot be bound, a pattern whose scan
    ran out and more files than one round can freeze are refused in the owner's words (``AcceptanceRefused``) -- before
    C5-2 they were skipped silently and the frozen round lacked them. With no file at all the evidence names the order
    event that put the delivery in the customer's hands.
    """
    order_id = str(getattr(order, "order_id", "") or "")
    run_id = str(getattr(order, "launched_task_id", "") or "").strip() or f"order:{order_id}"
    root = workspace_root_for_project(engine, project_id)
    order_root = root / ORDERS / order_id
    if steps is None:
        steps, _version = await asyncio.to_thread(order_steps, api, engine, project_id, order)
    _name, deliverables = order_folder.last_deliverables(steps)
    paths: dict[str, Path] = {}
    problems: list[str] = []
    for pattern in deliverables:
        found = await asyncio.to_thread(order_folder.resolve, order_root, pattern)
        if found.truncated:
            problems.append(f"交付物「{pattern}」下的文件太多，没能数完")
            continue
        for path in found.paths:
            paths.setdefault(os.path.normcase(str(path)), path)
    if len(paths) > artifact_versions.MAX_FILES:
        problems.append(f"这一轮的交付文件有 {len(paths)} 个，一轮最多只能冻结 {artifact_versions.MAX_FILES} 个")
    receipts: list[dict[str, Any]] = []
    if not problems:
        resolved_root = root.resolve()
        for path in paths.values():
            try:
                shown = path.relative_to(root).as_posix()
            except ValueError:
                shown = path.name
            try:
                relative = path.resolve().relative_to(resolved_root).as_posix()
                bound = await asyncio.to_thread(artifact_versions.bind_artifact_evidence, engine, project_id, relative)
            except SnapshotError as exc:
                problems.append(plain_reason(exc, order_id))
                continue
            except (OSError, ValueError):
                problems.append(f"{_file(shown, order_id)}读不了")
                continue
            receipts.append({"run_id": run_id, "note": RECEIPT_NOTE, **bound})
    if problems:
        more = f"（另有 {len(problems) - 3} 处）" if len(problems) > 3 else ""
        raise AcceptanceRefused("；".join(problems[:3]) + more)
    if receipts:
        return receipts
    delivering_event_id = ""
    for event in api.business.events(order_id):
        if str(event.get("kind", "") or "") == "delivering":
            delivering_event_id = str(event.get("event_id", "") or "")
    return [{
        "run_id": run_id,
        "event_id": delivering_event_id or f"order:{order_id}:delivering",
        "note": RECEIPT_NOTE_NO_FILE,
    }]


# ── freezing a round ────────────────────────────────────────────────────────


async def _discard(engine: Any, project_id: str, reference: dict[str, Any]) -> None:
    try:
        await asyncio.to_thread(
            artifact_versions.discard_delivery_round, engine, project_id=project_id, snapshot=reference,
        )
    except Exception:
        # Left in place, it is referred to by nothing: the next attempt adopts it (same files) or replaces it.
        logger.opt(exception=True).warning("acceptance: a frozen round that nothing records could not be removed")


async def _freeze(
    engine: Any, project_id: str, order_id: str, round_no: int, evidence: list[dict[str, Any]],
) -> _Frozen | None:
    """Freeze and verify the files ``evidence`` names for this round; None when it names none.

    Called only for a round no recorded decision refers to, so a round an earlier attempt left on disk is adopted when
    it is exactly these files and replaced when it is not. What this call wrote is removed again when it fails.
    """
    paths: list[str] = []
    expected: dict[str, str] = {}
    for item in evidence:
        path = acceptance_evidence_path(item)
        if not path:
            continue
        paths.append(path)
        digest = str(item.get("artifact_hash", "") or "").strip().lower() if isinstance(item, dict) else ""
        if digest:
            key = os.path.normcase(path)
            if expected.get(key, digest) != digest:
                raise SnapshotError("one file was evidenced with two digests", reason=artifact_versions.CHANGED, path=path)
            expected[key] = digest
    if not paths:
        return None
    wanted = {os.path.normcase(path) for path in paths}
    reference, created = await asyncio.to_thread(
        artifact_versions.freeze_delivery_round,
        engine,
        project_id=project_id,
        order_id=order_id,
        round_no=max(1, int(round_no or 1)),
        artifact_paths=paths,
        expected_hashes=expected,
        replace_unreferenced=True,
    )
    try:
        verified = await asyncio.to_thread(
            artifact_versions.verify_delivery_snapshot, engine, project_id=project_id, snapshot=reference,
        )
        files = {str(item["source_path"]): str(item["sha256"]) for item in verified["files"]}
        folded = {os.path.normcase(path): digest for path, digest in files.items()}
        if set(folded) != wanted:
            raise SnapshotError("the frozen round does not hold the evidenced files", reason=artifact_versions.MISMATCH)
        for key, digest in expected.items():
            if folded.get(key) != digest:
                raise SnapshotError("a frozen copy is not the evidenced file", reason=artifact_versions.CHANGED, path=key)
    except BaseException:
        if created:
            await _discard(engine, project_id, reference)
        raise
    return _Frozen(reference, created, files)


async def _forget_unrecorded(engine: Any, project_id: str, frozen: _Frozen | None, record: AcceptanceRecord) -> None:
    """A round this call froze that the recorded decision does not refer to (it was decided meanwhile) is removed."""
    if frozen is None or not frozen.created:
        return
    recorded = str(dict(record.artifact_snapshot or {}).get("manifest_sha256", "") or "")
    if recorded != str(frozen.reference.get("manifest_sha256", "") or ""):
        await _discard(engine, project_id, frozen.reference)


# ── reading ─────────────────────────────────────────────────────────────────


async def verified_payload(engine: Any, project_id: str, acceptance: Any) -> dict[str, Any] | None:
    """The record as the console shows it, every frozen round verified on disk (``verified`` and previewable paths, or
    ``verified: false`` with why), and each round compared with the one before it."""
    if acceptance is None:
        return None
    payload = acceptance.to_payload() if callable(getattr(acceptance, "to_payload", None)) else dict(acceptance)
    errors = (ValueError, FileNotFoundError, OSError, json.JSONDecodeError)
    history: list[dict[str, Any]] = []
    verified_snapshots: list[tuple[dict[str, Any], dict[str, Any]]] = []
    current_snapshot = dict(payload.get("artifact_snapshot", {}) or {})
    if current_snapshot:
        try:
            payload["artifact_snapshot"] = await asyncio.to_thread(
                artifact_versions.verify_delivery_snapshot, engine, project_id=project_id, snapshot=current_snapshot,
            )
            verified_snapshots.append((payload, current_snapshot))
        except errors as exc:
            payload["artifact_snapshot"] = {**current_snapshot, "verified": False, "error": str(exc)}
    for raw in list(payload.get("history", []) or []):
        item = dict(raw) if isinstance(raw, dict) else {}
        snapshot = dict(item.get("artifact_snapshot", {}) or {})
        if snapshot:
            try:
                item["artifact_snapshot"] = await asyncio.to_thread(
                    artifact_versions.verify_delivery_snapshot, engine, project_id=project_id, snapshot=snapshot,
                )
                verified_snapshots.append((item, snapshot))
            except errors as exc:
                item["artifact_snapshot"] = {**snapshot, "verified": False, "error": str(exc)}
        history.append(item)
    if len(verified_snapshots) > 1:
        verified_snapshots.sort(key=lambda pair: int(pair[0].get("round_no", 0) or 0))
        for (earlier_item, earlier), (later_item, later) in zip(verified_snapshots, verified_snapshots[1:]):
            try:
                later_item["artifact_comparison"] = await asyncio.to_thread(
                    artifact_versions.compare_delivery_snapshots, engine, project_id=project_id,
                    earlier=earlier, later=later,
                )
            except errors as exc:
                later_item["artifact_comparison"] = {
                    "comparison": "byte_inventory",
                    "earlier_round": int(earlier_item.get("round_no", 0) or 0),
                    "later_round": int(later_item.get("round_no", 0) or 0),
                    "error": str(exc),
                }
    payload["history"] = history
    return payload


# ── the round, step by step ─────────────────────────────────────────────────


def define(
    api: Any, project_id: str, order_id: str, *, criteria: list[str], actor: str, workflow_version: str = "",
) -> AcceptanceRecord:
    """Write the round's criteria (once; the same criteria again are the same record)."""
    return api.business.define_acceptance(
        order_id, criteria, actor=actor, workflow_version=workflow_version, project_id=project_id,
    )


async def add_evidence(
    api: Any, engine: Any, project_id: str, order_id: str, *, criterion: str, evidence: dict[str, Any], actor: str,
) -> AcceptanceRecord:
    """Record one piece of evidence; one that names a file (``artifact_path``) is bound to its content first."""
    clean = dict(evidence) if isinstance(evidence, dict) else {}
    path = str(clean.get("artifact_path", "") or "").strip()
    async with _order_lock(project_id, order_id):
        if path:
            try:
                clean.update(await asyncio.to_thread(artifact_versions.bind_artifact_evidence, engine, project_id, path))
            except (SnapshotError, OSError, ValueError) as exc:
                raise _refused(exc, order_id, _EVIDENCE_TAIL) from exc
        return api.business.add_acceptance_evidence(
            order_id, criterion=criterion, evidence=clean, actor=actor, project_id=project_id,
        )


async def submit(api: Any, project_id: str, order_id: str, *, actor: str) -> AcceptanceRecord:
    """Submit the round once every criterion has evidence."""
    async with _order_lock(project_id, order_id):
        return api.business.submit_acceptance(order_id, actor=actor, project_id=project_id)


async def decide(
    api: Any, engine: Any, project_id: str, order_id: str, *, decision: str, actor: str, note: str = "",
    exceptions: list[str] | None = None,
) -> Outcome:
    """The card's decision on a submitted round -- 通过 / 带例外通过 / 打回 -- without closing the order.

    A passing decision is recorded only with the round's path-backed evidence frozen and verified; a round that cannot
    be frozen is refused in the owner's words and nothing is recorded. A rejection is frozen too when it can be (so its
    rework does not depend on the files staying as they were); when it cannot, it is recorded without, as before. A
    decision already recorded is answered as it is; a different one is refused.
    """
    clean_decision = str(decision or "").strip().lower()
    clean_actor = str(actor or "").strip()
    clean_order_id = str(order_id or "").strip()
    if clean_decision not in ACCEPTANCE_DECISIONS:
        raise AcceptanceRefused("不认识这个验收决定")
    if not clean_actor:
        raise AcceptanceRefused("验收决定要写明是谁做的")
    business = api.business
    async with _order_lock(project_id, clean_order_id):
        if business.get_order(clean_order_id, project_id=project_id) is None:
            raise AcceptanceRefused("找不到这张工单")
        record = business.acceptance(clean_order_id, project_id=project_id)
        frozen: _Frozen | None = None
        if record is not None and record.status == "submitted":
            if clean_decision == "rejected" and not str(note or "").strip():
                raise AcceptanceRefused("打回要写一句原因")
            if clean_decision == "accepted_with_exceptions" and not [e for e in exceptions or [] if str(e).strip()]:
                raise AcceptanceRefused("带例外通过要写明是哪些例外")
            try:
                frozen = await _freeze(engine, project_id, clean_order_id, record.round_no, list(record.evidence))
            except Exception as exc:
                if clean_decision != "rejected":
                    raise _refused(exc, clean_order_id, _DECIDE_TAIL) from exc
                logger.warning(
                    "acceptance: order {} is rejected without its round frozen ({}); its rework freezes what it still can",
                    clean_order_id, plain_reason(exc, clean_order_id),
                )
        try:
            record, _order = await asyncio.to_thread(
                business.settle_acceptance,
                clean_order_id,
                decision=clean_decision,
                actor=clean_actor,
                note=note,
                exceptions=list(exceptions or []),
                artifact_snapshot=frozen.reference if frozen is not None else None,
                frozen_files=frozen.files if frozen is not None else None,
                snapshot_required=clean_decision in ACCEPTANCE_PASSING,
                project_id=project_id,
            )
        except Exception as exc:
            if frozen is not None and frozen.created:
                await _discard(engine, project_id, frozen.reference)
            raise _refused(exc, clean_order_id, _DECIDE_TAIL) from exc
        await _forget_unrecorded(engine, project_id, frozen, record)
    return Outcome(record)


async def confirm_receipt_and_close(
    api: Any,
    engine: Any,
    project_id: str,
    order_id: str,
    *,
    decision: str,
    actor: str,
    note: str = "",
    exceptions: list[str] | None = None,
    close_pauses: Callable[[], Awaitable[Any]] | None = None,
) -> Outcome:
    """确认收货并结案: take a delivering order's round to a passing decision and close the order, all or nothing.

    ``actor`` is who confirmed the receipt (the console's login, or the owner's Feishu identity from a Feishu button).
    ``close_pauses`` closes what the order's run still has waiting once the order is closed.

    The steps, in order, and what a failure at each leaves -- from every one of them one more click closes the order:

    1. Checks, in the owner's words: a passing decision, an order that is there and delivering, a round not sent back.
       An order with no acceptance record (launched before criteria were written at launch) gets the launch's default
       criterion now (``generated_criterion``). That record stays if a later step fails: it is what the launch writes
       today, and the next click finds it.
    2. The receipt: for each criterion without evidence, the last step's deliverable files bound by content (the
       delivering event when there is no file). Read only; a file that cannot be bound refuses the close.
    3. The freeze: the round's path-backed evidence, recorded and planned, is copied into ``delivery-history`` and every
       copy verified against its evidence. A failure removes what this attempt wrote. A round left on disk anyway (the
       process stopped between steps 3 and 4) is referred to by nothing; the next attempt adopts it when it is exactly
       its files and replaces it when it is not, so a retry with different files succeeds.
    4. One transaction (``BusinessStore.settle_acceptance``): the planned evidence, the submission, the decision with
       its frozen round, and the order's close -- all recorded or none. On a failure the round of step 3 is removed.
       Nothing irreversible happens before this step, and after it the record is never decided without the order
       closed by this road.
    5. After it: the order's allowance is closed (in the store, as ``transition`` does) and ``close_pauses`` runs. Both
       are best-effort; a click on the closed order runs them again and answers as a success (an earlier click whose
       answer was lost), and the next start's sweep closes pauses as well.

    A round already passed on the card is only closed (its decision stands, nothing is frozen twice); a round sent back
    is refused: the next round is the way.
    """
    clean_decision = str(decision or "").strip().lower()
    clean_actor = str(actor or "").strip()
    clean_order_id = str(order_id or "").strip()
    if clean_decision not in ACCEPTANCE_PASSING:
        raise AcceptanceRefused("只有验收通过才能结案")
    if not clean_actor:
        raise AcceptanceRefused("结案要写明是谁确认的收货")
    business = api.business
    async with _order_lock(project_id, clean_order_id):
        order = business.get_order(clean_order_id, project_id=project_id)
        if order is None:
            raise AcceptanceRefused("找不到这张工单，不能结案")
        record = business.acceptance(clean_order_id, project_id=project_id)
        status = str(getattr(order, "status", "") or "")
        receipts: list[tuple[str, dict[str, Any]]] = []
        frozen: _Frozen | None = None
        if status == "closed":
            if record is None or record.status not in ACCEPTANCE_PASSING:
                raise AcceptanceRefused("这张工单已经结束了，不能再确认收货")
            clean_decision = record.status
        elif status != "delivering":
            raise AcceptanceRefused("这张工单不在客户验收中，不能确认收货结案")
        else:
            steps: list[dict[str, Any]] | None = None
            if record is None:
                try:
                    steps, version = await asyncio.to_thread(order_steps, api, engine, project_id, order)
                    record = _generated_record(business, project_id, clean_order_id, steps, version)
                except AcceptanceRefused:
                    raise
                except Exception as exc:
                    raise _refused(exc, clean_order_id, _CLOSE_TAIL) from exc
            if record.status == "rejected":
                raise AcceptanceRefused("这张工单的验收已经打回，要结案得先走下一轮验收")
            if record.status in ACCEPTANCE_PASSING:
                # Passed on the card, the order still delivering: its decision stands, whatever the button sent.
                clean_decision = record.status
            else:
                if clean_decision == "accepted_with_exceptions" and not [e for e in exceptions or [] if str(e).strip()]:
                    raise AcceptanceRefused("带例外通过要写明是哪些例外")
                if record.status in {"criteria_defined", "evidence_collecting"}:
                    covered = {str(item.get("criterion", "") or "") for item in record.evidence}
                    missing = [criterion for criterion in record.criteria if criterion not in covered]
                    if missing:
                        try:
                            found = await receipt_evidence(api, engine, project_id, order, steps=steps)
                        except AcceptanceRefused as exc:
                            raise AcceptanceRefused(f"{str(exc).rstrip('。')}。{_CLOSE_TAIL}。") from exc
                        except Exception as exc:
                            raise _refused(exc, clean_order_id, _CLOSE_TAIL) from exc
                        receipts = [(criterion, dict(item)) for criterion in missing for item in found]
                try:
                    frozen = await _freeze(
                        engine, project_id, clean_order_id, record.round_no,
                        [*record.evidence, *(item for _criterion, item in receipts)],
                    )
                except Exception as exc:
                    raise _refused(exc, clean_order_id, _CLOSE_TAIL) from exc
        try:
            record, closed = await asyncio.to_thread(
                business.settle_acceptance,
                clean_order_id,
                decision=clean_decision,
                actor=clean_actor,
                note=note,
                exceptions=list(exceptions or []),
                receipts=receipts,
                artifact_snapshot=frozen.reference if frozen is not None else None,
                frozen_files=frozen.files if frozen is not None else None,
                close_order=True,
                close_note=CLOSE_NOTE,
                project_id=project_id,
            )
        except Exception as exc:
            if frozen is not None and frozen.created:
                await _discard(engine, project_id, frozen.reference)
            raise _refused(exc, clean_order_id, _CLOSE_TAIL) from exc
        await _forget_unrecorded(engine, project_id, frozen, record)
        outcome = Outcome(record, str(getattr(closed, "status", "") or ""))
    if outcome.order_status == "closed" and close_pauses is not None:
        try:
            await close_pauses()
        except Exception:
            logger.opt(exception=True).warning(
                "close: what order {} still had waiting was not closed now; the next click or start closes it",
                clean_order_id,
            )
    return outcome


def _generated_record(
    business: Any, project_id: str, order_id: str, steps: list[dict[str, Any]], version: str,
) -> AcceptanceRecord:
    """B5-0002 / B5-0003 (policy): an order launched before criteria were written at launch has no
    record and sat in 客户验收中 for good. It gets the launch's default criterion, by the launch's own name."""
    criterion = generated_criterion(steps)
    try:
        business.define_acceptance(
            order_id, [criterion], actor=GENERATED_ACTOR, workflow_version=version, project_id=project_id,
        )
        logger.info("close: order {} had no acceptance criteria; one generated from its deliverables: {}",
                    order_id, criterion)
    except OrderTransitionError:
        pass  # written meanwhile (a relaunch's pin): that record is the one read below
    record = business.acceptance(order_id, project_id=project_id)
    if record is None:
        raise AcceptanceRefused(f"这张工单没有验收标准，也没能按交付物补上一条。{_CLOSE_TAIL}。")
    return record


async def rework(
    api: Any, engine: Any, project_id: str, order_id: str, *, target_node: str, actor: str, request_id: str,
) -> AcceptanceRecord:
    """Open the next round after a rejection and hand the rework to the runtime.

    The rejected round goes into the history with its frozen files: the round its rejection recorded, or -- for a
    rejection recorded without one -- a round frozen now from the files as they are, only if they are still what was
    evidenced (otherwise refused in the owner's words, nothing recorded). The same ``request_id`` again converges on
    the same round. The runtime command is dispatched after the round is recorded; one that fails stays pending.
    """
    clean_order_id = str(order_id or "").strip()
    clean_target = str(target_node or "").strip()
    clean_request = str(request_id or "").strip()
    if not clean_target:
        raise AcceptanceRefused("返工要选从哪一步开始重做")
    business = api.business
    async with _order_lock(project_id, clean_order_id):
        if business.get_order(clean_order_id, project_id=project_id) is None:
            raise AcceptanceRefused("找不到这张工单")
        current = business.acceptance(clean_order_id, project_id=project_id)
        snapshot: dict[str, Any] = {}
        frozen: _Frozen | None = None
        if current is not None and current.rework_request_id != clean_request and current.status == "rejected":
            snapshot = dict(current.artifact_snapshot or {})
            if not snapshot:
                try:
                    frozen = await _freeze(engine, project_id, clean_order_id, current.round_no, list(current.evidence))
                except Exception as exc:
                    raise _refused(exc, clean_order_id, _REWORK_TAIL) from exc
                snapshot = dict(frozen.reference) if frozen is not None else {}
        try:
            record = await asyncio.to_thread(
                business.start_acceptance_rework,
                clean_order_id,
                target_node=clean_target,
                actor=actor,
                request_id=clean_request,
                artifact_snapshot=snapshot,
                project_id=project_id,
            )
        except BaseException:
            if frozen is not None and frozen.created:
                await _discard(engine, project_id, frozen.reference)
            raise
        pending = business.pending_acceptance_rework_command(
            clean_order_id, request_id=clean_request, project_id=project_id,
        )
    if pending is not None:
        await api.dispatch_acceptance_rework(clean_order_id, request_id=clean_request, project_id=project_id)
    return record


async def export_round(api: Any, engine: Any, project_id: str, order_id: str, round_no: int) -> dict[str, Any]:
    """One frozen round as a verified ZIP (``artifact_versions.export_delivery_snapshot``)."""
    acceptance = api.acceptance(order_id, project_id=project_id)
    if acceptance is None:
        raise AcceptanceRefused("这张工单没有验收记录，没有可以导出的一轮")
    snapshot: dict[str, Any] = {}
    if int(acceptance.get("round_no", 0) or 0) == round_no:
        snapshot = dict(acceptance.get("artifact_snapshot", {}) or {})
    for raw in list(acceptance.get("history", []) or []):
        if snapshot:
            break
        item = dict(raw) if isinstance(raw, dict) else {}
        if int(item.get("round_no", 0) or 0) == round_no:
            snapshot = dict(item.get("artifact_snapshot", {}) or {})
            break
    if not snapshot:
        raise AcceptanceRefused("这一轮没有冻结下来的交付文件，不能导出")
    try:
        return await asyncio.to_thread(
            artifact_versions.export_delivery_snapshot, engine, project_id=project_id, snapshot=snapshot,
        )
    except SnapshotError as exc:
        raise _refused(exc, order_id, _EXPORT_TAIL) from exc
