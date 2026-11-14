"""持久化浏览器 Profile 与任务空间（对齐 ego lite 的两项核心能力）。

- Profile：一个账号分区一个磁盘目录，登录态跨进程/重启复用；
- TaskSpace：一个任务一个隔离工作区（下载、截图、日志），同 Profile 可并行多个 Space，
  但同一 Space 同时只允许一个执行者（租约文件）。
本模块不驱动浏览器，只管理身份与隔离边界；Playwright 与 ego 后端共用。

目录职责（C1）：profiles 子树 = 账号分区身份；spaces 子树 = 任务租约与证据（captures / receipt）。登录态在 account/Ego profile；任务产物进 Space（见 space_artifacts）。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")


class SpaceRefused(ValueError):
    pass


@dataclass
class Profile:
    profile_id: str
    directory: str
    created_at: float
    last_used_at: float
    notes: str = ""

    def path(self) -> Path:
        return Path(self.directory)


@dataclass
class Space:
    space_id: str
    profile_id: str
    task_id: str
    directory: str
    created_at: float
    state: str = "open"  # open | closed
    owner_pid: int = 0
    metadata: dict = field(default_factory=dict)


class BrowserSpaces:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.profiles_dir = self.root / "profiles"
        self.spaces_dir = self.root / "spaces"

    # ── profiles ─────────────────────────────────────────────
    def ensure_profile(self, profile_id: str, *, notes: str = "") -> Profile:
        if not _ID.match(profile_id or ""):
            raise SpaceRefused(f"invalid profile_id: {profile_id!r}")
        meta_path = self.profiles_dir / profile_id / "profile.json"
        now = time.time()
        if meta_path.is_file():
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            profile = Profile(**data)
            profile.last_used_at = now
        else:
            directory = self.profiles_dir / profile_id
            directory.mkdir(parents=True, exist_ok=True)
            profile = Profile(profile_id, str(directory), now, now, notes)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(asdict(profile), ensure_ascii=False, indent=1), encoding="utf-8")
        return profile

    def list_profiles(self) -> list[Profile]:
        if not self.profiles_dir.is_dir():
            return []
        result = []
        for meta in sorted(self.profiles_dir.glob("*/profile.json")):
            try:
                result.append(Profile(**json.loads(meta.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        return result

    # ── spaces ───────────────────────────────────────────────
    def open_space(self, profile_id: str, task_id: str) -> Space:
        self.ensure_profile(profile_id)
        if not _ID.match(task_id or ""):
            raise SpaceRefused(f"invalid task_id: {task_id!r}")
        for existing in self.list_spaces(profile_id=profile_id):
            if existing.task_id == task_id and existing.state == "open":
                raise SpaceRefused("task already has an open space")
        space_id = f"{profile_id}__{task_id}__{int(time.time() * 1000)}"
        directory = self.spaces_dir / space_id
        directory.mkdir(parents=True, exist_ok=False)
        lock = directory / ".lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            raise SpaceRefused("space already open") from None
        space = Space(space_id, profile_id, task_id, str(directory), time.time(), "open", os.getpid())
        (directory / "space.json").write_text(json.dumps(asdict(space), ensure_ascii=False, indent=1), encoding="utf-8")
        return space

    def close_space(self, space_id: str) -> Space:
        path = self.spaces_dir / space_id / "space.json"
        if not path.is_file():
            raise SpaceRefused("space not found")
        space = Space(**json.loads(path.read_text(encoding="utf-8")))
        space.state = "closed"
        path.write_text(json.dumps(asdict(space), ensure_ascii=False, indent=1), encoding="utf-8")
        lock = Path(space.directory) / ".lock"
        if lock.is_file():
            lock.unlink()
        return space

    def list_spaces(self, *, profile_id: str | None = None) -> list[Space]:
        if not self.spaces_dir.is_dir():
            return []
        rows = []
        for meta in sorted(self.spaces_dir.glob("*/space.json")):
            try:
                space = Space(**json.loads(meta.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if profile_id is None or space.profile_id == profile_id:
                rows.append(space)
        return rows
