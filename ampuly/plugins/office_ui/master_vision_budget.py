"""Attachment vision uses the consuming master task's existing model budget.

No allowance or task is allocated here. The established Provider preparation,
reservation, final claim and usage-settlement mechanisms remain authoritative.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ampuly.llm.budget import (
    CallNotCharged, ModelBudgetStop, RouteRefused, before_model_dispatch,
    mark_dispatched, model_budget_scope, observe_budget_usage,
    prepared_budget_call, request_budget,
)


def validate_task(engine: Any, task: Any, *, project_id: str, session_id: str) -> None:
    metadata = dict(getattr(task, "metadata", {}) or {})
    home = str(metadata.get("ampuly_home", "") or "")
    engine_home = str(getattr(engine, "ampuly_home", "") or "")
    if (not getattr(task, "id", "") or not session_id
            or str(getattr(task, "project_id", "")) != project_id
            or str(getattr(task, "session_id", "")) != session_id
            or not metadata.get("budget_id") or not home or not engine_home
            or Path(home).resolve() != Path(engine_home).resolve()):
        raise ModelBudgetStop("Vision requires the consuming master task and its original budget")


def _provider(engine: Any, env_name: str, key: str):
    from ampuly.core.config import LLMConfig
    from ampuly.llm.provider import LLMProvider
    from ampuly.plugins.office_ui.master_vision import DASHSCOPE_BASE, OUTPUT_TOKENS, TIMEOUT_S, VISION_MODEL

    # Only the host's reviewed tariff table is copied. Task/attachment metadata
    # cannot supply a price or change the endpoint, model or credential source.
    host_config = getattr(getattr(engine, "llm", None), "config", None)
    config = LLMConfig(default_model=f"openai/{VISION_MODEL}",
        api_base=DASHSCOPE_BASE, api_key_env=env_name, max_tokens=OUTPUT_TOKENS,
        request_timeout=TIMEOUT_S, request_retries=0,
        cny_text_tariffs=getattr(host_config, "cny_text_tariffs", []) or [])
    provider = LLMProvider(config, ampuly_home=Path(engine.ampuly_home))
    if not key or provider._call_api_key() != key:
        raise RouteRefused("Vision credential changed before preparation", code="vision_key_changed")
    return provider


def _tariff_revision(engine: Any) -> str:
    config = getattr(getattr(engine, "llm", None), "config", None)
    rows = getattr(config, "cny_text_tariffs", []) or []
    return json.dumps([row.model_dump(mode="json") if hasattr(row, "model_dump") else row
        for row in rows], sort_keys=True, separators=(",", ":"), ensure_ascii=True)


async def read(engine: Any, task: Any, *, project_id: str, session_id: str,
               business: Any, env_name: str, key: str, images, prompt: str,
               transport, cancelled: asyncio.Event) -> dict[str, Any]:
    from ampuly.llm.budget import _budget_order_id, _ledger
    from ampuly.layer3_agent.employee_runtime import employee_dispatch_checkpoint
    from ampuly.plugins.office_ui import master_vision
    from ampuly.plugins.office_ui.master_runtime import customer_for

    validate_task(engine, task, project_id=project_id, session_id=session_id)
    metadata = dict(task.metadata)
    if await customer_for(business, project_id) != str(metadata.get("customer_id", "") or ""):
        raise ModelBudgetStop("Vision customer does not match the consuming master task")
    tariff_revision = _tariff_revision(engine)
    target = _provider(engine, env_name, key)
    body = master_vision.request_body(images, prompt)
    prepared = target._prepare_call(body["messages"], tools=None, task_type=None,
        temperature=0, max_tokens=body["max_tokens"], stream=False, extra={})

    def current_budget() -> None:
        row = _ledger(str(metadata["ampuly_home"])).run_budget(str(metadata["budget_id"]))
        expected = dict(project_id=project_id,
            customer_id=str(metadata.get("customer_id", "") or ""),
            environment_id=str(metadata.get("environment_id", "") or ""),
            order_id=_budget_order_id(metadata))
        if (not row or row.get("state") != "open"
                or any(str(row.get(k, "") or "") != v for k, v in expected.items())):
            raise ModelBudgetStop("Vision budget closed, held or changed before dispatch")

    result = None
    with model_budget_scope(task):
        try:
            async with request_budget(target=target, prepared=prepared) as bounded:
                if not bounded:
                    raise ModelBudgetStop("Vision requires a bound budget")
                frozen = prepared_budget_call(prepared)
                if (cancelled.is_set() or not master_vision.enabled()
                        or await master_vision._key_for(business, project_id) != (env_name, key)):
                    raise RouteRefused("Vision stopped before dispatch", code="vision_policy_changed")
                await employee_dispatch_checkpoint("model")
                await before_model_dispatch(frozen)
                await employee_dispatch_checkpoint("recheck")
                target._check_employee_dispatch_model(frozen)
                validate_task(engine, task, project_id=project_id, session_id=session_id)
                if dict(task.metadata) != metadata:
                    raise ModelBudgetStop("Vision task authority changed before dispatch")
                if await customer_for(business, project_id) != str(metadata.get("customer_id", "") or ""):
                    raise ModelBudgetStop("Vision customer changed before dispatch")
                if _tariff_revision(engine) != tariff_revision:
                    raise RouteRefused("Vision reviewed tariff changed before dispatch", code="vision_tariff_changed")
                # Count-only budgets also recheck closure after the reservation.
                # No await separates this final check from handing off transport.
                current_budget()
                if cancelled.is_set() or not master_vision.enabled():
                    raise RouteRefused("Vision stopped before dispatch", code="vision_policy_changed")
                mark_dispatched()
                wire = {"model": master_vision.VISION_MODEL,
                    "messages": frozen["messages"], "max_tokens": frozen["max_tokens"],
                    "temperature": frozen["temperature"]}
                result = await master_vision.read_images(images, key=key, prompt=prompt,
                    transport=transport, prepared_body=wire)
                if result.get("not_charged"):
                    raise CallNotCharged("vision supplier refused or was not reached")
                if result["outcome"] == "unknown" or not result.get("usage_known"):
                    raise RuntimeError("Vision terminal usage or outcome is unknown")
                observe_budget_usage(model=str(result.get("served_model", "")),
                    prompt_tokens=result["tokens_in"], completion_tokens=result["tokens_out"])
        except CallNotCharged:
            return result
        except ModelBudgetStop as exc:
            if result is None:
                raise
            # Let apply publish the file's terminal state before surfacing the
            # stop. The caller must not proceed to the next file or text call.
            result["budget_stop"] = exc
    return result
