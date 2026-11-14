"""Encrypt CRM-bearing project-backup members; key custody outside the archive.

L4: an unencrypted project backup carries ``sales.sqlite.hmac-key`` next to the
database snapshot, so anyone with the zip can recompute keyed digests offline.
Sensitive CRM members are sealed with AES-256-GCM. The wrapping key lives at
``<AMPULY_HOME>/plugins/crm/backup-keys/<project>.key`` — outside
``backups/projects/``, outside the project tree, and never inside the zip.
Putting the key beside the encrypted blob would defeat encryption.
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import stat

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .contracts import fail, identifier

MAGIC = b"CRMBAKv1"
NONCE_BYTES = 12
KEY_BYTES = 32
KEY_DIR = ("plugins", "crm", "backup-keys")
# Members whose plaintext must not travel in a project archive.
SENSITIVE_SUFFIXES = (
    "/plugins/crm/sales.sqlite",
    "/plugins/crm/sales.sqlite.hmac-key",
)
SENSITIVE_NAMES = frozenset({"sales.sqlite", "sales.sqlite.hmac-key"})


def is_crm_sensitive_member(member: str) -> bool:
    text = str(member or "").replace("\\", "/")
    return any(text.endswith(suffix) for suffix in SENSITIVE_SUFFIXES)


def key_path(ampuly_home: Path, project_id: str) -> Path:
    identifier(project_id)
    return Path(ampuly_home).joinpath(*KEY_DIR, f"{project_id}.key")


def _safe(path: Path, *, is_file: bool = True) -> None:
    for part in (*reversed(path.parents), path):
        if not part.exists() and not part.is_symlink():
            continue
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            fail("backup_key_unavailable")
        if part == path and is_file and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            fail("backup_key_unavailable")


def load_or_create_project_key(ampuly_home: Path, project_id: str) -> bytes:
    """Per-project backup wrapping key: random, exclusive-created, never logged."""
    path = key_path(ampuly_home, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe(path.parent, is_file=False)
    _safe(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        pass
    else:
        try:
            os.write(fd, secrets.token_bytes(KEY_BYTES))
            os.fsync(fd)
        finally:
            os.close(fd)
    if not path.exists():
        fail("backup_key_unavailable")
    _safe(path)
    data = path.read_bytes()
    if len(data) != KEY_BYTES:
        fail("backup_key_unavailable")
    return data


def load_project_key(ampuly_home: Path, project_id: str) -> bytes:
    path = key_path(ampuly_home, project_id)
    _safe(path)
    if not path.exists():
        fail("backup_key_unavailable")
    data = path.read_bytes()
    if len(data) != KEY_BYTES:
        fail("backup_key_unavailable")
    return data


def is_sealed(blob: bytes) -> bool:
    return isinstance(blob, (bytes, bytearray)) and bytes(blob[: len(MAGIC)]) == MAGIC


def seal(plaintext: bytes, *, key: bytes, aad: bytes) -> bytes:
    if len(key) != KEY_BYTES:
        fail("backup_key_unavailable")
    if not isinstance(plaintext, (bytes, bytearray)):
        fail("input_invalid")
    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, bytes(plaintext), aad)
    return MAGIC + nonce + ciphertext


def open_sealed(blob: bytes, *, key: bytes, aad: bytes) -> bytes:
    if len(key) != KEY_BYTES:
        fail("backup_key_unavailable")
    raw = bytes(blob)
    if not is_sealed(raw):
        return raw  # legacy plaintext member
    body = raw[len(MAGIC):]
    if len(body) < NONCE_BYTES + 16:
        fail("backup_ciphertext_invalid")
    nonce, ciphertext = body[:NONCE_BYTES], body[NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except Exception:
        fail("backup_ciphertext_invalid")


def open_crm_tree_in_place(crm_dir: Path, *, ampuly_home: Path, project_id: str) -> int:
    """Decrypt sealed CRM files under a restored project tree. Returns how many were opened."""
    root = Path(crm_dir)
    if not root.is_dir():
        return 0
    opened = 0
    key = None
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.name not in SENSITIVE_NAMES:
            continue
        blob = path.read_bytes()
        if not is_sealed(blob):
            continue
        if key is None:
            key = load_project_key(ampuly_home, project_id)
        member = f"project/{project_id}/plugins/crm/{path.name}"
        plain = open_sealed(blob, key=key, aad=member.encode("utf-8"))
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            tmp.write_bytes(plain)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
        opened += 1
    return opened
