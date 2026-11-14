#!/usr/bin/env python3
"""只读网页取文的受管工具（试跑联网准入 R1–R5）。

规则见 ``docs/4.8-TRIAL-NETWORK-POLICY-2026-10-24.zh.md``：
只读、域白名单、无凭据、限额（10 s / 512 KB / 每步 3 次）、可审计。

白名单来自 ``SELFMEDIA_FETCH_ALLOW``（逗号或空格分隔的主机后缀）。**未配置即全部拒绝**，
不提供默认公网名单。任何重定向都必须仍在白名单内。
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

PUBLIC_FETCH_TOOL = "public_fetch"
ALLOW_ENV = "SELFMEDIA_FETCH_ALLOW"
MAX_BYTES = 512 * 1024
TIMEOUT_S = 10.0
MAX_CALLS_PER_STEP = 3
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml")


def allowed_hosts() -> tuple[str, ...]:
    raw = os.environ.get(ALLOW_ENV, "")
    hosts: list[str] = []
    for item in raw.replace(",", " ").split():
        host = item.strip().lower()
        if "://" in host:
            host = urllib.parse.urlsplit(host).hostname or ""
        if host and host not in hosts:
            hosts.append(host)
    return tuple(hosts)


def _host_allowed(host: str, hosts: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in hosts)


def validate_target(url: str, *, hosts: tuple[str, ...] | None = None) -> str | None:
    """返回拒绝原因；None 表示可以取。"""
    if not isinstance(url, str) or not url.strip():
        return "empty_url"
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return f"scheme_not_allowed:{parsed.scheme or 'none'}"
    if parsed.username or parsed.password:
        return "userinfo_not_allowed"
    host = parsed.hostname or ""
    if not host:
        return "host_missing"
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return "private_host"
    allow = allowed_hosts() if hosts is None else hosts
    if not allow:
        return "allowlist_empty"
    if not _host_allowed(host, allow):
        return f"host_not_allowed:{host}"
    return None


class _AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: tuple[str, ...]) -> None:
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if validate_target(newurl, hosts=self.hosts) is not None:
            raise urllib.error.HTTPError(newurl, code, "redirect_outside_allowlist", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener(hosts: tuple[str, ...]) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _AllowlistRedirect(hosts))


class _Response:
    def __init__(self, status: int, content_type: str, body: bytes) -> None:
        self.status = status
        self.content_type = content_type
        self._body = body

    def read(self, n: int) -> bytes:
        return self._body[:n]

    def geturl(self) -> str:
        return ""

    def close(self) -> None:
        pass


def fetch_text(url: str, *, hosts: tuple[str, ...] | None = None,
               opener: Callable[..., Any] | None = None,
               timeout_s: float = TIMEOUT_S, max_bytes: int = MAX_BYTES) -> dict[str, Any]:
    """GET 一段文本；返回可审计结果，不抛异常。"""
    allow = allowed_hosts() if hosts is None else hosts
    refusal = validate_target(url, hosts=allow)
    if refusal:
        return {"ok": False, "error": refusal, "url": url}
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.hostname is None or not _host_allowed(parsed.hostname, allow):
        return {"ok": False, "error": "host_not_allowed", "url": url}
    opener = opener or _default_opener(allow)
    request = urllib.request.Request(
        url.strip(), method="GET",
        headers={"User-Agent": "AmpulyTrial/1.0", "Accept": "text/html,application/json;q=0.9,*/*;q=0.5"},
    )
    try:
        response = opener.open(request, timeout=timeout_s)
        try:
            body = response.read(max_bytes + 1)
            content_type = str(response.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
            status = int(getattr(response, "status", 200) or 200)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"http_{exc.code}", "url": url, "status": int(exc.code)}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": f"fetch_failed:{type(exc).__name__}", "url": url}
    if len(body) > max_bytes:
        return {"ok": False, "error": "response_too_large", "url": url, "bytes": len(body)}
    if content_type and not any(content_type.startswith(t) for t in _TEXT_TYPES):
        return {"ok": False, "error": f"content_type_not_text:{content_type}", "url": url}
    text = body.decode("utf-8", errors="replace")
    return {"ok": True, "url": url, "status": status, "bytes": len(body), "text": text}


def check_arguments(arguments: dict[str, Any]) -> str:
    """试跑参数检查：只允许 url 一个键，且必须过白名单。返回拒绝原因或空串。"""
    if not isinstance(arguments, dict):
        return "arguments_invalid"
    extra = sorted(str(k) for k in arguments if k != "url")
    if extra:
        return f"unexpected_arguments:{','.join(extra)}"
    return validate_target(str(arguments.get("url", ""))) or ""


def create_public_fetch_tool():
    from ampuly.layer4_tools.registry import ToolDefinition

    async def public_fetch(url: str) -> str:
        result = await asyncio.to_thread(fetch_text, url)
        if result.get("ok"):
            # 正文只进隔离工作区/调用方；工具回执不重复整段正文。
            result = {**result, "text": result["text"][:20000]}
        return json.dumps(result, ensure_ascii=False)

    return ToolDefinition(
        name=PUBLIC_FETCH_TOOL,
        description="Fetch one allow-listed public page as text (read-only, no credentials, 512 KB / 10 s).",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "http(s) URL on the configured allowlist"}},
            "required": ["url"],
        },
        func=public_fetch,
        category="research",
        permission_effects=["network_access"],
    )
