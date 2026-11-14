"""Web search and fetch tools."""

from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from ampuly.layer4_tools.output_budget import clip_text, persist_tool_result
from ampuly.layer4_tools.registry import COMPANY_EFFECT_RUNTIME_INTERNAL, ToolDefinition


class _ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = attrs
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        if tag in {"p", "br", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in {"p", "div", "section", "article", "li", "tr"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(str(data or "").split())
        if text:
            self._parts.append(text + " ")

    def text(self) -> str:
        lines = []
        for raw in "".join(self._parts).splitlines():
            line = " ".join(raw.split())
            if line:
                lines.append(line)
        return "\n".join(lines)


def _html_to_text(value: str) -> str:
    parser = _ReadableHTMLParser()
    parser.feed(value)
    return parser.text() or value


class _DuckDuckGoResultsParser(HTMLParser):
    """Collect DuckDuckGo result titles and snippets without regexing nested HTML."""

    _VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.snippets: list[str] = []
        self._capture_kind = ""
        self._capture_depth = 0
        self._capture_href = ""
        self._capture_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capture_kind:
            if tag in self._VOID_TAGS:
                self._capture_parts.append(" ")
            else:
                self._capture_depth += 1
            return

        attributes = {name.lower(): value or "" for name, value in attrs}
        classes = set(attributes.get("class", "").split())
        if "result__a" in classes:
            self._start_capture("title", href=attributes.get("href", ""))
        elif "result__snippet" in classes:
            self._start_capture("snippet")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capture_kind:
            self._capture_parts.append(" ")
            return
        self.handle_starttag(tag, attrs)
        if self._capture_kind:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        _ = tag
        if not self._capture_kind:
            return
        self._capture_depth -= 1
        if self._capture_depth > 0:
            return

        value = " ".join(unescape("".join(self._capture_parts)).split())
        if self._capture_kind == "title":
            self.links.append((self._capture_href, value))
        else:
            self.snippets.append(value)
        self._capture_kind = ""
        self._capture_depth = 0
        self._capture_href = ""
        self._capture_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_kind:
            self._capture_parts.append(data)

    def _start_capture(self, kind: str, *, href: str = "") -> None:
        self._capture_kind = kind
        self._capture_depth = 1
        self._capture_href = href
        self._capture_parts = []


def _normalize_duckduckgo_url(value: str) -> str:
    """Return a direct result URL, retaining a safe DDG URL when decoding fails."""
    href = unescape(str(value or "").strip())
    if not href:
        return ""

    if href.startswith("//"):
        safe_fallback = f"https:{href}"
    else:
        safe_fallback = urljoin("https://duckduckgo.com/", href)

    try:
        parsed = urlsplit(safe_fallback)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""

    hostname = (parsed.hostname or "").lower()
    is_duckduckgo_redirect = (
        (hostname == "duckduckgo.com" or hostname.endswith(".duckduckgo.com"))
        and parsed.path.rstrip("/") == "/l"
    )
    if not is_duckduckgo_redirect:
        return safe_fallback

    try:
        target = unescape(parse_qs(parsed.query).get("uddg", [""])[0]).strip()
        target_parts = urlsplit(target)
    except (ValueError, UnicodeError):
        return safe_fallback
    if target_parts.scheme.lower() in {"http", "https"} and target_parts.netloc:
        return target
    return safe_fallback


def _parse_duckduckgo_results(text: str, max_results: int) -> list[dict[str, str]]:
    parser = _DuckDuckGoResultsParser()
    parser.feed(text)
    parser.close()

    results: list[dict[str, str]] = []
    for index, (url, title) in enumerate(parser.links[: max(0, int(max_results))]):
        snippet = parser.snippets[index] if index < len(parser.snippets) else ""
        results.append(
            {
                "title": title,
                "url": _normalize_duckduckgo_url(url),
                "snippet": snippet,
            }
        )
    return results


def _search_outcome(text: str, results: list[dict[str, str]]) -> tuple[str, str]:
    """Tell "the search ran and found nothing" apart from "it did not run".

    Zero results is a legitimate answer and also what a rate-limit page, a
    challenge page or a changed layout produces. Callers cannot tell those
    apart from the list alone, and one of them means the opposite of the other:
    in trademark screening, "found nothing" becomes a green light to register
    the name.
    """
    if results:
        return "ok", ""
    body = (text or "").lower()
    # The results container is present even when a query genuinely matches
    # nothing, so its absence means the page was not a results page.
    if "result__a" in body or "result__snippet" in body or "no results" in body:
        return "empty", "检索正常完成，未发现匹配结果。"
    if not body.strip():
        return "unverified", "搜索服务返回空响应，无法确认检索是否真正执行。"
    return (
        "unverified",
        "搜索服务未返回结果页（可能被限流或要求验证），"
        "无法区分「确实没有结果」与「本次检索未执行」。",
    )


async def web_search(query: str, max_results: int = 5) -> dict[str, Any]:
    """Search the web using DuckDuckGo HTML scraping (no API key needed).

    The response always carries ``status``: ``ok`` when results came back,
    ``empty`` when the search ran and matched nothing, ``unverified`` when we
    cannot tell whether it ran at all, and ``error`` when the request failed.
    An ``unverified`` result must not be reported as "nothing found".
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (compatible; AMPULY/1.0)"},
            )
            resp.raise_for_status()
            text = resp.text
            results = _parse_duckduckgo_results(text, max_results)
            status, detail = _search_outcome(text, results)
            payload: dict[str, Any] = {
                "results": results,
                "query": query,
                "status": status,
            }
            if detail:
                payload["detail"] = detail
            return payload
    except Exception as e:
        return {
            "error": str(e),
            "query": query,
            "status": "error",
            "results": [],
            "detail": "检索请求失败，本次未获得任何结果，不可视为「未发现匹配」。",
        }


async def web_fetch(
    url: str,
    max_length: int = 20000,
    offset: int = 0,
    save_full: bool = True,
    task: Any | None = None,
) -> dict[str, Any]:
    """Fetch a URL and return its text content."""
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; AMPULY/1.0)"})
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "text" in content_type or "json" in content_type or "xml" in content_type:
                text = _html_to_text(resp.text) if "html" in content_type else resp.text
                start = max(0, int(offset or 0))
                limit = max(1, int(max_length or 20000))
                sliced = text[start:]
                preview = clip_text(sliced, limit=limit, marker="web_fetch truncated")
                next_offset = start + preview.kept_chars if preview.truncated else None
                persisted = {}
                if save_full and (preview.truncated or start > 0):
                    persisted = persist_tool_result(
                        text,
                        tool_name="web_fetch",
                        task=task,
                        extension="txt",
                    )
                return {
                    "content": preview.text,
                    "url": str(resp.url),
                    "final_url": str(resp.url),
                    "status": resp.status_code,
                    "content_type": content_type,
                    "total_chars": len(text),
                    "offset": start,
                    "max_length": limit,
                    "truncated": preview.truncated,
                    "omitted_chars": preview.omitted_chars,
                    "next_offset": next_offset,
                    "full_content_path": persisted.get("full_output_path", ""),
                    "success": True,
                }
            else:
                return {"error": f"Unsupported content type: {content_type}", "url": str(resp.url)}
    except Exception as e:
        return {"error": str(e), "url": url}


def create_web_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="web_search",
            description="Search the web for information. Returns titles, URLs, and snippets.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Max results", "default": 5},
                },
                "required": ["query"],
            },
            func=web_search,
            category="search",
            company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
            permission_effects=["local_read", "network_access"],
        ),
        ToolDefinition(
            name="web_fetch",
            description="Fetch a URL and return its text content.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "max_length": {"type": "integer", "description": "Max content length", "default": 20000},
                    "offset": {"type": "integer", "description": "Character offset to start reading from", "default": 0},
                    "save_full": {"type": "boolean", "description": "Persist full fetched text when preview is truncated", "default": True},
                },
                "required": ["url"],
            },
            func=web_fetch,
            category="search",
            self_bounded_output=True,
            max_result_chars=80_000,
            company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
            permission_effects=["local_read", "network_access"],
        ),
    ]
