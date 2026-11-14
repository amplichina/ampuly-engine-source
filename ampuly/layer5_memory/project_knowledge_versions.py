"""Fixed visible project knowledge, captured only by the formal write path.

Content hashes are references, never grants. Readers must recheck the current
project/instance ACL before and after resolving a version. Construction and
read are inert: a missing historical version is never invented from current
text. Snapshots live with the project, so project backup/deletion includes them.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re

from ampuly.layer4_tools.workspace_fs import LexicalWorkspace, SecureWorkspace

MAX_KNOWLEDGE_BYTES = 256 * 1024
MAX_DELETE_VERSIONS = 10_000
_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_VERSION = re.compile(r"sha256:([a-f0-9]{64})\Z")
_FILENAME = re.compile(r"[a-f0-9]{64}\.md\Z")


class KnowledgeVersionError(ValueError):
    """The fixed source cannot be recorded/read without changing its meaning."""


class KnowledgeVersionTooLarge(KnowledgeVersionError):
    """Memory may be larger, but it was never an admissible fixed employee source."""


def _project(value):
    if type(value) is not str or _PROJECT.fullmatch(value) is None:
        raise KnowledgeVersionError("project knowledge scope is invalid")
    return value


def _normalise(content):
    if type(content) is not str:
        raise KnowledgeVersionError("knowledge content must be text")
    return content.replace("\r\n", "\n").replace("\r", "\n").strip()


def knowledge_version(content):
    raw = _normalise(content).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class ProjectKnowledgeVersions:
    def __init__(self, ampuly_home: Path):
        self.home = Path(ampuly_home).absolute()

    @contextmanager
    def _workspace(self):
        with SecureWorkspace.for_roots(str(self.home), str(self.home)) as workspace:
            if isinstance(workspace, LexicalWorkspace):
                raise KnowledgeVersionError("native project knowledge boundary is required")
            yield workspace

    @staticmethod
    def relative_root(project):
        return "projects/" + _project(project) + "/knowledge_versions/project-memory"

    @classmethod
    def relative_path(cls, project, version):
        match = _VERSION.fullmatch(version) if type(version) is str else None
        if match is None:
            raise KnowledgeVersionError("knowledge version is invalid")
        return cls.relative_root(project) + "/" + match.group(1) + ".md"

    @staticmethod
    def _read(workspace, relative):
        fd = workspace.open_read(workspace.resolve(relative))
        try:
            if os.fstat(fd).st_size > MAX_KNOWLEDGE_BYTES:
                raise KnowledgeVersionTooLarge("knowledge content exceeds its byte limit")
            raw = bytearray()
            while chunk := os.read(fd, min(65536, MAX_KNOWLEDGE_BYTES + 1 - len(raw))):
                raw.extend(chunk)
                if len(raw) > MAX_KNOWLEDGE_BYTES:
                    raise KnowledgeVersionTooLarge("knowledge content exceeds its byte limit")
            return bytes(raw).decode("utf-8", errors="strict")
        finally:
            os.close(fd)

    def read(self, project, version):
        relative = self.relative_path(project, version)
        with self._workspace() as workspace:
            content = self._read(workspace, relative)
        if not content or content != _normalise(content) or knowledge_version(content) != version:
            raise KnowledgeVersionError("fixed knowledge content does not match its version")
        return content

    def record(self, project, content):
        """Write a content-addressed object once; never overwrite or repair it.

        Empty/oversize memory is not an admissible employee knowledge source,
        so it keeps existing memory-save behaviour without making a snapshot.
        A failed/partial new snapshot is not silently repaired on another call.
        """
        _project(project)
        content = _normalise(content)
        raw = content.encode("utf-8")
        if not raw or len(raw) > MAX_KNOWLEDGE_BYTES:
            return None
        version = knowledge_version(content)
        relative = self.relative_path(project, version)
        with self._workspace() as workspace:
            target = workspace.resolve(relative)
            try:
                fd = workspace._open_file(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, create_dirs=True)
            except FileExistsError:
                existing = self._read(workspace, relative)
                if existing != content or knowledge_version(existing) != version:
                    raise KnowledgeVersionError("existing fixed knowledge content is corrupt")
            else:
                try:
                    view = memoryview(raw)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("fixed knowledge write made no progress")
                        view = view[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if self._read(workspace, relative) != content:
                    raise KnowledgeVersionError("fixed knowledge publication cannot be verified")
        return version

    def capture_current(self, project, visible_text):
        """Before replacing canonical memory, preserve its actual old bytes.

        visible_text is the original MarkdownMemoryStore projection, supplied
        by its write consumer. No import cycle or alternate hidden-memory view.
        """
        _project(project)
        if not callable(visible_text):
            raise KnowledgeVersionError("visible knowledge projection is required")
        relative = "memory/projects/" + project + ".md"
        with self._workspace() as workspace:
            try:
                raw = self._read(workspace, relative)
            except FileNotFoundError:
                return None
            except KnowledgeVersionTooLarge:
                return None
        return self.record(project, visible_text(raw))

    def replace_current(self, project, content):
        """The formal writer publishes canonical text after version capture."""
        relative = "memory/projects/" + _project(project) + ".md"
        content = _normalise(content)
        with self._workspace() as workspace:
            workspace.write_text(workspace.resolve(relative),
                                 (content + "\n") if content else "", create_dirs=True)

    def delete_project(self, project):
        """Existing explicit project deletion only; no recursive path delete.

        Leave empty directories; remove only bounded, native-pinned objects in
        this project's fixed-version namespace. Other projects are untouched.
        """
        relative = self.relative_root(project)
        with self._workspace() as workspace:
            try:
                rows = []
                for entry in workspace.iter_entries(workspace.resolve(relative), recursive=False):
                    if len(rows) >= MAX_DELETE_VERSIONS or entry.is_dir or _FILENAME.fullmatch(entry.relative_path) is None:
                        raise KnowledgeVersionError("project knowledge deletion needs explicit inventory review")
                    rows.append(relative + "/" + entry.relative_path)
            except FileNotFoundError:
                return
            for path in rows:
                workspace.unlink(workspace.resolve(path))
