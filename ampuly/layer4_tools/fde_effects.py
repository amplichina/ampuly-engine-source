"""Least-privilege FDE tools that can only propose governed side effects."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ampuly.effects import ApprovalType, ENTERPRISE_MESSAGE_SEND
from ampuly.layer2_organization.work_item_links import linked_work_item_id_for_task
from ampuly.layer4_tools.registry import (
    COMPANY_EFFECT_RUNTIME_INTERNAL,
    ToolDefinition,
    ToolInvocationValidationError,
)


_CHANNEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_DESTINATION_CHARS = 256
_MAX_CONTENT_CHARS = 4000


def _required_text(value: Any, field: str, *, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ToolInvocationValidationError(
            f"{field} must be a string",
            code="fde_effect_invalid_argument",
        )
    clean = value.strip()
    if not clean:
        raise ToolInvocationValidationError(
            f"{field} is required",
            code="fde_effect_missing_argument",
        )
    if len(clean) > max_chars:
        raise ToolInvocationValidationError(
            f"{field} exceeds {max_chars} characters",
            code="fde_effect_argument_too_long",
        )
    if "\x00" in clean:
        raise ToolInvocationValidationError(
            f"{field} contains an invalid control character",
            code="fde_effect_invalid_argument",
        )
    return clean


def _idempotency_key(
    *,
    project_id: str,
    run_id: str,
    work_item_id: str,
    channel: str,
    user_id: str,
    session_id: str,
    content: str,
) -> str:
    identity = {
        "version": 1,
        "project_id": project_id,
        "run_id": run_id,
        "work_item_id": work_item_id,
        "channel": channel,
        "user_id": user_id,
        "session_id": session_id,
        "content": content,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"customer-acceptance-reminder:{work_item_id}:{digest}"


def create_fde_effect_tools(engine: Any) -> list[ToolDefinition]:
    """Create FDE tools bound to one Engine-owned effect approval service."""

    async def request_customer_acceptance_reminder(
        channel: str,
        user_id: str,
        session_id: str,
        content: str,
        task: Any = None,
    ) -> dict[str, Any]:
        """Create an owner approval request; this function never sends directly."""

        if task is None:
            raise ToolInvocationValidationError(
                "a trusted runtime task is required",
                code="fde_effect_runtime_context_required",
                correction="Run this tool from the customer delivery work item.",
            )
        task_id = str(getattr(task, "id", "") or "").strip()
        if not task_id:
            raise ToolInvocationValidationError(
                "the delivery work item has no durable task anchor",
                code="fde_effect_runtime_anchor_required",
            )
        store = getattr(engine, "store", None)
        engine_project_id = str(getattr(engine, "project_id", "") or "default").strip() or "default"

        def assert_source():
            if (store is None or getattr(engine, "store", None) is not store
                    or (str(getattr(engine, "project_id", "") or "default").strip() or "default") != engine_project_id):
                raise ToolInvocationValidationError(
                    "the delivery runtime source changed",
                    code="fde_effect_runtime_source_changed",
                )

        async def read_source(method: str, identity: str, code: str):
            assert_source()
            reader = getattr(store, method, None)
            if not callable(reader):
                raise ToolInvocationValidationError("the delivery source is unavailable", code=code)
            try:
                result = await reader(identity)
            except Exception:
                raise ToolInvocationValidationError("the delivery source could not be read", code=code) from None
            assert_source()
            return result

        # The injected Task is only a lookup identity. Project/session/link and
        # authority metadata come from this engine's original persisted source.
        task = await read_source("get_task", task_id, "fde_effect_runtime_anchor_unavailable")
        if task is None or str(getattr(task, "id", "") or "").strip() != task_id:
            raise ToolInvocationValidationError(
                "the durable task anchor no longer exists", code="fde_effect_runtime_anchor_required",
            )
        work_item_id = linked_work_item_id_for_task(task)
        if not work_item_id:
            raise ToolInvocationValidationError(
                "the runtime task is not linked to a delivery work item",
                code="fde_effect_work_item_required",
                correction="Open the relevant FDE work item before requesting a reminder.",
            )

        clean_channel = _required_text(channel, "channel", max_chars=64)
        if not _CHANNEL_NAME.fullmatch(clean_channel):
            raise ToolInvocationValidationError(
                "channel must use only letters, numbers, '.', '_' or '-'",
                code="fde_effect_invalid_channel",
            )
        clean_user_id = _required_text(
            user_id,
            "user_id",
            max_chars=_MAX_DESTINATION_CHARS,
        )
        clean_session_id = _required_text(
            session_id,
            "session_id",
            max_chars=_MAX_DESTINATION_CHARS,
        )
        clean_content = _required_text(
            content,
            "content",
            max_chars=_MAX_CONTENT_CHARS,
        )

        task_session_id = str(getattr(task, "session_id", "") or "").strip()
        project_id = str(getattr(task, "project_id", "") or "default").strip() or "default"
        metadata = dict(getattr(task, "metadata", {}) or {})
        if not task_id or not task_session_id:
            raise ToolInvocationValidationError(
                "the delivery work item has no durable task/session anchor",
                code="fde_effect_runtime_anchor_required",
                correction="Resume the work item from its Office task before retrying.",
            )
        if project_id != engine_project_id:
            raise ToolInvocationValidationError(
                "the runtime task belongs to a different project",
                code="fde_effect_project_scope_mismatch",
                correction="Use the engine for the work item's own project.",
            )

        work_item = await read_source(
            "get_delegation_work_item", work_item_id, "fde_effect_work_item_unavailable",
        )
        if work_item is None:
            raise ToolInvocationValidationError(
                "the linked delivery work item no longer exists",
                code="fde_effect_work_item_not_found",
                correction="Refresh the workflow and use an active delivery work item.",
            )
        if str(getattr(work_item, "work_item_id", "") or "").strip() != work_item_id:
            raise ToolInvocationValidationError(
                "the delivery work item link changed", code="fde_effect_work_item_identity_mismatch",
            )
        run_id = str(getattr(work_item, "run_id", "") or "").strip()
        if not run_id:
            raise ToolInvocationValidationError(
                "the delivery work item has no run identity",
                code="fde_effect_run_scope_required",
            )
        task_run_id = str(metadata.get("delegation_run_id", "") or "").strip()
        if not task_run_id:
            raise ToolInvocationValidationError(
                "the durable task has no delegation run identity", code="fde_effect_run_scope_required",
            )
        if task_run_id != run_id:
            raise ToolInvocationValidationError(
                "the runtime task and delivery work item belong to different runs",
                code="fde_effect_run_identity_mismatch",
                correction="Resume the delivery work item from its own runtime task.",
            )
        linked_task = await read_source(
            "get_runtime_task_for_work_item", work_item_id, "fde_effect_work_item_link_unavailable",
        )
        if (linked_task is None or str(getattr(linked_task, "id", "") or "").strip() != task_id
                or linked_work_item_id_for_task(linked_task) != work_item_id):
            raise ToolInvocationValidationError(
                "the delivery work item is not linked to this durable task",
                code="fde_effect_work_item_identity_mismatch",
            )
        run = await read_source("get_delegation_run", run_id, "fde_effect_run_scope_unavailable")
        run_project_id = str(getattr(run, "project_id", "") or "default").strip() or "default"
        if (run is None or run_project_id != project_id
                or str(getattr(run, "run_id", "") or "").strip() != run_id):
            raise ToolInvocationValidationError(
                "the delivery run belongs to a different project",
                code="fde_effect_run_project_scope_mismatch",
                correction="Use the engine for the work item's own project.",
            )

        # Fixed second observations detect successful reads that raced a relink
        # or source change. They are not retries, metadata repair, or an atomic
        # transaction claim; the original Engine proposal rechecks its source.
        current_task = await read_source("get_task", task_id, "fde_effect_runtime_anchor_unavailable")
        current_item = await read_source(
            "get_delegation_work_item", work_item_id, "fde_effect_work_item_unavailable",
        )
        current_link = await read_source(
            "get_runtime_task_for_work_item", work_item_id, "fde_effect_work_item_link_unavailable",
        )
        current_run = await read_source("get_delegation_run", run_id, "fde_effect_run_scope_unavailable")
        authority_keys = (
            "delegation_run_id", "budget_id", "customer_id", "order_id", "environment_id",
            "company_run_controller_owner_token", "company_run_controller_lease_generation",
        )
        current_meta = dict(getattr(current_task, "metadata", {}) or {})
        if (current_task is None or str(getattr(current_task, "id", "") or "").strip() != task_id
                or (str(getattr(current_task, "project_id", "") or "default").strip() or "default") != project_id
                or str(getattr(current_task, "session_id", "") or "").strip() != task_session_id
                or linked_work_item_id_for_task(current_task) != work_item_id
                or any(current_meta.get(key) != metadata.get(key) for key in authority_keys)
                or current_item is None or str(getattr(current_item, "work_item_id", "") or "").strip() != work_item_id
                or str(getattr(current_item, "run_id", "") or "").strip() != run_id
                or current_link is None or str(getattr(current_link, "id", "") or "").strip() != task_id
                or linked_work_item_id_for_task(current_link) != work_item_id
                or current_run is None or str(getattr(current_run, "run_id", "") or "").strip() != run_id
                or (str(getattr(current_run, "project_id", "") or "default").strip() or "default") != project_id):
            raise ToolInvocationValidationError(
                "the durable delivery facts changed before proposal", code="fde_effect_runtime_source_changed",
            )

        payload = {
            "channel": clean_channel,
            "user_id": clean_user_id,
            "session_id": clean_session_id,
            "content": clean_content,
            "message_type": "reply",
            "task_ref": task_id,
            "metadata": {
                "purpose": "customer_acceptance_reminder",
                "work_item_id": work_item_id,
                "run_id": run_id,
            },
        }
        preview = {
            "channel": clean_channel,
            "recipient": clean_user_id,
            "destination": clean_session_id,
            "content": clean_content,
            "work_item_id": work_item_id,
        }
        publication = await engine.propose_effect_for_approval(
            provider=f"channel:{clean_channel}",
            kind=ENTERPRISE_MESSAGE_SEND,
            payload=payload,
            idempotency_key=_idempotency_key(
                project_id=project_id,
                run_id=run_id,
                work_item_id=work_item_id,
                channel=clean_channel,
                user_id=clean_user_id,
                session_id=clean_session_id,
                content=clean_content,
            ),
            prompt=f"是否通过 {clean_channel} 向客户发送这条验收提醒？",
            preview=preview,
            # This checkpoint authorizes the external API call.  It does not
            # represent the customer's contractual acceptance of the project.
            approval_type=ApprovalType.TECHNICAL,
            run_id=run_id,
            customer_id=str(
                metadata.get("customer_id") or metadata.get("order_id") or ""
            ).strip(),
            environment_id=str(metadata.get("environment_id") or "production").strip(),
            correlation_id=run_id,
            causation_id=work_item_id,
            task_id=task_id,
            session_id=task_session_id,
        )
        return {
            "status": publication.effect.state.value,
            "awaiting_owner_approval": publication.effect.state.value
            == "awaiting_approval",
            "effect_id": publication.effect.effect_id,
            "checkpoint_id": publication.checkpoint.checkpoint_id,
            "checkpoint_created": publication.checkpoint_created,
            "work_item_id": work_item_id,
            "run_id": run_id,
            "preview": preview,
        }

    return [
        ToolDefinition(
            name="request_customer_acceptance_reminder",
            description=(
                "Prepare one customer acceptance reminder for an existing enterprise "
                "channel. This tool never sends directly: it creates an exact preview "
                "and pauses for authenticated owner approval. Use only from the linked "
                "FDE delivery work item. Repeating the same content is idempotent."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Configured channel name, for example feishu or wecom.",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "Provider-specific customer identity.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Provider-specific conversation or chat identity.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Exact reminder body that the owner will review.",
                        "maxLength": _MAX_CONTENT_CHARS,
                    },
                },
                "required": ["channel", "user_id", "session_id", "content"],
                "additionalProperties": False,
            },
            func=request_customer_acceptance_reminder,
            category="fde_delivery",
            requires_confirmation=False,
            concurrency_safe=True,
            read_only=False,
            company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
            permission_effects=["runtime_internal"],
        )
    ]
