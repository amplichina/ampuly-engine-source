"""G01 / §12.6: browser automation reaches a page only through the Gateway, never around it through an MCP server.

The template config carries upstream's Playwright MCP entry (disabled). Enabled, it would start `npx
@playwright/mcp@latest` and register every tool it offers -- navigation, clicks, form fills, arbitrary page
scripts -- as ordinary MCP tools: no observation, no draft/submit split, no approval, no domain gate. Their
`browser_*` names would also sit in the product browser tools' namespace, which a delegation reads.

So the product refuses, it does not downgrade (plan 4.3 §3: the Playwright MCP server is not adopted; a future
browser capability enters as a Gateway-routed product tool):

* **A browser-automation server never starts.** Its command, url or name names a known browser-automation
  package, or its tools would land in the `browser_` namespace. Nothing is spawned or downloaded.
* **A server that turns out to offer browser tools registers nothing.** A tool in the `browser_` namespace is
  refused, and the connection is closed: its other tools drive the same ungoverned browser.
* **An MCP tool never replaces a registered tool.** The registry overwrites silently; a server that names a tool
  like an existing one would take its place under a name every check trusts.

A server with no marker and no such names still registers; its tools stay `category="mcp"`, ask before any
network or external effect, and company mode blocks them.

Desktop control and system commands are refused the same way (runtime contract): a server that clicks,
types and looks at the screen or at windows (Windows-MCP and its kind), or that runs PowerShell / shell commands or
edits the registry, would act on this computer around the desktop Gateway (observation, one-use capability, secure
input, emergency stop, no Enter) and around the engine's own command approval. Asking a person before each call is
not the same fence, so these are not downgraded either: the server never starts when a marker names it, registers
nothing when its tool names show it, and nothing may be named into the product desktop tools' `desktop_` namespace.
"""

from __future__ import annotations

import re
from typing import Any

# Packages whose tools drive a browser. Matched in the lowercased command, url and name.
BROWSER_AUTOMATION_MARKERS: tuple[str, ...] = (
    # "playwright/mcp" covers the npm package, a Windows install path once its backslashes are read as slashes, and the
    # container image mcr.microsoft.com/playwright/mcp.
    "playwright/mcp", "playwright-mcp", "mcp-server-playwright", "chrome-devtools-mcp", "puppeteer",
    "browser-use", "browser_use", "browserbase", "browsermcp", "mcp-selenium", "mcp-chrome", "chrome-mcp",
)
# Playwright itself runs an MCP server as a subcommand: `playwright mcp` (or the older `run-mcp-server`).
_PLAYWRIGHT_MCP_SUBCOMMAND = re.compile(
    r"(?:^|[\s/@])playwright(?:-core)?(?:@[^\s/]+)?(?:\.cmd|\.exe)?(?:/(?:driver/package/)?cli\.js)?\s+(?:mcp|run-mcp-server)(?:\s|$)"
)
# A tool set that navigates and also clicks, types or runs page script drives a browser, whatever its names are prefixed with.
_NAVIGATE_NAMES = frozenset({"navigate", "navigate_page", "open_url", "goto", "open_page", "browse"})
_DRIVE_NAMES = frozenset({"click", "fill", "type", "send_keys", "evaluate", "execute_script", "press_key", "fill_form", "click_element",
                          "fill_or_select", "keyboard", "inject_script", "evaluate_script", "type_text", "hover"})
# The product browser tools' namespace (ampuly/layer4_tools/browser.py): nothing else may be named into it.
BROWSER_TOOL_PREFIX = "browser_"
SHADOWS_REGISTERED_TOOL = "shadows_registered_tool"
BROWSER_AUTOMATION_SERVER = "browser_automation_mcp_bypasses_gateway"
BROWSER_AUTOMATION_TOOL = "browser_automation_tool_bypasses_gateway"

# Packages whose tools drive the desktop or run system commands. Matched like the browser markers.
DESKTOP_CONTROL_MARKERS: tuple[str, ...] = (
    "windows-mcp", "windows_mcp", "desktop-commander", "desktopcommander", "desktop_commander", "pyautogui",
    "mcp-server-commands", "mcp-shell-server", "shell-mcp-server", "powershell-mcp", "mcp-powershell",
    "computer-control-mcp", "mcp-desktop-automation", "nut-tree/nut-js",
)
# The product desktop tools' namespace (ampuly/plugins/office_ui/desktop_connector.py: desktop_observe / desktop_act /
# desktop_finish): nothing else may be named into it.
DESKTOP_TOOL_PREFIX = "desktop_"
DESKTOP_CONTROL_SERVER = "desktop_control_mcp_bypasses_gateway"
DESKTOP_CONTROL_TOOL = "desktop_control_tool_bypasses_gateway"
SYSTEM_COMMAND_TOOL = "system_command_tool_bypasses_approval"
# A tool set that acts with the mouse or keyboard and also sees the screen or controls windows / apps drives the desktop.
# Each rule has words that also count behind a prefix (win_click) and common words that count only as the whole name
# (a "type" tool is typing; "get_type" is not).
_DESKTOP_INPUT_NAMES = frozenset({
    "click", "left_click", "right_click", "double_click", "middle_click", "mouse_click", "mouse_move", "move_mouse",
    "drag", "drag_and_drop", "scroll", "type_text", "press_key", "key_press", "hotkey", "shortcut", "keyboard", "send_keys",
})
_DESKTOP_INPUT_WHOLE = frozenset({"type", "key", "move", "mouse"})
_DESKTOP_SIGHT_NAMES = frozenset({
    "screenshot", "take_screenshot", "screen_capture", "capture_screen", "get_screenshot", "desktop_state",
    "launch_app", "open_app", "start_app", "switch_app", "switch_window", "focus_window", "list_windows", "get_windows",
})
_DESKTOP_SIGHT_WHOLE = frozenset({"screen", "state", "snapshot", "launch", "switch", "window", "app"})
# A tool that runs system commands or edits the registry, on its own.
_SYSTEM_COMMAND_NAMES = frozenset({
    "powershell", "run_powershell", "execute_powershell", "run_shell", "shell_exec", "execute_shell", "run_cmd",
    "run_command", "execute_command", "exec_command", "run_process", "start_process", "run_terminal_command",
    "reg_query", "reg_set", "read_registry", "write_registry",
})
_SYSTEM_COMMAND_WHOLE = frozenset({"shell", "bash", "cmd", "terminal", "registry"})


def _sanitize(name: str) -> str:
    # The MCP client's rule for a tool name prefix (mcp_client._sanitize), without importing the client.
    return re.sub(r"[^a-zA-Z0-9_]", "_", str(name or ""))


def refuse_mcp_server(server_cfg: Any) -> str:
    """Why this configured server must not start, or "" when it may."""
    name = str(getattr(server_cfg, "name", "") or "")
    command = getattr(server_cfg, "command", None) or []
    parts = [name, str(getattr(server_cfg, "url", "") or ""), *(str(item) for item in (command if isinstance(command, (list, tuple)) else [command]))]
    text = " ".join(parts).lower().replace("\\", "/")
    if any(marker in text for marker in BROWSER_AUTOMATION_MARKERS) or _PLAYWRIGHT_MCP_SUBCOMMAND.search(text):
        return BROWSER_AUTOMATION_SERVER
    if f"{_sanitize(name)}_".lower().startswith(BROWSER_TOOL_PREFIX):
        return BROWSER_AUTOMATION_SERVER
    if any(marker in text for marker in DESKTOP_CONTROL_MARKERS):
        return DESKTOP_CONTROL_SERVER
    if f"{_sanitize(name)}_".lower().startswith(DESKTOP_TOOL_PREFIX):
        return DESKTOP_CONTROL_SERVER
    return ""


def drives_a_browser(tool_names: Any) -> bool:
    """Whether a server's tools, by their own names, navigate and also act on a page."""
    names = {re.sub(r"^.*[./:]", "", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name or "")).lower()) for name in tool_names}

    def named(table: frozenset[str]) -> bool:
        # A tool set named with a prefix (chrome_navigate, puppeteer_click) is the same tool set.
        return any(name == word or name.endswith("_" + word) or name.endswith("-" + word) for name in names for word in table)

    return named(_NAVIGATE_NAMES) and named(_DRIVE_NAMES)


def _tool_words(tool_names: Any) -> set[str]:
    # The browser rule's reading of a name (camelCase split, lowercased, path-like prefixes cut), plus the "-Tool" /
    # "_tool" suffix desktop servers put on every name (Windows-MCP: "Click-Tool", "Powershell-Tool") and "-" read as "_".
    words: set[str] = set()
    for name in tool_names or ():
        word = re.sub(r"^.*[./:]", "", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name or "")).lower()).replace("-", "_")
        words.add(re.sub(r"_tool$", "", word))
    return words


def _named(words: set[str], table: frozenset[str], whole: frozenset[str] = frozenset()) -> bool:
    # A tool set named with a prefix (win_click, desktop_screenshot) is the same tool set; the common words count only
    # as the whole name.
    return any(word in whole for word in words) or any(
        word == entry or word.endswith("_" + entry) for word in words for entry in table)


def drives_the_desktop(tool_names: Any) -> bool:
    """Whether a server's tools, by their own names, act with the mouse or keyboard and also see the screen or control
    windows / apps."""
    words = _tool_words(tool_names)
    return (_named(words, _DESKTOP_INPUT_NAMES, _DESKTOP_INPUT_WHOLE)
            and _named(words, _DESKTOP_SIGHT_NAMES, _DESKTOP_SIGHT_WHOLE))


def runs_system_commands(tool_names: Any) -> bool:
    """Whether any of a server's tools, by its name, runs PowerShell / shell commands or edits the registry."""
    return _named(_tool_words(tool_names), _SYSTEM_COMMAND_NAMES, _SYSTEM_COMMAND_WHOLE)


def refuse_mcp_tool_set(tool_names: Any) -> str:
    """Why a server offering these tools must register none of them, or "" when it may: it drives a browser, drives
    the desktop, or runs system commands."""
    names = list(tool_names or ())
    if drives_a_browser(names):
        return BROWSER_AUTOMATION_TOOL
    if drives_the_desktop(names):
        return DESKTOP_CONTROL_TOOL
    if runs_system_commands(names):
        return SYSTEM_COMMAND_TOOL
    return ""


def refuse_mcp_tool(prefixed_name: str, original_name: str, registered: Any) -> str:
    """Why this discovered tool must not be registered, or "" when it may."""
    if prefixed_name in registered:
        return SHADOWS_REGISTERED_TOOL
    if prefixed_name.lower().startswith(BROWSER_TOOL_PREFIX) or str(original_name or "").lower().startswith(BROWSER_TOOL_PREFIX):
        return BROWSER_AUTOMATION_TOOL
    if prefixed_name.lower().startswith(DESKTOP_TOOL_PREFIX) or str(original_name or "").lower().startswith(DESKTOP_TOOL_PREFIX):
        return DESKTOP_CONTROL_TOOL
    return ""
