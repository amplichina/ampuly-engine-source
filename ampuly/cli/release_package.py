"""Create a deterministic, reviewed source archive from a public repository commit.

Runtime data and developer records are never distribution inputs. Source archives
carry declared dependencies and a frontend lockfile; they do not claim to contain
a resolved Python runtime, browser binary, or a production acceptance result.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Public source distribution uses an allowlist as well as runtime exclusions.
RUNTIME_EXCLUDED_PREFIXES = (
    ".ampuly/", ".opc/", ".tmp/", ".backups/", "deploy/customers/",
)
EXCLUDED_PREFIXES = RUNTIME_EXCLUDED_PREFIXES + (
    "docs/", "upstream/", "_handoff/", "packaging/", "dist/", "build/",
    "ampuly/plugins/office_ui/frontend_dist/",
)
EXCLUDED_NAMES = ("FDE-PLAN.md", "ENGINE-PLAN.md", "CLAUDE.md", "AGENTS.md")
PUBLIC_SOURCE_PREFIXES = (
    "ampuly/", "config/", "skills/core/",
    "examples/isolation/", "licenses/",
)
PUBLIC_SOURCE_FILES = frozenset({
    "LICENSE", "NOTICE.md", "THIRD_PARTY_NOTICES.md", "README.md", "SECURITY.md",
    "SPONSORSHIP.zh.md",
    "pyproject.toml", ".gitignore", ".gitattributes",
    "distribution/prompts/talent/general-default-employee.md",
    "scripts/__init__.py", "scripts/process_probe.py", "scripts/product_backup.py",
    "scripts/runtime_lock.py", "scripts/runtime_sbom.py", "scripts/generate_sbom.py",
    "tests/test_public_release.py",
})
RETAINED_DOCUMENT_ASSETS = frozenset({
    "examples/isolation/README.md", "examples/isolation/sample-clean.txt",
    "examples/isolation/CLEAN-PARAMS-POINTER.md", "examples/isolation/clean-params-template.json",
})
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
RELEASE_SCHEMA = "ampuly-source-release/1"
REQUIRED_RELEASE_EVIDENCE = (
    "LICENSE", "NOTICE.md", "THIRD_PARTY_NOTICES.md", "pyproject.toml",
    "licenses/OpenOPC-MIT-LICENSE", "SPONSORSHIP.zh.md",
    "ampuly/plugins/office_ui/frontend_src/package-lock.json",
    "examples/isolation/clean-params-template.json",
)


def public_source_path(path: str) -> bool:
    """Whether a committed path is eligible for a public source archive."""
    if path.startswith("/") or "\\" in path:
        return False
    parts = path.split("/")
    if any(part.lower() in {"", ".", "..", ".git", ".ampuly", ".opc", ".venv", "node_modules", "__pycache__", ".tmp", ".backups", "sessions", "logs"} for part in parts):
        return False
    if any(part == ".env" or part.startswith(".env.") for part in parts):
        return False
    if path.lower().endswith((".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".db-journal", ".log", ".jsonl", ".pyc", ".pyo", ".pem", ".key", ".p12", ".pfx", ".zip")):
        return False
    return (
        (path in PUBLIC_SOURCE_FILES or path.startswith(PUBLIC_SOURCE_PREFIXES))
        and not path.startswith(EXCLUDED_PREFIXES)
        and parts[-1] not in EXCLUDED_NAMES
    )


class PackagingError(RuntimeError):
    pass


@dataclass
class ReleasePackage:
    archive: Path
    sbom: Path
    manifest: Path
    commit: str
    file_count: int
    archive_sha256: str
    working_tree_dirty: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "archive": self.archive.name,
            "sbom": self.sbom.name,
            "commit": self.commit,
            "file_count": self.file_count,
            "archive_sha256": self.archive_sha256,
            "working_tree_dirty": self.working_tree_dirty,
        }


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "--no-optional-locks", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise PackagingError(f"git {' '.join(args)} failed: {(result.stderr or '').strip()}")
    return result.stdout


def head_commit(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def working_tree_dirty(repo: Path) -> bool:
    """Whether tracked files differ from HEAD.

    The archive comes from HEAD either way; this is recorded so a package
    built while someone was mid-edit is not mistaken for a clean release.
    """
    return bool(_git(repo, "status", "--porcelain", "--untracked-files=no").strip())


def shipped_paths(repo: Path, commit: str) -> list[str]:
    listing = _git(repo, "ls-tree", "-r", "--name-only", "-z", commit)
    paths = [path for path in listing.split("\0") if path]
    kept = [
        path for path in paths
        if public_source_path(path)
    ]
    if not kept:
        raise PackagingError(f"commit {commit[:12]} contains nothing to ship")
    return sorted(kept)


def _blob(repo: Path, commit: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "--no-optional-locks", "show", f"{commit}:{path}"], cwd=str(repo), capture_output=True,
    )
    if result.returncode != 0:
        raise PackagingError(f"cannot read {path} at {commit[:12]}")
    return result.stdout


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_archive(repo: Path, destination: Path, *, commit: str) -> tuple[Path, int]:
    """Write a deterministic zip of ``commit`` and return it with its file count."""
    paths = shipped_paths(repo, commit)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            info = zipfile.ZipInfo(filename=path, date_time=FIXED_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, _blob(repo, commit, path))
    return destination, len(paths)


def _sbom_builder(repo: Path):
    """``scripts/generate_sbom.py`` is a script, not an importable package."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_generate_sbom", repo / "scripts" / "generate_sbom.py")
    if spec is None or spec.loader is None:
        raise PackagingError("scripts/generate_sbom.py is missing; cannot produce an SBOM")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_sbom


def package_customer_release(
    repo: Path,
    customer: str,
    *,
    commit: str | None = None,
    generated_at: str | None = None,
) -> ReleasePackage:
    """Build the archive, its SBOM and a manifest under the customer's ``dist/``."""
    customer_root = repo / "deploy" / "customers" / customer
    if not customer_root.is_dir():
        raise PackagingError(f"no customer deployment at {customer_root}; run `ampuly distribution init {customer}` first")
    resolved = (commit or head_commit(repo)).strip()
    shipped = set(shipped_paths(repo, resolved))
    missing_evidence = [path for path in REQUIRED_RELEASE_EVIDENCE if path not in shipped]
    if missing_evidence:
        raise PackagingError(
            "release evidence is incomplete at " + resolved[:12] + ": " + ", ".join(missing_evidence)
        )
    short = resolved[:12]
    dist = customer_root / "dist"
    archive_path = dist / f"ampuly-engine-source-{short}.zip"
    archive, file_count = build_archive(repo, archive_path, commit=resolved)
    digest = sha256_file(archive)

    build_sbom = _sbom_builder(repo)

    sbom_path = dist / f"ampuly-engine-source-{short}.spdx.json"
    document = build_sbom(repo, artifact=archive)
    sbom_path.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    dirty = working_tree_dirty(repo)
    manifest_path = dist / "manifest.json"
    manifest = {
        "schema": RELEASE_SCHEMA,
        "customer": customer,
        "commit": resolved,
        "generated_at": generated_at or datetime.now().isoformat(timespec="seconds"),
        "archive": archive.name,
        "archive_sha256": digest,
        "sbom": sbom_path.name,
        "sbom_sha256": sha256_file(sbom_path),
        "file_count": file_count,
        "excluded_prefixes": list(EXCLUDED_PREFIXES),
        "required_release_evidence": list(REQUIRED_RELEASE_EVIDENCE),
        "working_tree_dirty": dirty,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return ReleasePackage(
        archive=archive, sbom=sbom_path, manifest=manifest_path, commit=resolved,
        file_count=file_count, archive_sha256=digest, working_tree_dirty=dirty,
    )


def verify_customer_release(repo: Path, customer: str) -> list[str]:
    """Problems with an archived package, or an empty list.

    A package whose archive no longer matches its manifest -- or whose SBOM
    is bound to a different file -- is worse than no package at all, because
    it looks like evidence.
    """
    dist = repo / "deploy" / "customers" / customer / "dist"
    manifest_path = dist / "manifest.json"
    if not manifest_path.is_file():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{customer}: dist/manifest.json is unreadable ({exc})"]

    problems: list[str] = []
    schema = str(manifest.get("schema", "") or "")
    archive = dist / str(manifest.get("archive", "") or "")
    if not archive.is_file():
        problems.append(f"{customer}: the manifest names an archive that is not there ({archive.name})")
    else:
        digest = sha256_file(archive)
        if digest != str(manifest.get("archive_sha256", "") or ""):
            problems.append(f"{customer}: {archive.name} does not match the digest in its manifest")
        if schema == RELEASE_SCHEMA:
            try:
                with zipfile.ZipFile(archive) as bundle:
                    members = bundle.namelist()
            except (OSError, zipfile.BadZipFile) as exc:
                problems.append(f"{customer}: {archive.name} is not a readable ZIP ({exc})")
            else:
                if len(members) != len(set(members)):
                    problems.append(f"{customer}: {archive.name} contains duplicate member names")
                if len(members) != int(manifest.get("file_count", -1)):
                    problems.append(f"{customer}: {archive.name} file count does not match the manifest")
                required = manifest.get("required_release_evidence", [])
                if required != list(REQUIRED_RELEASE_EVIDENCE):
                    problems.append(f"{customer}: manifest release-evidence contract is incomplete")
                for required_path in REQUIRED_RELEASE_EVIDENCE:
                    if required_path not in members:
                        problems.append(f"{customer}: {archive.name} is missing {required_path}")
                for member in members:
                    if not public_source_path(member):
                        problems.append(f"{customer}: {archive.name} contains a non-public source path {member}")
                        break

    sbom_path = dist / str(manifest.get("sbom", "") or "")
    if not sbom_path.is_file():
        problems.append(f"{customer}: the manifest names an SBOM that is not there ({sbom_path.name})")
        return problems
    if schema == RELEASE_SCHEMA:
        expected_sbom = str(manifest.get("sbom_sha256", "") or "")
        if not expected_sbom or sha256_file(sbom_path) != expected_sbom:
            problems.append(f"{customer}: {sbom_path.name} does not match the digest in its manifest")
    try:
        document = json.loads(sbom_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [*problems, f"{customer}: {sbom_path.name} is unreadable ({exc})"]
    bound = {
        str(checksum.get("checksumValue", ""))
        for entry in document.get("files", []) or []
        for checksum in entry.get("checksums", []) or []
        if str(checksum.get("algorithm", "")).upper() == "SHA256"
    }
    if str(manifest.get("archive_sha256", "") or "") not in bound:
        problems.append(f"{customer}: {sbom_path.name} is not bound to the archive in its manifest")
    return problems


def packaged_customers(repo: Path) -> list[str]:
    root = repo / "deploy" / "customers"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "dist" / "manifest.json").is_file())
