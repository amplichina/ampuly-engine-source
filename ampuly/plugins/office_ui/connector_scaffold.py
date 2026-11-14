"""Deterministic, inert FastMCP scaffolds for governed connector candidates.

The scaffold deliberately has no HTTP client and reads no credential. Every
tool delegates to ``superones_connector_bridge`` so activation can bind it to
the product's scope, budget, audit and Effect Gateway. The generated
``mcp.json`` is disabled; generation is not installation or admission.
"""

from __future__ import annotations

import keyword
import re
from typing import Any

from .connector_contract import ConnectorContract


class ConnectorScaffoldError(ValueError):
    """A validated connector contract still cannot be rendered as safe code."""


def scaffold_files(contract: ConnectorContract) -> dict[str, Any]:
    """Return generated file payloads; the caller owns atomic persistence."""

    parameters: dict[str, tuple[str, ...]] = {}
    for operation in contract.operations:
        clean: list[str] = []
        for raw in operation.params:
            name = str(raw or "").strip()
            if (
                not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,63}", name)
                or keyword.iskeyword(name)
                or name.startswith("_")
                or name in {"mcp", "dispatch", "effect", "connector", "operation"}
            ):
                raise ConnectorScaffoldError(
                    f"{operation.op_id}: parameter {name!r} cannot be emitted as a Python argument"
                )
            if name in clean:
                raise ConnectorScaffoldError(f"{operation.op_id}: parameter {name!r} is duplicated")
            clean.append(name)
        parameters[operation.op_id] = tuple(clean)

    required_env = [contract.base_url_env]
    if contract.credential_env:
        required_env.append(contract.credential_env)
    return {
        "mcp.json": {
            "mcpServers": {
                contract.name: {
                    "command": "python",
                    "args": ["fastmcp_server.py"],
                    "disabled": True,
                    "superones": {
                        "candidate_only": True,
                        "contract_fingerprint": contract.fingerprint(),
                        "required_env_names": required_env,
                        "activation": "owner_reviewed_registry_only",
                    },
                }
            }
        },
        "fastmcp_server.py": _server_source(contract, parameters),
        "FAST_MCP_README.md": _readme(contract),
    }


def _server_source(contract: ConnectorContract, parameters: dict[str, tuple[str, ...]]) -> str:
    lines = [
        '\"\"\"Generated candidate; disabled until SuperOnes admission binds the bridge.\"\"\"',
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "",
        "from fastmcp import FastMCP",
        "from superones_connector_bridge import dispatch as _dispatch",
        "",
        f"mcp = FastMCP({contract.name!r})",
        "",
    ]
    for operation in contract.operations:
        params = parameters[operation.op_id]
        signature = ", ".join(f"{name}: str" for name in params)
        arguments = ", ".join(f"{name!r}: {name}" for name in params)
        doc = operation.summary or f"{operation.kind} operation {operation.op_id}"
        lines.extend([
            "@mcp.tool",
            f"def {operation.op_id}({signature}) -> Any:",
            f"    {doc!r}",
            "    return _dispatch(",
            f"        connector={contract.name!r}, operation={operation.op_id!r},",
            f"        arguments={{{arguments}}}, effect={operation.kind!r},",
            f"        contract_fingerprint={contract.fingerprint()!r},",
            "    )",
            "",
        ])
    lines.extend(['if __name__ == "__main__":', "    mcp.run()", ""])
    return "\n".join(lines)


def _readme(contract: ConnectorContract) -> str:
    writes = [operation.op_id for operation in contract.writes]
    return "\n".join([
        f"# Generated FastMCP candidate: {contract.name}",
        "",
        "This is an inert scaffold, not an installed connector.",
        "It contains no credential value and performs no direct network call.",
        "All tools delegate to the SuperOnes connector bridge so project/customer scope,",
        "budget, audit and Effect Gateway policy remain authoritative.",
        "",
        f"Contract fingerprint: `{contract.fingerprint()}`",
        f"Write operations (approval required): `{', '.join(writes) if writes else 'none'}`",
        "",
        "The generated mcp.json stays disabled until contract tests pass, the owner approves",
        "the exact fingerprint, read-only health passes, and registry activation is separately granted.",
        "",
    ])
