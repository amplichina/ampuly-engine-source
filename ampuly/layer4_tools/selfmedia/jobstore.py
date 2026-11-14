"""发布 job 的持久状态：原子写、到期轮询、崩溃恢复。

只存编排所需的最小事实：job_id、账号分区、请求键、阶段、尝试次数、下次轮询时间。
不存正文、媒体或凭据；不替代 Effect/回执账本。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

TERMINAL = {"verified", "unverified", "login_required", "readback_error"}


@dataclass
class Job:
    job_id: str
    account_ref: str
    request_key: str
    phase: str = "submitted"
    attempts: int = 0
    next_poll_at: str = ""
    updated_at: str = ""
    error: str = ""
    evidence: dict = field(default_factory=dict)


class JobStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, rows: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(rows, handle, ensure_ascii=False, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def get(self, job_id: str) -> Job | None:
        row = self._load().get(job_id)
        return Job(**row) if row else None

    def upsert(self, job: Job, *, now_iso: str) -> Job:
        rows = self._load()
        job.updated_at = now_iso
        rows[job.job_id] = asdict(job)
        self._save(rows)
        return job

    def due(self, now_iso: str) -> list[Job]:
        return [Job(**row) for row in self._load().values()
                if row.get("phase") not in TERMINAL and str(row.get("next_poll_at", "")) <= now_iso]

    def recover(self, *, now_iso: str) -> list[Job]:
        """进程崩溃后：非终态且没有下次轮询时间的，重新排一次轮询（不重发）。"""
        rows = self._load()
        changed: list[Job] = []
        for row in rows.values():
            if row.get("phase") in TERMINAL:
                continue
            if not row.get("next_poll_at"):
                row["next_poll_at"] = now_iso
                row["attempts"] = int(row.get("attempts", 0)) + 1
                row["updated_at"] = now_iso
                changed.append(Job(**row))
        if changed:
            self._save(rows)
        return changed
