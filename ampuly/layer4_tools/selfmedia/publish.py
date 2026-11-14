#!/usr/bin/env python3
"""发布编排：适配器契约 + 幂等账本 + 四态回执（安普里自写）。

设计目标（吸纳 MatrixMedia 的账号分区与 submitted 语义，但不复制其代码）：
- 平台差异收敛到 ``PlatformAdapter`` 四个方法：preflight / submit / readback；
- 一次发布一个幂等键，提交后先记 ``submitted``，读回才写终态；
- 未知（unverified/login_required/readback_error）只查询，绝不重发；
- 账号按 ``<platform>:<account_key>`` 分区，一个分区一个登录态。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ampuly.layer4_tools.selfmedia.platform_rules import rule_for

DISCLOSURES = ("ai_generated", "fiction", "none")
PHASES = ("submitted", "verified", "unverified", "login_required", "readback_error")
_ACCOUNT = re.compile(r"^[a-z][a-z0-9_-]{1,31}:[A-Za-z0-9._@-]{1,64}$")



class PublishRefused(ValueError):
    """请求本身不合规（校验失败）；不产生任何外部动作。"""


@dataclass(frozen=True)
class PublishRequest:
    request_id: str
    account_ref: str
    platform: str
    title: str
    body: str
    media_paths: tuple[str, ...]
    disclosure: str
    scheduled_at: str = ""

    def idempotency_key(self) -> str:
        media = []
        for path in sorted(self.media_paths):
            handle = Path(path)
            digest = hashlib.sha256(handle.read_bytes()).hexdigest() if handle.is_file() else ""
            media.append(f"{handle.name}:{digest}")
        payload = [self.account_ref, self.platform, self.title, self.body, media, self.disclosure]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


@dataclass
class Receipt:
    request_id: str
    phase: str
    idempotency_key: str
    job_id: str = ""
    platform_content_id: str = ""
    url: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise ValueError(f"unknown publish phase: {self.phase!r}")

    @property
    def may_resubmit(self) -> bool:
        return False

    def as_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "phase": self.phase,
                "idempotency_key": self.idempotency_key, "job_id": self.job_id, "platform_content_id": self.platform_content_id,
                "url": self.url, "evidence": self.evidence, "error": self.error}


class PlatformAdapter(Protocol):
    def preflight(self, account_ref: str) -> dict[str, Any]: ...
    def submit(self, request: PublishRequest, *, key: str) -> dict[str, Any]: ...
    def readback(self, request: PublishRequest, *, since_ms: int) -> dict[str, Any]: ...


def validate_request(request: PublishRequest) -> None:
    if not _ACCOUNT.match(request.account_ref or ""):
        raise PublishRefused("account_ref must be <platform>:<account_key>")
    if request.account_ref.split(":", 1)[0] != request.platform:
        raise PublishRefused("account_ref platform does not match request platform")
    if request.disclosure not in DISCLOSURES:
        raise PublishRefused(f"disclosure must be one of {DISCLOSURES}")
    rule = rule_for(request.platform)
    if not request.title.strip() or len(request.title.strip()) > rule.title_max:
        raise PublishRefused(f"title invalid for {request.platform} (limit {rule.title_max})")
    if not request.body.strip() and rule.body_required:
        raise PublishRefused("body required")
    if request.disclosure not in rule.disclosures:
        raise PublishRefused(f"disclosure not supported by {request.platform}")
    if not request.media_paths:
        raise PublishRefused("media_paths required")
    for path in request.media_paths:
        handle = Path(path)
        if not handle.is_file() or handle.stat().st_size == 0:
            raise PublishRefused(f"media missing or empty: {path}")


class Ledger:
    """一次发布一个键；只追加，不改写。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def find(self, key: str) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        latest: dict[str, Any] | None = None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("idempotency_key") == key:
                latest = row  # 同一键可能先 submitted 后 verified，终态取最后一条
        return latest

    def append(self, receipt: Receipt) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt.as_dict(), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def publish_once(adapter: PlatformAdapter, request: PublishRequest, *, ledger: Ledger,
                 readback_attempts: int = 3, sleep=time.sleep) -> Receipt:
    """校验 → 查账本 → 预检 → 提交 → 读回。绝不重复提交。"""
    validate_request(request)
    key = request.idempotency_key()
    previous = ledger.find(key)
    if previous is not None:
        return Receipt(**{k: previous.get(k, "") for k in
                          ("request_id", "phase", "idempotency_key", "platform_content_id",
                           "url", "evidence", "error")})
    pre = adapter.preflight(request.account_ref)
    if not pre.get("logged_in"):
        receipt = Receipt(request.request_id, "login_required", key,
                          evidence={"preflight": pre}, error="login_required")
        ledger.append(receipt)
        return receipt
    submitted = adapter.submit(request, key=key)
    if not submitted.get("ok"):
        receipt = Receipt(request.request_id, "readback_error", key,
                          evidence={"submit": submitted}, error=str(submitted.get("error", "submit_failed")))
        ledger.append(receipt)
        return receipt
    started = int(time.time() * 1000)
    submitted_receipt = Receipt(request.request_id, "submitted", key,
                                platform_content_id=str(submitted.get("id", "")), evidence={"preflight": pre})
    ledger.append(submitted_receipt)
    last: dict[str, Any] = {}
    for attempt in range(max(1, readback_attempts)):
        last = adapter.readback(request, since_ms=started)
        phase = str(last.get("phase", "unverified"))
        if phase == "verified":
            receipt = Receipt(request.request_id, "verified", key,
                              platform_content_id=str(last.get("id", submitted.get("id", ""))),
                              url=str(last.get("url", "")),
                              evidence={"preflight": pre, "attempts": attempt + 1, "readback": last})
            ledger.append(receipt)
            return receipt
        if phase in {"login_required", "readback_error"}:
            break
        if attempt + 1 < readback_attempts:
            sleep(0)
    phase = str(last.get("phase", "unverified"))
    if phase not in {"login_required", "readback_error"}:
        phase = "unverified"
    receipt = Receipt(request.request_id, phase, key,
                      platform_content_id=str(submitted.get("id", "")),
                      evidence={"preflight": pre, "readback": last}, error=phase)
    ledger.append(receipt)
    return receipt

# ── 异步 job 模型（吸纳 MatrixMedia 的 jobId + reused 语义） ──────────────
import threading


class AccountLocks:
    """一个账号分区同一时刻只跑一个发布任务。"""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def acquire(self, account_ref: str):
        with self._guard:
            lock = self._locks.setdefault(account_ref, threading.Lock())
        return lock


_DEFAULT_LOCKS = AccountLocks()


def _receipt_from(row: dict) -> Receipt:
    return Receipt(request_id=row.get("request_id", ""), phase=row.get("phase", "unverified"),
                   idempotency_key=row.get("idempotency_key", ""),
                   job_id=row.get("job_id", ""),
                   platform_content_id=row.get("platform_content_id", ""),
                   url=row.get("url", ""), evidence=row.get("evidence") or {}, error=row.get("error", ""))


def publish_job(adapter: PlatformAdapter, request: PublishRequest, *, ledger: Ledger,
                locks: AccountLocks | None = None) -> Receipt:
    """只提交并返回 submitted + job_id；读回由 poll_job 负责。同参数复用同一 job。"""
    validate_request(request)
    key = request.idempotency_key()
    previous = ledger.find(key)
    if previous is not None:
        return _receipt_from(previous)
    lock = (locks or _DEFAULT_LOCKS).acquire(request.account_ref)
    with lock:
        pre = adapter.preflight(request.account_ref)
        if not pre.get("logged_in"):
            receipt = Receipt(request.request_id, "login_required", key, job_id=key[:16],
                              evidence={"preflight": pre}, error="login_required")
            ledger.append(receipt)
            return receipt
        submitted = adapter.submit(request, key=key)
        if not submitted.get("ok"):
            receipt = Receipt(request.request_id, "readback_error", key, job_id=key[:16],
                              evidence={"submit": submitted}, error=str(submitted.get("error", "submit_failed")))
            ledger.append(receipt)
            return receipt
        receipt = Receipt(request.request_id, "submitted", key,
                          job_id=str(submitted.get("id") or key[:16]),
                          platform_content_id=str(submitted.get("id", "")), evidence={"preflight": pre})
        ledger.append(receipt)
        return receipt


def poll_job(adapter: PlatformAdapter, request: PublishRequest, *, ledger: Ledger,
             readback_attempts: int = 3, sleep=time.sleep) -> Receipt:
    """查询一个已提交 job；终态直接返回，未终态读回。绝不重发。"""
    key = request.idempotency_key()
    previous = ledger.find(key)
    if previous is None:
        raise PublishRefused("unknown job: submit it first")
    if previous.get("phase") in {"verified", "unverified", "login_required", "readback_error"}:
        return _receipt_from(previous)
    last: dict = {}
    for attempt in range(max(1, readback_attempts)):
        last = adapter.readback(request, since_ms=0)
        if last.get("phase") == "verified":
            receipt = Receipt(request.request_id, "verified", key, job_id=previous.get("job_id", ""),
                              platform_content_id=str(last.get("id", "")), url=str(last.get("url", "")),
                              evidence={"attempts": attempt + 1, "readback": last})
            ledger.append(receipt)
            return receipt
        if last.get("phase") in {"login_required", "readback_error"}:
            break
        if attempt + 1 < readback_attempts:
            sleep(0)
    phase = str(last.get("phase", "unverified"))
    if phase not in {"login_required", "readback_error"}:
        phase = "unverified"
    receipt = Receipt(request.request_id, phase, key, job_id=previous.get("job_id", ""),
                      evidence={"readback": last}, error=phase)
    ledger.append(receipt)
    return receipt


class ScheduleQueue:
    """受管定时队列：只算到期，不常驻；由宿主按计划触发。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def add(self, request: PublishRequest, *, publish_at: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"request_id": request.request_id, "account_ref": request.account_ref,
               "platform": request.platform, "publish_at": publish_at}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def due(self, now_iso: str) -> list[dict]:
        if not self.path.is_file():
            return []
        rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [row for row in rows if str(row.get("publish_at", "")) <= now_iso]

# ── JobStore 接线与调度（claim / complete / expire） ─────────────────────
from datetime import datetime, timedelta
from ampuly.layer4_tools.selfmedia.jobstore import Job, JobStore, TERMINAL as JOB_TERMINAL


def _plus(now_iso: str, seconds: int) -> str:
    return (datetime.fromisoformat(now_iso) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def publish_job_persisted(adapter, request, *, ledger, jobs: JobStore, now_iso: str,
                          locks: AccountLocks | None = None) -> Receipt:
    """提交并把 job 落盘；同参数复用已有 job，不重发。"""
    receipt = publish_job(adapter, request, ledger=ledger, locks=locks)
    job = Job(job_id=receipt.job_id or receipt.idempotency_key[:16],
              account_ref=request.account_ref, request_key=receipt.idempotency_key,
              phase=receipt.phase, attempts=1 if receipt.phase == "submitted" else 0,
              next_poll_at="" if receipt.phase in JOB_TERMINAL else now_iso)
    jobs.upsert(job, now_iso=now_iso)
    return receipt


def poll_job_persisted(adapter, request, *, ledger, jobs: JobStore, now_iso: str,
                       retry_after_s: int = 60, readback_attempts: int = 3) -> Receipt:
    """轮询并把结果写回 job；终态清空下次轮询时间。"""
    receipt = poll_job(adapter, request, ledger=ledger, readback_attempts=readback_attempts)
    job = jobs.get(receipt.job_id or receipt.idempotency_key[:16])
    if job is None:
        job = Job(receipt.job_id or receipt.idempotency_key[:16], request.account_ref,
                  receipt.idempotency_key, phase=receipt.phase)
    job.phase = receipt.phase
    job.attempts = int(job.attempts) + 1
    job.error = receipt.error
    job.next_poll_at = "" if receipt.phase in JOB_TERMINAL else _plus(now_iso, retry_after_s)
    jobs.upsert(job, now_iso=now_iso)
    return receipt


class Scheduler:
    """到期领取、完成写回、超次过期；不常驻，由宿主周期调用。"""

    def __init__(self, jobs: JobStore, *, max_attempts: int = 5) -> None:
        self.jobs = jobs
        self.max_attempts = max_attempts

    def claim_due(self, now_iso: str) -> list[Job]:
        return self.jobs.due(now_iso)

    def complete(self, job_id: str, phase: str, *, now_iso: str) -> Job | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        job.phase = phase
        job.next_poll_at = "" if phase in JOB_TERMINAL else _plus(now_iso, 60)
        return self.jobs.upsert(job, now_iso=now_iso)

    def expire(self, *, now_iso: str) -> list[Job]:
        expired: list[Job] = []
        for job in self.jobs.due(now_iso):
            if int(job.attempts) >= self.max_attempts:
                job.phase = "readback_error"
                job.error = "poll_attempts_exceeded"
                job.next_poll_at = ""
                expired.append(self.jobs.upsert(job, now_iso=now_iso))
        return expired
