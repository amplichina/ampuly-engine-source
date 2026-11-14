"""User-owned trust decisions for project-local Ampuly configuration.

Project configuration is intentionally powerful: it can select executables,
network endpoints, and credential sources.  Trust records therefore live
outside ``AMPULY_HOME`` so a repository cannot grant trust to itself by
committing files below ``.ampuly``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


_log = logging.getLogger(__name__)

_TRUST_STORE_VERSION = 2
_AMPULY_SOURCE_PREFIX = b"ampuly-workspace-authority-source-v1\0"
_OPENOPC_SOURCE_PREFIX = b"openopc-workspace-authority-source-v1\0"
_OPENOPC_AUTHORITY_V2 = b"openopc-effective-authority-v2\0"
_OPENOPC_AUTHORITY_V1 = b"openopc-effective-authority-v1\0"
_AUTHORITY_SOURCE_FILES = (
    "system_config.yaml",
    "llm_config.yaml",
    "agent_config.yaml",
    "channel_config.yaml",
)
_AUTHORITY_CONFIG_FIELDS = (
    "system",
    "llm",
    "agents",
    "channels",
    "autonomy",
    "capabilities",
)


def canonical_workspace(path: str | Path) -> Path:
    """Return a stable workspace identity across relative and symlink paths."""

    return Path(path).expanduser().resolve(strict=False)


def user_config_root() -> Path:
    """Return an OS-appropriate, user-owned config root independent of AMPULY_HOME."""

    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base.expanduser().resolve(strict=False) / "ampuly"


def default_trust_store_path() -> Path:
    return user_config_root() / "trusted_workspaces.json"


def legacy_openopc_trust_store_path() -> Path:
    """Trust store written when the gate still recognized ``<workspace>/.opc/config``."""

    return user_config_root().parent / "openopc" / "trusted_workspaces.json"


def authority_file_token(path: str | Path) -> str:
    """Content identity of one authority file. A missing file is its own token.

    Symlinks contribute their target, not the bytes it points at, matching
    :func:`authority_source_fingerprint`.
    """

    file_path = Path(path)
    try:
        metadata = file_path.lstat()
    except OSError:
        return "missing"
    if stat.S_ISLNK(metadata.st_mode):
        try:
            target = os.readlink(file_path)
        except OSError as exc:
            return f"symlink:unreadable:{type(exc).__name__}"
        return "symlink:" + os.fsdecode(os.fsencode(target))
    if not stat.S_ISREG(metadata.st_mode):
        return f"nonregular:{stat.S_IFMT(metadata.st_mode)}"
    digest = hashlib.sha256()
    try:
        with file_path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"
    return "sha256:" + digest.hexdigest()


def _source_fingerprint(config_dir: str | Path, prefix: bytes) -> str:
    """Hash project-controlled files that can grant runtime authority.

    Symlinks are represented by their link target without following it.  This
    keeps the pre-parse gate bounded to repository metadata; the normalized
    effective-config check below catches changes in a linked YAML target before
    any engine sink is reached.
    """

    root = Path(config_dir).expanduser()
    digest = hashlib.sha256()
    digest.update(prefix)
    for relative_name in _AUTHORITY_SOURCE_FILES:
        path = root / relative_name
        digest.update(relative_name.encode("utf-8"))
        digest.update(b"\0")
        try:
            metadata = path.lstat()
        except OSError as exc:
            digest.update(f"missing:{type(exc).__name__}".encode("ascii"))
            digest.update(b"\0")
            continue

        if stat.S_ISLNK(metadata.st_mode):
            try:
                target = os.readlink(path)
            except OSError as exc:
                target = f"unreadable:{type(exc).__name__}"
            digest.update(b"symlink\0")
            digest.update(os.fsencode(target))
            digest.update(b"\0")
            continue
        if not stat.S_ISREG(metadata.st_mode):
            digest.update(f"nonregular:{stat.S_IFMT(metadata.st_mode)}".encode("ascii"))
            digest.update(b"\0")
            continue

        digest.update(b"regular\0")
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            digest.update(f"unreadable:{type(exc).__name__}".encode("ascii"))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def authority_source_fingerprint(config_dir: str | Path) -> str:
    """Hash the authority files the current gate checks."""

    return _source_fingerprint(config_dir, _AMPULY_SOURCE_PREFIX)


def legacy_openopc_source_fingerprint(config_dir: str | Path) -> str:
    """Source fingerprint the pre-rename gate stored for ``.opc/config``."""

    return _source_fingerprint(config_dir, _OPENOPC_SOURCE_PREFIX)


def _authority_digest(config: Any, *, prefix: bytes, exclude_defaults: bool) -> str:
    authority: dict[str, Any] = {}
    for field_name in _AUTHORITY_CONFIG_FIELDS:
        value = getattr(config, field_name, None)
        if hasattr(value, "model_dump"):
            authority[field_name] = value.model_dump(mode="json", exclude_defaults=exclude_defaults)
        else:
            authority[field_name] = value
    serialized = json.dumps(
        authority,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(prefix + serialized)
    return f"sha256:{digest.hexdigest()}"


def effective_authority_fingerprint(config: Any) -> str:
    """Hash the configuration fields that control privileged sinks, as far as they differ from the defaults.

    Version 2 (2026-09-26). Version 1 hashed every field with its default, so a release that merely added a field
    with a default (``LLMConfig.stream_idle_timeout``) changed the fingerprint of every trusted workspace and locked
    its owner out at the next start. A value that differs from the default still changes the fingerprint.
    """

    return _authority_digest(config, prefix=b"ampuly-effective-authority-v2\0", exclude_defaults=True)


def legacy_authority_fingerprint(config: Any) -> str:
    """Version 1 of :func:`effective_authority_fingerprint`: every field, defaults included. Read only, to upgrade."""

    return _authority_digest(config, prefix=b"ampuly-effective-authority-v1\0", exclude_defaults=False)


def legacy_openopc_authority_fingerprint(config: Any) -> str:
    """Version 2 authority fingerprint stored beside a ``.opc/config`` trust decision."""

    return _authority_digest(config, prefix=_OPENOPC_AUTHORITY_V2, exclude_defaults=True)


def legacy_openopc_authority_fingerprint_v1(config: Any) -> str:
    """Version 1 authority fingerprint stored beside a ``.opc/config`` trust decision."""

    return _authority_digest(config, prefix=_OPENOPC_AUTHORITY_V1, exclude_defaults=False)


def _is_this_projects_opc_config(workspace: str | Path, config_dir: str | Path) -> bool:
    """True only for ``<this workspace>/.opc/config``. ``.ampuly/config`` is never eligible."""

    directory = Path(config_dir).expanduser()
    if directory.name != "config" or directory.parent.name != ".opc":
        return False
    try:
        root = canonical_workspace(workspace)
        return canonical_workspace(directory.parent.parent) == root and directory.resolve(strict=False) == (
            root / ".opc" / "config"
        ).resolve(strict=False)
    except OSError:
        return False


def _config_for_trust_match(config_dir: Path) -> Any | None:
    """Parse a copy of the on-disk config. The original directory is not written."""

    import shutil
    import tempfile

    from ampuly.core.config import AmpulyConfig

    parent = Path(tempfile.mkdtemp(prefix="ampuly-trust-match-"))
    try:
        copied = parent / "config"
        shutil.copytree(config_dir, copied, symlinks=True)
        return AmpulyConfig.load(copied, trusted_source=True)
    except Exception:
        return None
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def matching_config_trust(workspace: str | Path, config_dir: str | Path) -> dict[str, Any] | None:
    """Find a trust decision for this project's old ``.opc/config``, if one still matches.

    Precedence, and nothing else:

    1. The current ampuly store, which is the user's formal store outside the
       project. Its record is used only when it is keyed to this workspace and
       both fingerprints equal the values recomputed now from the on-disk
       ``.opc/config`` files with the ampuly algorithms.
    2. Otherwise the pre-rename openopc store, under the same workspace key,
       with both fingerprints recomputed using the openopc algorithms.

    A record for ``.ampuly/config``, for any other workspace, or with a stale
    fingerprint is not a match. A stale current-store record does not hide a
    matching openopc grant. This does not grant trust and does not change
    :meth:`WorkspaceTrustStore.require`.
    """

    if not _is_this_projects_opc_config(workspace, config_dir):
        return None
    loaded = _config_for_trust_match(Path(config_dir))
    if loaded is None:
        return None
    current_store = WorkspaceTrustStore()
    current = current_store._record(workspace)
    if (
        current is not None
        and current.get("workspace") == str(canonical_workspace(workspace))
        and current["source_fingerprint"] == authority_source_fingerprint(config_dir)
        and authority_grant_matches("ampuly", current, loaded)
    ):
        return {"kind": "ampuly", "store": str(current_store.path), "record": current}
    legacy_store = WorkspaceTrustStore(legacy_openopc_trust_store_path())
    legacy = legacy_store._record(workspace)
    if (
        legacy is not None
        and legacy.get("workspace") == str(canonical_workspace(workspace))
        and legacy["source_fingerprint"] == legacy_openopc_source_fingerprint(config_dir)
        and authority_grant_matches("openopc", legacy, loaded)
    ):
        return {"kind": "openopc", "store": str(legacy_store.path), "record": legacy}
    return None


def authority_grant_matches(kind: str, record: dict[str, Any], config: Any) -> bool:
    """True when ``record`` trusted this parsed config, not merely the same path."""

    stored = record.get("authority_fingerprint")
    if kind == "openopc":
        return stored in {
            legacy_openopc_authority_fingerprint(config),
            legacy_openopc_authority_fingerprint_v1(config),
        }
    return stored in {
        effective_authority_fingerprint(config),
        legacy_authority_fingerprint(config),
    }


def project_workspace_for_config(
    config_dir: str | Path,
    *,
    active_project_root: str | Path | None = None,
) -> Path | None:
    """Identify ``<workspace>/.ampuly/config`` without classifying arbitrary homes.

    A normal project is recognized by ``pyproject.toml``.  The active project
    root fallback preserves Ampuly's existing behavior in directories that do
    not contain one: ``_find_project_root`` returns the current directory there.
    """

    lexical = Path(os.path.abspath(os.fspath(Path(config_dir).expanduser())))
    if lexical.name != "config" or lexical.parent.name != ".ampuly":
        return None

    workspace = canonical_workspace(lexical.parent.parent)
    if (workspace / "pyproject.toml").is_file():
        return workspace

    if active_project_root is None:
        return None
    active = canonical_workspace(active_project_root)
    expected = (active / ".ampuly" / "config").resolve(strict=False)
    if lexical.resolve(strict=False) == expected:
        return active
    return None


def workspace_from_user_path(path: str | Path) -> Path:
    """Resolve a CLI path to the nearest workspace containing ``.ampuly/config``."""

    candidate = Path(path).expanduser()
    if candidate.name == "config" and candidate.parent.name == ".ampuly":
        workspace = candidate.parent.parent
        if candidate.is_dir():
            return canonical_workspace(workspace)
    elif candidate.name == ".ampuly":
        if (candidate / "config").is_dir():
            return canonical_workspace(candidate.parent)

    start = candidate if candidate.is_dir() else candidate.parent
    start = canonical_workspace(start)
    for parent in (start, *start.parents):
        if (parent / ".ampuly" / "config").is_dir():
            return canonical_workspace(parent)
    raise ValueError(f"No .ampuly/config directory found for {path}")


class WorkspaceTrustRequired(RuntimeError):
    """Raised before project-controlled configuration can be loaded."""

    def __init__(
        self,
        workspace: Path,
        config_dir: Path,
        *,
        reason: str = "untrusted",
        current_fingerprint: str = "",
    ) -> None:
        self.workspace = workspace
        self.config_dir = config_dir
        self.reason = reason
        self.current_fingerprint = current_fingerprint
        if reason == "source_changed":
            summary = "Workspace authority configuration changed since it was trusted"
        elif reason == "authority_changed":
            summary = "Effective workspace authority changed since it was trusted"
        elif reason == "legacy_record":
            summary = "Workspace trust record must be renewed with a configuration fingerprint"
        else:
            summary = "Workspace is not trusted"
        super().__init__(
            f"{summary}: {self.workspace}. Review {self.config_dir} and run "
            f"`ampuly trust add {self.workspace}`.",
        )


class WorkspaceTrustStore:
    """User-owned trust store bound to workspace authority fingerprints."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_trust_store_path()

    def _empty_payload(self) -> dict[str, Any]:
        return {"version": _TRUST_STORE_VERSION, "trusted_workspaces": []}

    def load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return self._empty_payload()

        if not isinstance(raw, dict):
            return self._empty_payload()
        if raw.get("version") != _TRUST_STORE_VERSION:
            return self._empty_payload()
        entries = raw.get("trusted_workspaces", [])
        if not isinstance(entries, list):
            return self._empty_payload()
        normalized_entries: dict[str, dict[str, str]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            workspace = entry.get("workspace")
            source_fingerprint = entry.get("source_fingerprint")
            authority_fingerprint = entry.get("authority_fingerprint")
            if not all(
                isinstance(value, str) and value.strip()
                for value in (workspace, source_fingerprint, authority_fingerprint)
            ):
                continue
            path = Path(workspace).expanduser()
            if not path.is_absolute():
                continue
            canonical = str(canonical_workspace(path))
            normalized_entries[canonical] = {
                "workspace": canonical,
                "source_fingerprint": source_fingerprint,
                "authority_fingerprint": authority_fingerprint,
            }
        normalized = [normalized_entries[key] for key in sorted(normalized_entries)]
        return {"version": _TRUST_STORE_VERSION, "trusted_workspaces": normalized}

    def _save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                tmp_path.chmod(0o600)
            except OSError:
                pass
            os.replace(tmp_path, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except Exception:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def _record(self, workspace: str | Path) -> dict[str, str] | None:
        key = str(canonical_workspace(workspace))
        return next(
            (
                entry
                for entry in self.load()["trusted_workspaces"]
                if entry["workspace"] == key
            ),
            None,
        )

    def is_trusted(self, workspace: str | Path) -> bool:
        return self._record(workspace) is not None

    def trust(
        self,
        workspace: str | Path,
        config_dir: str | Path,
        config: Any,
    ) -> Path:
        canonical = canonical_workspace(workspace)
        payload = self.load()
        entries = {
            entry["workspace"]: entry
            for entry in payload["trusted_workspaces"]
            if entry["workspace"] != str(canonical)
        }
        entries[str(canonical)] = {
            "workspace": str(canonical),
            "source_fingerprint": authority_source_fingerprint(config_dir),
            "authority_fingerprint": effective_authority_fingerprint(config),
        }
        self._save(
            {
                "version": _TRUST_STORE_VERSION,
                "trusted_workspaces": [entries[key] for key in sorted(entries)],
            }
        )
        return canonical

    def untrust(self, workspace: str | Path) -> bool:
        canonical = canonical_workspace(workspace)
        payload = self.load()
        entries = {
            entry["workspace"]: entry
            for entry in payload["trusted_workspaces"]
            if entry["workspace"] != str(canonical)
        }
        removed = len(entries) != len(payload["trusted_workspaces"])
        if removed:
            self._save(
                {
                    "version": _TRUST_STORE_VERSION,
                    "trusted_workspaces": [entries[key] for key in sorted(entries)],
                }
            )
        return removed

    def restore_workspace_entry(self, workspace: str | Path, previous: dict[str, Any] | None) -> None:
        """Put one workspace entry back. ``None`` removes it. Other workspaces stay.

        Fingerprints are written as given. This does not recompute them and does
        not make :meth:`require` accept a different config.
        """

        canonical = str(canonical_workspace(workspace))
        payload = self.load()
        entries = {
            entry["workspace"]: entry
            for entry in payload["trusted_workspaces"]
            if entry["workspace"] != canonical
        }
        if previous is not None:
            source_fingerprint = previous.get("source_fingerprint")
            authority_fingerprint = previous.get("authority_fingerprint")
            if not all(
                isinstance(value, str) and value.strip()
                for value in (source_fingerprint, authority_fingerprint)
            ):
                raise ValueError("trust entry is missing a fingerprint")
            entries[canonical] = {
                "workspace": canonical,
                "source_fingerprint": source_fingerprint,
                "authority_fingerprint": authority_fingerprint,
            }
        self._save(
            {
                "version": _TRUST_STORE_VERSION,
                "trusted_workspaces": [entries[key] for key in sorted(entries)],
            }
        )

    def list_trusted(self) -> list[Path]:
        return [Path(entry["workspace"]) for entry in self.load()["trusted_workspaces"]]

    def require(
        self,
        workspace: str | Path,
        config_dir: str | Path,
        config: Any | None = None,
    ) -> None:
        canonical = canonical_workspace(workspace)
        resolved_config_dir = Path(config_dir).resolve(strict=False)
        current_source = authority_source_fingerprint(config_dir)
        record = self._record(canonical)
        if record is None:
            reason = "legacy_record" if self._has_legacy_record(canonical) else "untrusted"
            raise WorkspaceTrustRequired(
                canonical,
                resolved_config_dir,
                reason=reason,
                current_fingerprint=current_source,
            )
        if record["source_fingerprint"] != current_source:
            raise WorkspaceTrustRequired(
                canonical,
                resolved_config_dir,
                reason="source_changed",
                current_fingerprint=current_source,
            )
        if config is not None:
            current_authority = effective_authority_fingerprint(config)
            stored_authority = record["authority_fingerprint"]
            if stored_authority != current_authority:
                if stored_authority != legacy_authority_fingerprint(config):
                    raise WorkspaceTrustRequired(
                        canonical,
                        resolved_config_dir,
                        reason="authority_changed",
                        current_fingerprint=current_source,
                    )
                # A record written by version 1 for exactly this authority: the same trust, re-stated in version 2.
                self._upgrade_authority_fingerprint(canonical, stored_authority, current_authority)

    def _upgrade_authority_fingerprint(self, workspace: Path, old: str, new: str) -> None:
        """Rewrite one record's authority fingerprint from version 1 to 2; a failure only means it is checked by v1 again."""

        try:
            payload = self.load()
            changed = False
            for entry in payload["trusted_workspaces"]:
                if entry["workspace"] == str(workspace) and entry["authority_fingerprint"] == old:
                    entry["authority_fingerprint"] = new
                    changed = True
            if changed:
                self._save(payload)
        except Exception:
            _log.warning("trust record for %s could not be upgraded to the v2 authority fingerprint", workspace,
                         exc_info=True)

    def _has_legacy_record(self, workspace: Path) -> bool:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        entries = raw.get("trusted_workspaces", []) if isinstance(raw, dict) else []
        if not isinstance(entries, list):
            return False
        key = str(canonical_workspace(workspace))
        return any(
            isinstance(entry, str)
            and Path(entry).expanduser().is_absolute()
            and str(canonical_workspace(entry)) == key
            for entry in entries
        )


def save_explicit_workspace_authority_change(
    config: Any,
    config_dir: str | Path,
) -> Path | None:
    """Persist an explicit UI/CLI authority change without invalidating trust.

    A project cannot call this before it is trusted: the current source
    fingerprint is verified first.  The user-facing permission controls call
    it only after an explicit action.  If either serialization or the
    user-owned trust-store update fails, authority source files are restored
    so the next startup never sees a half-applied change.
    """

    root = Path(config_dir).expanduser().resolve(strict=False)
    workspace = project_workspace_for_config(
        root,
        active_project_root=Path.cwd(),
    )
    if workspace is None:
        config.save(root)
        return None

    store = WorkspaceTrustStore()
    store.require(workspace, root)
    snapshots: dict[Path, bytes | None] = {}
    for relative_name in _AUTHORITY_SOURCE_FILES:
        path = root / relative_name
        try:
            snapshots[path] = path.read_bytes()
        except FileNotFoundError:
            snapshots[path] = None

    try:
        config.save(root)
        normalized = config.__class__.load(root, trusted_source=True)
        store.trust(workspace, root, normalized)
        bind = getattr(config, "bind_workspace_trust", None)
        if callable(bind):
            bind(workspace, root)
        return workspace
    except BaseException:
        for path, payload in snapshots.items():
            if payload is None:
                path.unlink(missing_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "wb",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".restore",
                    delete=False,
                ) as handle:
                    tmp_path = Path(handle.name)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, path)
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
        raise


def _bound_workspace_for(config: Any, root: Path) -> Path | None:
    """The workspace a loaded config was bound to, if it was bound for exactly ``root``."""

    workspace = getattr(config, "_trusted_workspace", None)
    bound_dir = getattr(config, "_trusted_config_dir", None)
    if workspace is None or bound_dir is None:
        return None
    if Path(bound_dir).resolve(strict=False) != root:
        return None
    return Path(workspace)


class ProductAuthorityRebind:
    """Renew a workspace's trust fingerprint after the product rewrites its own authority files.

    ``AmpulyConfig.save`` re-renders the four authority source files on every save, even when it
    was only asked to persist an organization change (a hire at dispatch time, an installed
    package).  Before this guard existed that re-render changed their byte fingerprint, and the
    next start refused the workspace as "changed since it was trusted": the product's own write
    locked the owner out.

    The guard is created *before* the files are written and finished *after*.  It never widens
    trust.  A rebind happens only when, before the write, the workspace already had a trust
    record and the files on disk still matched it (so a hand edit is never laundered into the
    product's write), and when the effective authority on disk after the write is exactly the
    one that record already trusts.  Only the byte fingerprint is renewed then.  Anything else
    leaves the record alone, and the next start asks the owner as before.  Deliberate authority
    changes keep going through :func:`save_explicit_workspace_authority_change`.
    """

    def __init__(
        self,
        store: WorkspaceTrustStore,
        workspace: Path,
        root: Path,
        config: Any,
        authority_fingerprint: str,
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.root = root
        self.config = config
        self.authority_fingerprint = authority_fingerprint

    @classmethod
    def begin(cls, config: Any, config_dir: str | Path) -> "ProductAuthorityRebind | None":
        """Check the pre-write state; ``None`` means the write will not touch the trust record."""

        root = Path(config_dir).expanduser().resolve(strict=False)
        workspace = _bound_workspace_for(config, root)
        if workspace is None:
            try:
                active = Path.cwd()
            except OSError:
                active = None
            workspace = project_workspace_for_config(root, active_project_root=active)
        if workspace is None:
            return None
        store = WorkspaceTrustStore()
        try:
            # Source bytes on disk and the authority about to be written must both still be
            # the trusted ones; a stale or missing record is left for the owner to renew.
            store.require(workspace, root, config)
        except WorkspaceTrustRequired:
            return None
        record = store._record(workspace)
        if record is None:
            return None
        # require() passing means the record holds exactly this authority -- in version 2, or in version 1 when its
        # in-place upgrade could not be written. Keep it in the version finish() computes, not as the record spells it.
        return cls(store, workspace, root, config, effective_authority_fingerprint(config))

    def finish(self) -> Path | None:
        """Renew the byte fingerprint if the authority on disk is still the trusted one."""

        try:
            written = authority_source_fingerprint(self.root)
            try:
                normalized = self.config.__class__.load(self.root, trusted_source=True)
            except Exception:
                _log.warning(
                    "config written by the product could not be re-read from %s; renewing trust from memory",
                    self.root,
                    exc_info=True,
                )
                normalized = self.config
            if effective_authority_fingerprint(normalized) != self.authority_fingerprint:
                _log.warning(
                    "product write to %s changed the effective authority; trust record left for the owner",
                    self.root,
                )
                return None
            if authority_source_fingerprint(self.root) != written:
                # Something else touched the files between the write and now; not ours to vouch for.
                return None
            self.store.trust(self.workspace, self.root, normalized)
            bind = getattr(self.config, "bind_workspace_trust", None)
            if callable(bind):
                bind(self.workspace, self.root)
            return self.workspace
        except Exception:
            _log.warning(
                "workspace trust could not be renewed after the product wrote %s",
                self.root,
                exc_info=True,
            )
            return None
