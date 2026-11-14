"""Fail-closed inspection and publication for browser downloads.

This is not an antivirus replacement.  It prevents the product from turning
obvious executable content, type-confused files, active documents and unsafe
archives into delivery artifacts.  A file is first saved under a random name
in a product-owned quarantine directory; only inspected bytes are published.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

DEFAULT_SAFE_TYPES = frozenset({
    "pdf", "png", "jpg", "jpeg", "gif", "webp", "bmp",
    "csv", "txt", "md", "json", "yaml", "yml", "xml", "svg",
    "zip", "docx", "xlsx", "pptx",
})
DANGEROUS_TYPES = frozenset({
    "exe", "dll", "com", "scr", "msi", "msp", "bat", "cmd", "ps1", "psm1",
    "vbs", "vbe", "js", "jse", "wsf", "wsh", "hta", "lnk", "url", "reg",
    "jar", "apk", "app", "dmg", "pkg", "deb", "rpm", "sh", "bash", "py",
    "docm", "xlsm", "pptm", "xlam", "iso", "img",
})

_TEXT_TYPES = frozenset({"csv", "txt", "md", "json", "yaml", "yml", "xml", "svg"})
_ZIP_TYPES = frozenset({"zip", "docx", "xlsx", "pptx"})
_ACTIVE_TEXT = re.compile(
    rb"<\s*script\b|javascript\s*:|on(?:load|error|click)\s*=|powershell(?:\.exe)?\s+-(?:enc|encodedcommand)\b|"
    rb"(?:cmd(?:\.exe)?\s+/c)|(?:wscript|cscript)(?:\.exe)?\b",
    re.IGNORECASE,
)
_PDF_ACTIVE = re.compile(rb"/(?:JavaScript|JS|OpenAction|Launch)\b", re.IGNORECASE)
_EICAR = b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE"


def normalized_types(types: object) -> tuple[str, ...]:
    requested = {
        str(item or "").strip().lower().lstrip(".")
        for item in list(types or [])
        if str(item or "").strip()
    }
    return tuple(sorted(requested or DEFAULT_SAFE_TYPES))


def preflight_type(filename: str, allowed_types: tuple[str, ...]) -> tuple[bool, str, str]:
    suffix = Path(filename).suffix.lower().lstrip(".")
    if not suffix:
        return False, "type_not_allowed:none", suffix
    if suffix in DANGEROUS_TYPES:
        return False, f"dangerous_type:{suffix}", suffix
    if suffix not in set(allowed_types):
        return False, f"type_not_allowed:{suffix}", suffix
    return True, "", suffix


def safe_filename(suggested: str) -> str:
    name = Path(str(suggested or "download")).name
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", name).strip(" .")[:180]
    if not name:
        name = "download"
    stem = Path(name).stem.upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
        name = f"_{name}"
    return name


def deliverable_filename(suffix: str, digest: str) -> str:
    """Return a product-owned name; page-controlled names never enter the business workspace."""
    clean_suffix = str(suffix or "").strip().lower().lstrip(".")
    clean_digest = str(digest or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", clean_digest):
        raise ValueError("invalid download digest")
    if clean_suffix not in DEFAULT_SAFE_TYPES:
        raise ValueError("invalid download type")
    return f"download-{clean_digest[:12]}.{clean_suffix}"


def prepare_confined_directory(directory: Path, root: Path) -> None:
    """Create a destination below its authority root without following redirected components."""
    root = Path(root)
    directory = Path(directory)
    try:
        relative = directory.relative_to(root)
    except ValueError as exc:
        raise ValueError("download directory is outside its authority root") from exc
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise ValueError("download authority root is redirected")
    current = root
    for part in relative.parts:
        current = current / part
        current.mkdir(exist_ok=True)
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("download directory contains a redirected component")
    try:
        directory.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("download directory escaped its authority root") from exc


def _kind(head: bytes) -> str:
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "zip"
    if head.startswith(b"MZ") or head.startswith(b"\x7fELF") or head.startswith(
        (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce")
    ):
        return "executable"
    if b"\x00" not in head:
        try:
            head.decode("utf-8-sig")
            return "text"
        except UnicodeDecodeError:
            pass
    return "unknown"


def _safe_archive(path: Path, max_bytes: int) -> str:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return "content_type_mismatch:zip"
    total = 0
    with archive:
        infos = archive.infolist()
        if len(infos) > 5_000:
            return "archive_too_many_entries"
        for info in infos:
            raw = str(info.filename or "").replace("\\", "/")
            member = PurePosixPath(raw)
            if (
                not raw or raw.startswith("/") or "\x00" in raw or ":" in raw
                or any(part in {"", ".", ".."} for part in member.parts)
            ):
                return "archive_unsafe_path"
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                return "archive_contains_link"
            total += max(0, int(info.file_size or 0))
            if total > max(max_bytes * 4, 100 * 1024 * 1024):
                return "archive_expands_too_large"
            suffix = member.suffix.lower().lstrip(".")
            lowered = raw.casefold()
            if suffix in DANGEROUS_TYPES or lowered.endswith("vbaproject.bin"):
                return f"archive_contains_active_content:{suffix or 'vba'}"
    return ""


def inspect_quarantined(path: Path, *, suffix: str, max_bytes: int) -> dict[str, object]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        return {"ok": False, "reason": "not_a_regular_file"}
    size = int(info.st_size)
    if size > max_bytes:
        return {"ok": False, "reason": f"too_large:{size}>{max_bytes}", "bytes": size}
    with path.open("rb") as handle:
        head = handle.read(min(max_bytes + 1, 1024 * 1024))
    if _EICAR in head.upper():
        return {"ok": False, "reason": "malware_test_signature", "bytes": size}
    kind = _kind(head)
    if kind == "executable":
        return {"ok": False, "reason": "executable_content", "bytes": size, "detected": kind}
    expected = "jpeg" if suffix in {"jpg", "jpeg"} else "zip" if suffix in _ZIP_TYPES else "text" if suffix in _TEXT_TYPES else suffix
    if kind != expected:
        return {"ok": False, "reason": f"content_type_mismatch:{suffix}:{kind}", "bytes": size, "detected": kind}
    if kind == "pdf" and _PDF_ACTIVE.search(head):
        return {"ok": False, "reason": "active_pdf_content", "bytes": size, "detected": kind}
    if suffix in {"svg", "xml"} and _ACTIVE_TEXT.search(head):
        return {"ok": False, "reason": "active_text_content", "bytes": size, "detected": kind}
    if suffix in _ZIP_TYPES:
        reason = _safe_archive(path, max_bytes)
        if reason:
            return {"ok": False, "reason": reason, "bytes": size, "detected": kind}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return {"ok": True, "bytes": size, "sha256": digest.hexdigest(), "detected": kind, "scanner": "superones_builtin_v1"}


def publish_without_overwrite(quarantined: Path, directory: Path, filename: str, digest: str) -> Path:
    """Publish the already-inspected inode, never replacing an existing path."""
    directory.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(filename).stem, Path(filename).suffix
    candidates = [filename]
    candidates.extend(f"{stem}-{index}{suffix}" for index in range(2, 100))
    for candidate in candidates:
        target = directory / candidate
        try:
            os.link(quarantined, target)
            quarantined.unlink(missing_ok=True)
            return target
        except FileExistsError:
            continue
        except OSError:
            # Some filesystems do not support hard links. Exclusive create
            # preserves the no-overwrite guarantee; the bytes were already
            # inspected and the source remains in the same product directory.
            try:
                with quarantined.open("rb") as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination, length=1 << 20)
                quarantined.unlink(missing_ok=True)
                return target
            except FileExistsError:
                continue
            except Exception:
                target.unlink(missing_ok=True)
                raise
    raise FileExistsError("could not allocate a unique download name")
