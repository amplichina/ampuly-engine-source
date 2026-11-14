"""总控施工模式 (policy, B1): one build job works freely inside a DRAFT of a business line.

The owner says "build me a line" or "change this step of the VI line" in the master conversation; master control opens
a build job (``master_start_build``). The job clones the line into a draft of this project's own (or starts a blank
one), and a worker -- the product's own ``NativeAgent`` behind the Harness, the same executor an order's AI step runs
on -- edits the draft's skills, steps, team setup and tests, checks them and tries them offline, then hands over a
candidate. Going live, sending, paying and deleting stay the owner's; the design is the module contract.

What this module is careful about, each because the easy version is quietly wrong:

**The worker is fenced by what it is given, not by what it is told.** Its tool registry holds exactly the worker's
file, process, todo, web and read-only sub-helper tools plus the five build tools, and nothing else: no browser, no
image generation, no git, no collaboration tools, no MCP, and none of the promotion calls (record_test_run,
submit_for_testing, request_release_approval, decide_release_approval, publish_package, rollback_package,
install_package). File tools are fenced to the draft (``workspace_root``); a shell runs confined; the network and a
path outside the draft are ASKED through the engine's own approval engine, which is required -- a build turn without
it refuses to start.

**The approval level is pinned to "auto" for the build session**, whatever the global setting, and a stricter one
wins (the per-root-session mechanism of ``NativePermissionPolicyResolver``).

**Every model call is on one run budget per job** (``model_call`` from the setting, default 300), and a turn is
bounded (150 steps; 4 hours of wall clock). A stop is reported, never retried.

**The console is reached through a small host** (:class:`BuildHost`): this module imports nothing from the console's
handler. Messages go into the 总控 conversation the job was started from (``BuildJob.thread_id``; the project's first
conversation ``master:{pid}`` for a job recorded before conversations were split) with ``metadata.master_build``; the
one ack is ``master_build_activity``.

**A job record is a small JSON file** under the project's own folder (``<ampuly_home>/projects/<pid>/build_jobs/``),
written atomically; deleting a project removes it with the rest of ``projects/<pid>`` and a project backup carries it
(``core/data_map.py`` ``project_dir``). No database table is added.

**A job never brings a deleted project back.** A job's first record is written only into a project folder that
exists, and every later write needs that record still in place (:func:`save_job`, :func:`job_is_home`). A job whose
project was deleted (or renamed) under it -- running, or waiting on a question -- is stopped the moment the runner
notices (its next tool call, the question watcher's next look, its next record or message), and after that nothing is
written or said for it: no record, no message into the gone project's conversation.

**A build's question is answered only while the build's own turn waits for it.** Taken in any other time -- after a
console restart, or once the turn that asked has ended -- the engine's own recovery would resume the build's task on its
general executor, outside the fence. So a turn ends by closing what it left open, a recovery closes what a stopped
console left (at the console's start, and again until it could), a turn closes what earlier turns left before it starts,
and the console refuses (and closes) an answer to a build's question no turn here waits for
(:meth:`MasterBuildRunner.refuse_left_answer`).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Protocol

from loguru import logger

from ampuly.core.config import replace_with_retry
from ampuly.layer3_agent.native_agent import NativeAgent

# ── what a build job may be, and what bounds it ──────────────────────────────────────────────────────────────────

STATES = ("running", "waiting_owner", "delivered", "stopped", "failed", "interrupted", "capped")
LIVE_STATES = frozenset({"running", "waiting_owner"})
CONTINUABLE_STATES = frozenset({"delivered", "stopped", "interrupted", "capped", "failed"})
STATE_ZH = {
    "running": "在干",
    "waiting_owner": "等你回话",
    "delivered": "交了候选版本",
    "stopped": "停下了",
    "failed": "出错停了",
    "interrupted": "中断了",
    "capped": "用完了模型调用次数",
}

MAX_LIVE_JOBS_PER_MACHINE = 2
DEFAULT_MODEL_CALLS = 300
MAX_TURN_ITERATIONS = 150
TURN_WALL_CLOCK_SEC = 4 * 3600
CHECKPOINT_POLL_SEC = 1.0
PROGRESS_MIN_INTERVAL_SEC = 60.0
PROGRESS_MAX_CHARS = 300
MAX_GOAL_CHARS = 4000
MAX_SNAPSHOT_FILES = 5000
# Refused tool calls after the hand-over before the runner stops the turn itself.
POST_DELIVERY_REFUSALS = 2
# The same tool call (name and arguments) this many times in a row is refused instead of run.
REPEATED_CALL_LIMIT = 3

NOTES_FILE = "BUILD-NOTES.md"
NOTES_STARTER = "# 施工笔记\n\n（这条业务线还没有施工记录。每次施工开工先读这里，交付前把做了什么、为什么、还剩什么写回来。）\n"

# The worker's own tools, by the names the engine registers them under. Nothing else of the engine's registry is
# copied in: that absence, not an instruction, is what keeps the browser, image generation, git, the collaboration
# tools and MCP out of reach. request_user_input is not here either: it ends a turn waiting for an answer the build
# job cannot consume (the engine's own resume path would pick it up, not this job), so a build lists what it needs to
# know in its hand-over (open_questions) instead.
WORKER_TOOLS: tuple[str, ...] = (
    "file_read", "file_write", "file_edit", "apply_patch", "grep", "glob", "file_search", "list_dir",
    "shell_exec", "python_exec",
    "todo_write", "todo_read",
    "web_search", "web_fetch",
    "agent_spawn", "agent_wait", "agent_send", "agent_list",
)
BUILD_TOOLS: tuple[str, ...] = (
    "build_line_overview", "build_check_draft", "build_offline_trial", "build_progress", "build_deliver",
)
# A sub-helper that only reads, plans or verifies. "implement" works in an isolated copy that is thrown away without
# being merged back, so its work would be lost; "general" is implement by another name.
READ_ONLY_SUBAGENT_PROFILES = frozenset({"explore", "plan", "verify"})
# The promotion road stays the owner's, on the builder. None of these is a tool of the build agent.
PROMOTION_CALLS: tuple[str, ...] = (
    "record_test_run", "submit_for_testing", "request_release_approval", "decide_release_approval",
    "publish_package", "rollback_package", "install_package",
)
# Skipped when a draft's files are listed and compared: the runtime's own folders, never a change the owner made.
_SNAPSHOT_SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", ".releases"})

# What a step is, in the owner's words (the raw tool name travels beside it for the page's data attribute only).
STEP_ZH = {
    "file_read": "读文件", "file_write": "写文件", "file_edit": "改文件", "apply_patch": "改文件",
    "grep": "查找内容", "glob": "找文件", "file_search": "找文件", "list_dir": "看目录",
    "shell_exec": "跑命令", "python_exec": "跑 Python",
    "todo_write": "排待办", "todo_read": "看待办",
    "web_search": "上网搜索", "web_fetch": "读网页",
    "agent_spawn": "派子助手", "agent_wait": "等子助手", "agent_send": "给子助手补一句", "agent_list": "看子助手",
    "build_line_overview": "看在用的业务线", "build_check_draft": "检查草稿", "build_offline_trial": "离线试跑草稿",
    "build_progress": "报进度", "build_deliver": "交付",
}

_JOB_ID = re.compile(r"^bj[0-9a-f]{12}$")
_DRAFT_ID = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
# A build's task and session are ``build-<job id>``; a sub-helper's session sits under it (``build-<job id>:...``).
_BUILD_SESSION = re.compile(r"^build-(bj[0-9a-f]{12})(?::.*)?$")


class BuildRefused(RuntimeError):
    """A build did not start (or continue), and why: ``zh`` for the owner, the message for the log and the model."""

    def __init__(self, zh: str, en: str = "", *, code: str = "refused") -> None:
        super().__init__(en or zh)
        self.zh = str(zh)
        self.en = str(en or zh)
        self.code = str(code or "refused")


@dataclass
class BuildAnswer:
    """What start / continue answer: whether it happened, the owner's sentence, and the job."""

    ok: bool
    zh: str
    en: str
    job: dict[str, Any] | None = None
    code: str = ""


# ── the record ────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class BuildJob:
    job_id: str
    project_id: str
    goal: str
    name: str = ""
    source_id: str = ""          # the line it started from ("" for a new line)
    source_version: str = ""
    draft_id: str = ""
    draft_dir: str = ""
    session_id: str = ""         # the Harness session every turn of this job runs on
    turn_id: str = ""            # the turn running now ("" between turns)
    state: str = "running"
    model: str = ""              # what the build's calls are sent with ("" = the engine's default)
    budget_id: str = ""
    model_call_cap: int = DEFAULT_MODEL_CALLS
    created_at: str = ""
    updated_at: str = ""
    turns: int = 0
    delivery: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""         # in the owner's words
    stopped_by: str = ""         # owner | clock | steps | ended | "" -- why a stopped job stopped
    snapshot: dict[str, str] = field(default_factory=dict)   # draft files at the start: path -> sha256
    snapshot_truncated: bool = False
    checkpoint: dict[str, Any] = field(default_factory=dict)  # the owner question it waits on, while waiting_owner
    # 总控对话栏 (policy, 第七节): the 总控 conversation the build was started from -- the console session id of
    # the turn that called master_start_build. Every message of the job goes into that conversation, whoever continues
    # it; "" (a job recorded before conversations were split) is the project's first conversation.
    thread_id: str = ""

    @property
    def task_id(self) -> str:
        return f"build-{self.job_id}"

    @property
    def task_session_id(self) -> str:
        return f"build-{self.job_id}"

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "BuildJob":
        known = {name for name in cls.__dataclass_fields__}
        data = {key: value for key, value in dict(raw or {}).items() if key in known}
        job = cls(**data)
        if job.state not in STATES:
            job.state = "failed"
        return job

    def card(self) -> dict[str, Any]:
        """``metadata.master_build`` on a message: what the page's build card needs, and ids only as data."""
        card: dict[str, Any] = {
            "job_id": self.job_id,
            "state": self.state,
            "state_zh": STATE_ZH.get(self.state, "状态不明"),
            "goal": self.goal,
            "name": self.name,
            "draft_id": self.draft_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "thread_id": self.thread_id,
        }
        if self.state == "waiting_owner" and self.checkpoint:
            card["checkpoint"] = dict(self.checkpoint)
        if self.state == "delivered" and self.delivery:
            card["delivery"] = dict(self.delivery)
        if self.last_error:
            card["last_error"] = self.last_error
        return card

    def summary(self) -> dict[str, Any]:
        """What master control reads (master_list_builds): the state in words, never a raw code for the owner.

        ``thread_id`` is the conversation the job belongs to; the console turns it into words for the model
        (``master_loop``: this conversation or another one, by its title) before the model reads the row.
        """
        return {
            "job_id": self.job_id,
            "thread_id": self.thread_id,
            "state_zh": STATE_ZH.get(self.state, "状态不明"),
            "can_continue": self.state in CONTINUABLE_STATES,
            "goal": self.goal,
            "name": self.name,
            "draft_id": self.draft_id,
            "from_line": self.source_id,
            "turns": self.turns,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            **({"handed_over": {
                "summary": str(self.delivery.get("summary", "") or ""),
                "changed_files": len(list(self.delivery.get("changed_files") or [])),
                "open_questions": list(self.delivery.get("open_questions") or []),
                "tools_to_develop": list(self.delivery.get("tools_to_develop") or []),
            }} if self.delivery else {}),
            **({"last_error": self.last_error} if self.last_error else {}),
        }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _valid_project_id(project_id: str) -> bool:
    from ampuly.project_id import is_valid_project_id

    return is_valid_project_id(str(project_id or ""))


def jobs_root(ampuly_home: Path | str, project_id: str) -> Path:
    """The project's own build-job folder. The project id is checked before it becomes part of a path."""
    pid = str(project_id or "").strip()
    if not _valid_project_id(pid):
        raise BuildRefused("项目编号不对，施工任务没有记下来。", f"invalid project id {pid!r}", code="invalid_project")
    return Path(ampuly_home) / "projects" / pid / "build_jobs"


def job_path(ampuly_home: Path | str, project_id: str, job_id: str) -> Path:
    if not _JOB_ID.match(str(job_id or "")):
        raise BuildRefused("找不到这个施工任务。", f"not a build job id: {job_id!r}", code="unknown_job")
    return jobs_root(ampuly_home, project_id) / f"{job_id}.json"


def project_is_here(ampuly_home: Path | str, project_id: str) -> bool:
    """Whether the project still has its folder (``<ampuly_home>/projects/<pid>``), the way the project service asks it.

    The default project is never deleted and its folder may not have been made yet, so it is always here.
    """
    pid = str(project_id or "").strip()
    if pid == "default":
        return True
    if not _valid_project_id(pid):
        return False
    return (Path(ampuly_home) / "projects" / pid).is_dir()


def job_is_home(ampuly_home: Path | str, job: BuildJob) -> bool:
    """Whether this job's record is still where it was written.

    False means its project was deleted (or renamed) under it: deleting a project removes ``projects/<pid>`` and the
    record with it, and a project made again later with the same id does not have it. Only a definite "not there"
    counts -- a look Windows refuses while the record is being replaced is not a deleted project.
    """
    try:
        path = job_path(ampuly_home, job.project_id, job.job_id)
    except BuildRefused:
        return False
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


def build_job_of(checkpoint: Any) -> str:
    """The build job an owner question was asked for, or "" for any other question.

    Told by where it was asked: the build's own session or task (``build-<job id>``), a sub-helper's session under it,
    or the root session the approval engine recorded as the question's owner.
    """
    payload = getattr(checkpoint, "payload", None)
    interaction = payload.get("interaction") if isinstance(payload, dict) else None
    ownership = interaction.get("ownership") if isinstance(interaction, dict) else None
    ownership = ownership if isinstance(ownership, dict) else {}
    for raw in (getattr(checkpoint, "session_id", ""), getattr(checkpoint, "task_id", ""),
                ownership.get("root_session_id"), ownership.get("waiting_task_id")):
        match = _BUILD_SESSION.match(str(raw or "").strip())
        if match:
            return match.group(1)
    return ""


def save_job(ampuly_home: Path | str, job: BuildJob, *, create: bool = False) -> bool:
    """Write one record atomically: bytes to a temporary file beside it, then a bounded replace (Windows-safe).

    It never brings a deleted project back. ``create`` (a job's first record) writes only into a project folder that
    exists, making ``build_jobs`` and nothing above it (the default project excepted, see :func:`project_is_here`).
    Every later write needs the record still in place. A project that is gone gets nothing, and the answer is False.
    """
    path = job_path(ampuly_home, job.project_id, job.job_id)
    if create:
        if not project_is_here(ampuly_home, job.project_id):
            return False
        try:
            path.parent.mkdir(parents=job.project_id == "default", exist_ok=True)
        except (FileNotFoundError, NotADirectoryError):
            return False
    elif not job_is_home(ampuly_home, job):
        return False
    data = json.dumps(job.to_payload(), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    temporary = path.parent / f".{job.job_id}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        try:
            with open(temporary, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            replace_with_retry(temporary, path)
        except (FileNotFoundError, NotADirectoryError):
            # The folder went between the look and the write: the project was deleted just now.
            return False
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    return True


def load_job(ampuly_home: Path | str, project_id: str, job_id: str) -> BuildJob | None:
    path = job_path(ampuly_home, project_id, job_id)
    raw: Any = None
    for attempt in range(5):
        try:
            raw = json.loads(path.read_bytes().decode("utf-8"))
            break
        except FileNotFoundError:
            return None
        except PermissionError:
            # Windows refuses a read while the record is being replaced; the replace takes milliseconds.
            if attempt == 4:
                return None
            time.sleep(0.02)
        except (OSError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    job = BuildJob.from_payload(raw)
    if job.project_id != project_id or job.job_id != job_id:
        return None
    return job


def list_jobs(ampuly_home: Path | str, project_id: str) -> list[BuildJob]:
    """This project's jobs, newest first. One folder, one level: never a walk."""
    root = jobs_root(ampuly_home, project_id)
    if not root.is_dir():
        return []
    jobs: list[BuildJob] = []
    for entry in sorted(root.glob("bj*.json"))[:500]:
        job = load_job(ampuly_home, project_id, entry.stem)
        if job is not None:
            jobs.append(job)
    jobs.sort(key=lambda job: job.created_at, reverse=True)
    return jobs


def projects_with_jobs(ampuly_home: Path | str) -> list[str]:
    """Projects that have a build-job folder: the projects folder read one level down, bounded."""
    base = Path(ampuly_home) / "projects"
    if not base.is_dir():
        return []
    found: list[str] = []
    for child in sorted(base.iterdir())[:1000]:
        if child.is_dir() and _valid_project_id(child.name) and (child / "build_jobs").is_dir():
            found.append(child.name)
    return found


# ── the draft's files, before and after ─────────────────────────────────────────────────────────────────────────

def _is_redirect(entry: os.DirEntry[str]) -> bool:
    try:
        if entry.is_symlink():
            return True
        is_junction = getattr(entry, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attributes & 0x400)
    except OSError:
        return True


def snapshot_files(root: Path | str) -> tuple[dict[str, str], bool]:
    """(relative path -> sha256, truncated) for the real files in a draft. Links are not followed."""
    base = Path(root)
    found: dict[str, str] = {}
    truncated = False
    stack = [base]
    while stack:
        here = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if _is_redirect(entry):
                continue
            if entry.is_dir(follow_symlinks=False):
                if name in _SNAPSHOT_SKIP_DIRS or name.startswith(".ampuly"):
                    continue
                stack.append(Path(entry.path))
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            if len(found) >= MAX_SNAPSHOT_FILES:
                truncated = True
                continue
            try:
                digest = hashlib.sha256(Path(entry.path).read_bytes()).hexdigest()
            except OSError:
                continue
            found[Path(entry.path).relative_to(base).as_posix()] = digest
    return found, truncated


def changed_files(before: dict[str, str], after: dict[str, str]) -> list[dict[str, str]]:
    """What changed between two snapshots: added, changed or removed, by relative path."""
    changes: list[dict[str, str]] = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            changes.append({"path": path, "change": "added", "change_zh": "新增"})
        elif path not in after:
            changes.append({"path": path, "change": "removed", "change_zh": "删掉"})
        elif before[path] != after[path]:
            changes.append({"path": path, "change": "modified", "change_zh": "改了"})
    return changes


# ── the console, as a build sees it ─────────────────────────────────────────────────────────────────────────────

class BuildHost(Protocol):
    """What a build job needs from the console. The console implements it (ws_handler); a test implements it too."""

    def ampuly_home(self) -> Path:
        """The home the console's workflows/ and projects/ live in (the engine's own home)."""
        ...

    def packages(self) -> list[Any]:
        """The workflow packages the console knows, read now."""
        ...

    def business(self) -> Any:
        """The business store: run budgets, orders."""
        ...

    async def engine(self, project_id: str) -> Any:
        """The project's engine: its model, store, harness registry, approval engine and memory."""
        ...

    def team_org(self, team: str) -> tuple[str, Path | None]:
        """(team, org config path) a new line runs on: the named saved team, or the only one; (\"\", None) otherwise."""
        ...

    async def post(self, project_id: str, text: str, metadata: dict[str, Any], *, thread_id: str = "") -> None:
        """One message from 安安 into the project's 总控 conversation ``thread_id`` (its first one, ``master:{pid}``,
        when "" or when that conversation cannot be found), broadcast as a session message. A job with no
        conversation (recorded before conversations were split) calls it without ``thread_id``."""
        ...

    async def activity(self, ws: Any, fields: dict[str, Any]) -> None:
        """The ``master_build_activity`` ack to the socket that started or continued the job."""
        ...

    # Optional -- a host without it is left alone: ``async threads_changed(ws, project_id)``. A job's state changed, and
    # the project's 总控 conversation column (总控对话栏: which conversation has a build working or waiting on the
    # owner) is sent again to the socket that started or continued the job.

    def spawn(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        """A background task the console owns (tracked, cancelled at shutdown)."""
        ...

    def settings(self) -> dict[str, Any]:
        """{"model": str, "model_calls": int}: 施工用模型 and 施工单个任务最多模型调用次数."""
        ...


# ── the worker ───────────────────────────────────────────────────────────────────────────────────────────────────

def build_rules(job: BuildJob) -> str:
    """The build's own rules, in Chinese and English; they ride on the worker's role, after the product's contracts."""
    name = job.name or job.draft_id
    return (
        "## 施工模式规则\n"
        f"1. 你在给老板施工：老板的目标是「{job.goal}」。你的工作目录就是业务线「{name}」的草稿，只在这里读写；"
        "正在用的业务线、已上线的包、distribution/ 目录和工作目录以外的文件都不许动。\n"
        f"2. 开工先读草稿里的 {NOTES_FILE}（施工笔记）；交付前把这次做了什么、为什么、还剩什么写回去。\n"
        "3. 绝不尝试上线、对外发送、付费、删除。上线只能老板在搭建台自己点。上网、跑沙箱拦不住的命令、动草稿以外的文件，"
        "都会停下来问老板，问了就等答复，老板说不行就换别的办法。\n"
        "4. 需要改引擎源码才能做的代码工具不要写，在交付里列成「需要开发的工具」（tools_to_develop）。\n"
        "5. build_line_overview 看在用的业务线；build_check_draft 检查草稿（包问题、预检、技能契约测试）；"
        "build_offline_trial 在隔离环境里离线试跑整个草稿（会花模型调用次数）。离线试跑只算模拟证据，不要说成「真实测试通过」。\n"
        "6. 到关键节点用 build_progress 报一句进度（一分钟最多一次）。做完用 build_deliver 交付：写清改了什么、"
        "还要老板拿主意的事（open_questions）；交付之后这一轮就结束，不要再调用工具。\n"
        "7. 子助手只能派只读的（explore、plan、verify）。\n"
        "8. 目标要改的文件在草稿里不存在，就直接新建它，不要反复去找。草稿里以 .ampuly 开头的文件夹是系统自己用的，不用看也不用管。\n"
        "9. 同一个办法试了两三次还不行就换办法；实在做不下去，就用 build_deliver 交付，写清卡在哪、需要老板定什么。"
        "同一条命令连续重复三次会被拦下。\n"
        "## Build mode rules\n"
        "1. You build for the owner. Work only inside your working directory, which is a draft of the business line; "
        "never touch the line in use, a published package, distribution/ or anything outside the draft.\n"
        f"2. Read {NOTES_FILE} in the draft first and update it before you hand over.\n"
        "3. Never try to publish, send, pay or delete. Going online, a command the sandbox cannot hold and a path "
        "outside the draft are asked of the owner; wait for the answer and take no for an answer.\n"
        "4. A code tool that would need the engine's source is listed under tools_to_develop, not written.\n"
        "5. Your offline trial is simulated evidence only; never call it a real test.\n"
        "6. Report progress at milestones with build_progress (at most once a minute) and finish with build_deliver; "
        "after the hand-over the turn ends.\n"
        "7. Sub-helpers may only be explore, plan or verify.\n"
        "8. A file the goal names that the draft does not have: create it, do not keep searching. Folders starting "
        "with .ampuly are the system's own; leave them alone.\n"
        "9. After two or three failed tries change approach; if you cannot go on, hand over with build_deliver and "
        "say where it is stuck. The same call repeated three times in a row is refused.\n"
    )


def build_role(job: BuildJob, tool_names: list[str]) -> Any:
    from ampuly.core.models import AgentInfo

    return AgentInfo(
        role_id="master_builder",
        name="施工",
        responsibility=(
            "在一条业务线的草稿里施工：改技能、节点、组织配置和测试，检查、离线试跑，交一个候选版本给老板。"
            " Build inside a draft of one business line and hand over a candidate."
        ),
        tools=list(tool_names),
        prompt_refs=[build_rules(job)],
    )


class _BuildAgent(NativeAgent):
    """The product's worker, refusing to run without the approval engine that asks the owner."""

    async def execute(self, task: Any, on_progress: Any = None) -> Any:
        if self.permission_policy is None or self.approval_callback is None:
            raise BuildRefused(
                "施工必须经过权限审批才能动手，这一轮没有开始。",
                "a build turn needs the engine's permission policy and approval callback; it did not start",
                code="no_permission_policy",
            )
        return await super().execute(task, on_progress=on_progress)


def _fence(agent: Any, hook: Callable[[Any], Awaitable[dict[str, Any] | None]]) -> Any:
    """Put the build's pre-tool hook on this agent and on every sub-helper it makes, however deep."""
    loop = getattr(agent, "loop", None)
    if loop is None:
        return agent
    loop.register_pre_tool_hook("master_build_fence", hook)
    original = getattr(loop, "child_agent_factory", None)
    if callable(original):
        def factory(*args: Any, **kwargs: Any) -> Any:
            return _fence(original(*args, **kwargs), hook)

        loop.child_agent_factory = factory
    return agent


def make_build_agent(engine: Any, job: BuildJob, llm: Any, registry: Any, hook: Any) -> Any:
    """A ``NativeAgent`` built as ``engine._run_native_agent`` builds one, for this job.

    ``permission_policy`` and ``approval_callback`` are the engine's, always; an engine without them gets no build.
    """
    policy = getattr(engine, "approval_engine", None)
    callback = getattr(engine, "_tool_approval_callback", None)
    if policy is None or not callable(callback):
        raise BuildRefused(
            "这个引擎没有权限审批，施工不能动手，没有开工。",
            "the engine has no approval engine / approval callback; a build does not run without them",
            code="no_permission_policy",
        )
    parts_zh = {"memory": "记忆", "preferences": "偏好设置", "skills": "技能库", "context_assembler": "上下文组装",
                "event_bus": "事件通道"}
    for part, part_zh in parts_zh.items():
        if getattr(engine, part, None) is None:
            raise BuildRefused(f"引擎还没准备好（{part_zh}还没起来），施工没有开工。", f"engine has no {part}",
                               code="engine_not_ready")
    config = copy.deepcopy(getattr(engine, "config", None))
    if config is None:
        from ampuly.core.config import AmpulyConfig

        config = AmpulyConfig()
    config.system.max_agent_iterations = MAX_TURN_ITERATIONS
    tool_names = [tool.name for tool in registry.list_tools()]
    agent = _BuildAgent(
        role=build_role(job, tool_names),
        llm=llm,
        tool_registry=registry,
        context_assembler=engine.context_assembler,
        memory=engine.memory,
        preferences=engine.preferences,
        skills=engine.skills,
        event_bus=engine.event_bus,
        cost_tracker=getattr(engine, "cost_tracker", None),
        config=config,
        communication=getattr(engine, "communication", None),
        approval_callback=callback,
        permission_policy=policy,
        interaction_coordinator=getattr(engine, "interaction_coordinator", None),
        external_outcome_unknown=getattr(engine, "_external_effect_outcome_unknown", None),
    )
    return _fence(agent, hook)


def build_worker_registry(engine: Any, binding: Any) -> Any:
    """Exactly the worker tools the engine has plus the five build tools; nothing else of the engine's registry."""
    from ampuly.layer4_tools.registry import ToolRegistry
    from ampuly.plugins.office_ui.master_build_tools import create_build_tools

    source = getattr(engine, "tool_registry", None)
    registry = ToolRegistry()
    for name in WORKER_TOOLS:
        tool = source.get(name) if source is not None else None
        if tool is not None:
            registry.register(tool)
    for tool in create_build_tools(binding):
        registry.register(tool)
    return registry


def pin_approval_level(engine: Any, task: Any) -> str:
    """This build session runs at "auto" -- or stricter, when the setting or this session was made stricter."""
    rank = {"read-only": 0, "auto": 1, "full-access": 2}
    scope = str(getattr(task, "session_id", "") or "")
    levels = ["auto"]
    policy = getattr(getattr(engine, "approval_engine", None), "native_policy", None)
    if policy is not None:
        levels.append(str(policy.default_level))
        probe = SimpleNamespace(metadata={"native_permission_scope_id": scope}, session_id=scope, id=scope)
        levels.append(str(policy.context_for_task(probe).level))
    level = min((item for item in levels if item in rank), key=rank.__getitem__)
    task.metadata["native_approval_level"] = level
    task.metadata["native_permission_scope_id"] = scope
    if policy is not None:
        policy.set_session_level(scope, level)
    return level


def build_llm(engine: Any, model: str) -> tuple[Any, str]:
    """(provider, model name) the build's calls go to: 施工用模型 when set, else the engine's default."""
    from ampuly.llm.budget import RouteRefused

    llm = getattr(engine, "llm", None)
    if llm is None:
        raise BuildRefused("引擎还没有可用的模型，施工没有开工。", "engine has no model", code="no_model")
    default = str(getattr(getattr(llm, "config", None), "default_model", "") or "")
    chosen = str(model or "").strip()
    if not chosen or chosen == default:
        return llm, default
    for_model = getattr(llm, "for_model", None)
    if not callable(for_model):
        raise BuildRefused("这个引擎换不了模型，施工没有开工。", "provider has no for_model", code="no_model")
    try:
        return for_model(chosen, customer_id="", environment_id=""), chosen
    except RouteRefused as exc:
        raise BuildRefused(
            f"「施工用模型」设的是 {chosen}，它现在用不了（多半是密钥还没设好），施工没有开工，也没有花钱。"
            "请到「配置 → 模型」检查它的密钥，或者把「施工用模型」清空、用默认模型。",
            f"build model {chosen!r} refused before any call: {exc}",
            code="model_refused",
        ) from exc


def _plain(text: Any, limit: int = 300) -> str:
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _chinese(text: Any) -> bool:
    clean = str(text or "")
    return any("一" <= ch <= "鿿" for ch in clean)


def question_words(checkpoint: Any) -> str:
    """The owner question a tool_permission checkpoint asks, in plain words (no tool names, no ids)."""
    payload = dict(getattr(checkpoint, "payload", {}) or {})
    call = dict(payload.get("tool_call", {}) or {})
    name = str(call.get("name", "") or "")
    args = dict(call.get("arguments", {}) or {})
    rationale = str(dict(payload.get("approval", {}) or {}).get("rationale", "") or "").strip()
    if name == "web_fetch":
        said = f"施工想上网读一个网页：{_plain(args.get('url'), 200)}。"
    elif name == "web_search":
        said = f"施工想上网搜一下：{_plain(args.get('query'), 200)}。"
    elif name == "shell_exec":
        said = f"施工想跑一个命令：{_plain(args.get('command') or args.get('cmd'), 300)}。"
    elif name == "python_exec":
        said = "施工想运行一段 Python 代码。"
    elif name in {"file_read", "file_write", "file_edit", "apply_patch", "grep", "glob", "file_search", "list_dir"}:
        target = args.get("path") or args.get("file_path") or args.get("directory") or ""
        said = f"施工想动草稿以外的文件：{_plain(target, 200)}。" if target else "施工想动草稿以外的文件。"
    else:
        said = "施工想做一件需要你点头的事。"
    if rationale and _chinese(rationale):
        said += f"为什么要问你：{_plain(rationale, 200).rstrip('。')}。"
    return said + "允许吗？"


# The three answers the owner is offered, by the option ids the approval engine uses.
_ANSWER_ZH = {"approve_once": "允许一次", "approve_session": "这次施工都允许", "deny": "不行"}


def checkpoint_card(checkpoint: Any) -> dict[str, Any]:
    """What the build card needs to answer this question through ``interaction_reply`` (the existing action)."""
    payload = dict(getattr(checkpoint, "payload", {}) or {})
    offered = [
        str(option.get("id", "") or "")
        for option in list(dict(payload.get("interaction", {}) or {}).get("options", []) or [])
        if isinstance(option, dict)
    ]
    options = [{"id": option, "label": _ANSWER_ZH[option]} for option in ("approve_once", "approve_session", "deny")
               if option in offered]
    # The identity the engine checks an answer against (AmpulyEngine.can_answer_checkpoint): the anchors the approval
    # recorded on the question, else the task and session that asked.
    ownership = dict(dict(payload.get("interaction", {}) or {}).get("ownership", {}) or {})
    task_id = str(ownership.get("ui_anchor_task_id") or ownership.get("waiting_task_id")
                  or getattr(checkpoint, "task_id", "") or "")
    session_id = str(ownership.get("ui_anchor_session_id") or ownership.get("root_session_id")
                     or getattr(checkpoint, "session_id", "") or "")
    return {
        "checkpoint_id": str(getattr(checkpoint, "checkpoint_id", "") or ""),
        "checkpoint_type": str(getattr(checkpoint, "checkpoint_type", "") or ""),
        "requester_task_id": task_id,
        "requester_session_id": session_id,
        "question": question_words(checkpoint),
        "options": options,
        "answer_action": "interaction_reply",
    }


# ── one job while it runs: what its tools reach ─────────────────────────────────────────────────────────────────

class JobBinding:
    """One job's running turn, as its build tools see it. Built per turn."""

    def __init__(self, runner: "MasterBuildRunner", job: BuildJob, *, engine: Any, llm: Any, ws: Any = None) -> None:
        self.runner = runner
        self.job = job
        self.engine = engine
        self.llm = llm
        self.ws = ws
        self.delivered = False
        self.post_delivery_refusals = 0
        # Asks the turn to stop the way the owner's 停下 does; True when the request could be made.
        self.stop_requested: Callable[[], Awaitable[bool]] | None = None
        # The project was deleted under the turn: tool calls refused since, and whether the turn was asked to stop.
        self.gone_refusals = 0
        self.gone_stop_asked = False
        self._last_progress = 0.0
        self._sequence = 0
        # The same tool with the same arguments, back to back ( live check: a build ran one PowerShell count
        # of .ampuly-tmp dozens of times, every 4 seconds). The third identical call in a row is refused, not run.
        self.last_call = ""
        self.repeats = 0

    # the five build tools

    async def line_overview(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.runner.line_overview, self.job)

    async def check_draft(self) -> dict[str, Any]:
        return await asyncio.to_thread(check_draft, Path(self.job.draft_dir))

    async def offline_trial(self) -> dict[str, Any]:
        return await self.runner.offline_trial(self.job, self.llm)

    async def progress(self, text: str) -> dict[str, Any]:
        clean = _plain(text, PROGRESS_MAX_CHARS)
        if not clean:
            return {"posted": False, "reason": "进度是空的，没发。"}
        now = time.monotonic()
        if self._last_progress and now - self._last_progress < PROGRESS_MIN_INTERVAL_SEC:
            return {"posted": False, "reason": "一分钟内只报一次进度，这次没发；接着干，下一个节点再报。"}
        self._last_progress = now
        await self.runner.post(self.job, f"🏗 施工进度：{clean}", extra={"progress": clean})
        return {"posted": True}

    async def deliver(self, summary: str, open_questions: list[str], tools_to_develop: list[str]) -> dict[str, Any]:
        if self.delivered:
            return {"delivered": True, "note": "这一轮已经交付过了；结束这一轮，不要再调用工具。"}
        return await self.runner.deliver(self, summary, open_questions, tools_to_develop)

    # what the fence reports

    async def note_step(self, tool_name: str) -> None:
        self._sequence += 1
        await self.runner.activity(self, tool_name=tool_name, sequence=self._sequence)

    async def note_state(self) -> None:
        """A turn that started, a question asked or answered: the same ack, no tool, the state in words."""
        await self.note_step("")


def _placeholder_fields(package: Any) -> dict[str, str]:
    return {str(spec.get("id", "")): "（施工检查示例）" for spec in list(getattr(package, "intake_fields", []) or [])
            if str(spec.get("id", "") or "")}


def check_draft(draft_dir: Path) -> dict[str, Any]:
    """Package problems, the builder preflight and the skill contract tests of the draft as it is on disk now."""
    from ampuly.plugins.office_ui.workflow_packages import load_package, preflight_invocation, run_package_skill_tests

    package = load_package(Path(draft_dir))
    if package is None:
        return {
            "checked_draft": True, "ready": False, "problems": ["workflow.yaml cannot be read"],
            "in_words": "草稿的 workflow.yaml 读不出来（格式坏了或被删了），先把它修好。",
        }
    preflight = preflight_invocation(package, _placeholder_fields(package))
    tests = run_package_skill_tests(package)
    skill_rows = [{"skill": str(report.get("skill_id", "") or ""), "version": str(report.get("version", "") or ""),
                   "status": str(report.get("status", "") or "")}
                  for report in list(tests.get("skills") or [])]
    blockers = [str(item) for item in list(preflight.get("blockers") or [])]
    warnings = [str(item) for item in list(preflight.get("warnings") or [])]
    failed_skills = [row["skill"] for row in skill_rows if row["status"] != "passed"]
    words = [f"草稿「{package.name}」检查完了：" + ("预检通过。" if preflight.get("ready") else f"有 {len(blockers)} 个问题挡着。")]
    if package.problems:
        words.append(f"包本身有 {len(package.problems)} 个问题。")
    if skill_rows:
        words.append(f"{len(skill_rows)} 个技能里 {len(skill_rows) - len(failed_skills)} 个契约测试通过"
                     + (f"，没通过的：{'、'.join(failed_skills[:10])}。" if failed_skills else "。"))
    if warnings:
        words.append(f"还有 {len(warnings)} 条提醒。")
    return {
        "checked_draft": True,
        "ready": bool(preflight.get("ready")),
        "problems": list(package.problems)[:30],
        "blockers": blockers[:30],
        "warnings": warnings[:30],
        "skill_tests": skill_rows[:50],
        "skill_tests_status": str(tests.get("status", "") or ""),
        "step_count": len(list(preflight.get("steps") or [])),
        "in_words": "".join(words),
    }


# ── the runner: one per console ─────────────────────────────────────────────────────────────────────────────────

class MasterBuildRunner:
    """Every build job of this console: starts, continues, runs turns in the background, recovers after a restart."""

    turn_wall_clock_sec: float = TURN_WALL_CLOCK_SEC
    checkpoint_poll_sec: float = CHECKPOINT_POLL_SEC

    def __init__(self, host: BuildHost) -> None:
        self.host = host
        self._live: dict[str, asyncio.Task[Any]] = {}      # job id -> its running turn, in this process
        self._live_jobs: dict[str, BuildJob] = {}          # job id -> the record the running turn holds
        self._sockets: dict[str, Any] = {}                  # job id -> the socket that started or continued it
        self._turning: dict[str, JobBinding] = {}           # job id -> its turn while the Harness runs it
        self._gone: set[str] = set()                        # job ids whose project was deleted under them
        self._questions_left: dict[str, BuildJob] = {}      # jobs a recovery could not close the questions of yet
        self._start_lock = asyncio.Lock()
        self._save_locks: dict[str, asyncio.Lock] = {}

    # ── records ──

    def _home(self) -> Path:
        return Path(self.host.ampuly_home())

    async def _save(self, job: BuildJob, *, create: bool = False) -> bool:
        """Write the record; False (and the turn in flight stopped) when the job's project is gone."""
        if job.job_id in self._gone:
            return False
        job.updated_at = _now()
        snapshot = BuildJob.from_payload(job.to_payload())
        lock = self._save_locks.setdefault(job.job_id, asyncio.Lock())
        async with lock:
            saved = await asyncio.to_thread(save_job, self._home(), snapshot, create=create)
        if not saved:
            await self.project_gone(job)
        return saved

    async def project_gone(self, job: BuildJob, *, force: bool = False) -> bool:
        """True when this job's project was deleted (or renamed) under it; the job's turn in flight is then stopped.

        After that nothing more is written or said for the job: a record or a message would bring the deleted
        project's data back. The turn is asked to stop the way the owner's 停下 does; when that request cannot be made
        (the project's session records went with it), or ``force`` (it was made and a tool call still came), the
        turn's task is cancelled instead.
        """
        if job.job_id not in self._gone:
            if job_is_home(self._home(), job):
                return False
            self._gone.add(job.job_id)
            logger.warning(f"master build {job.job_id}: its project {job.project_id!r} is gone; "
                           "the build stops and writes nothing more")
        binding = self._turning.get(job.job_id)
        if binding is None:
            return True
        if not binding.gone_stop_asked:
            binding.gone_stop_asked = True
            stop = binding.stop_requested
            if stop is not None and await stop():
                return True
            force = True
        if force:
            task = self._live.get(job.job_id)
            if task is not None and not task.done():
                task.cancel()
        return True

    def holds(self, job_id: str) -> bool:
        task = self._live.get(job_id)
        return task is not None and not task.done()

    def waits_for_answer(self, job_id: str) -> bool:
        """Whether a turn of this job is in flight here: its approval call is then what waits on its open question.

        A turn closes what earlier turns of its job left open before it starts (:meth:`_turn`), so while one is in
        flight an open question of the job is that turn's own.
        """
        return job_id in self._turning and self.holds(job_id)

    async def refuse_left_answer(self, engine: Any, checkpoint: Any) -> bool:
        """True (and the question closed) for an answer to a build's question that no turn here waits for.

        After a console restart -- before :meth:`recover` has run, or when it could not close the question -- or once
        the turn that asked has ended, nobody here waits for the answer. Taken in, the engine's own recovery would
        resume the build's task on its general executor with the engine's whole toolset: no build fence, no clock, no
        job record. So the answer goes no further and the question is closed as stale. False for any other question,
        and for a question the build's own turn is waiting on now.
        """
        job_id = build_job_of(checkpoint)
        if not job_id or self.waits_for_answer(job_id):
            return False
        if await self._close_question(engine, checkpoint, job_id):
            logger.info(f"master build {job_id}: an answer came for a question no turn waits on; the question is closed")
        return True

    def questions_left(self) -> int:
        """How many jobs still have questions a recovery could not close (their engine was not up yet)."""
        return len(self._questions_left)

    def _live_in(self, project_id: str) -> list[str]:
        return [job_id for job_id, job in self._live_jobs.items() if job.project_id == project_id and self.holds(job_id)]

    def _live_count(self) -> int:
        return sum(1 for job_id in list(self._live) if self.holds(job_id))

    async def post(self, job: BuildJob, text: str, *, extra: dict[str, Any] | None = None) -> None:
        if await self.project_gone(job):
            # The project's conversation was deleted with it; a late message would make it again.
            return
        metadata = {"type": "master_build", "master_build": {**job.card(), **dict(extra or {})}}
        try:
            # Into the conversation the job was started from (总控对话栏), whichever conversation continued it. A job
            # recorded before conversations were split names none: the host's first conversation, asked the old way.
            if job.thread_id:
                await self.host.post(job.project_id, text, metadata, thread_id=job.thread_id)
            else:
                await self.host.post(job.project_id, text, metadata)
        except Exception:  # noqa: BLE001 - a message that could not be shown is logged; the job's record stands
            logger.opt(exception=True).warning(f"master build {job.job_id}: a message could not be posted")
        await self.threads_changed(job)

    async def threads_changed(self, job: BuildJob) -> None:
        """总控对话栏: the job's state (or its conversation's last activity) changed -- the column of its project is sent
        again to the socket that started or continued it (the host's optional ``threads_changed``). Display only: a
        column that could not be sent is not a reason to stop building."""
        refresh = getattr(self.host, "threads_changed", None)
        ws = self._sockets.get(job.job_id)
        if not callable(refresh) or ws is None:
            return
        try:
            await asyncio.wait_for(refresh(ws, job.project_id), timeout=2.0)
        except Exception:  # noqa: BLE001 - a lost display refresh; the page asks again when it next lists
            pass

    async def activity(self, binding: JobBinding, *, tool_name: str, sequence: int) -> None:
        job = binding.job
        # A step in words; with no tool (a turn that started, a question asked or answered) the state in words.
        step = STEP_ZH.get(tool_name, "做一步") if tool_name else STATE_ZH.get(job.state, "")
        fields = {
            "ok": True, "action": "master_build_activity", "project_id": job.project_id, "job_id": job.job_id,
            "state": job.state, "sequence": sequence, "tool_name": tool_name,
            "step": step, "session_id": job.session_id, "turn_id": job.turn_id,
        }
        try:
            await asyncio.wait_for(self.host.activity(binding.ws, fields), timeout=0.5)
        except Exception:  # noqa: BLE001 - a lost display event is not a reason to stop building
            pass

    # ── reads ──

    def lines(self, project_id: str) -> list[dict[str, Any]]:
        """The business lines this project may build on: global ones and its own, never another project's."""
        from ampuly.plugins.office_ui.workflow_packages import usable_by_project

        rows: list[dict[str, Any]] = []
        for package in list(self.host.packages() or []):
            manifest = getattr(package, "manifest", None)
            if not isinstance(manifest, dict) or not usable_by_project(package, project_id):
                continue
            rows.append({
                "line_id": str(package.id), "name": str(package.name), "status": str(package.status),
                "version": str(manifest.get("version", "") or ""),
            })
        return rows

    def describe(self, project_id: str) -> dict[str, Any]:
        """master_list_builds: this project's jobs and the lines a build may start from. Writes nothing."""
        jobs = list_jobs(self._home(), project_id)
        rows = []
        for job in jobs[:30]:
            summary = job.summary()
            if job.state in LIVE_STATES and not self.holds(job.job_id):
                # Marked live by a console that stopped: it is not running. recover() says so in the conversation.
                summary["state_zh"] = STATE_ZH["interrupted"]
                summary["can_continue"] = True
            rows.append(summary)
        return {
            "builds": rows,
            "lines": self.lines(project_id),
            "note": "job_id is for master_continue_build only; never read it out to the owner",
        }

    # ── start ──

    async def start_build(
        self, *, project_id: str, goal: str, line_id: str = "", name: str = "", team: str = "",
        actor: str = "owner", ws: Any = None, thread_id: str = "",
    ) -> BuildAnswer:
        try:
            return await self._start(project_id=project_id, goal=goal, line_id=line_id, name=name, team=team,
                                     actor=actor, ws=ws, thread_id=thread_id)
        except BuildRefused as refused:
            return BuildAnswer(False, refused.zh, refused.en, code=refused.code)
        except Exception as exc:  # noqa: BLE001 - said in words, logged in full; nothing runs
            logger.opt(exception=True).error("master build: a build could not be started")
            return BuildAnswer(False, "施工没有开工：出了意外（详情记在控制台日志里），没有花钱。",
                               f"build not started: {type(exc).__name__}: {exc}", code="start_failed")

    async def _start(self, *, project_id: str, goal: str, line_id: str, name: str, team: str, actor: str,
                     ws: Any, thread_id: str = "") -> BuildAnswer:
        from ampuly.plugins.office_ui.workflow_packages import usable_by_project

        pid = str(project_id or "").strip()
        if not _valid_project_id(pid):
            raise BuildRefused("项目编号不对，施工没有开工。", f"invalid project id {pid!r}", code="invalid_project")
        clean_goal = str(goal or "").strip()
        if not clean_goal:
            raise BuildRefused("没说要搭什么、改什么，施工没有开工。", "a build needs the owner's goal", code="goal_required")
        if len(clean_goal) > MAX_GOAL_CHARS:
            raise BuildRefused("目标说得太长了（超过 4000 字），请说得短一些。", "goal too long", code="goal_too_long")
        clean_line = str(line_id or "").strip()
        clean_name = str(name or "").strip()[:160]
        async with self._start_lock:
            if not await asyncio.to_thread(project_is_here, self._home(), pid):
                raise BuildRefused("这个项目已经不在了，施工没有开工。", f"project {pid!r} has no folder",
                                   code="project_gone")
            await self._recover(project_id=pid)
            recorded = await asyncio.to_thread(list_jobs, self._home(), pid)
            if self._live_in(pid) or any(job.state in LIVE_STATES for job in recorded):
                raise BuildRefused(
                    "这个项目已经有一个施工任务在干（或者在等你回话），同一个项目一次只干一个。要改方向就对它插话，或者先让它停下。",
                    "one live build per project; this project has one", code="project_busy",
                )
            if self._live_count() >= MAX_LIVE_JOBS_PER_MACHINE:
                raise BuildRefused(
                    "这台机器上已经有两个施工任务在干，最多同时两个；等其中一个干完或停下再开。",
                    "two live builds per machine at most", code="machine_busy",
                )
            engine = await self.host.engine(pid)
            _check_engine(engine)
            settings = dict(self.host.settings() or {})
            llm, model_name = build_llm(engine, str(settings.get("model", "") or ""))
            cap = _cap(settings.get("model_calls"))
            # The loaded packages are read from disk: off the event loop, once.
            packages = await asyncio.to_thread(lambda: list(self.host.packages() or []))
            source = None
            if clean_line:
                source = next((package for package in packages if package.id == clean_line), None)
                if source is None:
                    raise BuildRefused("找不到这条业务线，施工没有开工。", f"no line {clean_line!r}", code="unknown_line")
                if not usable_by_project(source, pid):
                    raise BuildRefused("这条业务线属于别的项目，这里不能在它上面施工。", "another project's line",
                                       code="other_projects_flow")
            job_id = f"bj{uuid.uuid4().hex[:12]}"
            taken = {str(package.id) for package in packages}
            draft_id = _draft_id(clean_line, job_id, taken)
            job = BuildJob(
                job_id=job_id, project_id=pid, goal=clean_goal,
                name=clean_name or (f"{source.name}（施工草稿）" if source is not None else clean_goal[:40]),
                source_id=str(getattr(source, "id", "") or ""),
                source_version=str(dict(getattr(source, "manifest", {}) or {}).get("version", "") or ""),
                draft_id=draft_id, model=model_name, model_call_cap=cap, created_at=_now(),
                thread_id=str(thread_id or "").strip(),
            )
            # The allowance first (nothing to undo but a closed budget), then the draft: what the owner will find on
            # the builder, whatever happens after.
            business = self.host.business()
            budget = await asyncio.to_thread(
                business.create_run_budget, project_id=pid, limits={"model_call": cap, "image": 0}, actor=actor or "owner",
            )
            job.budget_id = str(budget["budget_id"])
            try:
                package = await asyncio.to_thread(self._create_draft, job, source, team, actor)
            except BaseException:
                try:
                    await asyncio.to_thread(business.close_run_budget, job.budget_id, actor=actor or "owner",
                                            reason="build draft not created")
                except Exception:  # noqa: BLE001 - an open, unused allowance spends nothing
                    pass
                raise
            job.draft_dir = str(Path(package.root).resolve())
            job.snapshot, job.snapshot_truncated = await asyncio.to_thread(snapshot_files, Path(job.draft_dir))
            # The Harness session every turn of this job runs on, opened now so the card can stop or steer it from
            # the first message; its workspace is the draft and its budget the job's.
            from ampuly.layer3_agent.harness import HarnessScope

            opener = engine._native_harness_provider(None)
            if opener is None:
                raise BuildRefused("这个引擎没有会话记录，施工不能开工。", "no harness registry", code="no_registry")
            job.session_id = await opener.create_session(
                HarnessScope(project_id=pid, workspace_path=job.draft_dir),
                context={"task_id": job.task_id, "budget_id": job.budget_id},
            )
            job.state = "running"
            job.turns = 1
            if not await self._save(job, create=True):
                # Deleted while the draft was being made: nothing runs, and nothing is written into its place.
                self._gone.discard(job_id)
                raise BuildRefused("这个项目已经不在了，施工没有开工。", f"project {pid!r} went while the build started",
                                   code="project_gone")
            self._sockets[job_id] = ws
            await self.post(
                job,
                f"🏗 开了一个施工任务：{clean_goal}\n"
                f"在「{job.name}」的草稿里干，正在用的业务线不动；有进展我在这里说，要上网或者越界会先问你。"
                "你随时可以让它停下，或者插话改方向。",
            )
            instruction = (
                f"老板的目标：{clean_goal}\n"
                f"你的工作目录是草稿「{job.name}」。先读 {NOTES_FILE}，"
                + ("再用 build_line_overview 看一下在用的业务线，" if source is not None else "这是一条新业务线，")
                + "然后开始施工；做完用 build_deliver 交付。"
            )
            self._launch(job, instruction, first=True, llm=llm)
        # Working now (its turn is held): the conversation it belongs to shows so (总控对话栏).
        await self.threads_changed(job)
        return BuildAnswer(
            True,
            f"🏗 开了一个施工任务（草稿「{job.name}」），在后台干；进展和要你拿主意的事会在对话里说。",
            f"build job started in draft {job.draft_id}; progress and questions will come in this conversation",
            job=job.card(),
        )

    def _create_draft(self, job: BuildJob, source: Any, team: str, actor: str) -> Any:
        from ampuly.plugins.office_ui.workflow_packages import create_blank_workflow_draft, create_workflow_draft, load_package

        home = self._home()
        if source is not None:
            try:
                package = create_workflow_draft(
                    source, ampuly_home=home, draft_id=job.draft_id, actor=actor or "owner", name=job.name,
                    change_note=f"施工草稿：{job.goal[:200]}", project_id=job.project_id,
                )
            except ValueError as exc:
                raise BuildRefused(f"草稿没建起来：{_plain(exc, 200)}", f"draft not created: {exc}", code="draft_failed") from exc
            notes = Path(source.root) / NOTES_FILE
            try:
                carried = notes.read_bytes() if notes.is_file() and not notes.is_symlink() else b""
            except OSError:
                carried = b""
        else:
            _team, org_path = self.host.team_org(team)
            if org_path is None or not Path(org_path).is_file():
                raise BuildRefused(
                    "新业务线要先说用哪个团队（组织）来跑：现在保存的团队有好几个，或者一个都没有。请说一下用哪个团队。",
                    "a new line needs a saved team", code="team_required",
                )
            try:
                package = create_blank_workflow_draft(
                    ampuly_home=home, draft_id=job.draft_id, actor=actor or "owner", org_source=Path(org_path),
                    name=job.name, summary=job.goal[:2000], created_from=f"master_build:{job.project_id}:{job.job_id}",
                )
            except ValueError as exc:
                raise BuildRefused(f"草稿没建起来：{_plain(exc, 200)}", f"draft not created: {exc}", code="draft_failed") from exc
            # A blank draft is written with no owner; this one is this project's own, like a cloned one.
            _stamp_project(Path(package.root), job.project_id)
            package = load_package(Path(package.root)) or package
            carried = b""
        notes_path = Path(package.root) / NOTES_FILE
        if not notes_path.exists():
            notes_path.write_bytes(carried or NOTES_STARTER.encode("utf-8"))
        return package

    # ── continue ──

    async def continue_build(self, *, project_id: str, job_id: str, instruction: str, actor: str = "owner",
                             ws: Any = None) -> BuildAnswer:
        try:
            return await self._continue(project_id=project_id, job_id=job_id, instruction=instruction, actor=actor, ws=ws)
        except BuildRefused as refused:
            return BuildAnswer(False, refused.zh, refused.en, code=refused.code)

    async def _continue(self, *, project_id: str, job_id: str, instruction: str, actor: str, ws: Any) -> BuildAnswer:
        pid = str(project_id or "").strip()
        clean = str(instruction or "").strip()
        if not clean:
            raise BuildRefused("没说接着改什么，这次没有接着干。", "continue needs the owner's instruction", code="instruction_required")
        if len(clean) > MAX_GOAL_CHARS:
            raise BuildRefused("这句话太长了（超过 4000 字），请说得短一些。", "instruction too long", code="instruction_too_long")
        if not _valid_project_id(pid):
            raise BuildRefused("找不到这个施工任务。", f"invalid project id {pid!r}", code="unknown_job")
        async with self._start_lock:
            await self._recover(project_id=pid)
            job = await asyncio.to_thread(load_job, self._home(), pid, str(job_id or "").strip())
            if job is None:
                raise BuildRefused("找不到这个施工任务。", f"no build job {job_id!r} in this project", code="unknown_job")
            if self.holds(job.job_id) or job.state in LIVE_STATES:
                raise BuildRefused("它正在干，用插话告诉它就行。", "the job is running; steer it instead", code="running")
            if job.state not in CONTINUABLE_STATES:
                raise BuildRefused("这个施工任务现在不能接着干。", f"job is {job.state}", code="not_continuable")
            if self._live_in(pid):
                raise BuildRefused("这个项目已经有别的施工任务在干，一次只干一个。", "one live build per project",
                                   code="project_busy")
            if self._live_count() >= MAX_LIVE_JOBS_PER_MACHINE:
                raise BuildRefused("这台机器上已经有两个施工任务在干，等其中一个干完或停下再接着干。",
                                   "two live builds per machine at most", code="machine_busy")
            engine = await self.host.engine(pid)
            _check_engine(engine)
            settings = dict(self.host.settings() or {})
            llm, model_name = build_llm(engine, str(settings.get("model", "") or ""))
            if job.state == "capped":
                # The owner said to go on after the cap: that is the owner granting another allowance of the same size.
                cap = _cap(settings.get("model_calls"))
                business = self.host.business()
                current = await asyncio.to_thread(business.run_budget, job.budget_id) or {}
                limit = float(dict(current.get("limits", {}) or {}).get("model_call", job.model_call_cap) or job.model_call_cap)
                try:
                    await asyncio.to_thread(
                        business.raise_run_budget, job.budget_id, limits={"model_call": limit + cap},
                        actor=actor or "owner", reason="老板让施工用完额度后接着干",
                    )
                except Exception as exc:  # noqa: BLE001 - said in words; nothing started
                    raise BuildRefused(
                        "额度加不上去（可能有一次模型调用结果不明，额度被扣着等人核对），这次没有接着干。",
                        f"budget not raised: {exc}", code="budget_not_raised",
                    ) from exc
                job.model_call_cap = int(limit + cap)
            # A console that stopped mid-turn may have left that turn unsettled; the owner going on settles it.
            recovered_from_restart = job.state == "interrupted"
            job.state = "running"
            job.model = model_name
            job.turns += 1
            job.last_error = ""
            job.stopped_by = ""
            job.checkpoint = {}
            job.turn_id = ""
            if not await self._save(job):
                self._gone.discard(job.job_id)
                raise BuildRefused("这个项目已经不在了，没有接着干。", f"project {pid!r} went while the build continued",
                                   code="project_gone")
            self._sockets[job.job_id] = ws
            await self.post(job, f"🏗 接着施工：{clean}")
            self._launch(job, f"接着施工。老板这次说：{clean}", first=False, llm=llm,
                         settle_left_turn=recovered_from_restart)
        await self.threads_changed(job)
        return BuildAnswer(True, "🏗 接着施工了，进展会在对话里说。", "the build continues", job=job.card())

    # ── the turn ──

    def _launch(self, job: BuildJob, instruction: str, *, first: bool, llm: Any, settle_left_turn: bool = False) -> None:
        self._live_jobs[job.job_id] = job
        task = self.host.spawn(self._run_turn(job, instruction, first=first, llm=llm, settle_left_turn=settle_left_turn))
        self._live[job.job_id] = task

    async def _run_turn(self, job: BuildJob, instruction: str, *, first: bool, llm: Any, settle_left_turn: bool) -> None:
        try:
            await self._turn(job, instruction, first=first, llm=llm, settle_left_turn=settle_left_turn)
        except asyncio.CancelledError:
            # The console is shutting down: the record stays "running" and the next start says it was interrupted.
            raise
        except BuildRefused as refused:
            await self._settle(job, "failed", error=refused.zh)
        except Exception:  # noqa: BLE001 - said to the owner, logged in full, never retried
            logger.opt(exception=True).error(f"master build {job.job_id}: the turn failed")
            await self._settle(job, "failed", error="施工出了意外停下了（详情记在控制台日志里）。草稿里已经改的都还在；要接着干就说一声。")
        finally:
            self._live.pop(job.job_id, None)
            self._live_jobs.pop(job.job_id, None)
            self._turning.pop(job.job_id, None)
            self._gone.discard(job.job_id)

    async def _turn(self, job: BuildJob, instruction: str, *, first: bool, llm: Any, settle_left_turn: bool) -> None:
        from ampuly.core.models import TaskStatus
        from ampuly.layer3_agent.harness import HarnessScope
        from ampuly.llm.budget import BUDGET_STOP_ARTIFACT
        from ampuly.plugins.office_ui.master_runtime import ensure_master_task_row

        if await self.project_gone(job):
            return
        engine = await self.host.engine(job.project_id)
        _check_engine(engine)
        binding = JobBinding(self, job, engine=engine, llm=llm, ws=self._sockets.get(job.job_id))
        registry = build_worker_registry(engine, binding)
        agent = make_build_agent(engine, job, llm, registry, _fence_hook(binding))
        provider = engine._native_harness_provider(agent)
        if provider is None:
            raise BuildRefused("这个引擎没有会话记录，施工不能开工。", "no harness registry", code="no_registry")
        task = self._task(job, instruction)
        pin_approval_level(engine, task)
        if not await ensure_master_task_row(getattr(engine, "store", None), task):
            raise BuildRefused("记账用的任务记录写不进去，施工没有开工，也没有花钱。", "task row unavailable",
                               code="accounting_unavailable")
        ensure_session = getattr(getattr(engine, "memory", None), "ensure_session", None)
        if callable(ensure_session):
            await ensure_session(task.session_id, project_id=job.project_id, title=task.title,
                                 metadata={"runtime_kind": "master_build", "user_visible": False})
        if not job.session_id:
            job.session_id = await provider.create_session(
                HarnessScope(project_id=job.project_id, workspace_path=job.draft_dir),
                context={"task_id": task.id, "budget_id": job.budget_id},
            )
            await self._save(job)
        elif not first:
            # The same session as every earlier turn of this job: its transcript carries over.
            registry_ = provider.registry
            pending = await asyncio.to_thread(registry_.pending_turn_id, job.session_id)
            live_here = bool(getattr(engine, "harness_turn_is_live", lambda _sid: False)(job.session_id))
            if pending and settle_left_turn and not live_here:
                # The console stopped mid-turn and the owner asked to go on: the owner settles that turn, never a timer.
                await provider.settle_pending_turn(job.session_id, reason="owner continued the build after a console restart",
                                                   actor="owner")
            if not await provider.resume_session(job.session_id):
                raise BuildRefused("施工的会话接不上了，没有接着干。可以开一个新的施工任务。", "session could not resume",
                                   code="session_not_resumed")
        provider.adopt_task(job.session_id, task)

        async def on_event(event: Any) -> None:
            if getattr(event, "event_type", "") == "turn_started" and getattr(event, "session_id", "") == job.session_id:
                job.turn_id = str(getattr(event, "turn_id", "") or "")
                await self._save(job)
                await binding.note_state()

        async def stop_turn() -> bool:
            try:
                await provider.request_interrupt(job.session_id, actor="master-build")
            except Exception:  # noqa: BLE001
                logger.opt(exception=True).warning(f"master build {job.job_id}: the turn could not be stopped")
                return False
            return True

        binding.stop_requested = stop_turn
        clock = {"stopped": False, "waiting": False}

        async def wall_clock() -> None:
            await asyncio.sleep(max(0.0, float(self.turn_wall_clock_sec)))
            clock["stopped"] = True
            # The clock counts the time a question waited on the owner too; the message says which it was.
            clock["waiting"] = job.state == "waiting_owner"
            await stop_turn()

        if await self.project_gone(job):
            return
        # What an earlier turn of this job left open (a console stopped before it could close it) is closed before this
        # turn starts: while it runs, an open question of this job is then always its own (``waits_for_answer``).
        await self._close_questions(engine, job, reason="left open by an earlier turn of this build")
        live_turns = engine.__dict__.setdefault("_harness_live_turns", set())
        live_providers = engine.__dict__.setdefault("_harness_live_providers", {})
        live_turns.add(job.session_id)
        live_providers[job.session_id] = provider
        self._turning[job.job_id] = binding
        watchers = [asyncio.create_task(wall_clock()),
                    asyncio.create_task(self._watch_questions(job, binding, engine, task.session_id))]
        try:
            result = await provider.start_turn(
                job.session_id, instruction, inputs={"task_id": task.id},
                callbacks=SimpleNamespace(on_event=on_event),
            )
        finally:
            self._turning.pop(job.job_id, None)
            for watcher in watchers:
                watcher.cancel()
            await asyncio.gather(*watchers, return_exceptions=True)
            live_turns.discard(job.session_id)
            if live_providers.get(job.session_id) is provider:
                live_providers.pop(job.session_id, None)
        if await self.project_gone(job):
            # The project went while the turn ran: its store went with it, and nothing more is written or said.
            return
        # A question the turn left open (it was stopped while it waited) is closed: answered later, nobody here would
        # be waiting, and the engine's own recovery would take the answer up outside this build's fence.
        await self._close_questions(engine, job)
        job.turn_id = ""
        job.checkpoint = {}
        if binding.delivered:
            job.state = "delivered"
            await self._save(job)
            return
        status = getattr(result, "status", None)
        artifacts = dict(getattr(result, "artifacts", {}) or {})
        content = str(getattr(result, "content", "") or "")
        stop_kind = str(artifacts.get(BUDGET_STOP_ARTIFACT, "") or "")
        if status == TaskStatus.CANCELLED:
            if clock["stopped"] and clock["waiting"]:
                await self._settle(job, "stopped", by="clock", text=(
                    "⏱ 这一轮连干带等满了 4 小时（最后一直在等你回话，那个问题已经作废），先停下；要接着干就说一声。"))
            elif clock["stopped"]:
                await self._settle(job, "stopped", by="clock", text="⏱ 干满 4 小时，先停下；要接着干就说一声。")
            else:
                await self._settle(job, "stopped", by="owner",
                                   text="⏹ 施工停下了。草稿里已经改的都还在；要接着干就说「接着改」。")
        elif stop_kind == "exhausted":
            await self._settle(job, "capped", text=(
                f"施工用完了这个任务的模型调用次数（{job.model_call_cap} 次），先停下了，不会自己重试。"
                "草稿里已经改的都还在；要接着干就说一声，会再给它一份同样多的额度。"))
        elif stop_kind:
            await self._settle(job, "failed", error=(
                "模型调用在发出前被拒绝（多半是密钥或所选模型的问题），施工停下了；请到「配置 → 模型」看一下。"
                if stop_kind == "route_refused" else
                "有一次模型调用的结果不明，额度被扣着等人核对，施工先停下了，不会自己重试。"))
        elif status == TaskStatus.FAILED and content.startswith("Exceeded maximum iterations"):
            await self._settle(job, "stopped", by="steps", text=(
                f"这一轮干满 {MAX_TURN_ITERATIONS} 步，先停下；草稿里已经改的都还在，要接着干就说一声。"))
        elif status == TaskStatus.FAILED:
            await self._settle(job, "failed", error=(
                "施工这一轮出错停下了" + (f"：{_plain(content, 200)}" if _chinese(content) else "") +
                "。草稿里已经改的都还在；要接着干就说一声。"))
        else:
            said = _plain(content, 300)
            await self._settle(job, "stopped", by="ended", text=(
                "施工这一轮结束了，但没有交候选版本。" + (f"它最后说：{said}" if said else "")
                + "\n要接着干就说「接着改」。"))

    def _task(self, job: BuildJob, instruction: str) -> Any:
        from ampuly.core.models import Task

        ledger_home = _ledger_home(self.host.business(), self._home())
        return Task(
            id=job.task_id,
            title=f"施工：{job.name or job.goal[:40]}"[:120],
            description=instruction,
            project_id=job.project_id,
            session_id=job.task_session_id,
            metadata={
                "workspace_root": job.draft_dir,
                "interface": "master_build",
                "budget_id": job.budget_id,
                "customer_id": "",
                "environment_id": "",
                "ampuly_home": str(ledger_home),
                "master_build_job_id": job.job_id,
            },
            context_snapshot={"runtime_resume": {"runtime_session_id": job.task_session_id}},
        )

    async def _close_questions(self, engine: Any, job: BuildJob, *,
                               reason: str = "the build turn that asked this has ended") -> int | None:
        """Close the owner questions this job's session (or a sub-helper of it) left unanswered; says how many.

        None when they could not all be looked at or closed (no store yet, a read or a close that failed): a recovery
        then tries again (:meth:`_recover`), and meanwhile an answer to one is refused (:meth:`refuse_left_answer`).
        """
        store = getattr(engine, "store", None)
        coordinator = getattr(engine, "interaction_coordinator", None)
        getter = getattr(store, "get_pending_checkpoints", None)
        if not callable(getter) or not callable(getattr(coordinator, "close_pending_owner_checkpoint", None)):
            return None
        try:
            pending = await getter(project_id=job.project_id, checkpoint_types=["tool_permission", "action_permission"])
        except Exception:  # noqa: BLE001 - the next start looks again
            logger.opt(exception=True).warning(f"master build {job.job_id}: its open questions could not be read")
            return None
        closed, complete = 0, True
        for checkpoint in list(pending or []):
            if build_job_of(checkpoint) != job.job_id:
                continue
            applied = await self._close_question(engine, checkpoint, job.job_id, reason=reason)
            if applied is None:
                complete = False
            closed += 1 if applied else 0
        return closed if complete else None

    async def _close_question(self, engine: Any, checkpoint: Any, job_id: str, *,
                              reason: str = "no build turn waits for its answer") -> bool | None:
        """Close one open question of a build as stale: True when this call closed it, None when it could not."""
        closer = getattr(getattr(engine, "interaction_coordinator", None), "close_pending_owner_checkpoint", None)
        if not callable(closer):
            return None
        try:
            _row, applied = await closer(
                str(checkpoint.checkpoint_id), checkpoint_type=str(checkpoint.checkpoint_type), status="stale",
                payload_patch={"administrative_reason": reason, "administrative_closed_at": _now()},
            )
        except Exception:  # noqa: BLE001 - logged; the next look tries again
            logger.opt(exception=True).warning(f"master build {job_id}: an open question could not be closed")
            return None
        return bool(applied)

    async def _settle(self, job: BuildJob, state: str, *, text: str = "", error: str = "", by: str = "") -> None:
        job.state = state
        job.turn_id = ""
        job.checkpoint = {}
        job.stopped_by = by
        job.last_error = error if state == "failed" else ""
        await self._save(job)
        await self.post(job, text or error or STATE_ZH.get(state, ""))

    async def _watch_questions(self, job: BuildJob, binding: JobBinding, engine: Any, task_session_id: str) -> None:
        """While the turn runs: an owner question it opened makes the job wait on the owner, an answer sets it going."""
        store = getattr(engine, "store", None)
        getter = getattr(store, "get_pending_checkpoints", None)
        if not callable(getter):
            return
        asked: str = ""
        while True:
            await asyncio.sleep(max(0.01, float(self.checkpoint_poll_sec)))
            # A turn waiting on a question makes no tool call for as long as the owner takes: this is where a project
            # deleted under a waiting build is noticed, and the build stopped.
            if await self.project_gone(job):
                return
            try:
                pending = await getter(project_id=job.project_id,
                                       checkpoint_types=["tool_permission", "action_permission"])
            except Exception:  # noqa: BLE001 - a busy store is asked again next round
                continue
            mine = [item for item in list(pending or [])
                    if str(getattr(item, "session_id", "") or "") == task_session_id
                    or str(getattr(item, "session_id", "") or "").startswith(task_session_id + ":")]
            open_ids = {str(getattr(item, "checkpoint_id", "") or "") for item in mine}
            # The oldest open question first (the store lists the newest first). One answered while another is still
            # open (two calls asked at once) moves the card on to the next one instead of leaving it on the old one.
            if mine and (job.state == "running" or (job.state == "waiting_owner" and asked not in open_ids)):
                checkpoint = mine[-1]
                asked = str(getattr(checkpoint, "checkpoint_id", "") or "")
                job.state = "waiting_owner"
                job.checkpoint = checkpoint_card(checkpoint)
                await self._save(job)
                await self.post(job, f"❓ 施工需要你点头：{job.checkpoint['question']}")
                await binding.note_state()
            elif not mine and job.state == "waiting_owner" and asked:
                asked = ""
                job.state = "running"
                job.checkpoint = {}
                await self._save(job)
                await binding.note_state()
                await self.threads_changed(job)  # no message says so: the column is told directly

    # ── what the build tools reach ──

    def line_overview(self, job: BuildJob) -> dict[str, Any]:
        """The line as it is live (read only): name, version, status, steps, skills, test records, recent orders."""
        if not job.source_id:
            return {"new_line": True, "in_words": "这是一条新业务线：还没有在用的版本，也没有订单。"}
        package = next((item for item in list(self.host.packages() or []) if item.id == job.source_id), None)
        if package is None:
            return {"found": False, "in_words": "在用的那条业务线现在读不到了（可能被改名或挪走），只能按草稿里的内容来。"}
        type_zh = {"ai_do": "AI 做", "human_do": "人做", "human_review": "人审", "invoke_workflow": "调用另一条流程"}
        nodes = [{
            "name": str(node.get("name", "") or node.get("id", "")),
            "who": type_zh.get(str(node.get("type", "") or ""), str(node.get("type", "") or "")),
            "role": str(node.get("role", "") or ""),
            "delivers": [str(item) for item in list(node.get("deliverables") or [])][:10],
            "skills": [str(item) for item in list(node.get("skills") or [])][:10],
            "tools": [str(item) for item in list(node.get("tools") or [])][:10],
        } for node in list(package.nodes)[:60]]
        skills = [{"skill": contract.skill_id, "version": contract.version} for contract in list(package.skill_contracts)[:60]]
        tests = [{"date": test.date, "outcome": test.outcome, "mode": test.execution_mode, "notes": _plain(test.notes, 200)}
                 for test in list(package.tests)[-5:]]
        orders = {"closed": 0, "rejected": 0, "open": 0, "looked_at": 0}
        try:
            recent = [order for order in self.host.business().list_orders(project_id=job.project_id, limit=200)
                      if str(getattr(order, "workflow_id", "") or "") == package.id]
        except Exception:  # noqa: BLE001 - the orders are context, not the answer
            recent = []
        for order in recent:
            status = str(getattr(order, "status", "") or "")
            orders["looked_at"] += 1
            orders["closed" if status == "closed" else "rejected" if status == "rejected" else "open"] += 1
        status_zh = {"draft": "草稿", "testing": "测试中", "published": "已上线", "retired": "已退役"}.get(package.status, package.status)
        return {
            "found": True,
            "line": {
                "name": package.name, "version": str(package.manifest.get("version", "") or ""), "status": status_zh,
                "steps": nodes, "skills": skills, "latest_tests": tests, "recent_orders_in_this_project": orders,
            },
            "in_words": (
                f"在用的业务线「{package.name}」（{status_zh}，版本 {package.manifest.get('version', '')}）："
                f"{len(nodes)} 步、{len(skills)} 个技能；本项目最近 {orders['looked_at']} 张单里结案 {orders['closed']}、"
                f"打回 {orders['rejected']}、还在走 {orders['open']}。你改的是草稿，这条线本身不动。"
            ),
        }

    async def offline_trial(self, job: BuildJob, llm: Any) -> dict[str, Any]:
        """The whole draft through the product's own offline runner, on this job's budget. Simulated evidence only."""
        from ampuly.plugins.office_ui.trial_runner import CompanyTrialRunner
        from ampuly.plugins.office_ui.workflow_packages import load_package

        package = await asyncio.to_thread(load_package, Path(job.draft_dir))
        if package is None:
            return {"passed": False, "in_words": "草稿的 workflow.yaml 读不出来，试跑没有开始。"}
        if not package.nodes:
            return {"passed": False, "in_words": "草稿还没有步骤，试跑没有开始。"}
        home = _ledger_home(self.host.business(), self._home())
        runner = CompanyTrialRunner(
            llm=llm, ampuly_home=home,
            budget_context={"budget_id": job.budget_id, "project_id": job.project_id,
                            "customer_id": "", "environment_id": "", "order_id": ""},
        )
        workflow = {"id": job.draft_id, "name": package.name, "nodes": [dict(node) for node in package.nodes]}
        try:
            passed, failed_step, detail = await runner(workflow)
        except Exception as exc:  # noqa: BLE001 - a trial that could not run is a result the build can act on
            return {"passed": False, "evidence": "simulated", "in_words": f"离线试跑没跑起来（{_plain(type(exc).__name__, 60)}）。"}
        names = {str(node.get("id", "") or ""): str(node.get("name", "") or node.get("id", "")) for node in package.nodes}
        step = names.get(str(failed_step or ""), str(failed_step or ""))
        words = "离线试跑（模拟证据，不算真实测试）：" + ("通过了。" if passed else "没通过。")
        if step and not passed:
            words += f"卡在「{step}」。"
        return {"passed": bool(passed), "failed_step": step, "detail": _plain(detail, 600), "evidence": "simulated",
                "in_words": words}

    async def deliver(self, binding: JobBinding, summary: str, open_questions: list[str],
                      tools_to_develop: list[str]) -> dict[str, Any]:
        job = binding.job
        after, truncated = await asyncio.to_thread(snapshot_files, Path(job.draft_dir))
        changes = changed_files(job.snapshot, after)
        notes_updated = any(change["path"] == NOTES_FILE for change in changes)
        clean_summary = _plain(summary, 1000) or "（没写小结）"
        questions = [_plain(item, 300) for item in list(open_questions or []) if _plain(item, 300)][:20]
        tools = [_plain(item, 300) for item in list(tools_to_develop or []) if _plain(item, 300)][:20]
        job.delivery = {
            "summary": clean_summary,
            "changed_files": changes[:200],
            "changed_files_truncated": bool(truncated or job.snapshot_truncated or len(changes) > 200),
            "open_questions": questions,
            "tools_to_develop": tools,
            "notes_updated": notes_updated,
            "evidence": "simulated",
            "delivered_at": _now(),
        }
        job.state = "delivered"
        binding.delivered = True
        if not await self._save(job):
            return {"delivered": False, "refused": "project_gone",
                    "error": "这个项目已经不在了（删掉了或改了名），交付没有记下；这一轮到此为止，不要再调用工具。"}
        lines = [f"✅ 施工交了一个候选版本：{clean_summary}"]
        if changes:
            lines.append("改了这些文件：")
            lines.extend(f"- {change['change_zh']} {change['path']}" for change in changes[:30])
            if len(changes) > 30:
                lines.append(f"……还有 {len(changes) - 30} 个")
        else:
            lines.append("草稿里没有文件改动。")
        if questions:
            lines.append("还要你拿主意的事：")
            lines.extend(f"- {item}" for item in questions)
        if tools:
            lines.append("需要开发的工具（要改引擎源码，施工没有写）：")
            lines.extend(f"- {item}" for item in tools)
        if not notes_updated:
            lines.append("（这次没有更新施工笔记。）")
        lines.append("这些都是在草稿里做的，离线试跑只算模拟，不算真实测试。满意就在搭建台打开这个草稿，走测试、批准、上线；"
                     "不满意就说「接着改」。")
        await self.post(job, "\n".join(lines))
        return {
            "delivered": True,
            "changed_files": [f"{change['change']}: {change['path']}" for change in changes[:50]],
            "note": "交付已记下。用一句话告诉老板交付了，然后结束这一轮，不要再调用工具。",
        }

    # ── recover ──

    async def recover(self, *, project_id: str = "") -> list[str]:
        """Jobs still marked running / waiting on the owner that no turn here holds: interrupted, said once each.

        Under the start lock: a start or continue saves its job as running and posts before its turn is launched, and a
        recovery in that gap would take a job that is about to run for one left behind by a stopped console.
        """
        async with self._start_lock:
            return await self._recover(project_id=project_id)

    async def _recover(self, *, project_id: str = "") -> list[str]:
        """:meth:`recover`, for a caller that already holds the start lock."""
        home = self._home()
        # Jobs an earlier pass said were interrupted but could not close the questions of (the project's engine was
        # not up so soon after a start): tried again first. One running again, or whose project is gone, is dropped --
        # a turn closes what its job left open before it starts.
        for job_id, left in list(self._questions_left.items()):
            if project_id and left.project_id != project_id:
                continue
            if self.holds(job_id) or not job_is_home(home, left) or await self._close_left(left) is not None:
                self._questions_left.pop(job_id, None)
        projects = [project_id] if project_id else await asyncio.to_thread(projects_with_jobs, home)
        settled: list[str] = []
        for pid in projects:
            if not _valid_project_id(pid):
                continue
            try:
                jobs = await asyncio.to_thread(list_jobs, home, pid)
            except BuildRefused:
                continue
            for job in jobs:
                if job.state not in LIVE_STATES or self.holds(job.job_id):
                    continue
                # A question the stopped turn left open is closed with it: nobody here waits for its answer. One that
                # cannot be closed yet is tried again by the next pass, and an answer to it is refused meanwhile.
                if await self._close_left(job) is None:
                    self._questions_left[job.job_id] = BuildJob.from_payload(job.to_payload())
                job.state = "interrupted"
                job.checkpoint = {}
                job.stopped_by = ""
                if not await self._save(job):
                    continue
                settled.append(job.job_id)
                await self.post(job, "控制台重启过，施工停在半路；说「接着改」就从这里接着干。")
        return settled

    async def _close_left(self, job: BuildJob) -> int | None:
        """Close what a stopped console left open for this job; None when its engine or store cannot be reached yet."""
        try:
            engine = await self.host.engine(job.project_id)
        except Exception:  # noqa: BLE001 - logged; the next pass tries again
            logger.opt(exception=True).warning(f"master build {job.job_id}: its engine could not be reached")
            return None
        return await self._close_questions(engine, job, reason="left open by a console that stopped")


def _fence_hook(binding: JobBinding) -> Callable[[Any], Awaitable[dict[str, Any] | None]]:
    """Before every tool call of the build (and of its sub-helpers): the fence, and the step the owner sees."""

    async def hook(context: Any) -> dict[str, Any] | None:
        name = str(getattr(context, "tool_name", "") or "")
        # The project was deleted (or renamed) under the build: nothing more runs. The first refusal asks the turn to
        # stop; a tool call that still comes after that has its turn cancelled.
        if await binding.runner.project_gone(binding.job, force=binding.gone_refusals > 0):
            binding.gone_refusals += 1
            return {"result": {"error": "这个项目已经不在了（删掉了或改了名），施工到此为止；不要再调用工具。",
                               "success": False, "refused": "project_gone"},
                    "stop_execution": True}
        if binding.delivered:
            binding.post_delivery_refusals += 1
            if binding.post_delivery_refusals >= POST_DELIVERY_REFUSALS and binding.stop_requested is not None:
                await binding.stop_requested()
            return {"result": {"error": "施工已经交付，这一轮到此结束；不要再调用工具。", "success": False,
                               "refused": "delivered"},
                    "stop_execution": True}
        try:
            call = name + "\n" + json.dumps(dict(getattr(context, "arguments", {}) or {}), sort_keys=True,
                                            ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            call = name
        binding.repeats = binding.repeats + 1 if call == binding.last_call else 1
        binding.last_call = call
        if binding.repeats >= REPEATED_CALL_LIMIT:
            return {"result": {
                "error": (f"同一个调用已经连续 {binding.repeats} 次，结果不会变，这次没有执行。换个办法；"
                          "实在做不下去，就用 build_deliver 交付，写清卡在哪、需要老板定什么。"),
                "success": False, "refused": "repeated_call",
            }, "stop_execution": True}
        patch: dict[str, Any] | None = None
        if name == "agent_spawn":
            arguments = dict(getattr(context, "arguments", {}) or {})
            profile = str(arguments.get("profile") or arguments.get("subagent_type") or "general").strip().lower()
            if profile not in READ_ONLY_SUBAGENT_PROFILES:
                return {"result": {
                    "error": "施工只能派只读的子助手（explore、plan、verify）；改文件你自己在草稿里改。",
                    "success": False, "refused": "profile_not_allowed",
                }, "stop_execution": True}
            # A sub-helper works in the draft itself, on the build's own model: never an isolated copy that is thrown
            # away, never another model outside the build's setting.
            arguments = {key: value for key, value in arguments.items() if key not in {"model", "subagent_type"}}
            arguments["profile"] = profile
            arguments["isolation"] = "shared"
            patch = {"arguments": arguments}
        await binding.note_step(name)
        return patch

    return hook


def _check_engine(engine: Any) -> None:
    if engine is None:
        raise BuildRefused("这个项目的引擎还没起来，施工没有开工。", "no engine for the project", code="no_engine")
    if getattr(engine, "harness_registry", None) is None or not callable(getattr(engine, "_native_harness_provider", None)):
        raise BuildRefused("这个引擎没有会话记录，施工不能开工。", "no harness registry", code="no_registry")
    if getattr(engine, "approval_engine", None) is None or not callable(getattr(engine, "_tool_approval_callback", None)):
        raise BuildRefused("这个引擎没有权限审批，施工不能动手，没有开工。", "no permission policy", code="no_permission_policy")
    if getattr(engine, "store", None) is None:
        raise BuildRefused("这个引擎的记录库还没准备好，施工没有开工。", "no store", code="no_store")


def _cap(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MODEL_CALLS
    return value if 1 <= value <= 5000 else DEFAULT_MODEL_CALLS


def _draft_id(line_id: str, job_id: str, taken: set[str]) -> str:
    tail = job_id[2:10]
    base = f"{line_id[:40]}-build-{tail}" if line_id else f"line-{tail}"
    base = re.sub(r"[^a-z0-9_-]+", "-", base.lower()).strip("-_")
    if not base[:1].isalpha():
        base = f"b{base}"
    candidate = base[:64]
    number = 2
    while candidate in taken or not _DRAFT_ID.match(candidate):
        candidate = f"{base[:58]}-{number}"
        number += 1
    return candidate


def _stamp_project(root: Path, project_id: str) -> None:
    """Record the project on a blank draft's manifest, written the same atomic way."""
    import yaml

    manifest_path = root / "workflow.yaml"
    manifest = yaml.safe_load(manifest_path.read_bytes().decode("utf-8")) or {}
    if not isinstance(manifest, dict):
        return
    manifest["project_id"] = project_id[:128]
    temporary = root / f".workflow.{uuid.uuid4().hex[:8]}.tmp"
    temporary.write_bytes(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False).encode("utf-8"))
    replace_with_retry(temporary, manifest_path)


def _ledger_home(business: Any, fallback: Path) -> Path:
    """Where the budget ledger lives: the business store's own folder (model_budget_scope reads it from the task)."""
    path = getattr(business, "path", None)
    return Path(path).parent if path else Path(fallback)
