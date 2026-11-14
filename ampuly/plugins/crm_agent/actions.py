"""Owner-admitted CRM wire actions; scope and providers are host-only."""
from .contracts import CRMError, fail
from .settings import ADMISSION_SYNTHETIC

DATA_ACTIONS = ("crm_list", "crm_get", "crm_save", "crm_archive", "crm_export", "crm_erasure_preview", "crm_erase")
SETTINGS_ACTIONS = ("crm_settings_get", "crm_settings_set")
ACTIONS_NAMES = DATA_ACTIONS + SETTINGS_ACTIONS


async def dispatch(console, ws, data, *, action):
    request_id = None
    try:
        if action not in ACTIONS_NAMES or type(data) is not dict or set(data) - {"type", "project_id", "params", "request_id"}:
            fail("request_invalid")
        if data.get("type") != action or type(data.get("params")) is not dict:
            fail("request_invalid")
        request_id = data.get("request_id")
        if type(request_id) is not str or not 1 <= len(request_id) <= 128:
            fail("request_invalid")
        # A CRM host seam must be installed explicitly. Never reuse the
        # customer-service authority window, or accept scope from the wire.
        admit = getattr(console, "crm_context", None)
        if not callable(admit):
            fail("host_not_admitted")
        import inspect
        settings_action = action in SETTINGS_ACTIONS
        context = (admit(ws, data.get("project_id"), open_store=False) if settings_action
                   else admit(ws, data.get("project_id")))
        if inspect.isawaitable(context):
            context = await context
        context.check()
        scope, store = context.scope, context.store
        scope.check()
        params = data["params"]
        settings = getattr(context, "settings", None)
        if settings is None:
            fail("not_enabled")
        if not settings_action:
            # Backend gate, independent of the UI: default OFF, synthetic only.
            if settings.enabled is not True:
                fail("not_enabled")
            if settings.admission != ADMISSION_SYNTHETIC or getattr(store, "admission", None) != ADMISSION_SYNTHETIC:
                fail("real_data_admission_closed")
        if action == "crm_settings_get":
            if params:
                fail("request_invalid")
            result = settings.public()
        elif action == "crm_settings_set":
            if set(params) != {"enabled", "admission", "expected_revision"} or not callable(getattr(context, "update_settings", None)):
                fail("request_invalid")
            result = context.update_settings(**params).public()
        elif action == "crm_list":
            result = {"records": store.list(scope, **params), "owners": list(scope.owner_ids),
                      "customer_id": scope.customer_id}
        elif action == "crm_get":
            result = store.get(scope, **params)
        elif action == "crm_save":
            result = store.save(scope, **params)
        elif action == "crm_archive":
            result = store.archive(scope, **params)
        elif action == "crm_export":
            result = store.export(scope, **params)
        elif action == "crm_erasure_preview":
            if set(params) != {"record_id", "mode"}:
                fail("request_invalid")
            result = store.erasure_preview(scope, **params, revalidate=context.check)
        else:
            if set(params) != {"token", "record_id", "mode", "phrase", "confirmed", "request_key"}:
                fail("request_invalid")
            result = store.erase(scope, **params, revalidate=context.check)
        context.check()
        await console.send_ack(ws, action=action, ok=True, request_id=request_id, crm=result)
    except Exception as exc:
        code = exc.code if isinstance(exc, CRMError) else (
            "employee_binding_runtime_unavailable" if getattr(exc, "code", "") == "employee_binding_runtime_unavailable"
            else "crm_operation_failed")
        await console.send_ack(ws, action=action, ok=False, request_id=request_id, error=code)


def _handler(action):
    async def run(console, ws, data):
        await dispatch(console, ws, data, action=action)
    return run


ACTIONS = {name: _handler(name) for name in ACTIONS_NAMES}
