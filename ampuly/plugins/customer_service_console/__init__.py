"""Owner-only service acceptance; registration never starts a worker."""


def register_console_extension(register):
    from ampuly.plugins.office_ui.extensions import ConsoleExtension
    from .actions import ACTIONS
    register(ConsoleExtension(name="customer_service", ws_actions=ACTIONS))
