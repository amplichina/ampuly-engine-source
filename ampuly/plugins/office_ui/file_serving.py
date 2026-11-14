"""How this console hands a file to a browser -- data phase 2, D7.

One place for the parts the 资料 download and the attachment route share: who may ask (while no account exists the
console trusts this machine, so the request must actually come from it), what the reply says about the file, and how
the bytes are sent from a descriptor that was already checked.

The rules here exist because a file served inline is a page: an ``.html`` attachment opened in a tab on the console's
own origin can read the login token out of ``localStorage``. So everything that is not a picture or a video is an
``attachment`` of type ``application/octet-stream``, and every reply carries the headers that keep it out of caches
and out of other pages.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp.web

# What every file reply carries. ``sandbox`` with no value is the strictest form: an SVG served under it runs no
# script and reaches no origin of its own.
SAFE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}

# What a chat message renders by itself: pictures and video. Everything else is handed over as a download.
INLINE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".svg": "image/svg+xml",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".mpeg": "video/mpeg", ".mpg": "video/mpeg",
}
OCTET_STREAM = "application/octet-stream"
CHUNK = 256 * 1024

_LOOPBACK = {"127.0.0.1", "localhost", "::1", ""}
_ASCII_SAFE = re.compile(r"[^\x20-\x7e]|[\"\\;]")


def loopback_host(host: str) -> bool:
    """Whether this host name is this machine. An empty name is a request with no Host and no socket, as a test mock."""
    return str(host or "").strip() in _LOOPBACK


def host_name(request: Any) -> str:
    """The Host header without its port: ``[::1]:8766`` is ``::1``, ``localhost:8766`` is ``localhost``."""
    raw = str(getattr(request, "host", "") or "").strip().lower()
    if raw.startswith("["):
        closing = raw.find("]")
        return raw[1:closing] if closing > 0 else raw
    if raw.count(":") == 1:
        return raw.split(":", 1)[0]
    return raw  # a bare IPv6 address, or a name with no port


def bootstrap_request_allowed(request: Any, *, bootstrap: bool) -> bool:
    """Whether a request may be served while the console has no login yet.

    Until an owner account exists there is nothing to log in with, so a request from this machine counts as the
    console window itself. That trust is in the peer address today, which a page in the owner's own browser also
    has: it can reach ``127.0.0.1`` and, with a name that resolves there, send any Host it likes. So the name asked
    for must be this machine, and a page that declares an origin must be one of this machine's own.
    """
    if not bootstrap:
        return True
    if not loopback_host(host_name(request)):
        return False
    headers = getattr(request, "headers", {}) or {}
    # A plain cross-site GET -- an <img>, a <script>, a link the owner clicks on somebody else's page -- carries no
    # Origin at all, so the Origin check below would wave it through. Browsers say where a request came from in
    # ``Sec-Fetch-Site``: only the console's own pages (same-origin) and what the person typed or bookmarked (none)
    # may be served while there is nothing to log in with. A client that sends neither header is not a browser and
    # is left to the Host rule above.
    site = str(headers.get("Sec-Fetch-Site", "") or "").strip().lower()
    if site and site not in {"same-origin", "none"}:
        return False
    origin = str(headers.get("Origin", "") or "").strip()
    if not origin:
        return True
    if origin.lower() == "null":
        return False
    split = urlsplit(origin)
    if split.scheme not in {"http", "https"}:
        return False
    return loopback_host(str(split.hostname or ""))


def content_type_for(name: str, *, force_download: bool = False) -> tuple[str, str]:
    """``(content type, disposition)`` for a file name: a picture or a video inline, anything else a download."""
    suffix = PurePosixPath(str(name or "").replace("\\", "/")).suffix.lower()
    guessed = INLINE_TYPES.get(suffix)
    if guessed is None or force_download:
        return OCTET_STREAM, "attachment"
    return guessed, "inline"


def content_disposition(kind: str, name: str) -> str:
    """The header value, with an ASCII fallback and the real name percent-encoded (RFC 6266).

    A name carries whatever the customer called the file: Chinese characters, spaces, quotes, and in a crafted case a
    line break. Only the encoded form keeps the name; the fallback keeps the header parsable.
    """
    disposition = "inline" if str(kind) == "inline" else "attachment"
    clean = str(name or "").replace("\r", "").replace("\n", "").strip() or "file"
    fallback = _ASCII_SAFE.sub("_", clean) or "file"
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{quote(clean, safe='')}"


def parse_range(header: str, size: int) -> tuple[int, int] | None | str:
    """``(start, end)`` for one byte range, ``None`` for the whole file, ``"unsatisfiable"`` for a range past the end.

    Several ranges are answered with the whole file: a console download and a ``<video>`` seek each ask for one.
    """
    raw = str(header or "").strip()
    if not raw.lower().startswith("bytes=") or size <= 0:
        return None
    spec = raw[6:].strip()
    if "," in spec:
        return None
    # Bounded digits: Python refuses to parse an integer of more than 4,300 digits, and that refusal would come out
    # of the handler as a 500 on a file route.
    match = re.fullmatch(r"(\d{0,19})-(\d{0,19})", spec)
    if not match:
        return None
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return None
    if not first:  # the last N bytes
        length = int(last)
        if length <= 0:
            return "unsatisfiable"
        return max(0, size - length), size - 1
    start = int(first)
    if start >= size:
        return "unsatisfiable"
    end = min(int(last), size - 1) if last else size - 1
    if end < start:
        return "unsatisfiable"
    return start, end


async def stream_fd(request: Any, fd: int, *, size: int, content_type: str, disposition: str,
                    filename: str, extra_headers: dict[str, str] | None = None) -> Any:
    """Send the bytes of an already-checked descriptor, and close it whatever happens.

    The descriptor is the check: it was opened under the name that was asked for, so nothing between the check and
    the read can point it elsewhere -- which is what serving by path again allows.
    """
    total = max(0, int(size))
    headers = {**SAFE_HEADERS, **(extra_headers or {}),
               "Content-Disposition": content_disposition(disposition, filename),
               "Accept-Ranges": "bytes"}
    try:
        wanted = parse_range(str((getattr(request, "headers", {}) or {}).get("Range", "") or ""), total)
        if wanted == "unsatisfiable":
            return aiohttp.web.Response(status=416, text="range_not_satisfiable",
                                        headers={**headers, "Content-Range": f"bytes */{total}"},
                                        content_type="text/plain")
        start, end = wanted if isinstance(wanted, tuple) else (0, total - 1)
        length = max(0, end - start + 1) if total else 0
        response = aiohttp.web.StreamResponse(
            status=206 if isinstance(wanted, tuple) else 200,
            headers={**headers, "Content-Length": str(length), "Content-Type": content_type},
        )
        if isinstance(wanted, tuple):
            response.headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        await response.prepare(request)
        if start:
            await asyncio.to_thread(os.lseek, fd, start, os.SEEK_SET)
        left = length
        while left > 0:
            chunk = await asyncio.to_thread(os.read, fd, min(CHUNK, left))
            if not chunk:
                # The file shrank while it was being sent. Stopping here leaves the body shorter than the length
                # that was promised, so the browser marks the download failed instead of saving a truncated file.
                return response
            await response.write(chunk)
            left -= len(chunk)
        await response.write_eof()
        return response
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
