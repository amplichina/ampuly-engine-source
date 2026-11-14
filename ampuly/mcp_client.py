"""MCP (Model Context Protocol) client supporting local (stdio) and remote
(StreamableHTTP / SSE) servers.

Connects to MCP servers, discovers their tools, and registers them into the
AMPULY ToolRegistry so the agent loop can call them like any other built-in tool.
Tool names are prefixed with the server name to prevent collisions when
multiple servers expose identically-named tools.
"""

from __future__ import annotations

import asyncio
import base64
import re
from contextlib import AsyncExitStack
from typing import Any

from loguru import logger

try:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters
    MCP_IMPORT_ERROR: Exception | None = None
except ModuleNotFoundError as exc:  # pragma: no cover - exercised via import-time fallback
    ClientSession = Any  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    StdioServerParameters = Any  # type: ignore[assignment]
    MCP_IMPORT_ERROR = exc

from ampuly.layer4_tools.registry import ToolDefinition


def _sanitize(name: str) -> str:
    """Replace non-alphanumeric/underscore chars with ``_``."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _require_mcp_dependency() -> None:
    if MCP_IMPORT_ERROR is None:
        return
    raise RuntimeError(
        "MCP support requires the optional `mcp` package to be installed."
    ) from MCP_IMPORT_ERROR


def _extract_content(content_blocks: list[Any]) -> str:
    """Extract human-readable text from MCP tool result content blocks.

    Handles ``text``, ``image``, and ``resource`` block types.
    """
    parts: list[str] = []
    for block in content_blocks:
        btype = getattr(block, "type", "")
        if btype == "text":
            parts.append(getattr(block, "text", ""))
        elif btype == "image":
            mime = getattr(block, "mimeType", "unknown")
            data = getattr(block, "data", "")
            if data:
                parts.append(f"[image ({mime}): {len(base64.b64decode(data))} bytes]")
            else:
                parts.append(f"[image ({mime})]")
        elif btype == "resource":
            resource = getattr(block, "resource", None)
            if resource:
                uri = getattr(resource, "uri", "")
                text = getattr(resource, "text", None)
                if text:
                    parts.append(text)
                else:
                    parts.append(f"[resource: {uri}]")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Base connection interface
# ---------------------------------------------------------------------------

class _MCPConnectionBase:
    """Shared interface for local and remote MCP connections."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._exit_stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self._tools: list[dict[str, Any]] = []

    async def start(self) -> None:
        raise NotImplementedError

    async def discover_tools(self) -> list[dict[str, Any]]:
        assert self._session
        result = await self._session.list_tools()
        self._tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.inputSchema if hasattr(t, "inputSchema") else {},
            }
            for t in result.tools
        ]
        logger.info(f"MCP server '{self.name}' exposes {len(self._tools)} tools")
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert self._session
        result = await self._session.call_tool(tool_name, arguments)
        is_error = getattr(result, "isError", False)
        content_blocks = getattr(result, "content", [])
        text = _extract_content(content_blocks)
        if is_error:
            return {"error": text or "MCP tool returned an error", "success": False}
        return {"result": text, "success": True}

    async def stop(self) -> None:
        await self._exit_stack.aclose()
        self._session = None
        logger.info(f"MCP server '{self.name}' stopped")


# ---------------------------------------------------------------------------
# Local (stdio) connection
# ---------------------------------------------------------------------------

class MCPServerConnection(_MCPConnectionBase):
    """A connection to a local MCP server process via stdio."""

    def __init__(self, name: str, command: str, args: list[str], env: dict[str, str] | None = None) -> None:
        super().__init__(name)
        _require_mcp_dependency()
        self.server_params = StdioServerParameters(command=command, args=args, env=env)

    async def start(self) -> None:
        transport = await self._exit_stack.enter_async_context(
            stdio_client(self.server_params)
        )
        read_stream, write_stream = transport
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        result = await self._session.initialize()
        server_info = getattr(result, "serverInfo", None) or {}
        s_name = getattr(server_info, "name", "?") if not isinstance(server_info, dict) else server_info.get("name", "?")
        s_ver = getattr(server_info, "version", "?") if not isinstance(server_info, dict) else server_info.get("version", "?")
        logger.info(f"MCP server '{self.name}' initialized: {s_name} v{s_ver}")


# ---------------------------------------------------------------------------
# Remote (StreamableHTTP / SSE) connection
# ---------------------------------------------------------------------------

class MCPRemoteConnection(_MCPConnectionBase):
    """A connection to a remote MCP server via StreamableHTTP or SSE fallback."""

    def __init__(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(name)
        self.url = url
        self.headers = headers or {}

    async def start(self) -> None:
        _require_mcp_dependency()
        last_error: Exception | None = None

        # Strategy 1: StreamableHTTP
        try:
            from mcp.client.streamable_http import streamablehttp_client
            transport = await self._exit_stack.enter_async_context(
                streamablehttp_client(self.url, headers=self.headers)
            )
            read_stream, write_stream = transport[0], transport[1]
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            result = await self._session.initialize()
            server_info = getattr(result, "serverInfo", None) or {}
            s_name = getattr(server_info, "name", "?") if not isinstance(server_info, dict) else server_info.get("name", "?")
            s_ver = getattr(server_info, "version", "?") if not isinstance(server_info, dict) else server_info.get("version", "?")
            logger.info(f"MCP remote '{self.name}' connected via StreamableHTTP: {s_name} v{s_ver}")
            return
        except Exception as exc:
            last_error = exc
            logger.debug(f"MCP remote '{self.name}' StreamableHTTP failed, trying SSE: {exc}")
            # Reset exit stack for retry
            await self._exit_stack.aclose()
            self._exit_stack = AsyncExitStack()

        # Strategy 2: SSE fallback
        try:
            from mcp.client.sse import sse_client
            transport = await self._exit_stack.enter_async_context(
                sse_client(self.url, headers=self.headers)
            )
            read_stream, write_stream = transport[0], transport[1]
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            result = await self._session.initialize()
            server_info = getattr(result, "serverInfo", None) or {}
            s_name = getattr(server_info, "name", "?") if not isinstance(server_info, dict) else server_info.get("name", "?")
            s_ver = getattr(server_info, "version", "?") if not isinstance(server_info, dict) else server_info.get("version", "?")
            logger.info(f"MCP remote '{self.name}' connected via SSE: {s_name} v{s_ver}")
            return
        except Exception as exc:
            last_error = exc
            logger.debug(f"MCP remote '{self.name}' SSE also failed: {exc}")

        raise ConnectionError(
            f"Failed to connect to remote MCP server '{self.name}' at {self.url}: {last_error}"
        )


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class MCPManager:
    """Manages multiple MCP server connections and registers their tools."""

    def __init__(self) -> None:
        self._servers: dict[str, _MCPConnectionBase] = {}

    async def connect_local(
        self,
        name: str,
        command: list[str],
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> MCPServerConnection:
        if len(command) < 1:
            raise ValueError(f"MCP server '{name}' command is empty")
        conn = MCPServerConnection(name, command=command[0], args=command[1:], env=env)
        try:
            await asyncio.wait_for(conn.start(), timeout=timeout)
            self._servers[name] = conn
            return conn
        except Exception as exc:
            logger.warning(f"Failed to start local MCP server '{name}': {exc}")
            await self._stop_half_started(conn)
            raise

    # Backwards-compatible alias
    connect = connect_local

    async def connect_remote(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> MCPRemoteConnection:
        conn = MCPRemoteConnection(name, url=url, headers=headers)
        try:
            await asyncio.wait_for(conn.start(), timeout=timeout)
            self._servers[name] = conn
            return conn
        except Exception as exc:
            logger.warning(f"Failed to connect to remote MCP server '{name}': {exc}")
            await self._stop_half_started(conn)
            raise

    @staticmethod
    async def _stop_half_started(conn: _MCPConnectionBase) -> None:
        """A start that failed or timed out may have spawned the process already: tear it down, never leave it running."""
        try:
            await conn.stop()
        except Exception as exc:  # noqa: BLE001 - closing a cancelled start can itself raise; the failure stands
            logger.warning(f"Error stopping half-started MCP server '{conn.name}': {exc}")

    # What an arbitrary MCP tool can do, absent anything better. Not
    # pessimism: a third-party server's tool is code we did not write reaching
    # systems we do not know, and its schema says nothing about its effects.
    # Declaring this instead of leaving it undeclared is the difference
    # between an approval prompt that names the risk and one that says "no
    # declared native permission effects", and between a scoped sandbox grant
    # and `full-access`.
    DEFAULT_PERMISSION_EFFECTS: tuple[str, ...] = (
        "local_read",
        "network_access",
        "external_side_effect",
    )

    async def register_tools(
        self,
        conn: _MCPConnectionBase,
        tool_filter: set[str] | None = None,
        permission_effects: list[str] | tuple[str, ...] | None = None,
        refuse: Any = None,
        timeout: float | None = None,
        refuse_server: Any = None,
    ) -> list[ToolDefinition]:
        """The server's tools as definitions. `refuse(prefixed_name, original_name)` returns a reason to leave a
        tool out (the engine's boundary: no browser automation around the Gateway, no shadowing), or "".
        `refuse_server(names)` judges the whole tool set; `timeout` bounds the discovery."""
        discovery = conn.discover_tools()
        raw_tools = await (asyncio.wait_for(discovery, timeout=timeout) if timeout else discovery)
        if callable(refuse_server) and refuse_server([str(spec.get("name", "") or "") for spec in raw_tools]):
            return []
        definitions: list[ToolDefinition] = []
        server_prefix = _sanitize(conn.name)
        # An operator who knows a particular server is narrower may say so.
        # Absent that, the maximum stands: nothing widens by default and
        # nothing is inferred from a tool's name or description.
        effects = list(permission_effects or self.DEFAULT_PERMISSION_EFFECTS)
        for tool_spec in raw_tools:
            original_name = tool_spec["name"]
            prefixed_name = f"{server_prefix}_{_sanitize(original_name)}"
            # The boundary is judged on every tool the server offers, before the operator's filter: a filtered-out
            # browser tool still means the server drives a browser.
            if callable(refuse) and refuse(prefixed_name, original_name):
                continue
            if tool_filter and original_name not in tool_filter:
                continue
            td = ToolDefinition(
                name=prefixed_name,
                description=f"[MCP:{conn.name}] {tool_spec.get('description', '')}",
                parameters=tool_spec.get("inputSchema", {"type": "object", "properties": {}}),
                func=self._make_caller(conn, original_name),
                category="mcp",
                permission_effects=effects,
                # company_effect_kind stays `unknown` on purpose: company mode
                # blocks that outright (tool_hooks -> unknown_company_effect_
                # blocked), and an MCP tool has no enforceable company
                # capability. Declaring one here would widen access, which is
                # the opposite of what this change is for.
            )
            definitions.append(td)
        return definitions

    @staticmethod
    def _make_caller(conn: _MCPConnectionBase, tool_name: str) -> Any:
        async def _call(**kwargs: Any) -> dict[str, Any]:
            return await conn.call_tool(tool_name, kwargs)
        return _call

    async def discard(self, conn: _MCPConnectionBase) -> None:
        """Stop exactly this connection (never a same-named one registered earlier) and forget it."""
        if self._servers.get(conn.name) is conn:
            self._servers.pop(conn.name, None)
        await self._stop_half_started(conn)

    async def disconnect(self, name: str) -> None:
        """Stop one server and forget it (a server whose tools were refused registers nothing)."""
        conn = self._servers.pop(name, None)
        if conn is None:
            return
        try:
            await conn.stop()
        except Exception as exc:
            logger.warning(f"Error stopping MCP server '{name}': {exc}")

    async def shutdown(self) -> None:
        for conn in self._servers.values():
            try:
                await conn.stop()
            except Exception as exc:
                logger.warning(f"Error stopping MCP server '{conn.name}': {exc}")
        self._servers.clear()
