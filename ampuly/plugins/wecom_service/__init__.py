"""企业微信「微信客服」客户服务：the console extension that answers a deployed business's customers (policy).

An independent module inside the engine, shipped with it, switchable as a whole. The console never names it: it
reaches the console only through the console's extension points (``ampuly.plugins.office_ui.extensions``), and the
console reaches it only through what it registers there (``console.extension``). Switched off
(``AMPULY_DISABLED_EXTENSIONS=wecom_service``) nothing of it runs and only its tables' upkeep stays (``register_data_upkeep``);
not importable, it registers nothing; either way the console runs. The channel's own switch inside it
(``wecom_channel.SWITCH``, default off) is a separate thing and unchanged.

The package:

* ``wecom_kf``, ``wecom_relay_protocol`` -- the 微信客服 protocol and the relay envelope. Standard library plus
  ``cryptography`` only: the cloud relay (``scripts/wecom_relay``) ships them with this file and nothing else of the
  engine, so importing this package must never import the console (nothing here does, at import time);
* ``wecom_relay_client`` -- the console's one outbound link to the relay;
* ``wecom_kf_store`` -- the four tables of business schema version 4 (the schema itself is the kernel's,
  ``ampuly.database.schema_version``), read and written over the business store's extension connection;
* ``wecom_channel``, ``wecom_brain``, ``wecom_knowledge`` -- inbound, the AI 客服 (JEV first, the text model as the
  fallback), the owner's switch and bindings, the three owner-only ws actions;
* ``wecom_delivery``, ``delivery_card`` -- the delivery to the customer and its Feishu card;
* ``records``, ``lane`` -- the project's backup rows and the project centre's 客户确认 lane facts;
* ``host`` -- what the extension needs from the console (``ConsoleHost``), declared once;
* ``console`` -- the extension itself: every seam filled in one place.
"""

from __future__ import annotations

from typing import Any, Callable

# The extension's name: the registry's key, the switch-off word, the key of its per-console state.
NAME = "wecom_service"


def register_console_extension(register: Callable[[Any], None]) -> None:
    """Called by the console's registry (``extensions.load_builtin``) with its ``register``. Imports the console side
    only now, never at package import."""
    from ampuly.plugins.wecom_service.console import extension

    register(extension())


def register_data_upkeep(register: Callable[[Any], None]) -> None:
    """Called instead when the owner switched the extension off (``AMPULY_DISABLED_EXTENSIONS=wecom_service``): nothing of
    it runs -- no actions, no duty round, no card, no relay link -- but its tables' upkeep stays, so deleting a project
    still removes its customers' conversations and messages, and its backup and restore still carry them (owner data,
    : switching the channel off must not keep a destroyed project's customer data behind)."""
    from ampuly.plugins.office_ui.extensions import ConsoleExtension
    from ampuly.plugins.wecom_service.records import PROJECT_RECORDS

    register(ConsoleExtension(name=NAME, project_records=PROJECT_RECORDS))
