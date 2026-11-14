#!/usr/bin/env python3
# 来源：yt-dlp 项目（Unlicense，公有领域）https://github.com/yt-dlp/yt-dlp
# 固定点：9b58cb33d0cd3e823c8d8732f6adce8faa1f37e3（2026.10.21）
# 本文件是安普里自写的受管包装层，不复制 yt-dlp 源码；通过子进程调用其 CLI。
# 登记见 THIRD_PARTY_NOTICES.md。
"""yt-dlp 的受管采集包装。

只做三件事：校验来源与授权、用固定参数调用 yt-dlp、把媒体/元数据落进隔离目录并计算哈希。
不提供登录态默认值、不使用 `--exec`、不允许播放列表、不绕过 DRM/付费/登录墙。

调用方必须给出 ``authorization_basis``（自有内容、已获授权或明确许可）；本模块只记录，
不替调用方判断授权是否成立。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

DEFAULT_MAX_DURATION_S = 1800
MAX_MAX_DURATION_S = 6 * 3600
DEFAULT_MAX_FILESIZE = "2G"
DEFAULT_TIMEOUT_S = 900


class IngestRefused(ValueError):
    """来源或参数不允许采集；调用方应把它当作拒绝，而不是失败重试。"""


@dataclass
class IngestResult:
    outcome: str  # downloaded | refused | failed
    media_path: str = ""
    sha256: str = ""
    size_bytes: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "media_path": self.media_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "metadata": self.metadata,
            "error": self.error,
        }


def _host_is_private(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith(".localhost")
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


def validate_url(url: str) -> str:
    """只接受公开 http(s) 来源；返回规范化的 URL。"""
    if not isinstance(url, str) or not url.strip():
        raise IngestRefused("empty_url")
    value = url.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise IngestRefused(f"scheme_not_allowed:{parsed.scheme or 'none'}")
    if not parsed.hostname:
        raise IngestRefused("host_missing")
    if parsed.username or parsed.password:
        raise IngestRefused("userinfo_not_allowed")
    if _host_is_private(parsed.hostname):
        raise IngestRefused(f"private_host:{parsed.hostname}")
    return value


def build_args(url: str, out_dir: Path, *, max_duration_s: int = DEFAULT_MAX_DURATION_S,
               cookies_file: str | None = None, max_filesize: str = DEFAULT_MAX_FILESIZE,
               ) -> list[str]:
    """固定的安全参数。任何调用方都不能追加 --exec / --cookies-from-browser。"""
    if not isinstance(max_duration_s, int) or not 1 <= max_duration_s <= MAX_MAX_DURATION_S:
        raise IngestRefused(f"max_duration_invalid:{max_duration_s}")
    args = [
        "--no-config",
        "--no-playlist",
        "--no-exec",
        "--restrict-filenames",
        "--max-filesize", str(max_filesize),
        "--match-filter", f"duration <= {max_duration_s}",
        "--write-info-json",
        "--print", "after_move:filepath",
        "-o", str(out_dir / "%(title).80s [%(id)s].%(ext)s"),
    ]
    if cookies_file:
        if not Path(cookies_file).is_file():
            raise IngestRefused(f"cookies_file_missing:{cookies_file}")
        args += ["--cookies", str(cookies_file)]
    args += ["--", validate_url(url)]
    return args


def _command_prefix(ytdlp_bin: Sequence[str] | str | None) -> list[str]:
    if ytdlp_bin is None:
        found = shutil.which("yt-dlp")
        if not found:
            raise IngestRefused("ytdlp_not_installed")
        return [found]
    if isinstance(ytdlp_bin, str):
        return [ytdlp_bin]
    return list(ytdlp_bin)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _collect(out_dir: Path) -> tuple[Path | None, dict[str, Any]]:
    infos = sorted(out_dir.glob("*.info.json"))
    metadata: dict[str, Any] = {}
    if infos:
        try:
            metadata = json.loads(infos[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = {}
    media = [p for p in out_dir.rglob("*")
             if p.is_file() and not p.name.endswith((".info.json", ".part", ".ytdl"))]
    media.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return (media[0] if media else None), metadata


def ingest(url: str, out_dir: Path | str, *, authorization_basis: str,
           max_duration_s: int = DEFAULT_MAX_DURATION_S,
           ytdlp_bin: Sequence[str] | str | None = None,
           cookies_file: str | None = None,
           timeout_s: int = DEFAULT_TIMEOUT_S) -> IngestResult:
    """采集一个公开视频/音频到 ``out_dir``。不抛异常：拒绝与失败都落在返回值里。"""
    if not isinstance(authorization_basis, str) or len(authorization_basis.strip()) < 4:
        return IngestResult("refused", error="authorization_basis_missing")
    try:
        safe_url = validate_url(url)
        target = Path(out_dir)
        target.mkdir(parents=True, exist_ok=True)
        args = build_args(safe_url, target, max_duration_s=max_duration_s, cookies_file=cookies_file)
        prefix = _command_prefix(ytdlp_bin)
    except IngestRefused as exc:
        return IngestResult("refused", error=str(exc))
    except OSError as exc:
        return IngestResult("failed", error=f"workspace_error:{exc}")

    try:
        proc = subprocess.run(prefix + args, capture_output=True, text=True,
                              timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return IngestResult("failed", error=f"timeout:{timeout_s}s")
    except OSError as exc:
        return IngestResult("failed", error=f"spawn_failed:{exc}")

    media, metadata = _collect(target)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        return IngestResult("failed", error=f"exit_{proc.returncode}:{tail}", metadata=metadata)
    if media is None:
        return IngestResult("failed", error="no_media_produced", metadata=metadata)

    metadata = {**metadata, "authorization_basis": authorization_basis.strip(),
                "requested_url": safe_url}
    return IngestResult("downloaded", media_path=str(media), sha256=_sha256(media),
                        size_bytes=media.stat().st_size, metadata=metadata)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="受管视频采集：校验来源、调用 yt-dlp、产出哈希与元数据")
    parser.add_argument("url")
    parser.add_argument("--out", required=True)
    parser.add_argument("--authorization", required=True, help="授权依据：自有/已获授权/明确许可")
    parser.add_argument("--max-duration", type=int, default=DEFAULT_MAX_DURATION_S)
    parser.add_argument("--cookies", default=None)
    a = parser.parse_args(argv)
    result = ingest(a.url, a.out, authorization_basis=a.authorization,
                    max_duration_s=a.max_duration, cookies_file=a.cookies)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0 if result.outcome == "downloaded" else 2


if __name__ == "__main__":
    sys.exit(main())
