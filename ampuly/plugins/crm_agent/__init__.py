"""Project-scoped CRM console extension; registration starts no workers."""


def register_console_extension(register):
    from ampuly.plugins.office_ui.extensions import ConsoleExtension
    from .actions import ACTIONS
    register(ConsoleExtension(name="crm_agent", ws_actions=ACTIONS))
