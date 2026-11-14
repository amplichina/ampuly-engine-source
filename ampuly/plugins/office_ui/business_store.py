"""The business database: one place for who, what order, and who signed it.

Architecture review 3.2 (the module contract): business truth used
to sit in seven files. ``<ampuly_home>/business.db`` now holds the business
layer's own objects, separate from the kernel's runtime store:

* ``accounts`` - the owner and employee sub-accounts (moved here from
  accounts.json; the JSON file is imported once and renamed);
* ``work_orders`` - a customer order through its life
  ``collecting -> pending_review -> launched -> producing -> delivering -> closed | rejected``;
* ``order_events`` - who did what to an order, when.

Plain sqlite3, synchronous, tiny: these tables see a handful of writes per
order. The kernel never reads this file.
"""

from __future__ import annotations

import base64
import json
import re
import math
import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .ai_employee_run_contract import FrozenEmployeePins

ORDER_STATUSES = ("collecting", "pending_review", "launching", "launched", "producing", "delivering", "closed", "rejected")
# An order that has ended stays in the book; these two are the end.
ENDED_ORDER_STATUSES = frozenset({"closed", "rejected"})
# ``list_orders`` never hands back more rows than this, whatever it is asked for. A caller that reads a list as "all
# of them" is wrong past this line: ask by status or by id, count with ``count_orders``, or say the list was cut.
ORDER_LIST_HARD_CAP = 500
# ``list_rollouts`` answers the newest this many unless asked otherwise; the console says so when there were more.
ROLLOUT_LIST_LIMIT = 50
# 4.7-A08: where a page of orders stops, handed out by ``list_orders_page`` and taken back as ``before``. Versioned so
# a cursor from an older page can be told apart from garbage.
ORDER_CURSOR_PREFIX = "o1."
ORDER_TRANSITIONS: dict[str, frozenset[str]] = {
    "collecting": frozenset({"pending_review", "rejected"}),
    "pending_review": frozenset({"launching", "launched", "rejected", "collecting"}),
    # A claimed order is being started right now; it can only finish starting
    # or fall back to review when the launch itself failed.
    "launching": frozenset({"launched", "pending_review", "rejected"}),
    "launched": frozenset({"producing", "rejected", "closed"}),
    "producing": frozenset({"delivering", "closed", "rejected"}),
    "delivering": frozenset({"closed", "producing"}),
    "closed": frozenset(),
    "rejected": frozenset({"collecting"}),
}

ACCEPTANCE_STATUSES = (
    "criteria_defined",
    "evidence_collecting",
    "submitted",
    "accepted",
    "rejected",
    "accepted_with_exceptions",
)
_SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")

ACCEPTANCE_DECISIONS = frozenset(
    {"accepted", "rejected", "accepted_with_exceptions"}
)
# The decisions that pass a round: the only ones an order is closed on (确认收货并结案).
ACCEPTANCE_PASSING = frozenset({"accepted", "accepted_with_exceptions"})
# A decision in the owner's words.
_ACCEPTANCE_DECISION_WORDS = {"accepted": "通过", "accepted_with_exceptions": "带例外通过", "rejected": "打回"}


def acceptance_evidence_path(item: Any) -> str:
    """The workplace file one acceptance evidence item names (``artifact_path``, or an older ``path``); "" for none."""
    if not isinstance(item, dict):
        return ""
    return str(item.get("artifact_path", item.get("path", "")) or "").strip().replace("\\", "/")

PROJECT_LIFECYCLE_STATUSES = ("active", "archived", "destroyed")

# What belongs to a project in this store: exactly what ``purge_project_operational_data`` removes. These are the
# console's own tables; a registered console extension adds its own (``extensions.ProjectRecords``), and
# ``PROJECT_RECORD_TABLES`` (``project_record_tables``) is both.
CORE_PROJECT_RECORD_TABLES = (
    "work_orders", "order_events", "gate_cards", "gate_events", "business_outbox",
    # 4.7 Q01: which flows the project adopted, and how that changed.
    "project_workflow_adoptions", "project_workflow_adoption_events",
    # 4.7-A08: the owner's distribution authorizations. Purged with the project and carried in its backup for the
    # record, but -- like the outbox -- never put back by a restore: a restored project must not bring back a live
    # permission to send. The drafted/approved/revoked trail comes back with order_events. Absent on a business
    # database still at schema version 1; the purge and the export check for it.
    "distribution_authorizations",
    # 前端构型第 3 步第 2 页 (business schema version 3): the project's business boundary and its connection plans are
    # the project's own configuration -- purged with it (a project created again under the same id must not inherit
    # the old one's boundary), carried in its backup and put back by a restore (they are no permission to send).
    # Absent on a database below version 3; the purge, the export and the restore check for the tables.
    "project_profiles", "project_connection_plans",
)


def _extension_project_records() -> tuple[Any, ...]:
    """The registered console extensions' own project tables and their rules (``extensions.ProjectRecords``)."""
    from ampuly.plugins.office_ui import extensions

    return extensions.project_records()


def project_record_tables() -> tuple[str, ...]:
    """Every table a project's records carry: the console's own, then the registered extensions' (in their order)."""
    tables = list(CORE_PROJECT_RECORD_TABLES)
    for records in _extension_project_records():
        tables.extend(name for name in records.tables if name not in tables)
    return tuple(tables)


def __getattr__(name: str) -> Any:
    # ``PROJECT_RECORD_TABLES`` is read when asked (PEP 562), so it names the tables of the extensions registered then.
    if name == "PROJECT_RECORD_TABLES":
        return project_record_tables()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# 数据与文件 P3: a project's money. Two tables carry `project_id`; the other two hang off a budget, so they are
# selected through the budgets this project owns. Kept apart from PROJECT_RECORD_TABLES because a purge does NOT
# remove them today -- listing them there would quietly change what deleting a project destroys.
PROJECT_BUDGET_TABLES = ("run_budgets", "budget_reservations", "budget_effect_holds", "blocker_snoozes")

# Who handles an order, by when, for how much (``update_order_handling``).
HANDLING_KEY = "_handling"
HANDLING_FIELDS = ("assignee_employee_id", "due_at", "amount_quoted", "amount_received", "currency")
_HANDLING_CURRENCIES = ("CNY", "USD", "EUR", "HKD", "JPY")
_HANDLING_AMOUNT_MAX = 1_000_000_000


def normalize_handling_value(name: str, value: Any) -> Any:
    """One handling field, cleaned; ``None`` means "clear it".  Raises on a value that is not that kind of fact."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if name == "assignee_employee_id":
        text = str(value).strip()
        if len(text) > 120 or any(ch in text for ch in "\r\n\t"):
            raise OrderTransitionError("assignee_employee_id is not an employee id")
        return text
    if name == "due_at":
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise OrderTransitionError("due_at must be a date (YYYY-MM-DD) or an ISO date-time") from exc
        # A bare date stays a date: "due on the 25th" is not "due at midnight".
        return text[:10] if len(text) <= 10 else parsed.isoformat(timespec="minutes")
    if name in {"amount_quoted", "amount_received"}:
        if isinstance(value, bool):
            raise OrderTransitionError(f"{name} must be a number")
        try:
            amount = float(value)
        except (TypeError, ValueError) as exc:
            raise OrderTransitionError(f"{name} must be a number") from exc
        if amount != amount or amount in (float("inf"), float("-inf")) or amount < 0 or amount > _HANDLING_AMOUNT_MAX:
            raise OrderTransitionError(f"{name} must be between 0 and {_HANDLING_AMOUNT_MAX}")
        return round(amount, 2)
    if name == "currency":
        text = str(value).strip().upper()
        if text not in _HANDLING_CURRENCIES:
            raise OrderTransitionError("currency must be one of " + ", ".join(_HANDLING_CURRENCIES))
        return text
    raise OrderTransitionError(f"unsupported handling field {name!r}")

# A policy change reaches a work item only when its plan is built: the
# planner copies the runtime policy into plan metadata and company_mode
# reads it from there. Work items already planned carry the old policy
# and are not retroactively governed, so the record says so rather than
# leaving a reader to assume otherwise.
POLICY_CHANGE_SCOPE = (
    "plans built after this change; work items already planned keep the policy "
    "they were planned with"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    login TEXT PRIMARY KEY,
    login_key TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    employee_id TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    disabled INTEGER NOT NULL DEFAULT 0,
    salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS account_proxies (
    login TEXT PRIMARY KEY,
    proxy_for TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_orders (
    order_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    org_id TEXT NOT NULL DEFAULT '',
    customer_id TEXT NOT NULL DEFAULT '',
    environment_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    customer_name TEXT NOT NULL DEFAULT '',
    brand_name TEXT NOT NULL DEFAULT '',
    fields_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT '',
    source_task_id TEXT NOT NULL DEFAULT '',
    invocation_key TEXT NOT NULL DEFAULT '',
    reviewer_employee_id TEXT NOT NULL DEFAULT '',
    review_note TEXT NOT NULL DEFAULT '',
    launched_task_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    note_json TEXT NOT NULL DEFAULT '{}',
    event_id TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL DEFAULT '',
    causation_id TEXT NOT NULL DEFAULT '',
    previous_hash TEXT NOT NULL DEFAULT '',
    event_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id, id);
CREATE TABLE IF NOT EXISTS gate_cards (
    checkpoint_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    order_id TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    sent_at TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_gate_cards_message ON gate_cards(message_id);
CREATE TABLE IF NOT EXISTS gate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    note_json TEXT NOT NULL DEFAULT '{}',
    event_id TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL DEFAULT '',
    causation_id TEXT NOT NULL DEFAULT '',
    previous_hash TEXT NOT NULL DEFAULT '',
    event_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_gate_events_cp ON gate_events(checkpoint_id, id);
CREATE TABLE IF NOT EXISTS business_outbox (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id TEXT NOT NULL UNIQUE,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_business_outbox_pending
    ON business_outbox(status, sequence);
CREATE INDEX IF NOT EXISTS idx_business_outbox_aggregate
    ON business_outbox(aggregate_type, aggregate_id, sequence);
CREATE TABLE IF NOT EXISTS solution_packages (
    solution_id TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (solution_id, version)
);
CREATE TABLE IF NOT EXISTS deployments (
    customer_id TEXT NOT NULL,
    environment_id TEXT NOT NULL DEFAULT '',
    solution_id TEXT NOT NULL,
    version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (customer_id, environment_id, solution_id)
);
CREATE TABLE IF NOT EXISTS deployment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    environment_id TEXT NOT NULL DEFAULT '',
    solution_id TEXT NOT NULL,
    from_version TEXT NOT NULL DEFAULT '',
    to_version TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS contacts (
    contact_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS contact_identities (
    channel TEXT NOT NULL,
    identity TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    bound_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (channel, identity)
);
CREATE TABLE IF NOT EXISTS outbound_message_links (
    message_key TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS process_specs (
    spec_id TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    PRIMARY KEY (spec_id, version)
);
CREATE TABLE IF NOT EXISTS builder_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_id TEXT NOT NULL,
    round_number INTEGER NOT NULL,
    spec_digest TEXT NOT NULL,
    outcome TEXT NOT NULL,
    failed_step TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    proposal_json TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connector_switches (
    connector TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    actor TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS policy_changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile TEXT NOT NULL DEFAULT 'custom',
    field TEXT NOT NULL,
    old_value TEXT NOT NULL DEFAULT 'null',
    new_value TEXT NOT NULL DEFAULT 'null',
    actor TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS metric_baselines (
    metric TEXT NOT NULL,
    customer_id TEXT NOT NULL DEFAULT '',
    workflow_id TEXT NOT NULL DEFAULT '',
    value INTEGER NOT NULL,
    unit TEXT NOT NULL DEFAULT 'cents',
    source TEXT NOT NULL DEFAULT 'stated',
    evidence TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    PRIMARY KEY (metric, customer_id, workflow_id)
);
CREATE TABLE IF NOT EXISTS rollouts (
    rollout_id TEXT PRIMARY KEY,
    solution_id TEXT NOT NULL,
    version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'planned',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rollout_targets (
    rollout_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    environment_id TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    from_version TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending',
    health TEXT NOT NULL DEFAULT 'unknown',
    checks_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (rollout_id, customer_id, environment_id)
);
CREATE TABLE IF NOT EXISTS credential_references (
    reference_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    customer_id TEXT NOT NULL DEFAULT '',
    environment_id TEXT NOT NULL DEFAULT '',
    env_var TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_credential_reference_scope
    ON credential_references(purpose, customer_id, environment_id);
CREATE TABLE IF NOT EXISTS run_budgets (
    budget_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL DEFAULT 'default',
    customer_id TEXT NOT NULL DEFAULT '',
    environment_id TEXT NOT NULL DEFAULT '',
    order_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    limits_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'open',
    health TEXT NOT NULL DEFAULT 'ok',
    state_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_run_budgets_order ON run_budgets(order_id);
CREATE TABLE IF NOT EXISTS budget_effect_holds (
    budget_id TEXT NOT NULL,
    effect_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (budget_id, effect_id)
);
CREATE TABLE IF NOT EXISTS blocker_snoozes (
    project_id TEXT NOT NULL DEFAULT 'default',
    checkpoint_id TEXT NOT NULL,
    until_at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, checkpoint_id)
);
CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    budget_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    settled_amount REAL NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL DEFAULT '',
    request_fingerprint TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_budget_reservations_budget ON budget_reservations(budget_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_reservations_idem
    ON budget_reservations(budget_id, idempotency_key) WHERE idempotency_key != '';
CREATE TABLE IF NOT EXISTS solution_blueprints (
    blueprint_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    source_order_id TEXT NOT NULL DEFAULT '',
    source_customer_id TEXT NOT NULL DEFAULT '',
    workflow_id TEXT NOT NULL DEFAULT '',
    workflow_version TEXT NOT NULL DEFAULT '',
    plan_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_solution_blueprints_created ON solution_blueprints(created_at);
CREATE TABLE IF NOT EXISTS observation_audit (
    observation_id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL DEFAULT '',
    project_id TEXT NOT NULL,
    customer_id TEXT NOT NULL DEFAULT '',
    environment_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'success',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_observation_audit_scope
    ON observation_audit(project_id, customer_id, at);
CREATE TABLE IF NOT EXISTS project_lifecycle (
    project_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    latest_backup_path TEXT NOT NULL DEFAULT '',
    customer_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS project_lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_project_lifecycle_events_project
    ON project_lifecycle_events(project_id, id);
-- Project workflow adoption: which published version of a flow a project runs,
-- and whether it takes new orders. No row = the flow was never adopted by the project; the old global path stays.
CREATE TABLE IF NOT EXISTS project_workflow_adoptions (
    project_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    candidate_fingerprint TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    adopted_by TEXT NOT NULL DEFAULT '',
    adopted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, workflow_id)
);
CREATE TABLE IF NOT EXISTS project_workflow_adoption_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    from_version TEXT NOT NULL DEFAULT '',
    to_version TEXT NOT NULL DEFAULT '',
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_project_workflow_adoption_events
    ON project_workflow_adoption_events(project_id, workflow_id, id);
-- 数据与文件 P4：客户档案。在这之前「客户」不是一个东西，只是一个到处传的裸字符串——由人在三个自由
-- 文本框里手打，谁都不校验它存不存在，打错一个字母那个账号就静默地什么都看不到。这张表先把名单立起来。
-- 它不带 project_id：客户是跨项目的业务事实，跟组织架构一样属于整台机器，不属于某一个项目的备份。
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
-- 数据与文件 P4：项目档案。它回答 project_lifecycle 答不了的那一个问题：**这个项目到底存不存在**。
-- 生命周期表对一个从没存在过的名字照样答 active（没有行就当 active），于是「不存在」和「存在但从没改
-- 过」在库里长得一模一样。状态仍然归生命周期表管，这里只管身份：登记过没有、谁登记的、什么时候。
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
"""


@dataclass
class WorkOrder:
    order_id: str
    workflow_id: str
    status: str
    org_id: str = ""
    customer_id: str = ""
    environment_id: str = ""
    customer_name: str = ""
    brand_name: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    source_task_id: str = ""
    invocation_key: str = ""
    reviewer_employee_id: str = ""
    review_note: str = ""
    launched_task_id: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def project_id(self) -> str:
        """Return the order's canonical project scope.

        The business schema predates project scoping, so the scope is carried
        in the existing ``fields_json`` document. Legacy rows remain in the
        default project until explicitly re-created.
        """
        value = self.fields.get("project_id", "default") if isinstance(self.fields, dict) else "default"
        return str(value or "default").strip() or "default"

    def to_payload(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "workflow_id": self.workflow_id,
            "org_id": self.org_id,
            "project_id": self.project_id,
            "customer_id": self.customer_id,
            "environment_id": self.environment_id,
            "status": self.status,
            "customer_name": self.customer_name,
            "brand_name": self.brand_name,
            "fields": dict(self.fields),
            "source": self.source,
            "source_task_id": self.source_task_id,
            "invocation_key": self.invocation_key,
            "reviewer_employee_id": self.reviewer_employee_id,
            "review_note": self.review_note,
            "launched_task_id": self.launched_task_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class AcceptanceRecord:
    """Event-sourced contractual acceptance for one work order.

    This deliberately lives in the existing business truth store.  It does
    not reuse action-permission checkpoints and it does not add a third
    database while the cross-domain Outbox is still pending.
    """

    order_id: str
    status: str
    criteria: list[str] = field(default_factory=list)
    criteria_hash: str = ""
    workflow_version: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    evidence_hash: str = ""
    submitted_by: str = ""
    submitted_at: str = ""
    decided_by: str = ""
    decided_at: str = ""
    decision_note: str = ""
    exceptions: list[str] = field(default_factory=list)
    round_no: int = 1
    rework_target: str = ""
    rework_feedback: str = ""
    rework_request_id: str = ""
    previous_evidence_hash: str = ""
    previous_artifact_snapshot: dict[str, Any] = field(default_factory=dict)
    artifact_snapshot: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "status": self.status,
            "criteria": list(self.criteria),
            "criteria_hash": self.criteria_hash,
            "workflow_version": self.workflow_version,
            "evidence": [dict(item) for item in self.evidence],
            "evidence_hash": self.evidence_hash,
            "submitted_by": self.submitted_by,
            "submitted_at": self.submitted_at,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "decision_note": self.decision_note,
            "exceptions": list(self.exceptions),
            "round_no": self.round_no,
            "rework_target": self.rework_target,
            "rework_feedback": self.rework_feedback,
            "rework_request_id": self.rework_request_id,
            "previous_evidence_hash": self.previous_evidence_hash,
            "previous_artifact_snapshot": dict(self.previous_artifact_snapshot),
            "artifact_snapshot": dict(self.artifact_snapshot),
            "history": [dict(item) for item in self.history],
        }


@dataclass
class FDEControlPlane:
    """A read-only, order-scoped projection of the FDE delivery objects.

    The first FDE slice deliberately keeps these objects in the existing
    work-order truth: no third database or parallel mutable state is created.
    Structured values may be supplied under ``fields.fde_control_plane``;
    acceptance, release version and event trace are always taken from their
    authoritative existing records.
    """

    order_id: str
    customer_id: str
    environment_id: str
    phase: str
    as_is: str = ""
    to_be: str = ""
    uat: dict[str, Any] = field(default_factory=dict)
    sla: dict[str, Any] = field(default_factory=dict)
    release: dict[str, Any] = field(default_factory=dict)
    rollback: dict[str, Any] = field(default_factory=dict)
    roi: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "customer_id": self.customer_id,
            "environment_id": self.environment_id,
            "phase": self.phase,
            "as_is": self.as_is,
            "to_be": self.to_be,
            "uat": dict(self.uat),
            "sla": dict(self.sla),
            "release": dict(self.release),
            "rollback": dict(self.rollback),
            "roi": dict(self.roi),
            "trace": dict(self.trace),
        }


@dataclass(frozen=True)
class BusinessOutboxRecord:
    sequence: int
    outbox_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    status: str
    attempt_count: int
    last_error: str
    created_at: str
    published_at: str


@dataclass(frozen=True)
class BusinessOutboxDecodeDiagnostic:
    """Internal retained-row diagnosis, not a dispatchable Outbox record.

    No payload, aggregate scope, last_error or exception text belongs here.
    Identity is included only when its stored shape is safe and bounded.
    """

    code: str
    sequence: int | None
    outbox_id: str | None


@dataclass(frozen=True)
class ObservationAuditRecord:
    """Durable, scope-bound record of a read/observation operation.

    Observation audit is deliberately an audit projection, not a second
    business truth.  A project id is mandatory so callers cannot create an
    unscoped read record by accident; customer/environment are carried when
    the object exposes them.
    """

    observation_id: str
    at: str
    actor: str
    action: str
    object_type: str
    object_id: str
    project_id: str
    customer_id: str
    environment_id: str
    reason: str
    outcome: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "at": self.at,
            "actor": self.actor,
            "action": self.action,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "project_id": self.project_id,
            "customer_id": self.customer_id,
            "environment_id": self.environment_id,
            "reason": self.reason,
            "outcome": self.outcome,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ProjectLifecycleRecord:
    project_id: str
    status: str
    updated_at: str
    updated_by: str
    reason: str = ""
    latest_backup_path: str = ""
    # Which customer's delivery space this project is.  A project holds that
    # customer's memory, workspace and artifacts, so the binding is what makes
    # "an employee bound to acme cannot read globex" true of the files and not
    # only of the order list.
    customer_id: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "status": self.status,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
            "reason": self.reason,
            "latest_backup_path": self.latest_backup_path,
            "customer_id": self.customer_id,
        }


class OrderTransitionError(ValueError):
    pass


class BudgetExceeded(OrderTransitionError):
    """The run asked to spend more than this order was given."""


class OrderCursorError(OrderTransitionError):
    """A ``before`` that is not a cursor this store handed out. The sentence reaches the owner as it is."""


def order_cursor(order: WorkOrder) -> str:
    """Where a page of orders stopped: the last order's ``(created_at, order_id)``, opaque to the page."""
    raw = json.dumps([str(order.created_at or ""), str(order.order_id or "")], ensure_ascii=False, separators=(",", ":"))
    return ORDER_CURSOR_PREFIX + base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def parse_order_cursor(cursor: str) -> tuple[str, str]:
    """``(created_at, order_id)`` out of a cursor from :func:`order_cursor`; anything else is refused, not guessed."""
    text = str(cursor or "").strip()
    refused = OrderCursorError("翻页位置读不懂（可能是旧页面留下的），请刷新工单列表后再试。")
    if not text.startswith(ORDER_CURSOR_PREFIX) or len(text) > 1000:
        raise refused
    body = text[len(ORDER_CURSOR_PREFIX):]
    try:
        value = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:  # binascii.Error is a ValueError
        raise refused from exc
    if not (isinstance(value, list) and len(value) == 2 and all(isinstance(item, str) for item in value) and value[1].strip()):
        raise refused
    return value[0], value[1]


# ── 4.7-A08 批量自动化 · 分发授权单（每单一张） ──
# One authorization per order: what may be sent out (对象), where (渠道), which moves (允许的动作), for how long.
# Drafted and decided by the owner only. The table is added by business schema version 2
# (ampuly/database/schema_version.py): a new database gets it at creation; an existing one only after the owner runs
# `ampuly db-version --apply` -- this store never alters a database already in use.
DISTRIBUTION_AUTHORIZATION_TABLE = "distribution_authorizations"
DISTRIBUTION_AUTHORIZATION_STATUSES = ("draft", "approved", "revoked")
# upload = 上传位, publish = 发布, look_back = 凭内容编号回查 (design 3.2). Nothing else can be authorized here.
DISTRIBUTION_ACTIONS = ("upload", "publish", "look_back")
DISTRIBUTION_ACTION_LABELS = {"upload": "上传", "publish": "发布", "look_back": "回查"}
# D1 (policy): a channel is the website a batch step works on, named by its host exactly as the
# step's browser connector names it (browser_connector.clean_domain: lower case, no scheme or path). A draft offers
# only the sites the order's own batch steps declare. These are the names the owner reads for the common ones; any
# other host is shown as it is. Display only: matching is by host, never by name.
DISTRIBUTION_CHANNEL_NAMES = {
    "kuaishou.com": "快手", "douyin.com": "抖音", "xiaohongshu.com": "小红书", "channels.weixin.qq.com": "视频号",
    "bilibili.com": "哔哩哔哩", "weibo.com": "微博", "taobao.com": "淘宝", "1688.com": "1688",
}
# The owner's sentence for a batch step that has not said which of the three it does.
DISTRIBUTION_ACTION_MISSING_ZH = "这一步没选做什么（上传 / 发布 / 回查），分发授权单没法核对。在搭建台这一步的设置里选一个再发起。"
# policy (design 6, item 3): at most 14 days; a longer run opens a new one.
DISTRIBUTION_AUTHORIZATION_MAX_DAYS = 14
DISTRIBUTION_AUTHORIZATION_DEFAULT_DAYS = 7
# The same ceiling as a batch step's own list (browser_bridge.ORDER_ITEMS_LIMIT): an authorization never names more
# rows than a batch step may work.
DISTRIBUTION_TARGETS_MAX = 200
DISTRIBUTION_CHANNELS_MAX = 20
# How long one target may be is the kernel's own ITEM_VALUE_MAX (ampuly/computer_use/browser_bridge.py), read where a
# draft is written: a batch step names a row by at most that many characters of it, so an authorization does too
# (LP2-A). A longer name is kept as the kernel keeps it, not refused -- refused, that row could never be authorized.
# A channel is a host since D1, so it may be as long as a host may be (was 40 for a free word).
_DISTRIBUTION_CHANNEL_MAX = 253


# ── S3-2A 项目中心 · 业务边界与接入规划 ──
# Two small tables added by business schema version 3 (ampuly/database/schema_version.py): a new database gets them at
# creation; an existing one only after the owner runs `ampuly db-version --apply` -- this store never alters a database
# already in use. Until then every read here answers empty and every write refuses in the owner's words.
PROJECT_PROFILE_TABLE = "project_profiles"
PROJECT_CONNECTION_PLAN_TABLE = "project_connection_plans"
# The four texts of a project's business boundary (owner-entered; a planned manager is a note, never a grant).
PROJECT_PROFILE_FIELDS = ("start", "scope", "end", "manager_employee_id")
_PROJECT_PROFILE_COLUMNS = {"start": "start_text", "scope": "scope_text", "end": "end_text",
                            "manager_employee_id": "manager_employee_id"}
PROJECT_PROFILE_TEXT_MAX = 800
_PROJECT_PROFILE_ID_MAX = 120
# The four lanes of a project's software & connections, in the order the page shows them.
CONNECTION_LANES = ("intake", "review", "customer", "files")
CONNECTION_LANE_LABELS = {"intake": "业务来源", "review": "内部协作", "customer": "客户确认", "files": "交付存储"}
# What the owner enters per lane: which software, what is handed over, how, and which stage it hangs on.
CONNECTION_PLAN_FIELDS = ("tool", "purpose", "mode", "stage")
CONNECTION_PLAN_TEXT_MAX = 200
_CONNECTION_STAGE_MAX = 80


class ProjectProfileError(OrderTransitionError):
    """A project boundary or connection plan that cannot be saved as asked. Said in the owner's words."""


def _clean_profile_text(name: str, value: Any, *, longest: int) -> str:
    """One owner-entered text, stripped; refused rather than trimmed when it runs past ``longest``."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProjectProfileError(f"「{name}」要是一段文字")
    text = value.replace("\r\n", "\n").strip()
    if len(text) > longest:
        raise ProjectProfileError(f"「{name}」最多 {longest} 个字，现在有 {len(text)} 个")
    return text


def _clean_short_id(name: str, value: Any, *, longest: int) -> str:
    text = str(value or "").strip()
    if len(text) > longest or any(ch in text for ch in "\r\n\t"):
        raise ProjectProfileError(f"「{name}」不像一个编号")
    return text


class DistributionAuthorizationError(OrderTransitionError):
    """A distribution authorization that cannot be drafted, approved or revoked as asked. Said in the owner's words."""


def _clean_distribution_list(values: Any, *, what: str, most: int, longest: int,
                             cut: list[str] | None = None) -> list[str]:
    """A list of short single-line words, in the order given, duplicates dropped; refused rather than trimmed.

    Except where ``cut`` is given (the rows of a batch list, LP2-A): an item longer than ``longest`` is kept as its
    first ``longest`` characters, stripped -- the identity the kernel itself gives that row -- and the original is
    added to ``cut``. Two different items that are the same once cut are refused: the batch step could not tell them
    apart, so neither could an authorization.
    """
    if isinstance(values, str):
        values = [part for part in re.split(r"[\n,，、;；]+", values)]
    if not isinstance(values, (list, tuple)):
        raise DistributionAuthorizationError(f"{what}要写成一个列表。")
    cleaned: list[str] = []
    whole: dict[str, str] = {}
    for value in values:
        if isinstance(value, (dict, list, tuple, bool)) or value is None:
            raise DistributionAuthorizationError(f"{what}里有一项不是文字。")
        text = str(value).strip()
        if not text:
            continue
        original = text
        if cut is not None and len(text) > longest:
            cut.append(text)
            text = text[:longest].strip()
        if cut is not None and whole.setdefault(text, original) != original:
            raise DistributionAuthorizationError(
                f"{what}里有两项前 {longest} 个字完全一样（「{text[:20]}」开头），批量步骤只认前 {longest} 个字，"
                "分不清它们；请把它们改得前面就不一样。")
        if len(text) > longest or any(ch in text for ch in "\r\n\t"):
            raise DistributionAuthorizationError(f"{what}里「{text[:20]}」太长或带了换行（每项最多 {longest} 个字）。")
        if text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        raise DistributionAuthorizationError(f"{what}至少要写一项。")
    if len(cleaned) > most:
        raise DistributionAuthorizationError(f"{what}最多 {most} 项，这里有 {len(cleaned)} 项；一张授权单管不了这么多，请分开。")
    return cleaned


def _distribution_when(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text or "").strip()) if str(text or "").strip() else None
    except ValueError:
        return None


def _distribution_drafted(text: str) -> str:
    """How the owner tells one authorization from another in a sentence: when it was drafted (never its internal id)."""
    drafted = _distribution_when(text)
    return f"{drafted.strftime('%Y-%m-%d %H:%M')} 起草的那张" if drafted else "之前起草的那张"


def distribution_channel_name(host: Any) -> str:
    """The name the owner reads for a channel (D1): 快手 for kuaishou.com or a part of it (cp.kuaishou.com), the host
    itself for any other site -- and a stored word that is no host at all (a slip from before D1) as it was written."""
    text = str(host or "").strip()
    clean = text.lower()
    for known, name in DISTRIBUTION_CHANNEL_NAMES.items():
        if clean == known or clean.endswith("." + known):
            return name
    return text


def _distribution_channel_said(host: str) -> str:
    """A channel in a sentence: 「快手（kuaishou.com）」, or the host alone when it has no name of its own."""
    name = distribution_channel_name(host)
    return name if name == host else f"{name}（{host}）"


def _distribution_is_host(channel: str) -> bool:
    """Whether a stored channel is a site's host (D1) rather than a word written before it (「快手」)."""
    from ampuly.plugins.office_ui.browser_connector import clean_domain

    return "." in channel and clean_domain(channel) == channel


def _clean_distribution_channels(channels: Any, sites: Any) -> list[str]:
    """D1 (policy): the channels a draft names, as hosts, and only among ``sites`` -- the sites the
    order's own batch steps work on. Each may be written as the host (any case, with or without https://) or as the
    name the card shows for it (快手); anything else is refused in the owner's words, never kept as a free word.
    Returned in the order the sites are offered."""
    from ampuly.plugins.office_ui.browser_connector import clean_domain

    offered = [host for host in dict.fromkeys(clean_domain(site) for site in (
        sites if isinstance(sites, (list, tuple)) else [])) if host]
    if not offered:
        raise DistributionAuthorizationError(
            "这张工单的流程里没有写明批量步骤要去哪些网站，没法选渠道；先在搭建台给批量步骤写上网站，再起草。")
    named = _clean_distribution_list(channels, what="发到哪些渠道", most=DISTRIBUTION_CHANNELS_MAX,
                                     longest=_DISTRIBUTION_CHANNEL_MAX)
    names = [distribution_channel_name(host) for host in offered]
    # A name stands for a site only when exactly one offered site carries it (two parts of one platform do not).
    by_name = {name: host for name, host in zip(names, offered) if name != host and names.count(name) == 1}
    picked: set[str] = set()
    outside: list[str] = []
    for item in named:
        host = clean_domain(item)
        if host not in offered:
            host = by_name.get(item, host)  # 「1688」 is a name here, though it would pass for a one-word host
        if host in offered:
            picked.add(host)
        else:
            outside.append(item)
    if outside:
        raise DistributionAuthorizationError(
            f"渠道只能从这张工单的批量步骤要去的网站里选（{'、'.join(_distribution_channel_said(h) for h in offered)}），"
            f"这些不在其中：{'、'.join(outside[:5])}{' 等' if len(outside) > 5 else ''}。")
    return [host for host in offered if host in picked]


def _distribution_step(step: Any) -> dict[str, Any]:
    """One batch step as the gate reads it (D1): its name, its sites as hosts, its declared action ('' when none)."""
    from ampuly.plugins.office_ui.browser_connector import clean_domain

    raw = step if isinstance(step, dict) else {}
    sites = raw.get("sites") if isinstance(raw.get("sites"), (list, tuple)) else []
    action = raw.get("action")
    return {"name": str(raw.get("name", "") or "").strip(),
            "sites": [host for host in dict.fromkeys(clean_domain(site) for site in sites) if host],
            "action": action if isinstance(action, str) else ""}


def _distribution_step_not_covered(current: dict[str, Any], step: dict[str, Any]) -> tuple[str, str] | None:
    """Why the live authorization ``current`` does not cover this batch step, as (reason, the owner's sentence), or
    None when it does: every site the step works on is one of its channels, and the step's action one of its actions.
    Exact hosts only -- a channel covers the site it names, not a part of it nor a site it is part of."""
    lead = f"「{step['name']}」这一步" if step["name"] else "这一步"
    channels = [str(channel) for channel in current.get("channels") or []]
    sites = list(step["sites"])
    if not sites:
        return "channel_not_covered", f"{lead}没写要去的网站，对不上分发授权单的渠道。在搭建台这一步的设置里写上网站，再核对。"
    uncovered = [site for site in sites if site not in channels]
    if uncovered:
        drafted = _distribution_drafted(str(current.get("created_at", "") or ""))
        if any(not _distribution_is_host(channel) for channel in channels):
            return "channel_not_covered", (
                f"生效的分发授权单（{drafted}）写的渠道是「{'、'.join(channels)}」——那是起草时手写的名字，不是网站，"
                f"对不上{lead}要去的网站（{'、'.join(_distribution_channel_said(site) for site in sites)}）。"
                "请撤销它，按网站重新起草一张并批准。")
        return "channel_not_covered", (
            f"{lead}要去的网站 {'、'.join(_distribution_channel_said(site) for site in uncovered)} 不在生效的分发授权单"
            f"（{drafted}）的渠道里（渠道：{'、'.join(_distribution_channel_said(c) for c in channels) or '无'}）。"
            "请撤销旧的，勾上这些网站重新起草并批准。")
    if step["action"] not in list(current.get("actions") or []):
        allowed = "、".join(DISTRIBUTION_ACTION_LABELS.get(a, a) for a in current.get("actions") or []) or "无"
        return "action_not_covered", (
            f"{lead}要「{DISTRIBUTION_ACTION_LABELS.get(step['action'], step['action'])}」，"
            f"生效的分发授权单只允许「{allowed}」。请撤销旧的，按这一步要做的动作重新起草并批准。")
    return None


class BusinessStore:
    def __init__(self, ampuly_home: Path | str) -> None:
        from ampuly.database import schema_version

        self.path = Path(ampuly_home) / "business.db"
        conn = None
        primary = None
        close_failed = False
        with schema_version._database_opening(self.path, schema_version.BUSINESS) as opening:
            try:
                conn = sqlite3.connect(str(opening.path), timeout=5.0)
                conn.row_factory = sqlite3.Row
                schema_version._verify_database_opening(conn, opening)
                conn.executescript(_SCHEMA)
                self._ensure_work_order_scope_columns(conn)
                self._ensure_work_order_invocation_key(conn)
                self._ensure_account_scope_columns(conn)
                self._ensure_project_customer_column(conn)
                self._ensure_blueprint_version_column(conn)
                self._ensure_event_columns(conn, "order_events", "order_id")
                self._ensure_event_columns(conn, "gate_events", "checkpoint_id")
                # Legacy initialization can commit; only the new bundle + version
                # are atomic. Existing partial pilot schemas are never repaired.
                if conn.in_transaction:
                    conn.commit()
                schema_version.on_open(conn, self.path, schema_version.BUSINESS,
                    fresh=opening.fd is not None, _opening=opening)
                schema_version._verify_database_opening(conn, opening)
                conn.commit()
            except BaseException as error:
                primary = error
            if conn is not None:
                try:
                    if primary is not None and conn.in_transaction:
                        conn.rollback()
                except BaseException:
                    pass
                try:
                    conn.close()
                except BaseException as error:
                    if primary is None:
                        if isinstance(error, Exception):
                            close_failed = True
                        else:
                            primary = error
            if primary is not None:
                raise primary
            if close_failed:
                raise schema_version._SourceSchemaError("outcome_unknown") from None

    @staticmethod
    def _ensure_blueprint_version_column(conn: sqlite3.Connection) -> None:
        """Which workflow version the plan was proved on.

        A plan is only reusable against the process it was written for; a
        blueprint that does not remember its version cannot warn anyone when
        the workflow has moved on since.
        """
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(solution_blueprints)").fetchall()}
        if columns and "workflow_version" not in columns:
            conn.execute("ALTER TABLE solution_blueprints ADD COLUMN workflow_version TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _ensure_project_customer_column(conn: sqlite3.Connection) -> None:
        """Which customer a project belongs to; empty means unassigned."""
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(project_lifecycle)").fetchall()}
        if columns and "customer_id" not in columns:
            conn.execute("ALTER TABLE project_lifecycle ADD COLUMN customer_id TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _ensure_account_scope_columns(conn: sqlite3.Connection) -> None:
        """Which customers an account may see. Empty means every customer.

        Customer scope used to arrive in the request, so an employee could
        simply ask for another customer's orders. Storing it on
        the account makes the boundary the server's to enforce.
        """
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        if "customer_ids" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN customer_ids TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _ensure_work_order_scope_columns(conn: sqlite3.Connection) -> None:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(work_orders)").fetchall()}
        for name in ("customer_id", "environment_id"):
            if name not in columns:
                conn.execute(f"ALTER TABLE work_orders ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _ensure_work_order_invocation_key(conn: sqlite3.Connection) -> None:
        """Add a durable idempotency key for machine-created child orders.

        Empty keys preserve the historical console behaviour.  A non-empty key
        is unique across the business store, so a node retry or process restart
        can recover the same child order instead of silently buying the work
        twice.
        """
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(work_orders)").fetchall()}
        if "invocation_key" not in columns:
            conn.execute("ALTER TABLE work_orders ADD COLUMN invocation_key TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_work_orders_invocation_key "
            "ON work_orders(invocation_key) WHERE invocation_key != ''"
        )

    @staticmethod
    def _ensure_event_columns(conn: sqlite3.Connection, table: str, aggregate_column: str) -> None:
        """Add the v4.1 event-envelope columns to existing business databases.

        The migration is deliberately additive: legacy rows remain readable and
        new writes use the envelope/hash-chain path below.
        """
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        definitions = {
            "event_id": "TEXT NOT NULL DEFAULT ''",
            "correlation_id": "TEXT NOT NULL DEFAULT ''",
            "causation_id": "TEXT NOT NULL DEFAULT ''",
            "previous_hash": "TEXT NOT NULL DEFAULT ''",
            "event_hash": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in definitions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection,
        *,
        table: str,
        aggregate_column: str,
        aggregate_id: str,
        at: str,
        kind: str,
        actor: str = "",
        note: dict[str, Any] | None = None,
        correlation_id: str = "",
        causation_id: str = "",
    ) -> str:
        """Append a business event with the shared v4.1 envelope.

        ``previous_hash`` is scoped to the aggregate stream.  This keeps the
        chain useful for order/gate audit without introducing a third truth
        database or changing the existing read model.
        """
        previous = conn.execute(
            f"SELECT event_hash FROM {table} WHERE {aggregate_column} = ? ORDER BY id DESC LIMIT 1",
            (str(aggregate_id or ""),),
        ).fetchone()
        previous_hash = str(previous[0] or "") if previous is not None else ""
        event_id = f"{table}:{uuid.uuid4().hex}"
        clean_note = dict(note or {})
        envelope = {
            "schema_version": 1,
            "event_id": event_id,
            "aggregate_type": "work_order" if table == "order_events" else "gate_card",
            "aggregate_id": str(aggregate_id or ""),
            "at": str(at or ""),
            "kind": str(kind or ""),
            "actor": str(actor or ""),
            "correlation_id": str(correlation_id or aggregate_id or ""),
            "causation_id": str(causation_id or ""),
            "previous_hash": previous_hash,
            "note": clean_note,
        }
        event_hash = hashlib.sha256(
            json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        conn.execute(
            f"""INSERT INTO {table}
                ({aggregate_column}, at, kind, actor, note_json, event_id,
                 correlation_id, causation_id, previous_hash, event_hash)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                str(aggregate_id or ""),
                str(at or ""),
                str(kind or ""),
                str(actor or ""),
                json.dumps(clean_note, ensure_ascii=False, sort_keys=True),
                event_id,
                str(correlation_id or aggregate_id or ""),
                str(causation_id or ""),
                previous_hash,
                event_hash,
            ),
        )
        return event_id

    @contextmanager
    def _connect(self):
        # sqlite3's own context manager commits but never closes; on Windows
        # a leaked handle keeps the file locked for everyone else.
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def extension_connection(self):
        """This store's own connection, for a console extension's own tables (``extensions``): the same rules as every
        read and write here -- committed when the block ends, rolled back on an error, always closed. Use:
        ``with business.extension_connection() as conn: ...``."""
        return self._connect()

    # ── observation audit ──
    @staticmethod
    def _safe_observation_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
        """Keep audit context useful without persisting credential material."""
        safe: dict[str, Any] = {}
        for key, value in dict(metadata or {}).items():
            clean_key = str(key or "").strip()
            if not clean_key:
                continue
            lowered = clean_key.lower()
            if any(marker in lowered for marker in ("token", "secret", "password", "cookie", "private_key", "api_key")):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe[clean_key] = value
            else:
                safe[clean_key] = str(value)[:500]
        return safe

    def record_observation(
        self,
        *,
        project_id: str,
        action: str,
        object_type: str,
        object_id: str = "",
        actor: str = "",
        customer_id: str = "",
        environment_id: str = "",
        reason: str = "",
        outcome: str = "success",
        metadata: dict[str, Any] | None = None,
        at: str | None = None,
    ) -> ObservationAuditRecord:
        """Persist one scope-bound read observation.

        This method is intentionally synchronous like the business store. It
        is used at console boundaries after authorization and must fail closed
        on a missing project scope rather than silently creating an unscoped
        audit row.
        """
        clean_project = str(project_id or "").strip()
        clean_action = str(action or "").strip()
        clean_type = str(object_type or "").strip()
        if not clean_project:
            raise ValueError("project_id required for observation audit")
        if not clean_action or not clean_type:
            raise ValueError("action and object_type required for observation audit")
        now = str(at or datetime.now().isoformat())
        record = ObservationAuditRecord(
            observation_id=f"obs:{uuid.uuid4().hex}",
            at=now,
            actor=str(actor or "").strip(),
            action=clean_action,
            object_type=clean_type,
            object_id=str(object_id or "").strip(),
            project_id=clean_project,
            customer_id=str(customer_id or "").strip(),
            environment_id=str(environment_id or "").strip(),
            reason=str(reason or "").strip(),
            outcome=str(outcome or "success").strip() or "success",
            metadata=self._safe_observation_metadata(metadata),
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO observation_audit
                   (observation_id, at, actor, action, object_type, object_id,
                    project_id, customer_id, environment_id, reason, outcome,
                    metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.observation_id,
                    record.at,
                    record.actor,
                    record.action,
                    record.object_type,
                    record.object_id,
                    record.project_id,
                    record.customer_id,
                    record.environment_id,
                    record.reason,
                    record.outcome,
                    json.dumps(record.metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
        return record

    def observations(
        self,
        *,
        project_id: str,
        customer_id: str | None = None,
        environment_id: str | None = None,
        limit: int = 100,
    ) -> list[ObservationAuditRecord]:
        """List observations only inside the requested project scope."""
        clean_project = str(project_id or "").strip()
        if not clean_project:
            raise ValueError("project_id required for observation audit")
        bounded_limit = max(1, min(int(limit), 500))
        clauses = ["project_id = ?"]
        params: list[Any] = [clean_project]
        if customer_id is not None:
            clauses.append("customer_id = ?")
            params.append(str(customer_id or "").strip())
        if environment_id is not None:
            clauses.append("environment_id = ?")
            params.append(str(environment_id or "").strip())
        params.append(bounded_limit)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT observation_id, at, actor, action, object_type, object_id, "
                "project_id, customer_id, environment_id, reason, outcome, metadata_json "
                f"FROM observation_audit WHERE {' AND '.join(clauses)} ORDER BY at DESC, rowid DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        out: list[ObservationAuditRecord] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            out.append(ObservationAuditRecord(
                observation_id=str(row["observation_id"]),
                at=str(row["at"]),
                actor=str(row["actor"] or ""),
                action=str(row["action"] or ""),
                object_type=str(row["object_type"] or ""),
                object_id=str(row["object_id"] or ""),
                project_id=str(row["project_id"] or ""),
                customer_id=str(row["customer_id"] or ""),
                environment_id=str(row["environment_id"] or ""),
                reason=str(row["reason"] or ""),
                outcome=str(row["outcome"] or ""),
                metadata=metadata if isinstance(metadata, dict) else {},
            ))
        return out

    # ── project lifecycle ──
    @staticmethod
    def _row_to_project_lifecycle(row: sqlite3.Row | None, project_id: str) -> ProjectLifecycleRecord:
        if row is None:
            return ProjectLifecycleRecord(project_id=str(project_id), status="active", updated_at="", updated_by="")
        return ProjectLifecycleRecord(
            project_id=str(row["project_id"] or project_id),
            status=str(row["status"] or "active"),
            updated_at=str(row["updated_at"] or ""),
            updated_by=str(row["updated_by"] or ""),
            reason=str(row["reason"] or ""),
            latest_backup_path=str(row["latest_backup_path"] or ""),
            # Older databases predate the column; a row read from them has no
            # binding rather than a broken one.
            customer_id=str((row["customer_id"] if "customer_id" in row.keys() else "") or ""),
        )

    def set_project_customer(self, project_id: str, customer_id: str, *, actor: str) -> ProjectLifecycleRecord:
        """Bind a project to the customer whose delivery space it is.

        The binding is what an employee's customer scope is checked against,
        so it is recorded in the project's own lifecycle trail: who bound it,
        when, and what it was bound to before.
        """
        clean = str(project_id or "").strip()
        customer = str(customer_id or "").strip()
        if not clean:
            raise OrderTransitionError("project_id is required")
        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        current = self.project_lifecycle(clean)
        if current.status == "destroyed":
            raise OrderTransitionError(f"project {clean!r} is destroyed")
        # Rebinding a space that already holds another customer's orders would
        # silently reattribute their work. Say which orders, and let a person
        # decide what to do with them.
        if customer:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT customer_id FROM work_orders WHERE json_extract(fields_json, '$.project_id') = ? "
                    "AND customer_id != '' AND customer_id != ?",
                    (clean, customer),
                ).fetchall()
            others = sorted(str(row["customer_id"]) for row in rows)
            if others:
                raise OrderTransitionError(
                    f"project {clean!r} already holds orders for {', '.join(others)}; "
                    "move or close them before binding it to another customer"
                )
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO project_lifecycle(project_id, status, updated_at, updated_by, reason, latest_backup_path, customer_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET "
                "customer_id=excluded.customer_id, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (clean, current.status or "active", now, str(actor), current.reason, current.latest_backup_path, customer),
            )
            conn.execute(
                "INSERT INTO project_lifecycle_events(project_id, at, action, from_status, to_status, actor, reason, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    clean, now,
                    "project_customer_bound" if customer else "project_customer_cleared",
                    current.status, current.status, str(actor), "",
                    json.dumps({"from_customer_id": current.customer_id, "customer_id": customer}, ensure_ascii=False, sort_keys=True),
                ),
            )
        # 数据与文件 P4：绑一次，名单里就有了。纯追加——绑定的行为一点不变，只是顺手把打进去的那个名字
        # 记下来。不拿名单去拦人：建表之前就绑好的那些项目，名单里本来没有它们，拦了就是把真实存在的绑定
        # 判成非法。名单的第一个用处是能列出来。
        if customer:
            self.record_customer(customer, actor=str(actor))
        return self.project_lifecycle(clean)

    # ── 数据与文件 P4：客户与项目的档案 ──

    def record_customer(self, customer_id: str, *, display_name: str = "", actor: str = "",
                        note: str = "") -> dict[str, str]:
        """把一个客户记进名单。已经在名单里就不动它——登记时间和登记人是第一次的那一次。

        这是「客户」第一次成为一个可以列出来的东西。在这之前它只是一个字符串，谁都不知道总共有几个。
        """
        clean = str(customer_id or "").strip()
        if not clean:
            raise OrderTransitionError("customer_id is required")
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO customers(customer_id, display_name, created_at, created_by, note) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(customer_id) DO NOTHING",
                (clean, str(display_name or "").strip(), now, str(actor or "").strip(), str(note or "")),
            )
            row = conn.execute("SELECT * FROM customers WHERE customer_id = ?", (clean,)).fetchone()
        return {key: str(row[key] or "") for key in row.keys()}

    def list_customers(self) -> list[dict[str, str]]:
        """名单上现有的客户。控制台原来只有一个自由文本框，没有任何地方能回答「我有哪些客户」。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM customers ORDER BY created_at, customer_id").fetchall()
        return [{key: str(row[key] or "") for key in row.keys()} for row in rows]

    def record_project(self, project_id: str, *, display_name: str = "", actor: str = "",
                       note: str = "") -> dict[str, str]:
        """把一个项目记进档案。已经登记过就不动它。

        只管身份，不管状态：状态归 project_lifecycle。这两张表回答的是两个不同的问题，合在一起会变成
        第三个说不清的问题。
        """
        clean = str(project_id or "").strip()
        if not clean:
            raise OrderTransitionError("project_id is required")
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO projects(project_id, display_name, created_at, created_by, note) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(project_id) DO NOTHING",
                (clean, str(display_name or "").strip(), now, str(actor or "").strip(), str(note or "")),
            )
            row = conn.execute("SELECT * FROM projects WHERE project_id = ?", (clean,)).fetchone()
        return {key: str(row[key] or "") for key in row.keys()}

    def set_project_display_name(self, project_id: str, display_name: str, *, actor: str = "") -> dict[str, str]:
        """Change the name an owner sees. The project id stays the row key."""
        clean = str(project_id or "").strip()
        name = str(display_name or "").strip()
        if not clean or not name:
            raise OrderTransitionError("project_id and display_name are required")
        if len(name) > 128:
            raise OrderTransitionError("display_name is too long")
        self.record_project(clean, display_name=name, actor=actor)
        with self._connect() as conn:
            conn.execute("UPDATE projects SET display_name = ? WHERE project_id = ?", (name, clean))
            row = conn.execute("SELECT * FROM projects WHERE project_id = ?", (clean,)).fetchone()
        return {key: str(row[key] or "") for key in row.keys()}

    def project_display_names(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT project_id, display_name FROM projects").fetchall()
        return {
            str(row["project_id"]): str(row["display_name"] or "")
            for row in rows
            if str(row["display_name"] or "").strip()
        }

    def project_is_registered(self, project_id: str) -> bool:
        """这个项目登记过没有。

        **这是今天答不出来的那个问题。** `project_lifecycle` 对一个从没存在过的名字答 active，所以不能拿
        它当存在性凭据。这里答的是登记事实：有行就是有，没行就是没有，没有第三种。

        注意：现有项目在第 4 期之前建的，档案里没有它们的行——这不是「它们不存在」，是「还没登记」。
        补登记要动现用的库，属于迁移，按确认时刻另行报范围。所以现在**没有任何地方拿这个答案去拦人**。
        """
        clean = str(project_id or "").strip()
        if not clean:
            return False
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM projects WHERE project_id = ? LIMIT 1", (clean,)).fetchone() is not None

    def project_customer(self, project_id: str) -> str:
        return self.project_lifecycle(project_id).customer_id

    def project_customers(self) -> dict[str, str]:
        return {record.project_id: record.customer_id for record in self.project_lifecycles()}

    # ── S3-2A 项目中心 · 业务边界与接入规划 ──
    def _has_table(self, name: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
        return row is not None

    def project_center_ready(self) -> bool:
        """Whether this database has the two project-centre tables (business schema version 3). Read only: an
        existing database gets them when the owner runs ``ampuly db-version --apply``, never from here."""
        return self._has_table(PROJECT_PROFILE_TABLE) and self._has_table(PROJECT_CONNECTION_PLAN_TABLE)

    @staticmethod
    def project_center_not_ready_text() -> str:
        from ampuly.database.schema_version import OWNER_COMMAND

        return (f"项目的业务边界和接入规划还没启用：这台机器的业务库还是旧的数据格式，系统不会自己去改它。需要你先停掉"
                f"控制台，运行 `{OWNER_COMMAND}`（数据页上有带目录的完整命令；改之前会自动备份，出错会自动退回），"
                "再重新打开控制台。")

    @staticmethod
    def empty_project_profile() -> dict[str, str]:
        return {name: "" for name in PROJECT_PROFILE_FIELDS} | {"updated_at": "", "updated_by": ""}

    @staticmethod
    def _profile_row(row: Any) -> dict[str, str]:
        return {name: str(row[column] or "") for name, column in _PROJECT_PROFILE_COLUMNS.items()} | {
            "updated_at": str(row["updated_at"] or ""), "updated_by": str(row["updated_by"] or ""),
        }

    def project_profile(self, project_id: str) -> dict[str, str]:
        """The four boundary texts of one project (all empty when never written, or while the table is missing)."""
        clean = str(project_id or "").strip()
        if not clean or not self._has_table(PROJECT_PROFILE_TABLE):
            return self.empty_project_profile()
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM {PROJECT_PROFILE_TABLE} WHERE project_id = ?", (clean,)).fetchone()
        return self._profile_row(row) if row is not None else self.empty_project_profile()

    def project_profiles(self) -> dict[str, dict[str, str]]:
        """Every written profile by project id, in one query; ``{}`` while the table is missing."""
        if not self._has_table(PROJECT_PROFILE_TABLE):
            return {}
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM {PROJECT_PROFILE_TABLE} ORDER BY project_id").fetchall()
        return {str(row["project_id"]): self._profile_row(row) for row in rows}

    def set_project_profile(self, project_id: str, *, start: Any = "", scope: Any = "", end: Any = "",
                            manager_employee_id: Any = "", actor: str = "") -> dict[str, str]:
        """Write a project's business boundary. Whole-row: the page sends all four fields every time.

        Only the texts are checked here (at most :data:`PROJECT_PROFILE_TEXT_MAX` characters each); whether the
        planned manager is a member of the project is the console's question, answered where the accounts are.
        Writing one grants nothing.
        """
        clean = str(project_id or "").strip()
        if not clean:
            raise ProjectProfileError("project_id is required")
        if not self._has_table(PROJECT_PROFILE_TABLE):
            raise ProjectProfileError(self.project_center_not_ready_text())
        values = {
            "start_text": _clean_profile_text("从哪接手", start, longest=PROJECT_PROFILE_TEXT_MAX),
            "scope_text": _clean_profile_text("接管范围", scope, longest=PROJECT_PROFILE_TEXT_MAX),
            "end_text": _clean_profile_text("到哪结束", end, longest=PROJECT_PROFILE_TEXT_MAX),
            "manager_employee_id": _clean_short_id("规划管理者", manager_employee_id, longest=_PROJECT_PROFILE_ID_MAX),
        }
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                f"""INSERT INTO {PROJECT_PROFILE_TABLE}
                    (project_id, start_text, scope_text, end_text, manager_employee_id, updated_at, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET start_text = excluded.start_text,
                    scope_text = excluded.scope_text, end_text = excluded.end_text,
                    manager_employee_id = excluded.manager_employee_id, updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by""",
                (clean, values["start_text"], values["scope_text"], values["end_text"],
                 values["manager_employee_id"], now, str(actor or "").strip()),
            )
        return self.project_profile(clean)

    @staticmethod
    def empty_connection_plan(lane: str) -> dict[str, str]:
        return {"lane": str(lane), **{name: "" for name in CONNECTION_PLAN_FIELDS}, "updated_at": "", "updated_by": ""}

    def project_connection_plans(self, project_id: str) -> dict[str, dict[str, str]]:
        """The owner's plan per lane (only the lanes written); ``{}`` while the table is missing."""
        clean = str(project_id or "").strip()
        if not clean or not self._has_table(PROJECT_CONNECTION_PLAN_TABLE):
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {PROJECT_CONNECTION_PLAN_TABLE} WHERE project_id = ? ORDER BY lane", (clean,),
            ).fetchall()
        return {
            str(row["lane"]): {
                "lane": str(row["lane"]),
                **{name: str(row[name] or "") for name in CONNECTION_PLAN_FIELDS},
                "updated_at": str(row["updated_at"] or ""), "updated_by": str(row["updated_by"] or ""),
            }
            for row in rows
        }

    def set_project_connection_plans(self, project_id: str, plans: Any, *, actor: str = "") -> dict[str, dict[str, str]]:
        """Write the owner's plan for one or more lanes. A lane not named keeps what it had.

        ``plans`` is a list of ``{lane, tool, purpose, mode, stage}`` (or a mapping lane -> those fields). The texts
        are checked here; whether ``stage`` names a stage of one of the project's flows is the console's question.
        Nothing here connects to anything: a plan is words about what the owner intends.
        """
        clean = str(project_id or "").strip()
        if not clean:
            raise ProjectProfileError("project_id is required")
        if not self._has_table(PROJECT_CONNECTION_PLAN_TABLE):
            raise ProjectProfileError(self.project_center_not_ready_text())
        if isinstance(plans, dict):
            items = [{"lane": lane, **(dict(fields) if isinstance(fields, dict) else {})} for lane, fields in plans.items()]
        elif isinstance(plans, list):
            items = [dict(item) for item in plans if isinstance(item, dict)]
        else:
            raise ProjectProfileError("接入规划要按四条线路一条一条给")
        rows: list[tuple[str, ...]] = []
        seen: set[str] = set()
        for item in items:
            lane = str(item.get("lane", "") or "").strip()
            if lane not in CONNECTION_LANES:
                raise ProjectProfileError("线路只能是：" + "、".join(CONNECTION_LANE_LABELS[name] for name in CONNECTION_LANES))
            if lane in seen:
                raise ProjectProfileError(f"「{CONNECTION_LANE_LABELS[lane]}」给了两次")
            seen.add(lane)
            label = CONNECTION_LANE_LABELS[lane]
            rows.append((
                clean, lane,
                _clean_profile_text(f"{label} · 软件", item.get("tool"), longest=CONNECTION_PLAN_TEXT_MAX),
                _clean_profile_text(f"{label} · 交接什么", item.get("purpose"), longest=CONNECTION_PLAN_TEXT_MAX),
                _clean_profile_text(f"{label} · 交互方式", item.get("mode"), longest=CONNECTION_PLAN_TEXT_MAX),
                _clean_short_id(f"{label} · 阶段", item.get("stage"), longest=_CONNECTION_STAGE_MAX),
            ))
        if not rows:
            raise ProjectProfileError("没有要保存的线路")
        now = datetime.now().isoformat()
        by = str(actor or "").strip()
        with self._connect() as conn:
            conn.executemany(
                f"""INSERT INTO {PROJECT_CONNECTION_PLAN_TABLE}
                    (project_id, lane, tool, purpose, mode, stage, updated_at, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id, lane) DO UPDATE SET tool = excluded.tool, purpose = excluded.purpose,
                    mode = excluded.mode, stage = excluded.stage, updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by""",
                [(*row, now, by) for row in rows],
            )
        return self.project_connection_plans(clean)

    # ── 项目采用业务流 (4.7 Q01) ──
    ADOPTION_STATUSES = ("active", "paused", "withdrawn")

    @staticmethod
    def _adoption_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def workflow_adoptions(self, project_id: str) -> list[dict[str, Any]]:
        """What one project has adopted: flow, pinned version and fingerprint, whether it takes new orders."""
        clean = str(project_id or "").strip()
        if not clean:
            raise ValueError("project_id required for workflow adoptions")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM project_workflow_adoptions WHERE project_id = ? ORDER BY workflow_id", (clean,),
            ).fetchall()
        return [dict(self._adoption_row(row) or {}) for row in rows]

    def workflow_adoptions_of(self, workflow_id: str) -> list[dict[str, Any]]:
        """Every project's adoption of one flow (the version panel: who runs which version)."""
        clean = str(workflow_id or "").strip()
        if not clean:
            raise ValueError("workflow_id required for workflow adoptions")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM project_workflow_adoptions WHERE workflow_id = ? ORDER BY project_id", (clean,),
            ).fetchall()
        return [dict(self._adoption_row(row) or {}) for row in rows]

    def workflow_adoption(self, project_id: str, workflow_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM project_workflow_adoptions WHERE project_id = ? AND workflow_id = ?",
                (str(project_id or "").strip(), str(workflow_id or "").strip()),
            ).fetchone()
        return self._adoption_row(row)

    def workflow_adoption_events(self, project_id: str, workflow_id: str = "", *, limit: int = 50) -> list[dict[str, Any]]:
        clauses, params = "project_id = ?", [str(project_id or "").strip()]
        if str(workflow_id or "").strip():
            clauses += " AND workflow_id = ?"
            params.append(str(workflow_id).strip())
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM project_workflow_adoption_events WHERE {clauses} ORDER BY id DESC LIMIT ?",
                (*params, max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(self._adoption_row(row) or {}) for row in rows]

    def adopt_workflow(
        self, project_id: str, workflow_id: str, *, version: str, fingerprint: str, actor: str, note: str = "",
    ) -> dict[str, Any]:
        """Pin a published version of a flow to a project; again with a newer one is an upgrade.

        Orders already placed keep the version they were launched with (the launch command pins it); only new
        orders use the adopted one. Adopting the version already adopted and active changes nothing.
        """
        project, flow, clean_version = (str(project_id or "").strip(), str(workflow_id or "").strip(), str(version or "").strip())
        if not project or not flow or not clean_version:
            raise ValueError("project_id, workflow_id and version are required to adopt a flow")
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            before = self._adoption_row(conn.execute(
                "SELECT * FROM project_workflow_adoptions WHERE project_id = ? AND workflow_id = ?", (project, flow),
            ).fetchone())
            if before and before["status"] == "active" and before["workflow_version"] == clean_version:
                return before
            action = "adopt" if before is None or before["status"] == "withdrawn" else "upgrade" if before["workflow_version"] != clean_version else "resume"
            conn.execute(
                "INSERT INTO project_workflow_adoptions(project_id, workflow_id, workflow_version, candidate_fingerprint, status, "
                "adopted_by, adopted_at, updated_at, note) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?) "
                "ON CONFLICT(project_id, workflow_id) DO UPDATE SET workflow_version=excluded.workflow_version, "
                "candidate_fingerprint=excluded.candidate_fingerprint, status='active', adopted_by=excluded.adopted_by, "
                "adopted_at=excluded.adopted_at, updated_at=excluded.updated_at, note=excluded.note",
                (project, flow, clean_version, str(fingerprint or ""), str(actor or ""), now, now, str(note or "")[:500]),
            )
            conn.execute(
                "INSERT INTO project_workflow_adoption_events(project_id, workflow_id, at, action, from_version, to_version, "
                "from_status, to_status, actor, note) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (project, flow, now, action, (before or {}).get("workflow_version", ""), clean_version,
                 (before or {}).get("status", ""), str(actor or ""), str(note or "")[:500]),
            )
            after = self._adoption_row(conn.execute(
                "SELECT * FROM project_workflow_adoptions WHERE project_id = ? AND workflow_id = ?", (project, flow),
            ).fetchone())
        return dict(after or {})

    def set_adoption_status(self, project_id: str, workflow_id: str, status: str, *, actor: str, note: str = "") -> dict[str, Any]:
        """暂停接单 (paused), 恢复 (active) or 撤下 (withdrawn) an adopted flow; the version stays pinned."""
        project, flow, target = str(project_id or "").strip(), str(workflow_id or "").strip(), str(status or "").strip()
        if target not in self.ADOPTION_STATUSES:
            raise ValueError(f"adoption status must be one of {', '.join(self.ADOPTION_STATUSES)}")
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            before = self._adoption_row(conn.execute(
                "SELECT * FROM project_workflow_adoptions WHERE project_id = ? AND workflow_id = ?", (project, flow),
            ).fetchone())
            if before is None:
                raise OrderTransitionError(f"project {project!r} has not adopted {flow!r}")
            if before["status"] == "withdrawn" and target != "withdrawn":
                raise OrderTransitionError(f"{flow!r} was withdrawn from project {project!r}; adopt it again instead")
            if before["status"] == target:
                return before
            conn.execute(
                "UPDATE project_workflow_adoptions SET status = ?, updated_at = ?, note = ? WHERE project_id = ? AND workflow_id = ?",
                (target, now, str(note or before.get("note") or "")[:500], project, flow),
            )
            action = {"paused": "pause", "active": "resume", "withdrawn": "withdraw"}[target]
            conn.execute(
                "INSERT INTO project_workflow_adoption_events(project_id, workflow_id, at, action, from_version, to_version, "
                "from_status, to_status, actor, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (project, flow, now, action, before["workflow_version"], before["workflow_version"], before["status"], target,
                 str(actor or ""), str(note or "")[:500]),
            )
            after = self._adoption_row(conn.execute(
                "SELECT * FROM project_workflow_adoptions WHERE project_id = ? AND workflow_id = ?", (project, flow),
            ).fetchone())
        return dict(after or {})

    def project_lifecycle(self, project_id: str) -> ProjectLifecycleRecord:
        clean = str(project_id or "").strip()
        if not clean:
            raise ValueError("project_id required for project lifecycle")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT project_id, status, updated_at, updated_by, reason, latest_backup_path, customer_id "
                "FROM project_lifecycle WHERE project_id = ?",
                (clean,),
            ).fetchone()
        return self._row_to_project_lifecycle(row, clean)

    def project_lifecycles(self, project_ids: list[str] | None = None) -> list[ProjectLifecycleRecord]:
        clauses = ""
        params: tuple[Any, ...] = ()
        clean_ids = [str(value or "").strip() for value in list(project_ids or []) if str(value or "").strip()]
        if clean_ids:
            placeholders = ",".join("?" for _ in clean_ids)
            clauses = f" WHERE project_id IN ({placeholders})"
            params = tuple(clean_ids)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT project_id, status, updated_at, updated_by, reason, latest_backup_path, customer_id "
                f"FROM project_lifecycle{clauses} ORDER BY project_id",
                params,
            ).fetchall()
        return [self._row_to_project_lifecycle(row, str(row["project_id"])) for row in rows]

    def record_project_lifecycle(
        self,
        project_id: str,
        status: str,
        *,
        actor: str,
        reason: str = "",
        latest_backup_path: str = "",
        expected_status: str | None = None,
        at: str | None = None,
        metadata: dict[str, Any] | None = None,
        event_action: str | None = None,
        allow_destroyed_restore: bool = False,
    ) -> ProjectLifecycleRecord:
        clean = str(project_id or "").strip()
        target = str(status or "").strip().lower()
        if not clean:
            raise ValueError("project_id required for project lifecycle")
        if target not in PROJECT_LIFECYCLE_STATUSES:
            raise ValueError(f"unsupported project lifecycle status: {target}")
        now = str(at or datetime.now().isoformat())
        with self._connect() as conn:
            previous = conn.execute(
                "SELECT project_id, status, updated_at, updated_by, reason, latest_backup_path, customer_id "
                "FROM project_lifecycle WHERE project_id = ?",
                (clean,),
            ).fetchone()
            current = self._row_to_project_lifecycle(previous, clean)
            if expected_status is not None and current.status != str(expected_status).strip().lower():
                raise OrderTransitionError(
                    f"project {clean!r} lifecycle changed from {expected_status!r} to {current.status!r}"
                )
            if current.status == "destroyed" and target != "destroyed" and not allow_destroyed_restore:
                raise OrderTransitionError(f"project {clean!r} is destroyed")
            backup = str(latest_backup_path or current.latest_backup_path or "")
            conn.execute(
                "INSERT INTO project_lifecycle(project_id, status, updated_at, updated_by, reason, latest_backup_path, customer_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET "
                "status=excluded.status, updated_at=excluded.updated_at, updated_by=excluded.updated_by, "
                "reason=excluded.reason, latest_backup_path=excluded.latest_backup_path",
                (clean, target, now, str(actor or ""), str(reason or ""), backup, current.customer_id),
            )
            conn.execute(
                "INSERT INTO project_lifecycle_events(project_id, at, action, from_status, to_status, actor, reason, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    clean,
                    now,
                    str(event_action or f"project_{target}"),
                    current.status,
                    target,
                    str(actor or ""),
                    str(reason or ""),
                    json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.execute(
                "INSERT INTO business_outbox(outbox_id, aggregate_type, aggregate_id, event_type, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"project-lifecycle:{uuid.uuid4().hex}",
                    "project",
                    clean,
                    str(event_action or f"project_{target}"),
                    json.dumps({"project_id": clean, "from_status": current.status, "to_status": target, "actor": str(actor or ""), "reason": str(reason or "")}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        return ProjectLifecycleRecord(clean, target, now, str(actor or ""), str(reason or ""), backup, current.customer_id)

    def project_lifecycle_events(self, project_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        clean = str(project_id or "").strip()
        if not clean:
            raise ValueError("project_id required for project lifecycle")
        bounded = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, project_id, at, action, from_status, to_status, actor, reason, metadata_json "
                "FROM project_lifecycle_events WHERE project_id = ? ORDER BY id DESC LIMIT ?",
                (clean, bounded),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            output.append({
                "id": int(row["id"]),
                "project_id": str(row["project_id"]),
                "at": str(row["at"]),
                "action": str(row["action"]),
                "from_status": str(row["from_status"] or ""),
                "to_status": str(row["to_status"] or ""),
                "actor": str(row["actor"] or ""),
                "reason": str(row["reason"] or ""),
                "metadata": metadata if isinstance(metadata, dict) else {},
            })
        return output

    def purge_project_operational_data(self, project_id: str) -> dict[str, int]:
        """Remove project-scoped business records while retaining lifecycle audit."""
        clean = str(project_id or "").strip()
        if not clean:
            raise ValueError("project_id required for project purge")
        with self._connect() as conn:
            # 项目采用业务流 belongs to the project too: it goes with it (and comes back with a restore, see below).
            adoptions_removed = int(conn.execute("DELETE FROM project_workflow_adoptions WHERE project_id = ?", (clean,)).rowcount or 0)
            conn.execute("DELETE FROM project_workflow_adoption_events WHERE project_id = ?", (clean,))
            order_rows = conn.execute("SELECT order_id, fields_json FROM work_orders").fetchall()
            order_ids: list[str] = []
            for row in order_rows:
                try:
                    fields = json.loads(row["fields_json"] or "{}")
                except (TypeError, ValueError):
                    fields = {}
                if clean == "default" and (not isinstance(fields, dict) or not fields.get("project_id")):
                    order_ids.append(str(row["order_id"]))
                elif isinstance(fields, dict) and str(fields.get("project_id", "")).strip() == clean:
                    order_ids.append(str(row["order_id"]))
            # 4.7-A08: the project's distribution authorizations go with it -- an approval left behind would still be
            # found by the gate if an order with the same id came back. Only where the table exists (version 1 has none).
            authorizations_removed = 0
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                            (DISTRIBUTION_AUTHORIZATION_TABLE,)).fetchone() is not None:
                also = f" OR order_id IN ({','.join('?' for _ in order_ids)})" if order_ids else ""
                authorizations_removed = int(conn.execute(
                    f"DELETE FROM distribution_authorizations WHERE project_id = ?{also}", (clean, *order_ids),
                ).rowcount or 0)
            # Business schema version 3: the project's business boundary and connection plans go with it, where the
            # tables exist.
            centre_removed: dict[str, int] = {}
            for table in (PROJECT_PROFILE_TABLE, PROJECT_CONNECTION_PLAN_TABLE):
                centre_removed[table] = 0
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None:
                    centre_removed[table] = int(conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (clean,)).rowcount or 0)
            # A registered console extension's own project tables go with it too, by its own rules, where they exist.
            for records in _extension_project_records():
                centre_removed.update(records.purge(conn, clean))
            if not order_ids:
                return {"work_orders": 0, "order_events": 0, "gate_cards": 0, "gate_events": 0, "outbox": 0, "workflow_adoptions": adoptions_removed,
                        "distribution_authorizations": authorizations_removed, **centre_removed}
            placeholders = ",".join("?" for _ in order_ids)
            params = tuple(order_ids)
            counts: dict[str, int] = {}
            checkpoint_rows = conn.execute(f"SELECT checkpoint_id FROM gate_cards WHERE order_id IN ({placeholders})", params).fetchall()
            checkpoint_ids = [str(row["checkpoint_id"]) for row in checkpoint_rows]
            for table, column in (("order_events", "order_id"), ("gate_cards", "order_id")):
                cursor = conn.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", params)
                counts[table] = int(cursor.rowcount or 0)
            if checkpoint_ids:
                cp_placeholders = ",".join("?" for _ in checkpoint_ids)
                cursor = conn.execute(f"DELETE FROM gate_events WHERE checkpoint_id IN ({cp_placeholders})", tuple(checkpoint_ids))
                counts["gate_events"] = int(cursor.rowcount or 0)
            else:
                counts["gate_events"] = 0
            outbox_ids = order_ids + checkpoint_ids
            outbox_placeholders = ",".join("?" for _ in outbox_ids)
            cursor = conn.execute(f"DELETE FROM business_outbox WHERE aggregate_id IN ({outbox_placeholders})", tuple(outbox_ids))
            counts["outbox"] = int(cursor.rowcount or 0)
            cursor = conn.execute(f"DELETE FROM work_orders WHERE order_id IN ({placeholders})", params)
            counts["work_orders"] = int(cursor.rowcount or 0)
            counts["workflow_adoptions"] = adoptions_removed
            counts["distribution_authorizations"] = authorizations_removed
            counts.update(centre_removed)
            return counts

    # ── a project's business records, out and back in (the module contract, B5-A) ──

    @staticmethod
    def _project_order_ids(conn: sqlite3.Connection, project_id: str) -> list[str]:
        """The orders that belong to a project, by the same rule the purge uses (an order with no project is default's)."""
        order_ids: list[str] = []
        for row in conn.execute("SELECT order_id, fields_json FROM work_orders ORDER BY created_at, order_id").fetchall():
            try:
                fields = json.loads(row["fields_json"] or "{}")
            except (TypeError, ValueError):
                fields = {}
            owner = str(fields.get("project_id", "") or "").strip() if isinstance(fields, dict) else ""
            if owner == project_id or (project_id == "default" and not owner):
                order_ids.append(str(row["order_id"]))
        return order_ids

    def export_project_records(self, project_id: str) -> dict[str, Any]:
        """Everything the project purge would remove, as plain rows, with each chain's verdict.  Read only."""
        clean = str(project_id or "").strip()
        if not clean:
            raise ValueError("project_id required for project records export")
        extension_records = _extension_project_records()
        tables: dict[str, list[dict[str, Any]]] = {name: [] for name in project_record_tables()}
        with self._connect() as conn:
            order_ids = self._project_order_ids(conn, clean)
            if order_ids:
                marks = ",".join("?" for _ in order_ids)
                params = tuple(order_ids)
                tables["work_orders"] = [dict(row) for row in conn.execute(f"SELECT * FROM work_orders WHERE order_id IN ({marks}) ORDER BY created_at, order_id", params)]
                tables["order_events"] = [dict(row) for row in conn.execute(f"SELECT * FROM order_events WHERE order_id IN ({marks}) ORDER BY id", params)]
                tables["gate_cards"] = [dict(row) for row in conn.execute(f"SELECT * FROM gate_cards WHERE order_id IN ({marks}) ORDER BY checkpoint_id", params)]
                checkpoint_ids = [str(row["checkpoint_id"]) for row in tables["gate_cards"]]
                if checkpoint_ids:
                    cp_marks = ",".join("?" for _ in checkpoint_ids)
                    tables["gate_events"] = [dict(row) for row in conn.execute(f"SELECT * FROM gate_events WHERE checkpoint_id IN ({cp_marks}) ORDER BY id", tuple(checkpoint_ids))]
                outbox_ids = order_ids + checkpoint_ids
                ob_marks = ",".join("?" for _ in outbox_ids)
                tables["business_outbox"] = [dict(row) for row in conn.execute(f"SELECT * FROM business_outbox WHERE aggregate_id IN ({ob_marks}) ORDER BY sequence", tuple(outbox_ids))]
            tables["project_workflow_adoptions"] = [dict(row) for row in conn.execute(
                "SELECT * FROM project_workflow_adoptions WHERE project_id = ? ORDER BY workflow_id", (clean,))]
            tables["project_workflow_adoption_events"] = [dict(row) for row in conn.execute(
                "SELECT * FROM project_workflow_adoption_events WHERE project_id = ? ORDER BY id", (clean,))]
            # 4.7-A08: carried for the record (the same rows the purge removes); a restore does not put them back.
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                            (DISTRIBUTION_AUTHORIZATION_TABLE,)).fetchone() is not None:
                also = f" OR order_id IN ({','.join('?' for _ in order_ids)})" if order_ids else ""
                tables["distribution_authorizations"] = [dict(row) for row in conn.execute(
                    f"SELECT * FROM distribution_authorizations WHERE project_id = ?{also} "
                    "ORDER BY created_at, authorization_id", (clean, *order_ids))]
            # Business schema version 3: the boundary (one row) and the connection plans (one per lane), where the
            # tables exist; a restore puts them back.
            for table in (PROJECT_PROFILE_TABLE, PROJECT_CONNECTION_PLAN_TABLE):
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None:
                    tables[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table} WHERE project_id = ? ORDER BY 1, 2", (clean,))]
            # A registered console extension's own project tables, where they exist, by its own rules.
            for records in extension_records:
                tables.update(records.export(conn, clean))
        return {
            "schema_version": 1,
            "project_id": clean,
            "tables": tables,
            "counts": {name: len(rows) for name, rows in tables.items()},
            "order_chains": {order_id: self.verify_order_event_chain(order_id) for order_id in order_ids},
            "gate_chains": {str(row["checkpoint_id"]): self.verify_gate_event_chain(str(row["checkpoint_id"])) for row in tables["gate_cards"]},
        }

    def export_project_budgets(self, project_id: str) -> dict[str, Any]:
        """This project's budgets, reservations, effect holds and snoozed blockers. Read only (数据与文件 P3).

        A restored project used to come back unfunded with nothing saying so: the allowance a run had spent, what
        it still had reserved, and which blockers the owner had snoozed all lived in the shared business store and
        no backup ever carried them.
        """
        clean = str(project_id or "").strip()
        if not clean:
            raise ValueError("project_id required for project budgets export")
        tables: dict[str, list[dict[str, Any]]] = {name: [] for name in PROJECT_BUDGET_TABLES}
        with self._connect() as conn:
            tables["run_budgets"] = [dict(row) for row in conn.execute(
                "SELECT * FROM run_budgets WHERE project_id = ? ORDER BY created_at, budget_id", (clean,))]
            tables["blocker_snoozes"] = [dict(row) for row in conn.execute(
                "SELECT * FROM blocker_snoozes WHERE project_id = ? ORDER BY checkpoint_id", (clean,))]
            budget_ids = [str(row["budget_id"]) for row in tables["run_budgets"]]
            if budget_ids:
                marks = ",".join("?" for _ in budget_ids)
                params = tuple(budget_ids)
                tables["budget_reservations"] = [dict(row) for row in conn.execute(
                    f"SELECT * FROM budget_reservations WHERE budget_id IN ({marks}) ORDER BY at, reservation_id", params)]
                tables["budget_effect_holds"] = [dict(row) for row in conn.execute(
                    f"SELECT * FROM budget_effect_holds WHERE budget_id IN ({marks}) ORDER BY budget_id, effect_id", params)]
        return {"schema_version": 1, "project_id": clean, "tables": tables,
                "counts": {name: len(rows) for name, rows in tables.items()}}

    def export_project_history(self, project_id: str) -> dict[str, Any]:
        """This project's own history: its lifecycle, the events on it, and the browser observations audited for it.

        Found by :meth:`project_scoped_tables` rather than by memory (数据与文件 P3): these three tables name a
        project and no backup carried them, so a restored project came back with no record of when it was made,
        when it was backed up, or what its browser had been asked to look at.
        """
        clean = str(project_id or "").strip()
        if not clean:
            raise ValueError("project_id required for project history export")
        names = ("projects", "project_lifecycle", "project_lifecycle_events", "observation_audit")
        orders = {"projects": "project_id", "project_lifecycle": "project_id",
                  "project_lifecycle_events": "id", "observation_audit": "rowid"}
        tables: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
        with self._connect() as conn:
            existing = {str(row["name"]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            for name in names:
                if name not in existing:
                    continue
                tables[name] = [dict(row) for row in conn.execute(
                    f"SELECT * FROM {name} WHERE project_id = ? ORDER BY {orders[name]}", (clean,))]
        return {"schema_version": 1, "project_id": clean, "tables": tables,
                "counts": {name: len(rows) for name, rows in tables.items()}}

    def project_scoped_tables(self) -> tuple[str, ...]:
        """Every table in this store that carries a ``project_id`` column.

        Used by a test, not by the product: a table that names a project but appears in neither the record export
        nor the budget export nor the 「故意不带」 list is a table a backup has quietly forgotten. Finding that out
        from a red test beats finding it out from a restore.
        """
        found: list[str] = []
        with self._connect() as conn:
            names = [str(row["name"]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
            for name in names:
                columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({name})")}
                if "project_id" in columns:
                    found.append(name)
        return tuple(found)

    def import_project_records(self, project_id: str, records: dict[str, Any]) -> dict[str, int]:
        """Put an exported project's records back: all of them in one transaction, or none.

        Refused when the export is for another project or any of its orders, gate cards or flow adoptions already
        exists here -- a restore never overwrites or merges.  Event rows go back in their original order without their old row
        ids (the chain hashes do not cover the id, so every chain verifies as it did).  The outbox is *not*
        restored: those rows are messages that were waiting to be sent, and bringing a project back must not make
        the console send anything.  Nor are the distribution authorizations (4.7-A08), for the same reason: an
        approval that came back with a restore would let a batch order send again without the owner approving it
        again.  Their trail (drafted / approved / revoked) is in ``order_events`` and does come back.  A registered
        console extension's own project tables (``extensions.ProjectRecords``) come back by its own rules, checked in
        the same places and put in within the same transaction.
        """
        clean = str(project_id or "").strip()
        if not clean:
            raise ValueError("project_id required for project records import")
        if not isinstance(records, dict) or int(records.get("schema_version", 0) or 0) != 1:
            raise OrderTransitionError("unsupported project records export")
        if str(records.get("project_id", "") or "").strip() != clean:
            raise OrderTransitionError("project records belong to another project")
        tables = records.get("tables")
        if not isinstance(tables, dict):
            raise OrderTransitionError("project records carry no tables")
        extension_records = _extension_project_records()
        rows_of = {name: [row for row in (tables.get(name) or []) if isinstance(row, dict)] for name in project_record_tables()}
        order_ids = [str(row.get("order_id", "") or "") for row in rows_of["work_orders"]]
        checkpoint_ids = [str(row.get("checkpoint_id", "") or "") for row in rows_of["gate_cards"]]
        if any(not item for item in order_ids + checkpoint_ids):
            raise OrderTransitionError("project records carry a row without its id")
        for row in rows_of["work_orders"]:
            try:
                owner = str((json.loads(row.get("fields_json") or "{}") or {}).get("project_id", "") or "").strip()
            except (TypeError, ValueError):
                owner = ""
            if not (owner == clean or (clean == "default" and not owner)):
                raise OrderTransitionError("an order in the export belongs to another project")
        if any(str(row.get("order_id", "") or "") not in order_ids for row in rows_of["order_events"] + rows_of["gate_cards"]):
            raise OrderTransitionError("the export carries events or gate cards of an order it does not carry")
        if any(str(row.get("checkpoint_id", "") or "") not in checkpoint_ids for row in rows_of["gate_events"]):
            raise OrderTransitionError("the export carries gate events of a gate card it does not carry")
        adopted = [str(row.get("workflow_id", "") or "") for row in rows_of["project_workflow_adoptions"]]
        if any(str(row.get("project_id", "") or "") != clean
               for row in rows_of["project_workflow_adoptions"] + rows_of["project_workflow_adoption_events"]) or not all(adopted):
            raise OrderTransitionError("an adoption in the export belongs to another project")
        if any(str(row.get("project_id", "") or "") != clean for row in rows_of[PROJECT_PROFILE_TABLE] + rows_of[PROJECT_CONNECTION_PLAN_TABLE]):
            raise OrderTransitionError("a project profile or connection plan in the export belongs to another project")
        if any(not str(row.get("lane", "") or "") for row in rows_of[PROJECT_CONNECTION_PLAN_TABLE]):
            raise OrderTransitionError("the export carries a connection plan without its lane")
        for extension in extension_records:
            extension.check(clean, rows_of)

        counts = {name: 0 for name in rows_of}
        with self._connect() as conn:
            for table in (PROJECT_PROFILE_TABLE, PROJECT_CONNECTION_PLAN_TABLE):
                if not rows_of[table]:
                    continue
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is None:
                    raise OrderTransitionError(f"{table} is not in this database yet (business schema version 3); run `ampuly db-version --apply` first")
                if conn.execute(f"SELECT 1 FROM {table} WHERE project_id = ? LIMIT 1", (clean,)).fetchone():
                    raise OrderTransitionError(f"{table} already holds a row of this project; a restore never overwrites")
            # A registered console extension's own tables: never over a row already here, never onto a database that
            # has not got them yet; it may change the rows it puts in (its own rules).
            for extension in extension_records:
                extension.prepare_import(conn, clean, rows_of)
            for table, column, ids in (("work_orders", "order_id", order_ids), ("gate_cards", "checkpoint_id", checkpoint_ids)):
                if ids and conn.execute(f"SELECT 1 FROM {table} WHERE {column} IN ({','.join('?' for _ in ids)}) LIMIT 1", tuple(ids)).fetchone():
                    raise OrderTransitionError(f"{table} already holds a row of this export; a restore never overwrites")
            if adopted and conn.execute(
                f"SELECT 1 FROM project_workflow_adoptions WHERE project_id = ? AND workflow_id IN ({','.join('?' for _ in adopted)}) LIMIT 1",
                (clean, *adopted),
            ).fetchone():
                raise OrderTransitionError("project_workflow_adoptions already holds a row of this export; a restore never overwrites")
            for table, skip in (("work_orders", ()), ("order_events", ("id",)), ("gate_cards", ()), ("gate_events", ("id",)),
                                ("project_workflow_adoptions", ()), ("project_workflow_adoption_events", ("id",)),
                                (PROJECT_PROFILE_TABLE, ()), (PROJECT_CONNECTION_PLAN_TABLE, ()),
                                *(insert for extension in extension_records for insert in extension.inserts)):
                if not rows_of[table]:
                    continue
                known = [str(info["name"]) for info in conn.execute(f"PRAGMA table_info({table})").fetchall()]
                for row in rows_of[table]:
                    columns = [name for name in known if name in row and name not in skip]
                    conn.execute(
                        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                        tuple(row[name] for name in columns),
                    )
                    counts[table] += 1
        counts["business_outbox"] = 0
        counts["distribution_authorizations"] = 0
        # A chain that verified when it left must verify now that it is back.  If one does not, take out exactly what
        # was just put in (nothing else of the project is touched) and refuse: a tampered export is not a restore.
        broken = [
            order_id for order_id in order_ids
            if (records.get("order_chains") or {}).get(order_id) and not self.verify_order_event_chain(order_id)
        ]
        if broken:
            self.discard_imported_project_records(records)
            raise OrderTransitionError("an order's event chain no longer verifies; nothing was restored: " + ", ".join(sorted(broken)))
        return counts

    def discard_imported_project_records(self, records: dict[str, Any]) -> None:
        """Take back out exactly the rows an import of ``records`` put in -- nothing else of the project.

        For the two moments a restore must be all or nothing: a chain that no longer verifies, and a restore whose
        later step (the project's own record) failed after the rows were already in.
        """
        tables = records.get("tables") if isinstance(records, dict) else None
        if not isinstance(tables, dict):
            return
        order_ids = [str(row.get("order_id", "") or "") for row in (tables.get("work_orders") or []) if isinstance(row, dict)]
        checkpoint_ids = [str(row.get("checkpoint_id", "") or "") for row in (tables.get("gate_cards") or []) if isinstance(row, dict)]
        order_ids = [item for item in order_ids if item]
        checkpoint_ids = [item for item in checkpoint_ids if item]
        # The adoptions an import put in (it refused to run if any of these pairs was already here, so these rows are its).
        project = str(records.get("project_id", "") or "").strip()
        adopted = [str(row.get("workflow_id", "") or "") for row in (tables.get("project_workflow_adoptions") or []) if isinstance(row, dict)]
        adopted = [item for item in adopted if item]
        if project and adopted:
            marks = ",".join("?" for _ in adopted)
            with self._connect() as conn:
                conn.execute(f"DELETE FROM project_workflow_adoptions WHERE project_id = ? AND workflow_id IN ({marks})", (project, *adopted))
                conn.execute(f"DELETE FROM project_workflow_adoption_events WHERE project_id = ? AND workflow_id IN ({marks})", (project, *adopted))
        # The boundary and the plans an import put in (it refused to run if the project already had any here).
        if project:
            with self._connect() as conn:
                for table in (PROJECT_PROFILE_TABLE, PROJECT_CONNECTION_PLAN_TABLE):
                    if (tables.get(table) and conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                                                           (table,)).fetchone() is not None):
                        conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (project,))
                # A registered console extension's own rows likewise (the import refused if the project had any here).
                for extension in _extension_project_records():
                    extension.discard(conn, project, tables)
        if not order_ids:
            return
        with self._connect() as conn:
            if checkpoint_ids:
                conn.execute(f"DELETE FROM gate_events WHERE checkpoint_id IN ({','.join('?' for _ in checkpoint_ids)})", tuple(checkpoint_ids))
            marks = ",".join("?" for _ in order_ids)
            for table in ("gate_cards", "order_events", "work_orders"):
                conn.execute(f"DELETE FROM {table} WHERE order_id IN ({marks})", tuple(order_ids))

    # ── work orders ──
    @staticmethod
    def _row_to_order(row: sqlite3.Row) -> WorkOrder:
        try:
            fields = json.loads(row["fields_json"] or "{}")
        except ValueError:
            fields = {}
        return WorkOrder(
            order_id=row["order_id"],
            workflow_id=row["workflow_id"],
            status=row["status"],
            org_id=row["org_id"],
            customer_id=str(row["customer_id"] or ""),
            environment_id=str(row["environment_id"] or ""),
            customer_name=row["customer_name"],
            brand_name=row["brand_name"],
            fields=fields if isinstance(fields, dict) else {},
            source=row["source"],
            source_task_id=row["source_task_id"],
            invocation_key=str(row["invocation_key"] or "") if "invocation_key" in row.keys() else "",
            reviewer_employee_id=row["reviewer_employee_id"],
            review_note=row["review_note"],
            launched_task_id=row["launched_task_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_outbox(row: sqlite3.Row) -> BusinessOutboxRecord:
        if row["event_type"] == "workflow_launch_requested":
            # A broken launch command is not a legacy unpinned command. Never
            # discard its unknown original meaning and start with current data.
            def unique(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("duplicate launch key")
                    value[key] = item
                return value
            def finite(_value):
                raise ValueError("non-finite launch value")
            def finite_float(value):
                parsed = float(value)
                if not math.isfinite(parsed):
                    raise ValueError("non-finite launch number")
                return parsed
            try:
                payload = json.loads(row["payload_json"], object_pairs_hook=unique,
                                     parse_constant=finite, parse_float=finite_float)
                if (type(payload) is not dict or type(payload.get("schema_version")) is not int
                        or payload["schema_version"] != 1 or payload.get("command") != "workflow.launch"
                        or payload.get("order_id") != row["aggregate_id"]
                        or any(type(payload.get(key)) is not str or not payload[key].strip()
                               for key in ("order_id", "project_id", "workflow_id"))):
                    raise ValueError("incomplete launch identity")
            except (ValueError, TypeError, RecursionError):
                raise OrderTransitionError("launch command payload invalid; original record retained") from None
        else:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except ValueError:
                payload = {}
        return BusinessOutboxRecord(
            sequence=int(row["sequence"]),
            outbox_id=str(row["outbox_id"]),
            aggregate_type=str(row["aggregate_type"]),
            aggregate_id=str(row["aggregate_id"]),
            event_type=str(row["event_type"]),
            payload=payload if isinstance(payload, dict) else {},
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            last_error=str(row["last_error"]),
            created_at=str(row["created_at"]),
            published_at=str(row["published_at"]),
        )

    def create_order(
        self,
        *,
        workflow_id: str,
        fields: dict[str, Any],
        project_id: str = "default",
        org_id: str = "",
        source: str = "console",
        source_task_id: str = "",
        invocation_key: str = "",
        status: str = "pending_review",
        actor: str = "",
        acceptance_criteria: list[str] | None = None,
        workflow_version: str = "",
    ) -> WorkOrder:
        if status not in ORDER_STATUSES:
            raise OrderTransitionError(f"unknown order status {status!r}")
        now = datetime.now().isoformat()
        clean_project_id = str(project_id or "default").strip() or "default"
        requested_project_id = str(fields.get("project_id", "") or "").strip()
        if requested_project_id and requested_project_id != clean_project_id:
            raise OrderTransitionError("project scope conflicts with intake fields")
        # A delivery project belongs to one customer. Accepting an order for a
        # different one would put two customers' work -- and their files,
        # memory and workspace -- in the same space, which is exactly what the
        # binding exists to prevent.
        bound_customer = self.project_customer(clean_project_id)
        order_customer = str(fields.get("customer_id", "") or "").strip()
        if bound_customer and order_customer and order_customer != bound_customer:
            raise OrderTransitionError(
                f"project {clean_project_id!r} belongs to customer {bound_customer!r}; "
                f"this order names {order_customer!r}"
            )
        normalized_fields: dict[str, Any] = {"project_id": clean_project_id}
        for key, value in dict(fields or {}).items():
            # Intake fields are historically strings, but the FDE control
            # plane intentionally accepts JSON objects (SLA/ROI/UAT). Keep
            # JSON-shaped values structured while retaining the old string
            # normalization for scalar form inputs.
            normalized_fields[str(key)] = value if isinstance(value, (dict, list)) else str(value)
        order = WorkOrder(
            order_id=f"wo-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}",
            workflow_id=str(workflow_id or "").strip(),
            status=status,
            org_id=str(org_id or "").strip(),
            customer_id=str(fields.get("customer_id", "") or "").strip(),
            environment_id=str(fields.get("environment_id", "") or "").strip(),
            customer_name=str(fields.get("customer_name", "") or "").strip(),
            brand_name=str(fields.get("brand_name", "") or "").strip(),
            fields=normalized_fields,
            source=str(source or "").strip(),
            source_task_id=str(source_task_id or "").strip(),
            invocation_key=str(invocation_key or "").strip(),
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            # Six random hex characters per day: a few hundred orders on one day can draw the same id (birthday odds),
            # which used to fail the order with a raw UNIQUE error. Draw again until the id is free.
            for _ in range(8):
                if conn.execute("SELECT 1 FROM work_orders WHERE order_id = ?", (order.order_id,)).fetchone() is None:
                    break
                order.order_id = f"wo-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
            try:
                conn.execute(
                    """INSERT INTO work_orders(order_id, workflow_id, org_id, customer_id, environment_id, status,
                       customer_name, brand_name, fields_json, source, source_task_id, invocation_key, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        order.order_id, order.workflow_id, order.org_id, order.customer_id, order.environment_id,
                        order.status, order.customer_name, order.brand_name, json.dumps(order.fields, ensure_ascii=False),
                        order.source, order.source_task_id, order.invocation_key, now, now,
                    ),
                )
            except sqlite3.IntegrityError:
                # The only recoverable collision is the explicit invocation
                # key.  It is intentionally checked in the same transaction as
                # the insert so concurrent processes converge on one order.
                if not order.invocation_key:
                    raise
                row = conn.execute(
                    "SELECT * FROM work_orders WHERE invocation_key = ?",
                    (order.invocation_key,),
                ).fetchone()
                if row is None:
                    raise
                existing = self._row_to_order(row)
                same_invocation = (
                    existing.workflow_id == order.workflow_id
                    and existing.project_id == order.project_id
                    and existing.source == order.source
                    and existing.source_task_id == order.source_task_id
                    and existing.fields == order.fields
                )
                if not same_invocation:
                    raise OrderTransitionError(
                        "invocation key is already bound to a different child order"
                    )
                return existing
            self._append_event(
                conn,
                table="order_events",
                aggregate_column="order_id",
                aggregate_id=order.order_id,
                at=now,
                kind="created",
                actor=actor,
                note={"status": status, "source": source},
                correlation_id=order.order_id,
            )
            criteria = self._normalize_text_list(acceptance_criteria or [])
            if criteria:
                criteria_hash = self._digest(
                    {"criteria": criteria, "workflow_version": workflow_version}
                )
                self._append_event(
                    conn,
                    table="order_events",
                    aggregate_column="order_id",
                    aggregate_id=order.order_id,
                    at=now,
                    kind="acceptance.criteria_defined",
                    actor=actor,
                    note={
                        "criteria": criteria,
                        "criteria_hash": criteria_hash,
                        "workflow_version": str(workflow_version or "").strip(),
                    },
                    correlation_id=order.order_id,
                )
        return order

    def get_order(self, order_id: str, *, project_id: str | None = None) -> WorkOrder | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM work_orders WHERE order_id = ?", (str(order_id or ""),)).fetchone()
        if row is None:
            return None
        order = self._row_to_order(row)
        if project_id is not None:
            clean_project_id = str(project_id or "default").strip() or "default"
            if order.project_id != clean_project_id:
                return None
        return order

    def order_for_launched_task(self, task_id: str, *, project_id: str | None = None) -> WorkOrder | None:
        """The order whose production run is this task, or None. Only the run's own task counts: a work item
        under it is not the order."""
        clean = str(task_id or "").strip()
        if not clean:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM work_orders WHERE launched_task_id = ? ORDER BY created_at DESC LIMIT 1", (clean,)
            ).fetchone()
        if row is None:
            return None
        order = self._row_to_order(row)
        if project_id is not None and order.project_id != (str(project_id or "default").strip() or "default"):
            return None
        return order

    # Owner ruling : a parent order may fan out to at most eight
    # children, of which at most two are not yet ended. Bounded fan-out is a
    # safety limit, not a grant; the errors are stable codes for the caller.
    MAX_CHILD_ORDERS = 8
    MAX_ACTIVE_CHILD_ORDERS = 2

    def assert_child_order_capacity(self, project_id: str, parent_order_id: str) -> None:
        """Refuse a ninth child, or a third live child, before it is written."""
        project = str(project_id or "").strip()
        parent = str(parent_order_id or "").strip()
        if not project or not parent:
            raise OrderTransitionError("workflow_child_parent_invalid")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM work_orders "
                "WHERE json_valid(fields_json) AND json_extract(fields_json, '$.project_id') = ? "
                "AND json_extract(fields_json, '$._workflow_parent.parent_order_id') = ? "
                "GROUP BY status",
                (project, parent),
            ).fetchall()
        total = sum(int(row[1]) for row in rows)
        active = sum(int(row[1]) for row in rows if str(row[0]) not in ENDED_ORDER_STATUSES)
        if total >= self.MAX_CHILD_ORDERS:
            raise OrderTransitionError("workflow_child_limit")
        if active >= self.MAX_ACTIVE_CHILD_ORDERS:
            raise OrderTransitionError("workflow_child_concurrency")

    def order_by_invocation_key(
        self,
        invocation_key: str,
        *,
        project_id: str | None = None,
    ) -> WorkOrder | None:
        """Return the one child order created for a durable node invocation."""
        clean_key = str(invocation_key or "").strip()
        if not clean_key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM work_orders WHERE invocation_key = ?",
                (clean_key,),
            ).fetchone()
        if row is None:
            return None
        order = self._row_to_order(row)
        if project_id is not None:
            clean_project_id = str(project_id or "default").strip() or "default"
            if order.project_id != clean_project_id:
                return None
        return order

    def update_fde_control_plane(
        self,
        order_id: str,
        patch: dict[str, Any],
        *,
        actor: str,
        project_id: str = "default",
    ) -> WorkOrder:
        """Apply an owner-authored FDE plan patch in the order truth store.

        The control plane remains a projection over ``work_orders.fields``;
        this command updates only explicitly editable planning fields and
        appends an auditable event in the same transaction. Derived identity,
        phase, trace, acceptance and release status cannot be client-written.
        """
        clean_order = str(order_id or "").strip()
        clean_actor = str(actor or "").strip()
        if not clean_order:
            raise OrderTransitionError("order_id is required")
        if not clean_actor:
            raise OrderTransitionError("actor is required")
        if not isinstance(patch, dict) or not patch:
            raise OrderTransitionError("fde_control_plane patch must be a non-empty object")
        editable_scalars = {"as_is", "to_be"}
        editable_maps = {"uat", "sla", "release", "rollback", "roi"}
        forbidden = {"order_id", "customer_id", "environment_id", "phase", "trace"}
        if forbidden.intersection(str(key) for key in patch):
            raise OrderTransitionError("derived FDE control-plane fields are read-only")
        unknown = set(str(key) for key in patch) - editable_scalars - editable_maps
        if unknown:
            raise OrderTransitionError("unsupported FDE control-plane fields: " + ", ".join(sorted(unknown)))

        def _clean_text(value: Any, *, limit: int = 4000) -> str:
            return str(value or "").strip()[:limit]

        normalized: dict[str, Any] = {}
        for key, value in patch.items():
            name = str(key)
            if name in editable_scalars:
                normalized[name] = _clean_text(value)
            else:
                if not isinstance(value, dict):
                    raise OrderTransitionError(f"fde_control_plane.{name} must be an object")
                normalized[name] = {str(k): v for k, v in value.items()}

        now = datetime.now().isoformat()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM work_orders WHERE order_id = ?", (clean_order,)).fetchone()
            if row is None:
                raise OrderTransitionError(f"order {clean_order!r} not found")
            order = self._row_to_order(row)
            clean_project_id = str(project_id or "default").strip() or "default"
            if order.project_id != clean_project_id:
                raise OrderTransitionError("project scope denied")
            fields = dict(order.fields)
            current = fields.get("fde_control_plane", {})
            context = dict(current) if isinstance(current, dict) else {}
            for key, value in normalized.items():
                if key in editable_maps:
                    merged = dict(context.get(key, {}) or {}) if isinstance(context.get(key), dict) else {}
                    merged.update(value)
                    context[key] = merged
                else:
                    context[key] = value
            fields["fde_control_plane"] = context
            conn.execute(
                "UPDATE work_orders SET fields_json = ?, updated_at = ? WHERE order_id = ?",
                (json.dumps(fields, ensure_ascii=False), now, clean_order),
            )
            self._append_event(
                conn,
                table="order_events",
                aggregate_column="order_id",
                aggregate_id=clean_order,
                at=now,
                kind="fde_control_plane.updated",
                actor=clean_actor,
                note={"project_id": str(project_id or "default").strip() or "default", "patch": normalized},
                correlation_id=clean_order,
            )
        return self.get_order(clean_order, project_id=clean_project_id) or order

    # ── who handles an order, by when, for how much ──
    #
    # The console could say whose turn a *step* was on (a gate's signer, the
    # intake reviewer) but not who looks after the order, when it is due or
    # what was quoted: the engine recorded none of it, so the pages that
    # needed it said nothing rather than invent it.  These are attributes of
    # the order, so they live with the order (``fields["_handling"]``) and
    # every change is one event on the order's signed chain -- no new table,
    # no migration, and rolling this back leaves every order readable.

    def update_order_handling(
        self,
        order_id: str,
        patch: dict[str, Any],
        *,
        actor: str,
        project_id: str = "default",
    ) -> WorkOrder:
        """Set an order's assignee, due date, amounts or handover note.

        Only the keys in ``HANDLING_FIELDS`` are accepted; ``""`` / ``None``
        clears a value.  A patch that changes nothing is refused rather than
        recorded: the trail should not fill with events that say nothing.
        The assignee is who looks after the order -- it grants nothing; who
        may review or accept is decided where it always was.
        """
        clean_order = str(order_id or "").strip()
        clean_actor = str(actor or "").strip()
        if not clean_order:
            raise OrderTransitionError("order_id is required")
        if not clean_actor:
            raise OrderTransitionError("actor is required")
        if not isinstance(patch, dict) or not patch:
            raise OrderTransitionError("handling patch must be a non-empty object")
        unknown = set(str(key) for key in patch) - set(HANDLING_FIELDS) - {"note"}
        if unknown:
            raise OrderTransitionError("unsupported handling fields: " + ", ".join(sorted(unknown)))
        normalized = {str(key): normalize_handling_value(str(key), value) for key, value in patch.items() if str(key) != "note"}
        note = str(patch.get("note", "") or "").strip()[:400]
        if not normalized:
            raise OrderTransitionError("handling patch names no field to change")

        now = datetime.now().isoformat()
        clean_project_id = str(project_id or "default").strip() or "default"
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM work_orders WHERE order_id = ?", (clean_order,)).fetchone()
            if row is None:
                raise OrderTransitionError(f"order {clean_order!r} not found")
            order = self._row_to_order(row)
            if order.project_id != clean_project_id:
                raise OrderTransitionError("project scope denied")
            fields = dict(order.fields)
            current = fields.get(HANDLING_KEY)
            handling = dict(current) if isinstance(current, dict) else {}
            before = {key: handling.get(key) for key in normalized}
            changed = {key: value for key, value in normalized.items() if before.get(key) != value}
            if not changed:
                raise OrderTransitionError("handling unchanged")
            for key, value in changed.items():
                if value is None:
                    handling.pop(key, None)
                else:
                    handling[key] = value
            handling["updated_at"] = now
            handling["updated_by"] = clean_actor
            fields[HANDLING_KEY] = handling
            conn.execute(
                "UPDATE work_orders SET fields_json = ?, updated_at = ? WHERE order_id = ?",
                (json.dumps(fields, ensure_ascii=False), now, clean_order),
            )
            self._append_event(
                conn,
                table="order_events",
                aggregate_column="order_id",
                aggregate_id=clean_order,
                at=now,
                kind="handling.updated",
                actor=clean_actor,
                note={
                    "project_id": clean_project_id,
                    "before": {key: before.get(key) for key in changed},
                    "after": dict(changed),
                    "note": note,
                },
                correlation_id=clean_order,
            )
        return self.get_order(clean_order, project_id=clean_project_id) or order

    # ── solution packages and where they are deployed ──
    #
    # "Which customer, on which environment, is running which version" had no
    # answer before this: the workflow package knew its own version, and
    # nobody knew who was running it. A deployment row is that answer, and its
    # event log is how it got there.

    def save_solution_package(self, payload: dict[str, Any], *, actor: str) -> dict[str, Any]:
        """Store a verified package. A version is written once and never edited.

        Overwriting v1.0.0 in place would make every deployment that names it
        ambiguous -- the same version would mean two different things
        depending on when you looked.
        """
        from ampuly.plugins.office_ui.solution_package import SolutionPackageError, verify_payload

        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        try:
            verify_payload(payload)
        except SolutionPackageError as exc:
            raise OrderTransitionError(str(exc)) from exc
        solution_id = str(payload["solution_id"])
        version = str(payload["version"])
        digest = str(payload["digest"])
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT digest FROM solution_packages WHERE solution_id = ? AND version = ?",
                (solution_id, version),
            ).fetchone()
            if existing is not None:
                if str(existing["digest"]) != digest:
                    raise OrderTransitionError(
                        f"{solution_id} v{version} already exists with different content; "
                        "publish a new version instead of changing this one"
                    )
                return self.solution_package(solution_id, version) or {}
            conn.execute(
                "INSERT INTO solution_packages(solution_id, version, digest, payload_json, created_at, created_by) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (
                    solution_id, version, digest,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    datetime.now().isoformat(), str(actor),
                ),
            )
        return self.solution_package(solution_id, version) or {}

    def solution_package(self, solution_id: str, version: str) -> dict[str, Any] | None:
        """Read one back, checking on the way out that it is still itself."""
        from ampuly.plugins.office_ui.solution_package import SolutionPackageError, verify_payload

        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM solution_packages WHERE solution_id = ? AND version = ?",
                (str(solution_id or ""), str(version or "")),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
            verify_payload(payload)
        except (ValueError, SolutionPackageError) as exc:
            raise OrderTransitionError(
                f"stored package {solution_id} v{version} is unreadable or altered: {exc}"
            ) from exc
        return payload

    def list_solution_packages(self, *, solution_id: str = "") -> list[dict[str, Any]]:
        clause = " WHERE solution_id = ?" if solution_id else ""
        params = (str(solution_id),) if solution_id else ()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT solution_id, version, digest, created_at, created_by FROM solution_packages{clause} "
                "ORDER BY solution_id, created_at DESC, rowid DESC",
                params,
            ).fetchall()
        return [
            {
                "solution_id": str(row["solution_id"]),
                "version": str(row["version"]),
                "digest": str(row["digest"]),
                "created_at": str(row["created_at"]),
                "created_by": str(row["created_by"] or ""),
            }
            for row in rows
        ]

    def deploy_solution(
        self,
        *,
        customer_id: str,
        environment_id: str = "",
        solution_id: str,
        version: str,
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Record that this customer's environment now runs this version.

        The package has to exist here first: deploying a version nobody stored
        would leave a manifest pointing at nothing, which is worse than an
        empty one because it reads as an answer.
        """
        clean_customer = str(customer_id or "").strip()
        if not clean_customer:
            raise OrderTransitionError("a deployment belongs to a customer")
        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        if self.solution_package(solution_id, version) is None:
            raise OrderTransitionError(f"solution {solution_id!r} v{version} is not stored here")
        now = datetime.now().isoformat()
        with self._connect() as conn:
            previous = conn.execute(
                "SELECT version FROM deployments WHERE customer_id = ? AND environment_id = ? AND solution_id = ?",
                (clean_customer, str(environment_id or ""), str(solution_id)),
            ).fetchone()
            from_version = str(previous["version"]) if previous is not None else ""
            conn.execute(
                "INSERT INTO deployments(customer_id, environment_id, solution_id, version, state, updated_at, updated_by, note) "
                "VALUES(?, ?, ?, ?, 'active', ?, ?, ?) "
                "ON CONFLICT(customer_id, environment_id, solution_id) DO UPDATE SET "
                "version=excluded.version, state='active', updated_at=excluded.updated_at, "
                "updated_by=excluded.updated_by, note=excluded.note",
                (clean_customer, str(environment_id or ""), str(solution_id), str(version), now, str(actor), str(note or "")),
            )
            conn.execute(
                "INSERT INTO deployment_events(at, customer_id, environment_id, solution_id, from_version, to_version, action, actor, note) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now, clean_customer, str(environment_id or ""), str(solution_id), from_version, str(version),
                    "deployed" if not from_version else "upgraded", str(actor), str(note or ""),
                ),
            )
        return self.deployment(customer_id=clean_customer, environment_id=environment_id, solution_id=solution_id) or {}

    def retire_deployment(
        self,
        *,
        customer_id: str,
        environment_id: str = "",
        solution_id: str,
        actor: str,
        note: str = "",
    ) -> dict[str, Any] | None:
        """Stop running it here, without erasing that it ran."""
        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        now = datetime.now().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version FROM deployments WHERE customer_id = ? AND environment_id = ? AND solution_id = ?",
                (str(customer_id or ""), str(environment_id or ""), str(solution_id or "")),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE deployments SET state = 'retired', updated_at = ?, updated_by = ?, note = ? "
                "WHERE customer_id = ? AND environment_id = ? AND solution_id = ?",
                (now, str(actor), str(note or ""), str(customer_id), str(environment_id or ""), str(solution_id)),
            )
            conn.execute(
                "INSERT INTO deployment_events(at, customer_id, environment_id, solution_id, from_version, to_version, action, actor, note) "
                "VALUES(?, ?, ?, ?, ?, '', 'retired', ?, ?)",
                (now, str(customer_id), str(environment_id or ""), str(solution_id), str(row["version"]), str(actor), str(note or "")),
            )
        return self.deployment(customer_id=customer_id, environment_id=environment_id, solution_id=solution_id)

    def deployment(self, *, customer_id: str, environment_id: str = "", solution_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM deployments WHERE customer_id = ? AND environment_id = ? AND solution_id = ?",
                (str(customer_id or ""), str(environment_id or ""), str(solution_id or "")),
            ).fetchone()
        return self._deployment_row(row) if row is not None else None

    def list_deployments(self, *, customer_ids: tuple[str, ...] = (), include_retired: bool = False) -> list[dict[str, Any]]:
        """The manifest: who runs what, where, at which version."""
        clause = "" if include_retired else " WHERE state = 'active'"
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM deployments{clause} ORDER BY customer_id, environment_id, solution_id"
            ).fetchall()
        deployments = [self._deployment_row(row) for row in rows]
        if customer_ids:
            allowed = {str(c).strip() for c in customer_ids if str(c).strip()}
            deployments = [d for d in deployments if d["customer_id"] in allowed]
        return deployments

    def deployment_events(self, *, customer_id: str = "", solution_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        clauses, params = [], []
        if customer_id:
            clauses.append("customer_id = ?")
            params.append(str(customer_id))
        if solution_id:
            clauses.append("solution_id = ?")
            params.append(str(solution_id))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM deployment_events{where} ORDER BY id DESC LIMIT ?", tuple(params)
            ).fetchall()
        return [
            {
                "at": str(row["at"]),
                "customer_id": str(row["customer_id"]),
                "environment_id": str(row["environment_id"] or ""),
                "solution_id": str(row["solution_id"]),
                "from_version": str(row["from_version"] or ""),
                "to_version": str(row["to_version"] or ""),
                "action": str(row["action"]),
                "actor": str(row["actor"] or ""),
                "note": str(row["note"] or ""),
            }
            for row in rows
        ]

    @staticmethod
    def _deployment_row(row: Any) -> dict[str, Any]:
        return {
            "customer_id": str(row["customer_id"]),
            "environment_id": str(row["environment_id"] or ""),
            "solution_id": str(row["solution_id"]),
            "version": str(row["version"]),
            "state": str(row["state"] or "active"),
            "updated_at": str(row["updated_at"] or ""),
            "updated_by": str(row["updated_by"] or ""),
            "note": str(row["note"] or ""),
        }

    # ── who wrote this, and which order it is about ──
    #
    # The same person is one id in Feishu and another in DingTalk. Without the
    # join, a reply on one channel cannot reach the order discussed on
    # another, and "has the customer answered" is answered by remembering.

    def add_contact(
        self, *, contact_id: str, customer_id: str, display_name: str = "", actor: str,
    ) -> dict[str, Any]:
        clean_id = str(contact_id or "").strip()
        clean_customer = str(customer_id or "").strip()
        if not clean_id or not clean_customer:
            raise OrderTransitionError("a contact needs an id and the customer they belong to")
        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT customer_id FROM contacts WHERE contact_id = ?", (clean_id,)
            ).fetchone()
            if existing is not None and str(existing["customer_id"]) != clean_customer:
                raise OrderTransitionError(
                    f"contact {clean_id} already belongs to {existing['customer_id']}; "
                    "moving a person between customers is not a rename"
                )
            conn.execute(
                "INSERT INTO contacts(contact_id, customer_id, display_name, created_at, created_by) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(contact_id) DO UPDATE SET display_name=excluded.display_name",
                (clean_id, clean_customer, str(display_name or ""), datetime.now().isoformat(), str(actor)),
            )
        return self.contact(clean_id) or {}

    def bind_identity(self, *, channel: str, identity: str, contact_id: str, actor: str) -> dict[str, Any]:
        """Attach one provider identity to one contact.

        An identity already held by somebody else is refused, naming them.
        Two customers sharing an inbox is not a merge; it is two people about
        to read each other's delivery.
        """
        from ampuly.plugins.office_ui.contact_identity import ContactError, validate_channel, validate_identity

        try:
            clean_channel = validate_channel(channel)
            clean_identity = validate_identity(identity)
        except ContactError as exc:
            raise OrderTransitionError(str(exc)) from exc
        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        if self.contact(str(contact_id or "")) is None:
            raise OrderTransitionError(f"there is no contact {contact_id!r} to bind this to")
        with self._connect() as conn:
            held = conn.execute(
                "SELECT contact_id FROM contact_identities WHERE channel = ? AND identity = ?",
                (clean_channel, clean_identity),
            ).fetchone()
            if held is not None and str(held["contact_id"]) != str(contact_id):
                raise OrderTransitionError(
                    f"{clean_channel}:{clean_identity} is already {held['contact_id']}; "
                    "one inbox cannot belong to two people"
                )
            conn.execute(
                "INSERT INTO contact_identities(channel, identity, contact_id, bound_at, bound_by) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(channel, identity) DO NOTHING",
                (clean_channel, clean_identity, str(contact_id), datetime.now().isoformat(), str(actor)),
            )
        return self.contact_for_identity(channel=clean_channel, identity=clean_identity) or {}

    def contact(self, contact_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM contacts WHERE contact_id = ?", (str(contact_id or ""),)
            ).fetchone()
        if row is None:
            return None
        return {
            "contact_id": str(row["contact_id"]), "customer_id": str(row["customer_id"]),
            "display_name": str(row["display_name"] or ""), "created_at": str(row["created_at"] or ""),
        }

    def contact_for_identity(self, *, channel: str, identity: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT c.contact_id, c.customer_id, c.display_name FROM contact_identities i "
                "JOIN contacts c ON c.contact_id = i.contact_id "
                "WHERE i.channel = ? AND i.identity = ?",
                (str(channel or "").strip().lower(), str(identity or "").strip()),
            ).fetchone()
        if row is None:
            return None
        return {
            "contact_id": str(row["contact_id"]), "customer_id": str(row["customer_id"]),
            "display_name": str(row["display_name"] or ""),
        }

    def list_contacts(self, *, customer_ids: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM contacts ORDER BY customer_id, contact_id").fetchall()
            identities = conn.execute(
                "SELECT channel, identity, contact_id FROM contact_identities ORDER BY channel, identity"
            ).fetchall()
        by_contact: dict[str, list[dict[str, str]]] = {}
        for row in identities:
            by_contact.setdefault(str(row["contact_id"]), []).append(
                {"channel": str(row["channel"]), "identity": str(row["identity"])}
            )
        allowed = {str(c).strip() for c in customer_ids if str(c).strip()}
        out: list[dict[str, Any]] = []
        for row in rows:
            if allowed and str(row["customer_id"]) not in allowed:
                continue
            out.append({
                "contact_id": str(row["contact_id"]), "customer_id": str(row["customer_id"]),
                "display_name": str(row["display_name"] or ""),
                "identities": by_contact.get(str(row["contact_id"]), []),
            })
        return out

    def link_outbound_message(self, *, message_key: str, order_id: str, channel: str = "") -> None:
        """Remember which order a message we sent was about.

        This is what makes a reply attachable: the customer quotes our message
        and the quote carries its key back.
        """
        key = str(message_key or "").strip()
        if not key or not str(order_id or "").strip():
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO outbound_message_links(message_key, order_id, channel, at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(message_key) DO NOTHING",
                (key, str(order_id), str(channel or ""), datetime.now().isoformat()),
            )

    def order_for_outbound(self, message_key: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT order_id FROM outbound_message_links WHERE message_key = ?",
                (str(message_key or "").strip(),),
            ).fetchone()
        return str(row["order_id"]) if row is not None else ""

    def outbound_message_times(self, order_ids: Any) -> list[str]:
        """When messages about these orders went out (S3-2A 客户确认 evidence). Read only; ``[]`` for no orders."""
        ids = [str(item or "").strip() for item in list(order_ids or []) if str(item or "").strip()]
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT at FROM outbound_message_links WHERE order_id IN ({marks}) ORDER BY at", tuple(ids),
            ).fetchall()
        return [str(row["at"] or "") for row in rows]

    def open_orders_for_customer(self, customer_id: str) -> list[str]:
        """Orders still in flight for this customer, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT order_id FROM work_orders WHERE customer_id = ? AND status NOT IN "
                "('closed', 'rejected') ORDER BY created_at DESC, rowid DESC",
                (str(customer_id or "").strip(),),
            ).fetchall()
        return [str(row["order_id"]) for row in rows]

    # ── process descriptions and what trying them established ──
    #
    # A description is saved as written, unfinished included: the questions it
    # cannot answer are the point of writing it down early, and a store that
    # only accepted finished ones would push people back to YAML.

    def save_process_spec(self, payload: dict[str, Any], *, actor: str) -> dict[str, Any]:
        """Store one version of a description, finished or not."""
        from ampuly.plugins.office_ui.process_spec import ProcessSpecError, from_payload

        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        try:
            spec = from_payload(payload)
        except ProcessSpecError as exc:
            raise OrderTransitionError(str(exc)) from exc
        now = datetime.now().isoformat()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT digest FROM process_specs WHERE spec_id = ? AND version = ?",
                (spec.spec_id, spec.version),
            ).fetchone()
            if existing is not None and str(existing["digest"]) != spec.digest():
                raise OrderTransitionError(
                    f"{spec.spec_id} {spec.version} already exists with different content; "
                    "give the changed description a new version so the old one keeps meaning what it meant"
                )
            conn.execute(
                "INSERT INTO process_specs(spec_id, version, digest, payload_json, author, at) "
                "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(spec_id, version) DO NOTHING",
                (
                    spec.spec_id, spec.version, spec.digest(),
                    json.dumps(spec.to_payload(), ensure_ascii=False, sort_keys=True),
                    str(actor), now,
                ),
            )
        return self.process_spec(spec.spec_id, spec.version) or {}

    def process_spec(self, spec_id: str, version: str = "") -> dict[str, Any] | None:
        """The named version, or the most recently written one."""
        with self._connect() as conn:
            if version:
                row = conn.execute(
                    "SELECT * FROM process_specs WHERE spec_id = ? AND version = ?",
                    (str(spec_id or ""), str(version)),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM process_specs WHERE spec_id = ? ORDER BY at DESC, rowid DESC LIMIT 1",
                    (str(spec_id or ""),),
                ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except ValueError as exc:
            raise OrderTransitionError(f"stored description {spec_id} is unreadable: {exc}") from exc
        return {
            "spec_id": str(row["spec_id"]), "version": str(row["version"]),
            "digest": str(row["digest"]), "author": str(row["author"] or ""),
            "at": str(row["at"] or ""), "spec": payload,
        }

    def previous_process_spec(self, spec_id: str, version: str) -> dict[str, Any] | None:
        """Previous saved revision of this description, not another project fact."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT older.version FROM process_specs older JOIN process_specs current "
                "ON current.spec_id = older.spec_id WHERE current.spec_id = ? AND current.version = ? "
                "AND older.rowid < current.rowid ORDER BY older.rowid DESC LIMIT 1",
                (spec_id, version),
            ).fetchone()
        return self.process_spec(spec_id, str(row["version"])) if row else None

    def list_process_specs(self) -> list[dict[str, Any]]:
        from ampuly.plugins.office_ui.process_spec import from_payload

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT spec_id, version, digest, author, at, payload_json FROM process_specs "
                "ORDER BY spec_id, at DESC, rowid DESC"
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            # Single-node workflow trials reuse the durable ProcessSpec and
            # Builder round ledgers but are an implementation detail of the
            # canvas, not user-authored process descriptions.
            if str(row["spec_id"] or "").startswith("node_trial_"):
                continue
            try:
                spec = from_payload(json.loads(row["payload_json"]))
                missing = spec.missing()
            except Exception:
                missing = ["这份描述读不出来"]
            out.append({
                "spec_id": str(row["spec_id"]), "version": str(row["version"]),
                "digest": str(row["digest"]), "author": str(row["author"] or ""),
                "at": str(row["at"] or ""),
                # Carried in the listing on purpose: an unfinished description
                # should be visible as unfinished without opening it.
                "missing": missing, "ready": not missing,
            })
        return out

    def reserve_builder_round(self, spec_id: str, digest: str, *, actor: str, max_attempts: int = 5) -> dict[str, Any]:
        """Claim a bounded attempt before execution, atomically across consoles.

        A crash leaves a running claim, never an automatic replay or pass.
        Legacy shortened digests consume capacity but cannot prove readiness.
        """
        if not str(actor or "").strip() or len(digest) != 64:
            raise OrderTransitionError("actor and full spec digest are required")
        if not 1 <= max_attempts <= 5:
            raise OrderTransitionError("builder attempts must be between 1 and 5")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM process_specs WHERE spec_id = ? AND digest = ?", (spec_id, digest)).fetchone() is None:
                raise OrderTransitionError("description must be saved before a trial")
            if conn.execute(
                "SELECT 1 FROM builder_rounds WHERE spec_id = ? AND outcome = 'running' LIMIT 1",
                (spec_id,),
            ).fetchone() is not None:
                raise OrderTransitionError("trial_already_running: inspect the existing attempt before another trial")
            count = conn.execute(
                "SELECT COUNT(*) FROM builder_rounds WHERE spec_id = ? AND spec_digest IN (?, ?)",
                (spec_id, digest, digest[:12]),
            ).fetchone()[0]
            if count >= max_attempts:
                raise OrderTransitionError("trial_attempts_exhausted: review and revise this draft before another trial")
            number = conn.execute("SELECT COALESCE(MAX(round_number), 0) + 1 FROM builder_rounds WHERE spec_id = ?", (spec_id,)).fetchone()[0]
            cursor = conn.execute(
                "INSERT INTO builder_rounds(spec_id, round_number, spec_digest, outcome, reason, actor, at) VALUES(?, ?, ?, 'running', ?, ?, ?)",
                (spec_id, number, digest, "试跑已认领；无终态不代表通过，请勿自动重试", actor, datetime.now().isoformat()),
            )
            return {"id": int(cursor.lastrowid), "round": int(number), "spec_digest": digest}

    def finish_builder_round(self, claim_id: int, payload: dict[str, Any], *, actor: str, proposal: dict[str, Any] | None = None) -> None:
        from ampuly.plugins.office_ui.builder_loop import OUTCOMES

        if payload.get("outcome") not in OUTCOMES:
            raise OrderTransitionError("invalid trial outcome")
        with self._connect() as conn:
            changed = conn.execute(
                "UPDATE builder_rounds SET outcome = ?, failed_step = ?, reason = ?, detail = ?, proposal_json = ? "
                "WHERE id = ? AND outcome = 'running' AND actor = ? AND spec_digest = ?",
                (payload["outcome"], str(payload.get("failed_step", "")), str(payload.get("reason", "")),
                 str(payload.get("detail", "")), json.dumps(proposal or {}, ensure_ascii=False),
                 claim_id, actor, str(payload.get("spec_digest", ""))),
            ).rowcount
            if changed != 1:
                raise OrderTransitionError("trial claim changed or does not belong to this actor and draft")

    def record_builder_round(
        self, spec_id: str, round_payload: dict[str, Any], *, actor: str,
        proposal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write down one attempt, including the ones that never ran."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO builder_rounds(spec_id, round_number, spec_digest, outcome, failed_step, "
                "reason, detail, proposal_json, actor, at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(spec_id), int(round_payload.get("round", 0) or 0),
                    str(round_payload.get("spec_digest", "") or ""),
                    str(round_payload.get("outcome", "") or ""),
                    str(round_payload.get("failed_step", "") or ""),
                    str(round_payload.get("reason", "") or ""),
                    str(round_payload.get("detail", "") or ""),
                    json.dumps(proposal or {}, ensure_ascii=False),
                    str(actor), datetime.now().isoformat(),
                ),
            )
        return self.builder_rounds(spec_id)[0]

    def builder_rounds(self, spec_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM builder_rounds WHERE spec_id = ? ORDER BY id DESC LIMIT ?",
                (str(spec_id or ""), int(limit)),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                proposal = json.loads(row["proposal_json"] or "{}")
            except ValueError:
                proposal = {}
            out.append({
                "id": int(row["id"]), "round": int(row["round_number"]), "spec_digest": str(row["spec_digest"] or ""),
                "outcome": str(row["outcome"]), "failed_step": str(row["failed_step"] or ""),
                "reason": str(row["reason"] or ""), "detail": str(row["detail"] or ""),
                "proposal": proposal, "actor": str(row["actor"] or ""), "at": str(row["at"] or ""),
            })
        return out

    # ── the stop switch ──
    #
    # "Stop sending on that channel" needs an answer that does not require
    # editing a config file and restarting, because it is asked in the middle
    # of something going wrong. A connector nobody has switched is on, which
    # is what every existing installation already does.

    def set_connector_enabled(
        self, connector: str, enabled: bool, *, actor: str, reason: str = "",
    ) -> dict[str, Any]:
        """Switch one connector on or off, with who did it and why."""
        name = str(connector or "").strip()
        if not name:
            raise OrderTransitionError("which connector?")
        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        if not enabled and not str(reason or "").strip():
            # Switching a customer channel off is the kind of thing somebody
            # finds a week later and cannot explain. The reason is the
            # difference between a decision and a mystery.
            raise OrderTransitionError("say why this connector is being switched off")
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO connector_switches(connector, enabled, actor, at, reason) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(connector) DO UPDATE SET enabled=excluded.enabled, actor=excluded.actor, "
                "at=excluded.at, reason=excluded.reason",
                (name, 1 if enabled else 0, str(actor), now, str(reason or "")),
            )
        return self.connector_switch(name)

    def connector_switch(self, connector: str) -> dict[str, Any]:
        """This connector's switch, defaulting to on for one nobody has touched."""
        name = str(connector or "").strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM connector_switches WHERE connector = ?", (name,)
            ).fetchone()
        if row is None:
            return {"connector": name, "enabled": True, "actor": "", "at": "", "reason": "", "explicit": False}
        return {
            "connector": name,
            "enabled": bool(row["enabled"]),
            "actor": str(row["actor"] or ""),
            "at": str(row["at"] or ""),
            "reason": str(row["reason"] or ""),
            "explicit": True,
        }

    def connector_enabled(self, connector: str) -> bool:
        return bool(self.connector_switch(connector)["enabled"])

    def list_connector_switches(self) -> list[dict[str, Any]]:
        """Only the ones somebody has actually set; the rest are on by default."""
        with self._connect() as conn:
            rows = conn.execute("SELECT connector FROM connector_switches ORDER BY connector").fetchall()
        return [self.connector_switch(str(row["connector"])) for row in rows]

    # ── the governance policy, and what a change to it can honestly claim ──
    #
    # The org config file keeps the new value and nothing else: not who
    # changed it, not what it was, not why, not what the change does and
    # does not reach. Same argument as the connector switch -- a value
    # found changed a week later with none of that is a mystery rather
    # than a decision.

    def record_policy_change(
        self, field: str, old_value: Any, new_value: Any, *,
        actor: str, reason: str, profile: str = "custom",
        scope: str = POLICY_CHANGE_SCOPE,
    ) -> dict[str, Any]:
        """Who changed which policy field, from what to what, and why."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO policy_changes(profile, field, old_value, new_value, actor, at, reason, scope) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(profile or "custom"), str(field), json.dumps(old_value, ensure_ascii=False),
                    json.dumps(new_value, ensure_ascii=False), str(actor or ""), now,
                    str(reason or "")[:500], str(scope or ""),
                ),
            )
        return {
            "field": str(field), "old_value": old_value, "new_value": new_value,
            "actor": str(actor or ""), "at": now, "reason": str(reason or "")[:500],
            "scope": str(scope or ""),
        }

    def policy_changes(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """The recent policy decisions, newest first."""
        bounded = max(1, min(int(limit or 50), 500))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM policy_changes ORDER BY change_id DESC LIMIT ?", (bounded,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            for name in ("old_value", "new_value"):
                try:
                    item[name] = json.loads(str(item.get(name) or "null"))
                except ValueError:
                    item[name] = None
            out.append(item)
        return out

    # ── baselines, and the numbers computed from them ──
    #
    # A saving needs something to be a saving against. Nobody has that number
    # by default, so it is stored explicitly, with who said it and what they
    # were going on -- and until somebody does, the saving is not calculated
    # rather than zero.

    def set_metric_baseline(
        self,
        *,
        metric: str,
        value: int,
        actor: str,
        customer_id: str = "",
        workflow_id: str = "",
        unit: str = "cents",
        source: str = "stated",
        evidence: str = "",
    ) -> dict[str, Any]:
        """Record what the old way costs, and where that figure came from."""
        from ampuly.plugins.office_ui.metrics import SOURCES

        if not str(metric or "").strip():
            raise OrderTransitionError("a baseline needs to say what it is a baseline for")
        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise OrderTransitionError(f"{metric} must be a whole non-negative number of {unit}")
        if source not in SOURCES:
            raise OrderTransitionError(f"unknown source {source!r}; say how this number was obtained")
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO metric_baselines(metric, customer_id, workflow_id, value, unit, source, evidence, actor, at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(metric, customer_id, workflow_id) DO UPDATE SET "
                "value=excluded.value, unit=excluded.unit, source=excluded.source, "
                "evidence=excluded.evidence, actor=excluded.actor, at=excluded.at",
                (
                    str(metric), str(customer_id or ""), str(workflow_id or ""), int(value),
                    str(unit), str(source), str(evidence or ""), str(actor), now,
                ),
            )
        return self.metric_baseline(metric=metric, customer_id=customer_id, workflow_id=workflow_id) or {}

    def metric_baseline(
        self, *, metric: str, customer_id: str = "", workflow_id: str = "",
    ) -> dict[str, Any] | None:
        """The most specific baseline that applies, or nothing.

        Specific beats general for the same reason it does for credentials:
        the narrower one is the exception somebody wrote down deliberately.
        """
        candidates = [
            (str(customer_id or ""), str(workflow_id or "")),
            (str(customer_id or ""), ""),
            ("", str(workflow_id or "")),
            ("", ""),
        ]
        seen: list[tuple[str, str]] = []
        with self._connect() as conn:
            for scope in candidates:
                if scope in seen:
                    continue
                seen.append(scope)
                row = conn.execute(
                    "SELECT * FROM metric_baselines WHERE metric = ? AND customer_id = ? AND workflow_id = ?",
                    (str(metric), scope[0], scope[1]),
                ).fetchone()
                if row is not None:
                    return {
                        "metric": str(row["metric"]),
                        "customer_id": str(row["customer_id"] or ""),
                        "workflow_id": str(row["workflow_id"] or ""),
                        "value": int(row["value"]),
                        "unit": str(row["unit"] or "cents"),
                        "source": str(row["source"] or "stated"),
                        "evidence": str(row["evidence"] or ""),
                        "actor": str(row["actor"] or ""),
                        "at": str(row["at"] or ""),
                    }
        return None

    def list_metric_baselines(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT metric, customer_id, workflow_id FROM metric_baselines ORDER BY metric, customer_id, workflow_id"
            ).fetchall()
        return [
            self.metric_baseline(
                metric=str(row["metric"]), customer_id=str(row["customer_id"] or ""),
                workflow_id=str(row["workflow_id"] or ""),
            ) or {}
            for row in rows
        ]

    ORDER_DELIVERED_EVENT = "acceptance.accepted"

    def order_metrics(self, order_id: str, *, now: Any = None) -> dict[str, Any]:
        """What this order cost, saved and promised -- or which of those is unknown.

        Every number here has a source: the spend comes from the run budgets
        attached to this order, the clock from the order's own events, the
        baseline from whoever wrote it down. Nothing is defaulted. An order
        with no budget record has an *unknown* cost, not a cost of zero, and
        that distinction is the difference between a report and a sales
        brochure.
        """
        from ampuly.plugins.office_ui.metrics import (
            MetricInput,
            saving,
            sla_verdict,
            summarize,
        )

        order = self.get_order(str(order_id or ""))
        if order is None:
            raise OrderTransitionError(f"no work order {order_id!r}")
        fields = dict(order.fields or {})
        budgets = [b for b in self.list_run_budgets() if str(b.get("order_id", "")) == str(order_id)]
        spend_cents: int | None = None
        evidence = ""
        # A count-only budget is not a currency meter. Every attached budget
        # must carry a settled monetary observation; unpriced or still-held
        # work cannot silently contribute zero to a purported measured total.
        if budgets and all(
            "cost_cny" in dict(b.get("consumed", {}) or {})
            and not dict(b.get("held", {}) or {})
            and b.get("health", "ok") == "ok"
            for b in budgets
        ):
            total = sum(float(dict(b.get("consumed", {}) or {})["cost_cny"]) for b in budgets)
            spend_cents = int(round(total * 100))
            evidence = ", ".join(str(b.get("budget_id", "")) for b in budgets)
        baseline = self.metric_baseline(
            metric="manual_cost", customer_id=order.customer_id, workflow_id=order.workflow_id,
        )
        baseline_input = MetricInput(
            name="手工做这件事的成本",
            value=int(baseline["value"]) if baseline else None,
            unit=str(baseline["unit"]) if baseline else "cents",
            source=str(baseline["source"]) if baseline else "stated",
            evidence=str(baseline["evidence"]) if baseline else "",
        )
        spend_input = MetricInput(
            name="这一单的模型花费", value=spend_cents, unit="cents",
            source="measured", evidence=evidence,
        )
        # The list is the model spend and nothing else: human time on this
        # order is not recorded anywhere, so the total is a lower bound and
        # the saving computed from it is an upper one.
        result = saving(baseline_input, [spend_input], costs_are_complete=False)
        # Intake stores every field as text, so the promise arrives as "48".
        # A whole number of hours is checkable; anything else -- prose, a
        # range, an empty box -- is not, and pretending otherwise is how an
        # SLA badge comes to mean nothing.
        raw_promise = fields.get("sla_hours")
        promise = int(raw_promise) if str(raw_promise or "").strip().isdigit() else 0
        if promise <= 0:
            sla = {
                "promise_hours": 0, "state": "not_calculated", "elapsed_hours": None,
                "remaining_hours": None, "missing": ["sla_hours"],
                # A promise written as prose cannot be checked by anything,
                # which is worth saying rather than quietly not checking.
                "detail": "this order carries no sla_hours; a promise in prose is not one a machine can judge",
            }
        else:
            delivered = ""
            for event in self.events(str(order_id)):
                if str(event.get("kind", "")) == self.ORDER_DELIVERED_EVENT:
                    delivered = str(event.get("at", "") or "")
                    break
            sla = sla_verdict(
                promise_hours=int(promise), started_at=str(order.created_at or ""),
                delivered_at=delivered, now=now,
            ).to_payload()
        return {
            "order_id": str(order_id),
            "customer_id": order.customer_id,
            "workflow_id": order.workflow_id,
            "saving": result.to_payload(),
            "sla": sla,
            "summary": summarize([result]),
        }

    # ── controlled rollout ──
    #
    # An upgrade is not one event. It is: pick who moves, move them one at a
    # time, look at each one, and decide. The parts that go wrong are the ones
    # nobody writes down -- which targets were meant to move, whether anybody
    # actually looked, and what each target was running before, which is the
    # only thing a rollback can use.

    def plan_rollout(
        self,
        *,
        solution_id: str,
        version: str,
        targets: list[dict[str, Any]] | list[tuple[str, str]],
        actor: str,
        note: str = "",
        rollout_id: str = "",
    ) -> dict[str, Any]:
        """Name the version, the targets and where each one is starting from.

        The targets are fixed here on purpose. Adding one later would make
        "this rollout succeeded" describe something other than what was
        approved, so a wider rollout is a new rollout.
        """
        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        if self.solution_package(solution_id, version) is None:
            raise OrderTransitionError(f"solution {solution_id!r} v{version} is not stored here")
        entries: list[tuple[str, str]] = []
        for target in list(targets or []):
            if isinstance(target, dict):
                customer_id = str(target.get("customer_id", "") or "").strip()
                environment_id = str(target.get("environment_id", "") or "").strip()
            else:
                customer_id = str(target[0] or "").strip()
                environment_id = str(target[1] or "").strip() if len(target) > 1 else ""
            if not customer_id:
                raise OrderTransitionError("a rollout target belongs to a customer")
            if (customer_id, environment_id) in entries:
                raise OrderTransitionError(
                    f"{customer_id}/{environment_id or 'default'} is listed twice; "
                    "one target moves once"
                )
            entries.append((customer_id, environment_id))
        if not entries:
            raise OrderTransitionError("a rollout with no targets moves nobody")
        identifier = str(rollout_id or "").strip() or uuid.uuid4().hex
        now = datetime.now().isoformat()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT rollout_id FROM rollouts WHERE rollout_id = ?", (identifier,)
            ).fetchone()
            if existing is not None:
                raise OrderTransitionError(f"rollout {identifier} already exists")
            conn.execute(
                "INSERT INTO rollouts(rollout_id, solution_id, version, state, created_at, created_by, "
                "updated_at, updated_by, note) VALUES(?, ?, ?, 'planned', ?, ?, ?, ?, ?)",
                (identifier, str(solution_id), str(version), now, str(actor), now, str(actor), str(note or "")),
            )
            for position, (customer_id, environment_id) in enumerate(entries):
                current = conn.execute(
                    "SELECT version FROM deployments WHERE customer_id = ? AND environment_id = ? "
                    "AND solution_id = ? AND state = 'active'",
                    (customer_id, environment_id, str(solution_id)),
                ).fetchone()
                conn.execute(
                    "INSERT INTO rollout_targets(rollout_id, customer_id, environment_id, position, "
                    "from_version, state, health, checks_json, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, 'pending', 'unknown', '[]', ?)",
                    (
                        identifier, customer_id, environment_id, position,
                        str(current["version"]) if current is not None else "", now,
                    ),
                )
        return self.rollout(identifier) or {}

    def rollout(self, rollout_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM rollouts WHERE rollout_id = ?", (str(rollout_id or ""),)
            ).fetchone()
            if row is None:
                return None
            targets = conn.execute(
                "SELECT * FROM rollout_targets WHERE rollout_id = ? ORDER BY position",
                (str(rollout_id),),
            ).fetchall()
        return {
            "rollout_id": str(row["rollout_id"]),
            "solution_id": str(row["solution_id"]),
            "version": str(row["version"]),
            "state": str(row["state"]),
            "created_at": str(row["created_at"]),
            "created_by": str(row["created_by"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "updated_by": str(row["updated_by"] or ""),
            "note": str(row["note"] or ""),
            "targets": [self._rollout_target_row(t) for t in targets],
        }

    @staticmethod
    def _rollout_filter(*, solution_id: str, states: tuple[str, ...]) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if solution_id:
            clauses.append("solution_id = ?")
            params.append(str(solution_id))
        if states:
            clauses.append(f"state IN ({','.join('?' for _ in states)})")
            params.extend(str(s) for s in states)
        return clauses, params

    def list_rollouts(
        self, *, solution_id: str = "", states: tuple[str, ...] = (), limit: int = ROLLOUT_LIST_LIMIT,
    ) -> list[dict[str, Any]]:
        """The newest ``limit`` rollouts. Whether there were more is ``count_rollouts``'s to say, not this list's."""
        clauses, params = self._rollout_filter(solution_id=solution_id, states=states)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT rollout_id FROM rollouts{where} ORDER BY created_at DESC, rowid DESC LIMIT ?", tuple(params)
            ).fetchall()
        return [self.rollout(str(row["rollout_id"])) or {} for row in rows]

    def count_rollouts(self, *, solution_id: str = "", states: tuple[str, ...] = ()) -> int:
        """How many rollouts match, so a page can say when its newest-N list is not all of them."""
        clauses, params = self._rollout_filter(solution_id=solution_id, states=states)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM rollouts{where}", tuple(params)).fetchone()
        return int(row[0] if row else 0)

    def advance_rollout(
        self, rollout_id: str, *, customer_id: str, environment_id: str = "", actor: str,
    ) -> dict[str, Any]:
        """Move one target to the new version. Running it twice moves it once."""
        record = self._rollout_for_change(rollout_id)
        target = self._rollout_target(record, customer_id, environment_id)
        if target["state"] == "rolled_back":
            raise OrderTransitionError(
                "this target was rolled back; moving it again inside the same rollout "
                "would hide that it failed once"
            )
        if target["state"] == "pending":
            self.deploy_solution(
                customer_id=customer_id, environment_id=environment_id,
                solution_id=record["solution_id"], version=record["version"], actor=actor,
                note=f"rollout {record['rollout_id']}",
            )
            self._set_rollout_target(rollout_id, customer_id, environment_id, state="deployed")
        self._set_rollout_state(rollout_id, "in_progress", actor)
        return self.rollout(rollout_id) or {}

    def record_rollout_health(
        self,
        rollout_id: str,
        *,
        customer_id: str,
        environment_id: str = "",
        checks: list[Any] | None = None,
        actor: str,
    ) -> dict[str, Any]:
        """Write down what was checked, and let the weakest result decide."""
        from ampuly.plugins.office_ui.rollout import RolloutError, parse_checks, target_state_for, verdict

        record = self._rollout_for_change(rollout_id)
        target = self._rollout_target(record, customer_id, environment_id)
        if target["state"] == "pending":
            raise OrderTransitionError(
                "nothing is running on this target yet; health of what?"
            )
        try:
            parsed = parse_checks(checks)
        except RolloutError as exc:
            raise OrderTransitionError(str(exc)) from exc
        health = verdict(parsed)
        self._set_rollout_target(
            rollout_id, customer_id, environment_id,
            state=target_state_for(health), health=health,
            checks_json=json.dumps([c.to_payload() for c in parsed], ensure_ascii=False),
        )
        self._set_rollout_state(rollout_id, "in_progress", actor)
        return self.rollout(rollout_id) or {}

    def promote_rollout(self, rollout_id: str, *, actor: str) -> dict[str, Any]:
        """Finish, but only when every target has actually been established."""
        from ampuly.plugins.office_ui.rollout import blocking_targets, describe_block

        record = self._rollout_for_change(rollout_id)
        blocked = blocking_targets(record["targets"])
        if blocked:
            raise OrderTransitionError(
                "this rollout is not finished: " + "; ".join(describe_block(t) for t in blocked)
            )
        self._set_rollout_state(rollout_id, "succeeded", actor)
        return self.rollout(rollout_id) or {}

    def rollback_rollout(self, rollout_id: str, *, actor: str, note: str = "") -> dict[str, Any]:
        """Put every moved target back where it came from, and keep the attempt.

        Each target returns to *its own* previous version: two customers can
        have started from different places, and a single "previous version"
        would be wrong for one of them. A target that had nothing before is
        retired rather than pointed at an empty version.
        """
        record = self._rollout_for_change(rollout_id)
        for target in record["targets"]:
            if target["state"] in {"pending", "rolled_back"}:
                continue
            if target["from_version"]:
                self.deploy_solution(
                    customer_id=target["customer_id"], environment_id=target["environment_id"],
                    solution_id=record["solution_id"], version=target["from_version"], actor=actor,
                    note=f"rollback of rollout {record['rollout_id']}",
                )
            else:
                self.retire_deployment(
                    customer_id=target["customer_id"], environment_id=target["environment_id"],
                    solution_id=record["solution_id"], actor=actor,
                    note=f"rollback of rollout {record['rollout_id']}",
                )
            self._set_rollout_target(
                rollout_id, target["customer_id"], target["environment_id"], state="rolled_back",
            )
        self._set_rollout_state(rollout_id, "rolled_back", actor, note=note)
        return self.rollout(rollout_id) or {}

    def _rollout_for_change(self, rollout_id: str) -> dict[str, Any]:
        record = self.rollout(rollout_id)
        if record is None:
            raise OrderTransitionError(f"no rollout {rollout_id!r}")
        if record["state"] in {"succeeded", "rolled_back", "abandoned"}:
            raise OrderTransitionError(
                f"rollout {rollout_id} is {record['state']}; a finished rollout is history, "
                "not a place to keep working"
            )
        return record

    @staticmethod
    def _rollout_target(record: dict[str, Any], customer_id: str, environment_id: str) -> dict[str, Any]:
        wanted = (str(customer_id or "").strip(), str(environment_id or "").strip())
        for target in record["targets"]:
            if (target["customer_id"], target["environment_id"]) == wanted:
                return target
        raise OrderTransitionError(
            f"{wanted[0]}/{wanted[1] or 'default'} is not a target of this rollout; "
            "widening it here would change what was approved"
        )

    def _set_rollout_target(
        self, rollout_id: str, customer_id: str, environment_id: str, **fields: Any
    ) -> None:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        params = [*fields.values(), datetime.now().isoformat(), str(rollout_id),
                  str(customer_id or ""), str(environment_id or "")]
        with self._connect() as conn:
            conn.execute(
                f"UPDATE rollout_targets SET {assignments}, updated_at = ? "
                "WHERE rollout_id = ? AND customer_id = ? AND environment_id = ?",
                tuple(params),
            )

    def _set_rollout_state(self, rollout_id: str, state: str, actor: str, *, note: str = "") -> None:
        with self._connect() as conn:
            if note:
                conn.execute(
                    "UPDATE rollouts SET state = ?, updated_at = ?, updated_by = ?, note = ? WHERE rollout_id = ?",
                    (str(state), datetime.now().isoformat(), str(actor), str(note), str(rollout_id)),
                )
            else:
                conn.execute(
                    "UPDATE rollouts SET state = ?, updated_at = ?, updated_by = ? WHERE rollout_id = ?",
                    (str(state), datetime.now().isoformat(), str(actor), str(rollout_id)),
                )

    @staticmethod
    def _rollout_target_row(row: Any) -> dict[str, Any]:
        try:
            checks = json.loads(str(row["checks_json"] or "[]"))
        except ValueError:
            checks = []
        return {
            "customer_id": str(row["customer_id"]),
            "environment_id": str(row["environment_id"] or ""),
            "from_version": str(row["from_version"] or ""),
            "state": str(row["state"] or "pending"),
            "health": str(row["health"] or "unknown"),
            "checks": checks,
            "updated_at": str(row["updated_at"] or ""),
        }

    # ── credential references ──
    #
    # The name of the variable, never the key. Two customers on one machine
    # need different credentials for the same purpose, and this is the only
    # place that says which is whose. A value has no column to live in.

    def set_credential_reference(
        self,
        *,
        purpose: str,
        env_var: str,
        customer_id: str = "",
        environment_id: str = "",
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        from ampuly.plugins.office_ui.credential_refs import PURPOSES, validate_env_var

        clean_purpose = str(purpose or "").strip()
        if clean_purpose not in PURPOSES:
            raise OrderTransitionError(f"unknown credential purpose {clean_purpose!r}")
        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        try:
            clean_var = validate_env_var(env_var)
        except ValueError as exc:
            raise OrderTransitionError(str(exc)) from exc
        if environment_id and not customer_id:
            raise OrderTransitionError("an environment reference needs the customer it belongs to")
        now = datetime.now().isoformat()
        reference_id = f"cr-{uuid.uuid4().hex[:12]}"
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO credential_references(reference_id, purpose, customer_id, environment_id, env_var, "
                "created_at, created_by, note) VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(purpose, customer_id, environment_id) DO UPDATE SET "
                "env_var=excluded.env_var, created_at=excluded.created_at, created_by=excluded.created_by, note=excluded.note",
                (
                    reference_id, clean_purpose, str(customer_id or ""), str(environment_id or ""),
                    clean_var, now, str(actor), str(note or ""),
                ),
            )
        return self.credential_reference(
            purpose=clean_purpose, customer_id=customer_id, environment_id=environment_id, exact=True,
        ) or {}

    def credential_reference(
        self,
        *,
        purpose: str,
        customer_id: str = "",
        environment_id: str = "",
        exact: bool = False,
    ) -> dict[str, Any] | None:
        """The most specific reference that applies, unless ``exact``.

        Specific beats general: the narrower row is the deliberate exception
        somebody wrote down, and falling back to the machine-wide default when
        a customer has their own would use the wrong key.
        """
        clean_purpose = str(purpose or "").strip()
        customer = str(customer_id or "")
        environment = str(environment_id or "")
        candidates = [(customer, environment)] if exact else [
            (customer, environment),
            (customer, ""),
            ("", ""),
        ]
        with self._connect() as conn:
            for cand_customer, cand_environment in candidates:
                row = conn.execute(
                    "SELECT * FROM credential_references WHERE purpose = ? AND customer_id = ? AND environment_id = ?",
                    (clean_purpose, cand_customer, cand_environment),
                ).fetchone()
                if row is not None:
                    return {
                        "reference_id": str(row["reference_id"]),
                        "purpose": str(row["purpose"]),
                        "customer_id": str(row["customer_id"] or ""),
                        "environment_id": str(row["environment_id"] or ""),
                        "env_var": str(row["env_var"]),
                        "created_at": str(row["created_at"] or ""),
                        "created_by": str(row["created_by"] or ""),
                        "note": str(row["note"] or ""),
                    }
        return None

    def list_credential_references(self, *, customer_ids: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT purpose, customer_id, environment_id FROM credential_references "
                "ORDER BY purpose, customer_id, environment_id"
            ).fetchall()
        out: list[dict[str, Any]] = []
        allowed = {str(c).strip() for c in customer_ids if str(c).strip()}
        for row in rows:
            customer = str(row["customer_id"] or "")
            # A bound account sees its own customers' references and the
            # machine-wide default it would otherwise fall back to.
            if allowed and customer and customer not in allowed:
                continue
            reference = self.credential_reference(
                purpose=str(row["purpose"]), customer_id=customer,
                environment_id=str(row["environment_id"] or ""), exact=True,
            )
            if reference:
                out.append(reference)
        return out

    def delete_credential_reference(self, *, purpose: str, customer_id: str = "", environment_id: str = "") -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM credential_references WHERE purpose = ? AND customer_id = ? AND environment_id = ?",
                (str(purpose or ""), str(customer_id or ""), str(environment_id or "")),
            )
        return cursor.rowcount > 0

    # ── run budgets ──
    #
    # A project may use a paid tool; that is not permission for one order to
    # call it without end.  A budget holds an amount before the call and
    # settles the real cost after, so two calls racing cannot both believe the
    # money is there, and a crash between the two leaves a held reservation
    # rather than a silent overspend.
    #
    # Every state change is a conditional update that checks the state it
    # expected: a late callback arriving after a decision loses instead of
    # overwriting it.  The transitions are the whole story --
    #
    #     held    -> settled | released | unknown
    #     unknown -> settled | released      (only by a person, via resolve)
    #     settled, released                  terminal
    #
    # -- and "unknown" is deliberately a dead end for automation: releasing it
    # would invite a second paid attempt for something that may already have
    # run.
    BUDGET_KINDS = ("model_call", "image", "retry", "cost_cny")
    BUDGET_COUNT_KINDS = ("model_call", "image", "retry")
    RESERVATION_TRANSITIONS: dict[str, frozenset[str]] = {
        "held": frozenset({"settled", "released", "unknown"}),
        "unknown": frozenset({"settled", "released"}),
        "settled": frozenset(),
        "released": frozenset(),
    }

    @classmethod
    def _budget_amount(cls, kind: str, value: Any) -> float:
        """Every number that touches money is checked here, once.

        A NaN limit compares false against everything, so it would read as
        "no ceiling"; a negative settlement would mint budget out of nothing;
        ``True`` is an ``int`` in Python and would quietly mean one image.
        None of those may reach the database.
        """
        if isinstance(value, bool):
            raise OrderTransitionError(f"{kind}: a true/false value is not an amount")
        try:
            amount = float(value)
        except (TypeError, ValueError) as exc:
            raise OrderTransitionError(f"{kind}: {value!r} is not a number") from exc
        if math.isnan(amount) or math.isinf(amount):
            raise OrderTransitionError(f"{kind}: {value!r} is not a finite number")
        if amount < 0:
            raise OrderTransitionError(f"{kind}: an amount cannot be negative")
        if kind in cls.BUDGET_COUNT_KINDS and amount != int(amount):
            raise OrderTransitionError(f"{kind} counts whole calls; {value!r} is not a whole number")
        return amount

    @staticmethod
    def _reservation_fingerprint(kind: str, amount: float, note: str) -> str:
        """What an idempotency key stands for.

        A repeated key must mean "the same request again". Bound to nothing,
        it would let a second, larger request inherit the first one's
        authority.
        """
        payload = f"{kind}|{amount!r}|{str(note or '')}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def create_run_budget(
        self,
        *,
        project_id: str = "default",
        customer_id: str = "",
        environment_id: str = "",
        order_id: str = "",
        limits: dict[str, float] | None = None,
        actor: str,
    ) -> dict[str, Any]:
        """Open a budget for one run. An absent limit means unlimited, and says so."""
        clean_actor = str(actor or "").strip()
        if not clean_actor:
            raise OrderTransitionError("actor is required")
        clean_limits = self.clean_budget_limits(limits)
        budget_id = f"bg-{uuid.uuid4().hex[:12]}"
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO run_budgets(budget_id, project_id, customer_id, environment_id, order_id, "
                "created_at, created_by, limits_json, state, health) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'open', 'ok')",
                (
                    budget_id, str(project_id or "default"), str(customer_id or ""), str(environment_id or ""),
                    str(order_id or ""), now, clean_actor, json.dumps(clean_limits, sort_keys=True),
                ),
            )
        return self.run_budget(budget_id) or {}

    def ensure_direct_task_budget(
        self, *, project_id: str, customer_id: str = "", environment_id: str = "", actor: str,
    ) -> dict[str, Any]:
        """The project's open allowance for direct tasks, opened once and reused.

        A direct task has no business order; its allowance belongs to the project.
        Reusing the open one keeps every turn of a conversation on one budget
        tree. An empty limit set is said out loud ("unlimited, journaled"), not a
        silent way around a ceiling: a project that has a ceiling must use it.
        """
        clean_actor = str(actor or "").strip()
        clean_project = str(project_id or "").strip()
        if not clean_actor or not clean_project:
            raise OrderTransitionError("direct task budget requires an owner actor and a project")
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT budget_id FROM run_budgets WHERE project_id = ? AND order_id = '' "
                "AND state = 'open' ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (clean_project,),
            ).fetchone()
            if row is not None:
                budget_id = str(row["budget_id"])
            else:
                budget_id = f"bg-{uuid.uuid4().hex[:12]}"
                conn.execute(
                    "INSERT INTO run_budgets(budget_id, project_id, customer_id, environment_id, order_id, "
                    "created_at, created_by, limits_json, state, health) "
                    "VALUES(?, ?, ?, ?, '', ?, ?, '{}', 'open', 'ok')",
                    (budget_id, clean_project, str(customer_id or ""), str(environment_id or ""),
                     now, clean_actor),
                )
        return self.run_budget(budget_id) or {}

    def clean_budget_limits(self, limits: dict[str, Any] | None) -> dict[str, float]:
        """The limits as they would be stored, or a refusal. Callable before anything durable is written, so a
        launch with a bad ceiling is refused before its order exists."""
        clean: dict[str, float] = {}
        for kind, value in dict(limits or {}).items():
            name = str(kind).strip()
            if name not in self.BUDGET_KINDS:
                raise OrderTransitionError(f"unknown budget kind {name!r}")
            clean[name] = self._budget_amount(name, value)
        return clean

    def clean_order_allowance(self, limits: dict[str, Any] | None) -> dict[str, float]:
        """Validate order ceilings; each paid dispatch must enforce its own cap.

        Models require host-bound CNY tariffs. Unpriced paid tools and opaque
        CLI/SDK model transports remain refused under a monetary ceiling.
        """
        return self.clean_budget_limits(limits)

    def closed_budget_ids_for_order(self, order_id: str) -> list[str]:
        """The order's earlier allowances that are closed: a task still carrying one of them may be given the new one."""
        clean = str(order_id or "").strip()
        if not clean:
            return []
        with self._connect() as conn:
            rows = conn.execute("SELECT budget_id FROM run_budgets WHERE order_id = ? AND state = 'closed'", (clean,)).fetchall()
        return [str(row["budget_id"]) for row in rows]

    def open_budget_for_order(self, order_id: str, *, project_id: str | None = None) -> dict[str, Any] | None:
        """The open allowance granted to this order (the newest, if several), or None when it has none."""
        clean = str(order_id or "").strip()
        if not clean:
            return None
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT budget_id FROM run_budgets WHERE order_id = ? AND state = 'open' ORDER BY created_at DESC, rowid DESC",
                (clean,),
            ).fetchall()
        for row in rows:
            budget = self.run_budget(str(row["budget_id"]))
            if budget and (project_id is None or budget.get("project_id") == (str(project_id or "default").strip() or "default")):
                return budget
        return None

    @classmethod
    def _budget_limits(cls, row: Any) -> dict[str, float]:
        """Parse the limits, or refuse. A damaged ceiling is not "no ceiling"."""
        try:
            raw = row["limits_json"]
            # SQLite has already materialized this value. Bound only the JSON
            # decoder and its input here, not the database's I/O or memory.
            if type(raw) is not str or not raw or len(raw) > 8192 or len(raw.encode("utf-8")) > 8192:
                raise ValueError

            def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                if len(pairs) > len(cls.BUDGET_KINDS):
                    raise ValueError
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key not in cls.BUDGET_KINDS or key in result:
                        raise ValueError
                    result[key] = value
                return result

            def reject_constant(_value: str) -> Any:
                raise ValueError

            limits = json.loads(raw, object_pairs_hook=object_pairs, parse_constant=reject_constant)
            if type(limits) is not dict:
                raise ValueError
            clean: dict[str, float] = {}
            for kind, value in limits.items():
                if type(value) not in (int, float):
                    raise ValueError
                amount = cls._budget_amount(kind, value)
                clean[kind] = value if kind in cls.BUDGET_COUNT_KINDS and type(value) is int else amount
            return clean
        except Exception:
            raise OrderTransitionError("Stored budget limits are invalid; refusing to spend") from None

    @classmethod
    def _budget_usage(cls, rows: list[Any]) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
        """Validate every bounded row; unknown holds remain charged to usage."""
        try:
            if len(rows) > 10000:
                raise ValueError
            held: dict[str, float] = {}
            consumed: dict[str, float] = {}
            for item in rows:
                kind, state = item["kind"], item["state"]
                if type(kind) is not str or kind not in cls.BUDGET_KINDS:
                    raise ValueError
                if type(state) is not str or state not in cls.RESERVATION_TRANSITIONS:
                    raise ValueError
                if state == "released":
                    continue
                target = consumed if state == "settled" else held
                value = item["settled_amount"] if state == "settled" else item["amount"]
                if type(value) not in (int, float):
                    raise ValueError
                amount = cls._budget_amount(kind, value)
                if kind in cls.BUDGET_COUNT_KINDS:
                    amount = int(value)
                total = target.get(kind, 0) + amount
                if not math.isfinite(total):
                    raise ValueError
                target[kind] = total
            used = {kind: held.get(kind, 0) + consumed.get(kind, 0) for kind in cls.BUDGET_KINDS}
            if any(not math.isfinite(value) for value in used.values()):
                raise ValueError
            return held, consumed, used
        except Exception:
            raise OrderTransitionError("Stored budget usage is invalid; refusing to spend") from None

    @classmethod
    def _budget_available(cls, limits: dict[str, float], used: dict[str, float]) -> dict[str, float | None]:
        """Finite overspend is visible as a negative balance, never repaired."""
        try:
            available: dict[str, float | None] = {}
            for kind in cls.BUDGET_KINDS:
                limit = limits.get(kind)
                if limit is None:
                    balance = None
                elif kind in cls.BUDGET_COUNT_KINDS:
                    balance = int(limit) - used[kind]
                else:
                    balance = limit - used[kind]
                if balance is not None and not math.isfinite(balance):
                    raise ValueError
                available[kind] = balance
            return available
        except Exception:
            raise OrderTransitionError("Stored budget usage is invalid; refusing to spend") from None

    def run_budget(self, budget_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM run_budgets WHERE budget_id = ?", (str(budget_id or ""),)).fetchone()
            if row is None:
                return None
            reservations = conn.execute(
                "SELECT kind, amount, settled_amount, state FROM budget_reservations WHERE budget_id = ? LIMIT 10001",
                (str(budget_id),),
            ).fetchall()
        limits = self._budget_limits(row)
        held, consumed, used = self._budget_usage(reservations)
        available = self._budget_available(limits, used)
        lifecycle = str(row["state"] or "open")
        health = str((row["health"] if "health" in row.keys() else "ok") or "ok")
        return {
            "budget_id": str(row["budget_id"]),
            "project_id": str(row["project_id"] or "default"),
            "customer_id": str(row["customer_id"] or ""),
            "environment_id": str(row["environment_id"] or ""),
            "order_id": str(row["order_id"] or ""),
            "created_at": str(row["created_at"] or ""),
            "created_by": str(row["created_by"] or ""),
            # Closing is an administrative fact and health is an operational
            # one; the reported state is the one that stops spending first, but
            # both are kept so resolving a debt never reopens a closed run.
            "state": "closed" if lifecycle == "closed" else ("pending_verification" if health != "ok" else "open"),
            "lifecycle": lifecycle,
            "health": health,
            "state_reason": str(row["state_reason"] or ""),
            "limits": limits,
            "held": held,
            "consumed": consumed,
            "available": available,
        }

    def read_effect_budget_snapshot(
        self, budget_id: str, *, project_id: str, _account_store,
        expected_workspace_binding: str,
    ) -> dict[str, Any] | None:
        """Observe one original budget; never reserve, repair, or grant spending.

        A primary-key lookup exposes only a project-match flag for a foreign
        budget; SQL masks every DTO column before Python sees it. Missing means
        None, whereas a foreign/corrupt/unavailable source always refuses.
        """
        try:
            import time
            from .accounts import AccountStore
            from . import console_session_store as sessions

            def identifier(value):
                if (type(value) is not str or not 1 <= len(value.encode("utf-8")) <= 256
                        or value.strip() != value
                        or any(ch.isspace() or ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in value)):
                    raise ValueError

            identifier(budget_id)
            identifier(project_id)
            if (type(self) is not BusinessStore or type(_account_store) is not AccountStore
                    or _account_store.business is not self
                    or type(expected_workspace_binding) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", expected_workspace_binding) is None):
                raise ValueError
            raw_path = self.path
            if sessions.workspace_path_identity(raw_path) != expected_workspace_binding:
                raise ValueError
            deadline = time.monotonic() + 1.0
            result = None
            with BusinessStore._account_principal_ro_connection(self, _account_store=_account_store) as conn:
                if sessions.workspace_identity(raw_path, conn) != expected_workspace_binding:
                    raise ValueError
                conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64 * 1024)
                if conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) > 64 * 1024:
                    raise ValueError
                conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)

                def rows(sql, parameters=(), maximum=2):
                    cursor = sqlite3.Connection.cursor(conn)
                    try:
                        cursor.row_factory = None
                        cursor.execute(sql, parameters)
                        values = cursor.fetchmany(maximum + 1)
                        if len(values) > maximum or time.monotonic() > deadline:
                            raise ValueError
                        return values
                    finally:
                        cursor.close()

                def table_shape(table, required):
                    kind = rows("SELECT type,tbl_name FROM main.sqlite_schema WHERE name=? LIMIT 2", (table,), 1)
                    if kind != [("table", table)]:
                        raise ValueError
                    columns = rows(f"PRAGMA main.table_xinfo('{table}')", maximum=32)
                    if (any(len(row) != 7 or row[6] != 0 or type(row[1]) is not str for row in columns)
                            or len({row[1] for row in columns}) != len(columns)):
                        raise ValueError
                    indexed = {row[1]: row for row in columns}
                    for name, (affinity, primary) in required.items():
                        column = indexed.get(name)
                        if (column is None or column[2] != affinity or column[5] != primary
                                or (not primary and column[3] != 1)):
                            raise ValueError
                    return indexed

                budget_columns = table_shape("run_budgets", {
                    "budget_id": ("TEXT", 1), "project_id": ("TEXT", 0),
                    "customer_id": ("TEXT", 0), "environment_id": ("TEXT", 0),
                    "order_id": ("TEXT", 0), "created_at": ("TEXT", 0),
                    "created_by": ("TEXT", 0), "limits_json": ("TEXT", 0),
                    "state": ("TEXT", 0), "health": ("TEXT", 0), "state_reason": ("TEXT", 0),
                })
                reservation_columns = table_shape("budget_reservations", {
                    "reservation_id": ("TEXT", 1), "budget_id": ("TEXT", 0),
                    "kind": ("TEXT", 0), "amount": ("REAL", 0),
                    "settled_amount": ("REAL", 0), "state": ("TEXT", 0),
                })

                def verify_index(table, name, columns, expected, unique, origin):
                    indices = rows(f"PRAGMA main.index_list('{table}')", maximum=32)
                    matches = [row for row in indices if len(row) == 5 and row[1] == name]
                    if len(matches) != 1 or matches[0][2:] != (unique, origin, 0):
                        raise ValueError
                    keys = rows(f"PRAGMA main.index_xinfo('{name}')", maximum=8)
                    if any(len(row) != 6 for row in keys):
                        raise ValueError
                    actual = [row for row in keys if row[5] == 1]
                    wanted = [(seq, columns[column][0], column, 0, "BINARY", 1)
                              for seq, column in enumerate(expected)]
                    if actual != wanted:
                        raise ValueError

                verify_index("run_budgets", "sqlite_autoindex_run_budgets_1", budget_columns,
                             ("budget_id",), 1, "pk")
                verify_index("budget_reservations", "idx_budget_reservations_budget", reservation_columns,
                             ("budget_id", "state"), 0, "c")
                fields = {
                    "budget_id": 256, "project_id": 256, "customer_id": 256,
                    "environment_id": 256, "order_id": 256, "created_at": 128,
                    "created_by": 256, "state": 32, "health": 32,
                    "state_reason": 4096, "limits_json": 8192,
                }
                projection = ["CASE WHEN project_id = ? COLLATE BINARY THEN 1 ELSE 0 END"]
                for column, cap in fields.items():
                    projection.append(
                        f"CASE WHEN project_id = ? COLLATE BINARY AND typeof({column})='text' "
                        f"AND length(CAST({column} AS BLOB)) <= {cap} THEN {column} ELSE NULL END")
                found = rows("SELECT " + ",".join(projection)
                             + " FROM main.run_budgets INDEXED BY sqlite_autoindex_run_budgets_1 "
                               "WHERE budget_id=? COLLATE BINARY LIMIT 2",
                             (project_id,) * (len(fields) + 1) + (budget_id,), 1)
                if found:
                    value = found[0]
                    if len(value) != len(fields) + 1 or value[0] != 1:
                        raise ValueError
                    budget = dict(zip(fields, value[1:]))
                    if any(type(value) is not str for value in budget.values()):
                        raise ValueError
                    if budget["budget_id"] != budget_id or budget["project_id"] != project_id:
                        raise ValueError
                    if budget["state"] not in ("open", "closed") or budget["health"] not in ("ok", "pending_verification"):
                        raise ValueError
                    reservations = rows(
                        "SELECT CASE WHEN typeof(kind)='text' AND length(CAST(kind AS BLOB))<=32 THEN kind ELSE NULL END,"
                        "CASE WHEN typeof(amount) IN ('integer','real') THEN amount ELSE NULL END,"
                        "CASE WHEN typeof(settled_amount) IN ('integer','real') THEN settled_amount ELSE NULL END,"
                        "CASE WHEN typeof(state)='text' AND length(CAST(state AS BLOB))<=32 THEN state ELSE NULL END "
                        "FROM main.budget_reservations INDEXED BY idx_budget_reservations_budget "
                        "WHERE budget_id=? COLLATE BINARY LIMIT 10001", (budget_id,), 10000)
                    checked = []
                    for kind, amount, settled, state in reservations:
                        if kind not in BusinessStore.BUDGET_KINDS or state not in BusinessStore.RESERVATION_TRANSITIONS:
                            raise ValueError
                        for number in (amount, settled):
                            if type(number) not in (int, float):
                                raise ValueError
                            BusinessStore._budget_amount(kind, number)
                        checked.append({"kind": kind, "amount": amount, "settled_amount": settled, "state": state})
                    limits = BusinessStore._budget_limits(budget)
                    held, consumed, used = BusinessStore._budget_usage(checked)
                    available = BusinessStore._budget_available(limits, used)
                    lifecycle, health = budget["state"], budget["health"]
                    result = {key: budget[key] for key in (
                        "budget_id", "project_id", "customer_id", "environment_id", "order_id", "created_at", "created_by")}
                    result.update({
                        "state": "closed" if lifecycle == "closed" else ("pending_verification" if health != "ok" else "open"),
                        "lifecycle": lifecycle, "health": health, "state_reason": budget["state_reason"],
                        "limits": limits, "held": held, "consumed": consumed, "available": available,
                    })
                if (self.path is not raw_path or sessions.workspace_identity(raw_path, conn) != expected_workspace_binding
                        or time.monotonic() > deadline):
                    raise ValueError
            if self.path is not raw_path or sessions.workspace_path_identity(raw_path) != expected_workspace_binding:
                raise ValueError
        except Exception:
            raise RuntimeError("effect_budget_unavailable") from None
        return result

    def reserve_budget(
        self,
        budget_id: str,
        *,
        kind: str,
        amount: float,
        actor: str,
        idempotency_key: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """Hold an amount before the call. Refuses rather than overdrawing."""
        clean_kind, clean_amount, key, clean_note, fingerprint = (
            BusinessStore._normalize_budget_reservation_request(
                self, kind=kind, amount=amount, actor=actor,
                idempotency_key=idempotency_key, note=note,
            )
        )
        now = datetime.now().isoformat()
        reservation_id = f"rs-{uuid.uuid4().hex[:12]}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reservation_id, existing = BusinessStore._reserve_budget_core_on_connection(
                self, conn, budget_id=budget_id, clean_kind=clean_kind,
                clean_amount=clean_amount, actor=actor, key=key,
                clean_note=clean_note, fingerprint=fingerprint,
                now=now, reservation_id=reservation_id,
            )
            if existing:
                # Keep the original idempotent receipt read inside this with.
                return self.reservation(reservation_id) or {}
        return self.reservation(reservation_id) or {}

    def _normalize_budget_reservation_request(
        self,
        *,
        kind: str,
        amount: float,
        actor: str,
        idempotency_key: str,
        note: str,
    ) -> tuple[str, float, str, str, str]:
        """Original public normalization, not identity or spending permission."""
        clean_kind = str(kind or "").strip()
        if clean_kind not in self.BUDGET_KINDS:
            raise OrderTransitionError(f"unknown budget kind {clean_kind!r}")
        clean_amount = self._budget_amount(clean_kind, amount)
        if clean_amount <= 0:
            raise OrderTransitionError("a reservation must be for a positive amount")
        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        key = str(idempotency_key or "").strip()
        clean_note = str(note or "")
        fingerprint = self._reservation_fingerprint(clean_kind, clean_amount, clean_note)
        return clean_kind, clean_amount, key, clean_note, fingerprint

    def _reserve_budget_core_on_connection(
        self,
        conn: Any,
        *,
        budget_id: str,
        clean_kind: str,
        clean_amount: float,
        actor: str,
        key: str,
        clean_note: str,
        fingerprint: str,
        now: str,
        reservation_id: str,
    ) -> tuple[str, bool]:
        """Legacy SQL core; caller owns its transaction and receipt reading.

        Not a current-policy/Stop/employee gate. New consumers must use the
        strict owned wrapper, not this compatibility core.
        """
        row = conn.execute("SELECT * FROM run_budgets WHERE budget_id = ?", (str(budget_id or ""),)).fetchone()
        if row is None:
            raise OrderTransitionError(f"budget {budget_id!r} not found")
        limits = self._budget_limits(row)
        reservations = conn.execute(
            "SELECT kind, amount, settled_amount, state FROM budget_reservations WHERE budget_id = ? LIMIT 10001",
            (str(budget_id),),
        ).fetchall()
        _held, _consumed, used = self._budget_usage(reservations)
        available = self._budget_available(limits, used)
        if key:
            existing = conn.execute(
                "SELECT reservation_id, request_fingerprint FROM budget_reservations "
                "WHERE budget_id = ? AND idempotency_key = ?",
                (str(budget_id), key),
            ).fetchone()
            if existing is not None:
                if str(existing["request_fingerprint"] or "") != fingerprint:
                    raise OrderTransitionError(
                        f"idempotency key {key!r} was used for a different request; "
                        "a new request needs a new key"
                    )
                # The same request again: hand back the same hold. This is a
                # receipt, not a fresh licence to dispatch.
                return str(existing["reservation_id"]), True
        lifecycle = str(row["state"] or "open")
        health = str((row["health"] if "health" in row.keys() else "ok") or "ok")
        if lifecycle != "open":
            raise OrderTransitionError(
                f"budget {budget_id!r} is {lifecycle}"
                + (f": {row['state_reason']}" if row["state_reason"] else "")
            )
        if health != "ok":
            raise OrderTransitionError(
                f"budget {budget_id!r} is pending_verification"
                + (f": {row['state_reason']}" if row["state_reason"] else "")
            )
        if len(reservations) >= 10000:
            raise OrderTransitionError("Stored budget usage is invalid; refusing to spend")
        limit = limits.get(clean_kind)
        count_kind = clean_kind in self.BUDGET_COUNT_KINDS
        try:
            next_used = used[clean_kind] + (int(clean_amount) if count_kind else clean_amount)
            if not math.isfinite(next_used):
                raise ValueError
        except Exception:
            raise OrderTransitionError("Stored budget usage is invalid; refusing to spend") from None
        if limit is not None:
            if next_used > (int(limit) if count_kind else limit):
                raise BudgetExceeded(
                    f"{clean_kind}: asked for {clean_amount}, {available[clean_kind]} left of {limit}"
                )
        conn.execute(
            "INSERT INTO budget_reservations(reservation_id, budget_id, kind, amount, settled_amount, state, at, "
            "actor, idempotency_key, request_fingerprint, note) VALUES(?, ?, ?, ?, 0, 'held', ?, ?, ?, ?, ?)",
            (reservation_id, str(budget_id), clean_kind, clean_amount, now, str(actor), key, fingerprint, clean_note),
        )
        return reservation_id, False

    def _reserve_budget_owned_on_connection(
        self,
        conn: sqlite3.Connection,
        budget_id: str,
        *,
        kind: str,
        amount: int | float,
        actor: str,
        idempotency_key: str,
        expected_main: Path,
        note: str = "",
    ) -> tuple[str, bool]:
        """Prepare an original hold on one genuine owned RW transaction.

        No BEGIN/connect/commit/close or independent receipt query occurs.
        The tuple is a preparation/idempotency fact, not action authority.
        Host owns BEGIN IMMEDIATE, final source/policy/Stop/employee/finite
        ceiling checks, rollback/commit/close and every effect admission.
        Actor is a bounded display value, not authenticated identity.
        """
        if type(self) is not BusinessStore or type(conn) is not sqlite3.Connection:
            raise OrderTransitionError("Owned budget transaction invalid") from None
        fields = object.__getattribute__(self, "__dict__")
        protected = {
            "BUDGET_KINDS", "BUDGET_COUNT_KINDS", "RESERVATION_TRANSITIONS",
            "_budget_amount", "_reservation_fingerprint", "_budget_limits",
            "_budget_usage", "_budget_available",
        }
        if (type(fields) is not dict or len(fields) > 256
                or any(type(key) is not str for key in fields)
                or "path" not in fields or protected.intersection(fields)):
            raise OrderTransitionError("Owned budget transaction invalid") from None
        for value, maximum, empty, canonical in (
            (budget_id, 256, False, True), (kind, 32, False, True),
            (actor, 256, False, True), (idempotency_key, 256, False, True),
            (note, 4096, True, False),
        ):
            if (type(value) is not str or not value and not empty
                    or len(value) > maximum or "\x00" in value
                    or canonical and (value != value.strip()
                        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value))):
                raise OrderTransitionError("Owned budget request invalid") from None
            invalid_text = False
            try:
                invalid_text = len(value.encode("utf-8")) > maximum
            except UnicodeError:
                invalid_text = True
            if invalid_text:
                raise OrderTransitionError("Owned budget request invalid") from None
        if (type(amount) not in (int, float) or amount <= 0
                or type(amount) is int and amount > 2**63 - 1
                or type(amount) is float and not math.isfinite(amount)):
            raise OrderTransitionError("Owned budget request invalid") from None
        # Reuse the exact original normalization and original base algorithms.
        clean_kind, clean_amount, key, clean_note, fingerprint = (
            BusinessStore._normalize_budget_reservation_request(
                BusinessStore, kind=kind, amount=amount, actor=actor,
                idempotency_key=idempotency_key, note=note,
            )
        )
        from .current_owner_rw import _owned_rw_connection, _source_path

        cwd = Path.cwd()
        captured_path = fields["path"]
        if (type(expected_main) is not type(cwd) or not expected_main.is_absolute()
                or _source_path(expected_main, cwd) != expected_main
                or _source_path(captured_path, cwd) != expected_main
                or conn.row_factory is not sqlite3.Row):
            raise OrderTransitionError("Owned budget transaction invalid") from None
        _owned_rw_connection(conn, expected_main=expected_main)
        now = datetime.now().isoformat()
        reservation_id = f"rs-{uuid.uuid4().hex[:12]}"
        result = BusinessStore._reserve_budget_core_on_connection(
            BusinessStore, conn, budget_id=budget_id, clean_kind=clean_kind,
            clean_amount=clean_amount, actor=actor, key=key,
            clean_note=clean_note, fingerprint=fingerprint,
            now=now, reservation_id=reservation_id,
        )
        if (object.__getattribute__(self, "__dict__") is not fields
                or len(fields) > 256 or any(type(key) is not str for key in fields)
                or fields.get("path") is not captured_path or Path.cwd() != cwd
                or protected.intersection(fields)
                or _source_path(captured_path, cwd) != expected_main):
            raise OrderTransitionError("Owned budget transaction changed") from None
        _owned_rw_connection(conn, expected_main=expected_main)
        return result

    def reservation(self, reservation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM budget_reservations WHERE reservation_id = ?", (str(reservation_id or ""),)
            ).fetchone()
        if row is None:
            return None
        return {
            "reservation_id": str(row["reservation_id"]),
            "budget_id": str(row["budget_id"]),
            "kind": str(row["kind"]),
            "amount": float(row["amount"]),
            "settled_amount": float(row["settled_amount"]),
            "state": str(row["state"]),
            "at": str(row["at"]),
            "actor": str(row["actor"] or ""),
            "idempotency_key": str(row["idempotency_key"] or ""),
            "note": str(row["note"] or ""),
        }

    def _transition_reservation(
        self,
        conn: Any,
        reservation_id: str,
        *,
        expected: str,
        to_state: str,
        settled_amount: float | None = None,
        note: str | None = None,
    ) -> None:
        """Move a reservation, or lose to whoever moved it first.

        The update carries the state it expected. A late callback finds zero
        rows changed and is told what the reservation is now, instead of
        overwriting a decision someone already made.
        """
        allowed = self.RESERVATION_TRANSITIONS.get(expected, frozenset())
        if to_state not in allowed:
            raise OrderTransitionError(f"a {expected} reservation cannot become {to_state}")
        sets = ["state = ?"]
        params: list[Any] = [to_state]
        if settled_amount is not None:
            sets.append("settled_amount = ?")
            params.append(float(settled_amount))
        if note is not None:
            sets.append("note = ?")
            params.append(str(note))
        params.extend([str(reservation_id), expected])
        cursor = conn.execute(
            f"UPDATE budget_reservations SET {', '.join(sets)} WHERE reservation_id = ? AND state = ?",
            tuple(params),
        )
        if cursor.rowcount != 1:
            current = conn.execute(
                "SELECT state FROM budget_reservations WHERE reservation_id = ?", (str(reservation_id),)
            ).fetchone()
            now_state = str(current["state"]) if current is not None else "missing"
            raise OrderTransitionError(
                f"reservation {reservation_id!r} is {now_state}, not {expected}; refusing to overwrite it"
            )

    def settle_reservation(self, reservation_id: str, *, actual: float | None = None) -> dict[str, Any]:
        """Turn a hold into a real charge once the cost is known."""
        record = self.reservation(reservation_id)
        if record is None:
            raise OrderTransitionError(f"reservation {reservation_id!r} not found")
        if record["state"] == "settled":
            return record
        if record["state"] == "unknown":
            # An unknown outcome is a question for a person, not a settlement:
            # going through the ordinary door would skip the verification the
            # unknown state exists to force.
            raise OrderTransitionError(
                "this reservation's outcome is unknown; resolve it with resolve_unknown_reservation"
            )
        charged = self._budget_amount(record["kind"], record["amount"] if actual is None else actual)
        with self._connect() as conn:
            self._transition_reservation(
                conn, reservation_id, expected="held", to_state="settled", settled_amount=charged,
            )
        return self.reservation(reservation_id) or {}

    def release_reservation(self, reservation_id: str, *, reason: str = "") -> dict[str, Any]:
        """Give the hold back: the call never happened."""
        record = self.reservation(reservation_id)
        if record is None:
            raise OrderTransitionError(f"reservation {reservation_id!r} not found")
        if record["state"] == "released":
            # Asked again after a write whose answer was lost (a busy ledger): it is already what was asked for.
            return record
        if record["state"] == "unknown":
            raise OrderTransitionError(
                "an unknown outcome cannot be released automatically; it needs a person to check"
            )
        with self._connect() as conn:
            self._transition_reservation(
                conn, reservation_id, expected="held", to_state="released",
                note=str(reason or record["note"]),
            )
        return self.reservation(reservation_id) or {}

    # ── attention: snoozing a blocker without touching it ──

    def snooze_blocker(
        self, checkpoint_id: str, *, project_id: str, until_at: str, actor: str, note: str = "",
    ) -> dict[str, Any]:
        """Hold the reminder for one blocker until a time; the blocker itself is untouched."""
        clean_id = str(checkpoint_id or "").strip()
        clean_until = str(until_at or "").strip()
        clean_actor = str(actor or "").strip()
        if not clean_id or not clean_until or not clean_actor:
            raise OrderTransitionError("a snooze needs a checkpoint, an until time and an actor")
        try:
            parsed = datetime.fromisoformat(clean_until)
        except ValueError as exc:
            raise OrderTransitionError(f"until_at must be an ISO timestamp: {clean_until!r}") from exc
        if parsed.tzinfo is not None:
            # Stored and compared as local wall-clock time, like every other
            # timestamp in this store; a zoned value from a browser is folded
            # into that instead of making the comparison in active_snoozes
            # raise on aware-versus-naive.
            clean_until = parsed.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
        row = {
            "project_id": str(project_id or "default"),
            "checkpoint_id": clean_id,
            "until_at": clean_until,
            "actor": clean_actor,
            "note": str(note or "").strip()[:500],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO blocker_snoozes(project_id, checkpoint_id, until_at, actor, note, created_at) "
                "VALUES (:project_id, :checkpoint_id, :until_at, :actor, :note, :created_at) "
                "ON CONFLICT(project_id, checkpoint_id) DO UPDATE SET until_at = excluded.until_at, "
                "actor = excluded.actor, note = excluded.note, created_at = excluded.created_at",
                row,
            )
        return row

    def unsnooze_blocker(self, checkpoint_id: str, *, project_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM blocker_snoozes WHERE project_id = ? AND checkpoint_id = ?",
                (str(project_id or "default"), str(checkpoint_id or "").strip()),
            )
            return cur.rowcount > 0

    def active_snoozes(self, *, project_id: str, now: datetime | None = None) -> dict[str, dict[str, Any]]:
        """Snoozes that have not expired, by checkpoint id. Expired ones are dropped."""
        moment = now or datetime.now()
        out: dict[str, dict[str, Any]] = {}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT checkpoint_id, until_at, actor, note, created_at FROM blocker_snoozes WHERE project_id = ?",
                (str(project_id or "default"),),
            ).fetchall()
            expired: list[str] = []
            for row in rows:
                try:
                    until = datetime.fromisoformat(str(row["until_at"]))
                except ValueError:
                    expired.append(str(row["checkpoint_id"]))
                    continue
                if until.tzinfo is not None:
                    until = until.astimezone().replace(tzinfo=None)
                if until <= moment:
                    expired.append(str(row["checkpoint_id"]))
                    continue
                out[str(row["checkpoint_id"])] = {
                    "until_at": str(row["until_at"]), "actor": str(row["actor"]),
                    "note": str(row["note"]), "created_at": str(row["created_at"]),
                }
            for checkpoint_id in expired:
                conn.execute(
                    "DELETE FROM blocker_snoozes WHERE project_id = ? AND checkpoint_id = ?",
                    (str(project_id or "default"), checkpoint_id),
                )
        return out

    def mark_budget_pending_verification(self, budget_id: str, *, reason: str, effect_id: str = "") -> dict[str, Any] | None:
        """Stop a budget taking new reservations until a person verifies.

        For the case where the thing whose outcome is unknown was not a
        reservation of this budget but an external write charged to the same
        run (A12): the effect journal knows the receipt was lost, the budget
        does not, and until now nothing connected them. A closed budget stays
        closed; a budget already on hold keeps its first reason. Returns the
        budget, or None when there is no such budget.
        """
        clean_id = str(budget_id or "").strip()
        # Said to the owner as it is (P-c): nothing parses this default.
        clean_reason = str(reason or "").strip() or "一次对外操作的结果不明，预算已挂起待核实。"
        if not clean_id:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT state, health FROM run_budgets WHERE budget_id = ?", (clean_id,)).fetchone()
            if row is None:
                return None
            if str(row["state"] or "open") != "open":
                return self.run_budget(clean_id)
            clean_effect = str(effect_id or "").strip()
            if clean_effect:
                # Each outstanding effect hold is its own row; a second hold no longer collapses into the
                # first (adversarial Regression case), so resolving one effect cannot free the budget while
                # another effect's hold is still open.
                conn.execute(
                    "INSERT OR REPLACE INTO budget_effect_holds (budget_id, effect_id, reason, created_at) VALUES (?, ?, ?, ?)",
                    (clean_id, clean_effect, clean_reason, datetime.now().isoformat()),
                )
            if str(row["health"] or "ok") == "ok":
                conn.execute(
                    "UPDATE run_budgets SET health = 'pending_verification', state_reason = ? WHERE budget_id = ?",
                    (clean_reason, clean_id),
                )
        return self.run_budget(clean_id)

    def release_budget_hold_for_effect(self, effect_id: str, *, reason: str) -> list[dict[str, Any]]:
        """A hold placed because one effect's outcome was unknown is lifted once that effect is
        resolved by independent evidence (a look-back, C10). Only holds naming this very effect are
        lifted -- a hold placed for another effect, or for an unknown reservation, stays -- and a
        closed budget stays closed. Returns the budgets released."""
        clean_id = str(effect_id or "").strip()
        if not clean_id:
            return []
        prefix = f"effect {clean_id} outcome unknown"
        released: list[dict[str, Any]] = []
        with self._connect() as conn:
            # Budgets this effect held: rows in the holds table, plus legacy holds that only stamped the
            # freeze reason (no row) before this table existed.
            candidates = {str(r["budget_id"]) for r in conn.execute(
                "SELECT budget_id FROM budget_effect_holds WHERE effect_id = ?", (clean_id,)).fetchall()}
            for row in conn.execute(
                "SELECT budget_id, state_reason FROM run_budgets WHERE health = 'pending_verification' AND state = 'open'",
            ).fetchall():
                if str(row["state_reason"] or "").startswith(prefix):
                    candidates.add(str(row["budget_id"]))
            conn.execute("DELETE FROM budget_effect_holds WHERE effect_id = ?", (clean_id,))
            for budget_id in candidates:
                state = conn.execute("SELECT state FROM run_budgets WHERE budget_id = ?", (budget_id,)).fetchone()
                if state is None or str(state["state"] or "open") != "open":
                    continue
                remaining_reason = self._budget_outstanding_reason(conn, budget_id)
                if remaining_reason is None:
                    conn.execute("UPDATE run_budgets SET health = 'ok', state_reason = '' WHERE budget_id = ?", (budget_id,))
                    released.append({"budget_id": budget_id, "released_because": str(reason or "").strip()[:300]})
                else:
                    # Something is still outstanding: the freeze stays, and its reason reflects what remains.
                    conn.execute("UPDATE run_budgets SET state_reason = ? WHERE budget_id = ?", (remaining_reason, budget_id))
        return released

    def _budget_outstanding_reason(self, conn: Any, budget_id: str) -> str | None:
        """A reason string if this budget still has any outstanding hold (unknown reservation or effect hold),
        or None when nothing is outstanding and the freeze may lift."""
        reservation = conn.execute(
            "SELECT note FROM budget_reservations WHERE budget_id = ? AND state = 'unknown' ORDER BY at LIMIT 1",
            (budget_id,)).fetchone()
        if reservation is not None:
            return str(reservation["note"] or "") or "一次付费调用的结果不明，预算已挂起待核实。"
        hold = conn.execute(
            "SELECT reason FROM budget_effect_holds WHERE budget_id = ? ORDER BY created_at LIMIT 1",
            (budget_id,)).fetchone()
        if hold is not None:
            return str(hold["reason"] or "") or "一次对外操作的结果不明，预算已挂起待核实。"
        return None

    def mark_reservation_unknown(self, reservation_id: str, *, reason: str) -> dict[str, Any]:
        """The call may or may not have happened, and may or may not have cost.

        The hold stays -- releasing it would invite a second paid attempt for
        something that might already have run -- and the budget stops taking
        new reservations until a person says what happened. A budget that was
        already closed stays closed; this only records that a debt is open.
        """
        record = self.reservation(reservation_id)
        if record is None:
            raise OrderTransitionError(f"reservation {reservation_id!r} not found")
        clean_reason = str(reason or "").strip() or "outcome unknown"
        with self._connect() as conn:
            if record["state"] != "unknown":
                self._transition_reservation(
                    conn, reservation_id, expected="held", to_state="unknown", note=clean_reason,
                )
            # Already unknown (asked again after a lost answer): the budget is still put on hold below.
            conn.execute(
                "UPDATE run_budgets SET health = 'pending_verification', state_reason = ? WHERE budget_id = ?",
                (clean_reason, str(record["budget_id"])),
            )
        return self.reservation(reservation_id) or {}

    def resolve_unknown_reservation(
        self,
        reservation_id: str,
        *,
        happened: bool,
        actual: float | None = None,
        actor: str,
    ) -> dict[str, Any]:
        """A person says what actually happened.

        Settling the debt clears the health flag once nothing is outstanding.
        It never reopens a budget somebody closed: a cancelled run stays
        cancelled, and paying its last bill does not restart it.
        """
        if type(happened) is not bool:
            raise OrderTransitionError("请明确选择这次调用是否已经发生。")
        if type(actual) is bool:
            raise OrderTransitionError("实际金额不能是真/假值。")
        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        record = self.reservation(reservation_id)
        if record is None:
            raise OrderTransitionError(f"reservation {reservation_id!r} not found")
        if record["state"] != "unknown":
            raise OrderTransitionError("only an unknown reservation needs resolving")
        charged = (
            self._budget_amount(record["kind"], record["amount"] if actual is None else actual)
            if happened else None
        )
        with self._connect() as conn:
            if happened:
                self._transition_reservation(
                    conn, reservation_id, expected="unknown", to_state="settled", settled_amount=charged,
                )
            else:
                self._transition_reservation(
                    conn, reservation_id, expected="unknown", to_state="released",
                )
            remaining_reason = self._budget_outstanding_reason(conn, str(record["budget_id"]))
            if remaining_reason is None:
                conn.execute(
                    "UPDATE run_budgets SET health = 'ok', state_reason = '' WHERE budget_id = ?",
                    (str(record["budget_id"]),),
                )
            else:
                conn.execute(
                    "UPDATE run_budgets SET state_reason = ? WHERE budget_id = ?",
                    (remaining_reason, str(record["budget_id"])),
                )
        return self.reservation(reservation_id) or {}

    def close_run_budget(
        self,
        budget_id: str,
        *,
        actor: str,
        reason: str = "",
        release_held: bool = True,
    ) -> dict[str, Any]:
        """Stop this run spending. Outstanding debts can still be settled.

        Closing means nothing more will be dispatched, so amounts still held
        are money that was never spent and go back by default -- leaving them
        would make a finished order look like it had eaten its budget.

        Unknown reservations are never touched: those are the ones that may
        already have cost something, and only a person can say.
        """
        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        released: list[str] = []
        with self._connect() as conn:
            conn.execute(
                "UPDATE run_budgets SET state = 'closed', state_reason = ? WHERE budget_id = ?",
                (str(reason or ""), str(budget_id)),
            )
            if release_held:
                rows = conn.execute(
                    "SELECT reservation_id FROM budget_reservations WHERE budget_id = ? AND state = 'held' "
                    "AND note NOT LIKE 'dispatch:priced-runtime.v1:%'",
                    (str(budget_id),),
                ).fetchall()
                for row in rows:
                    self._transition_reservation(
                        conn, str(row["reservation_id"]), expected="held", to_state="released",
                        note=f"budget closed: {reason or 'run finished'}",
                    )
                    released.append(str(row["reservation_id"]))
        budget = self.run_budget(budget_id) or {}
        budget["released_on_close"] = released
        return budget

    def ensure_order_allowance(self, order_id: str, *, project_id: str = "default", actor: str) -> dict[str, Any]:
        """The order's open allowance, or a new one without ceilings (model-routing phase 0, C12).

        Every launched order is held: an allowance without a ceiling still journals every paid call, holds the order
        when a call's outcome is unknown, and stops spending when the order ends. Read and written in one write
        transaction, so a recovery racing a retry cannot open two. A new one is on the order's timeline as
        ``run_budget.granted`` (``source: launch_default``), with the order's own customer and environment.
        """
        clean_order = str(order_id or "").strip()
        clean_actor = str(actor or "").strip()
        if not clean_order:
            raise OrderTransitionError("order_id is required")
        if not clean_actor:
            raise OrderTransitionError("actor is required")
        project = str(project_id or "default").strip() or "default"
        order = self.get_order(clean_order, project_id=project)
        if order is None:
            raise OrderTransitionError(f"order {clean_order!r} not found")
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            status = conn.execute("SELECT status FROM work_orders WHERE order_id = ?", (clean_order,)).fetchone()
            if status is not None and str(status["status"] or "") in {"closed", "rejected"}:
                # An order that ended while it was being launched (review R6): its run must not start, and an allowance
                # opened now would be one the order-end close has already passed and nothing would ever close.
                raise OrderTransitionError(f"order {clean_order} is {status['status']}; it gets no allowance and its run does not start")
            row = conn.execute(
                "SELECT budget_id FROM run_budgets WHERE order_id = ? AND project_id = ? AND state = 'open' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (clean_order, project),
            ).fetchone()
            if row is not None:
                budget_id = str(row["budget_id"])
            else:
                budget_id = f"bg-{uuid.uuid4().hex[:12]}"
                conn.execute(
                    "INSERT INTO run_budgets(budget_id, project_id, customer_id, environment_id, order_id, "
                    "created_at, created_by, limits_json, state, health) VALUES(?, ?, ?, ?, ?, ?, ?, '{}', 'open', 'ok')",
                    (budget_id, project, str(order.customer_id or ""), str(order.environment_id or ""), clean_order, now, clean_actor),
                )
                self._append_event(
                    conn, table="order_events", aggregate_column="order_id", aggregate_id=clean_order, at=now,
                    kind="run_budget.granted", actor=clean_actor, correlation_id=clean_order,
                    note={"budget_id": budget_id, "limits": {}, "source": "launch_default"},
                )
        return self.run_budget(budget_id) or {}

    def set_run_budget_ceilings(self, budget_id: str, *, limits: dict[str, Any] | None, actor: str, reason: str = "") -> dict[str, Any]:
        """Set an OPEN budget's ceilings in place (model-routing phase 0, C12): same budget id, so every task that
        spends from it is held by the new ceilings at once and nothing has to be re-pinned.

        ``limits`` is the whole new set; ``{}`` lifts every ceiling -- the order stays held, every call is still
        journaled. A ceiling below what is already used or held is refused: it would strand running work. A budget
        held for verification only has its ceilings lifted until the held call is settled. A monetary ceiling cannot
        be enforced on an order yet and is refused, as at launch.
        """
        clean_actor = str(actor or "").strip()
        if not clean_actor:
            raise OrderTransitionError("actor is required")
        wanted = self.clean_order_allowance(limits)
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM run_budgets WHERE budget_id = ?", (str(budget_id or ""),)).fetchone()
            if row is None:
                raise OrderTransitionError(f"budget {str(budget_id)!r} not found")
            if str(row["state"] or "open") == "closed":
                raise OrderTransitionError("this budget is closed and is not reopened; grant the order a new allowance instead")
            if wanted and str((row["health"] if "health" in row.keys() else "ok") or "ok") != "ok":
                raise OrderTransitionError(
                    "this budget is held for verification; say what happened to the held call first"
                    + (f" ({row['state_reason']})" if row["state_reason"] else "")
                )
            current = self._budget_limits(row)
            if wanted == current:
                raise OrderTransitionError("these are already this budget's ceilings" if wanted else "this budget has no ceiling to lift")
            reservations = conn.execute(
                "SELECT kind, amount, settled_amount, state FROM budget_reservations WHERE budget_id = ? LIMIT 10001",
                (str(budget_id),),
            ).fetchall()
            _held, _consumed, used = self._budget_usage(reservations)
            self._budget_available(wanted, used)
            for kind, value in wanted.items():
                kind_used = used[kind]
                if kind_used > value:
                    raise OrderTransitionError(f"{kind}: {kind_used:g} is already used or held; a ceiling of {float(value):g} would strand running work")
            conn.execute("UPDATE run_budgets SET limits_json = ? WHERE budget_id = ?", (json.dumps(wanted, sort_keys=True), str(budget_id)))
            order_id = str(row["order_id"] or "")
            if order_id:
                self._append_event(
                    conn, table="order_events", aggregate_column="order_id", aggregate_id=order_id, at=now,
                    kind="run_budget.lifted" if not wanted else "run_budget.ceilings_set", actor=clean_actor, correlation_id=order_id,
                    note={"budget_id": str(budget_id), "before": current, "after": wanted, "reason": str(reason or "").strip()[:400]},
                )
        return self.run_budget(str(budget_id)) or {}

    def raise_run_budget(self, budget_id: str, *, limits: dict[str, Any] | None, actor: str, reason: str = "") -> dict[str, Any]:
        """Raise the ceilings of an OPEN budget in place: same budget id, so nothing that spends from it has to be
        told, and there is no moment without a ceiling (closing and granting anew has one).

        Only up, and only ceilings that exist: lowering one under what is already held would strand running work, and
        adding a ceiling where there was none is a tightening -- a different decision. A closed budget is not reopened
        (grant a new one), and a budget held for verification is not raised past the call that put it on hold.
        """
        clean_actor = str(actor or "").strip()
        if not clean_actor:
            raise OrderTransitionError("actor is required")
        wanted = self.clean_budget_limits(limits)
        if not wanted:
            raise OrderTransitionError("name at least one ceiling to raise")
        now = datetime.now().isoformat()
        with self._connect() as conn:
            # Read, check and update as one write transaction (as reserve_budget does): a close or a hold that lands
            # between the read and the update would otherwise be raised past.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM run_budgets WHERE budget_id = ?", (str(budget_id or ""),)).fetchone()
            if row is None:
                raise OrderTransitionError(f"budget {str(budget_id)!r} not found")
            if str(row["state"] or "open") == "closed":
                raise OrderTransitionError("this budget is closed and is not reopened; grant the order a new allowance instead")
            if str((row["health"] if "health" in row.keys() else "ok") or "ok") != "ok":
                raise OrderTransitionError(
                    "this budget is held for verification; say what happened to the held call first"
                    + (f" ({row['state_reason']})" if row["state_reason"] else "")
                )
            current = self._budget_limits(row)
            for kind, value in wanted.items():
                if kind not in current:
                    raise OrderTransitionError(f"{kind} has no ceiling on this budget; there is nothing to raise")
                if value <= current[kind]:
                    raise OrderTransitionError(f"{kind} is already {current[kind]:g}; a raise has to be higher")
            after = {**current, **wanted}
            reservations = conn.execute(
                "SELECT kind, amount, settled_amount, state FROM budget_reservations WHERE budget_id = ? LIMIT 10001",
                (str(budget_id),),
            ).fetchall()
            _held, _consumed, used = self._budget_usage(reservations)
            self._budget_available(after, used)
            conn.execute("UPDATE run_budgets SET limits_json = ? WHERE budget_id = ?", (json.dumps(after, sort_keys=True), str(budget_id)))
            order_id = str(row["order_id"] or "")
            if order_id:
                self._append_event(
                    conn, table="order_events", aggregate_column="order_id", aggregate_id=order_id, at=now,
                    kind="run_budget.raised", actor=clean_actor, correlation_id=order_id,
                    note={"budget_id": str(budget_id), "before": {k: current[k] for k in wanted}, "after": dict(wanted),
                          "reason": str(reason or "").strip()[:400]},
                )
        return self.run_budget(str(budget_id)) or {}

    def unknown_reservations(self, *, budget_ids: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
        """The calls of these budgets whose outcome nobody knows: each holds its budget until a person says what
        happened. Listed by budget so a caller shows only what its viewer may see."""
        wanted = [str(item).strip() for item in budget_ids if str(item).strip()]
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT reservation_id, budget_id, kind, amount, note, at FROM budget_reservations "
                f"WHERE state = 'unknown' AND budget_id IN ({marks}) ORDER BY at, rowid", wanted,
            ).fetchall()
        return [
            {"reservation_id": str(row["reservation_id"]), "budget_id": str(row["budget_id"]), "kind": str(row["kind"]),
             "amount": float(row["amount"]), "note": str(row["note"] or ""), "at": str(row["at"] or "")}
            for row in rows
        ]

    def sweep_stale_reservations(
        self,
        *,
        older_than_minutes: int,
        actor: str,
        budget_id: str = "",
        reason: str = "",
    ) -> list[dict[str, Any]]:
        """Mark long-held reservations unknown, for a person to settle.

        A crash between "hold" and "settle" leaves an amount held for a call
        that may or may not have gone out. Releasing it automatically would
        invite paying twice; leaving it held forever silently shrinks the
        budget. So it becomes the question it actually is -- never an answer,
        and never on a timer: somebody has to run this deliberately.
        """
        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        if int(older_than_minutes) < 1:
            raise OrderTransitionError("a sweep needs an age; sweeping everything just held is not a diagnosis")
        cutoff = (datetime.now() - timedelta(minutes=int(older_than_minutes))).isoformat()
        clean_reason = str(reason or "").strip() or f"held for more than {int(older_than_minutes)} minutes"
        params: list[Any] = [cutoff]
        clause = ""
        if budget_id:
            clause = " AND budget_id = ?"
            params.append(str(budget_id))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT reservation_id FROM budget_reservations WHERE state = 'held' AND at < ?{clause}",
                tuple(params),
            ).fetchall()
        swept: list[dict[str, Any]] = []
        for row in rows:
            swept.append(self.mark_reservation_unknown(str(row["reservation_id"]), reason=clean_reason))
        return swept

    def mark_orphaned_holds(self, *, before: str, actor: str, reason: str) -> list[dict[str, Any]]:
        """Held reservations written before ``before`` by a process that is no longer running become unknown
        (model-routing phase 0, open point 10, owner ruling: fail closed).

        Called once by the console as it starts, while it holds the single-instance lock for this home and before any
        run resumes, with ``before`` = the moment it started: a hold from then is a call a crashed process may or may
        not have sent. Left held, it blocks nothing under an allowance without a ceiling and the resumed turn sends
        again; as unknown it holds its order until a person says what happened. Unlike ``sweep_stale_reservations``
        this has no age: every earlier hold is an orphan, because no other console can be running on this home.
        """
        if not str(actor or "").strip():
            raise OrderTransitionError("actor is required")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT reservation_id FROM budget_reservations WHERE state = 'held' AND at < ?", (str(before),),
            ).fetchall()
        return [self.mark_reservation_unknown(str(row["reservation_id"]), reason=str(reason or "held by a process that stopped"))
                for row in rows]

    def list_run_budgets(self, *, project_id: str | None = None, customer_ids: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if project_id:
                # Asked per project on the order list now (S0-A): only that project's rows, not every budget of every
                # project read one by one and then dropped.
                rows = conn.execute("SELECT budget_id FROM run_budgets WHERE project_id = ? ORDER BY created_at DESC, rowid DESC",
                                    (str(project_id),)).fetchall()
            else:
                rows = conn.execute("SELECT budget_id FROM run_budgets ORDER BY created_at DESC, rowid DESC").fetchall()
        budgets = [self.run_budget(str(row["budget_id"])) or {} for row in rows]
        if project_id:
            budgets = [b for b in budgets if b.get("project_id") == str(project_id)]
        if customer_ids:
            allowed = {str(c).strip() for c in customer_ids if str(c).strip()}
            budgets = [b for b in budgets if str(b.get("customer_id", "")) in allowed]
        return budgets

    # ── reusable solution blueprints ──
    #
    # An FDE plan proved on one order is the asset the next customer pays
    # for.  A blueprint is a named snapshot of the *editable* plan only:
    # release status, trace and acceptance belong to the order that earned
    # them and are never carried over.
    BLUEPRINT_PLAN_FIELDS = ("as_is", "to_be", "uat", "sla", "rollback", "roi")

    @classmethod
    def authored_plan(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """The parts of an FDE plan a person actually wrote.

        An order with no plan still projects scaffolding -- ``uat`` carries
        ``status: not_defined`` and ``rollback`` carries ``available: False``
        -- so "non-empty" would call an untouched order a reusable asset.
        Only authored content travels.
        """
        authored: dict[str, Any] = {}
        for name in cls.BLUEPRINT_PLAN_FIELDS:
            value = payload.get(name)
            if name in {"as_is", "to_be"}:
                if str(value or "").strip():
                    authored[name] = str(value).strip()
                continue
            if not isinstance(value, dict):
                continue
            if name == "uat":
                criteria = [str(c).strip() for c in list(value.get("criteria", []) or []) if str(c).strip()]
                if criteria:
                    authored[name] = {**value, "criteria": criteria}
                continue
            if name == "rollback":
                if str(value.get("plan", "") or "").strip() or value.get("available") is True:
                    authored[name] = dict(value)
                continue
            if any(v not in (None, "", [], {}) for v in value.values()):
                authored[name] = dict(value)
        return authored

    def save_solution_blueprint(
        self,
        order_id: str,
        *,
        name: str,
        actor: str,
        note: str = "",
        workflow_version: str = "",
        project_id: str = "default",
    ) -> dict[str, Any]:
        clean_order = str(order_id or "").strip()
        clean_name = str(name or "").strip()
        clean_actor = str(actor or "").strip()
        if not clean_order:
            raise OrderTransitionError("order_id is required")
        if not clean_name:
            raise OrderTransitionError("blueprint name is required")
        if not clean_actor:
            raise OrderTransitionError("actor is required")
        order = self.get_order(clean_order, project_id=project_id)
        if order is None:
            raise OrderTransitionError(f"order {clean_order!r} not found")
        plane = self.fde_control_plane(clean_order, project_id=project_id)
        if plane is None:
            raise OrderTransitionError(f"order {clean_order!r} has no FDE plan to save")
        plan = self.authored_plan(plane.to_payload())
        if not plan:
            raise OrderTransitionError("the FDE plan is empty; nothing to reuse")
        payload = plane.to_payload()
        blueprint_id = f"bp-{uuid.uuid4().hex[:12]}"
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO solution_blueprints(blueprint_id, name, created_at, created_by, source_order_id, "
                "source_customer_id, workflow_id, workflow_version, plan_json, note) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    blueprint_id, clean_name, now, clean_actor, clean_order,
                    str(payload.get("customer_id", "") or ""), str(getattr(order, "workflow_id", "") or ""),
                    str(workflow_version or "").strip(),
                    json.dumps(plan, ensure_ascii=False), str(note or "").strip(),
                ),
            )
            self._append_event(
                conn,
                table="order_events",
                aggregate_column="order_id",
                aggregate_id=clean_order,
                at=now,
                kind="solution_blueprint.saved",
                actor=clean_actor,
                note={"blueprint_id": blueprint_id, "name": clean_name},
                correlation_id=clean_order,
            )
        return self.solution_blueprint(blueprint_id) or {}

    def acceptance_outcomes_for_version(self, workflow_id: str, version: str) -> list[dict[str, Any]]:
        """Customer verdicts on the orders that ran one workflow version.

        An order records the workflow version when its acceptance criteria
        are frozen (``acceptance.criteria_defined``), so that event is the
        join. An order with no verdict yet is reported as ``pending``: a
        missing verdict is not a good one.
        """
        clean_workflow = str(workflow_id or "").strip()
        clean_version = str(version or "").strip()
        if not clean_workflow or not clean_version:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT o.order_id AS order_id, o.customer_id AS customer_id, o.customer_name AS customer_name, "
                "e.at AS defined_at, e.note_json AS note_json "
                "FROM order_events e JOIN work_orders o ON o.order_id = e.order_id "
                "WHERE e.kind = 'acceptance.criteria_defined' AND o.workflow_id = ? ORDER BY e.at",
                (clean_workflow,),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                try:
                    note = json.loads(row["note_json"] or "{}")
                except ValueError:
                    note = {}
                if str((note or {}).get("workflow_version", "") or "") != clean_version:
                    continue
                verdict = conn.execute(
                    "SELECT kind, at FROM order_events WHERE order_id = ? AND kind LIKE 'acceptance.%' "
                    "AND kind != 'acceptance.criteria_defined' ORDER BY id DESC LIMIT 1",
                    (str(row["order_id"]),),
                ).fetchone()
                status = str(verdict["kind"]).split(".", 1)[1] if verdict is not None else "pending"
                # The customer's explicit redistribution grants for this exact version, if any (identity only).
                grants = conn.execute(
                    "SELECT note_json FROM order_events WHERE order_id = ? AND kind = ? ORDER BY id",
                    (str(row["order_id"]), self.REDISTRIBUTION_EVENT),
                ).fetchall()
                authorized: list[dict[str, str]] = []
                for grant in grants:
                    try:
                        grant_note = json.loads(grant["note_json"] or "{}")
                    except ValueError:
                        grant_note = {}
                    if str((grant_note or {}).get("version", "") or "") != clean_version:
                        continue
                    authorized.append({
                        "candidate_fingerprint": str(grant_note.get("candidate_fingerprint", "") or ""),
                        "manifest_sha256": str(grant_note.get("manifest_sha256", "") or ""),
                    })
                out.append({
                    "order_id": str(row["order_id"]),
                    "customer_id": str(row["customer_id"] or ""),
                    "customer_name": str(row["customer_name"] or ""),
                    "defined_at": str(row["defined_at"] or ""),
                    "status": status,
                    "at": str(verdict["at"]) if verdict is not None else "",
                    "redistribution": authorized,
                })
        return out

    def solution_blueprint(self, blueprint_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM solution_blueprints WHERE blueprint_id = ?", (str(blueprint_id or ""),)
            ).fetchone()
        return self._blueprint_row(row) if row is not None else None

    def list_solution_blueprints(self, *, customer_ids: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """Newest first.  ``customer_ids`` narrows to blueprints drawn from
        those customers' orders, for an account bound to them."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM solution_blueprints ORDER BY created_at DESC, rowid DESC").fetchall()
        blueprints = [self._blueprint_row(row) for row in rows]
        if not customer_ids:
            return blueprints
        allowed = {str(c).strip() for c in customer_ids if str(c).strip()}
        return [b for b in blueprints if str(b.get("source_customer_id", "") or "") in allowed]

    @staticmethod
    def _blueprint_row(row: Any) -> dict[str, Any]:
        try:
            plan = json.loads(row["plan_json"] or "{}")
        except ValueError:
            plan = {}
        return {
            "blueprint_id": str(row["blueprint_id"]),
            "name": str(row["name"]),
            "created_at": str(row["created_at"]),
            "created_by": str(row["created_by"] or ""),
            "source_order_id": str(row["source_order_id"] or ""),
            "source_customer_id": str(row["source_customer_id"] or ""),
            "workflow_id": str(row["workflow_id"] or ""),
            # Older rows predate the column; an unknown version is empty,
            # never a guess.
            "workflow_version": str((row["workflow_version"] if "workflow_version" in row.keys() else "") or ""),
            "note": str(row["note"] or ""),
            "plan": plan if isinstance(plan, dict) else {},
        }

    def apply_solution_blueprint(
        self,
        blueprint_id: str,
        order_id: str,
        *,
        actor: str,
        target_workflow_version: str = "",
        project_id: str = "default",
    ) -> WorkOrder:
        """Copy a blueprint's plan onto another order, recording where it came from.

        Reuse is only honest if the target order says which blueprint and
        which earlier order it inherited from, so the event carries both.
        """
        blueprint = self.solution_blueprint(blueprint_id)
        if blueprint is None:
            raise OrderTransitionError(f"blueprint {blueprint_id!r} not found")
        clean_order = str(order_id or "").strip()
        if blueprint["source_order_id"] == clean_order:
            raise OrderTransitionError("a blueprint cannot be applied to the order it came from")
        patch = self.authored_plan(dict(blueprint.get("plan", {}) or {}))
        if not patch:
            raise OrderTransitionError("the blueprint carries no plan to apply")
        order = self.update_fde_control_plane(clean_order, patch, actor=actor, project_id=project_id)
        with self._connect() as conn:
            self._append_event(
                conn,
                table="order_events",
                aggregate_column="order_id",
                aggregate_id=clean_order,
                at=datetime.now().isoformat(),
                kind="solution_blueprint.applied",
                actor=str(actor or "").strip(),
                note={
                    "blueprint_id": blueprint["blueprint_id"],
                    "name": blueprint["name"],
                    "source_order_id": blueprint["source_order_id"],
                    "source_customer_id": blueprint["source_customer_id"],
                    "fields": sorted(patch),
                    "workflow_version": blueprint.get("workflow_version", ""),
                    "target_workflow_version": str(target_workflow_version or ""),
                    # A plan proved on one version of the process is not
                    # automatically right for the next one. Recording the
                    # mismatch is what lets a person notice it later.
                    "workflow_version_changed": bool(
                        blueprint.get("workflow_version")
                        and str(target_workflow_version or "")
                        and blueprint.get("workflow_version") != str(target_workflow_version or "")
                    ),
                },
                correlation_id=clean_order,
            )
        return order

    @staticmethod
    def _order_filter(
        *,
        project_id: str | None,
        status: str | None,
        customer_id: str | None,
        environment_id: str | None,
    ) -> tuple[list[str], list[Any]]:
        """The WHERE clauses ``list_orders`` and ``count_orders`` share: one rule, so a count cannot disagree with
        the list it stands beside."""
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if customer_id is not None:
            clauses.append("customer_id = ?")
            params.append(str(customer_id or "").strip())
        if environment_id is not None:
            clauses.append("environment_id = ?")
            params.append(str(environment_id or "").strip())
        if project_id is not None:
            clean_project_id = str(project_id or "default").strip() or "default"
            clauses.append(
                "((json_valid(fields_json) = 1 AND json_extract(fields_json, '$.project_id') = ?) OR "
                "(? = 'default' AND (json_valid(fields_json) = 0 OR "
                "(json_valid(fields_json) = 1 AND (json_extract(fields_json, '$.project_id') IS NULL "
                "OR json_extract(fields_json, '$.project_id') = '')))))"
            )
            params.extend([clean_project_id, clean_project_id])
        return clauses, params

    def list_orders(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        customer_id: str | None = None,
        environment_id: str | None = None,
        limit: int = 200,
        before: str | None = None,
    ) -> list[WorkOrder]:
        """The newest orders that match, never more than ``ORDER_LIST_HARD_CAP`` of them.

        Nothing here says whether there were more. A caller that needs "all of them" asks a narrower question
        (by status, by id, by lineage) or counts with ``count_orders``; one that can only show a window says so.

        ``before`` (4.7-A08) is a cursor from :meth:`list_orders_page`: the orders strictly older than it, newest first
        by ``(created_at, order_id)``. Left out (the default), the list is exactly what it always was.
        """
        clauses, params = self._order_filter(
            project_id=project_id, status=status, customer_id=customer_id, environment_id=environment_id,
        )
        if before is not None:
            return self._paged_orders(clauses, params, before=before, count=max(1, min(int(limit), ORDER_LIST_HARD_CAP)))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), ORDER_LIST_HARD_CAP)))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM work_orders{where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._row_to_order(r) for r in rows]

    def _paged_orders(self, clauses: list[str], params: list[Any], *, before: str | None, count: int) -> list[WorkOrder]:
        """Newest first by ``(created_at, order_id)`` -- a total order, since ``order_id`` is unique -- starting after
        ``before`` when given. Every page, the first one included, is read in this one order, so walking the cursors
        lists each order once: two orders made in the same instant still sit on one side of a page boundary each."""
        where_parts = list(clauses)
        values = list(params)
        if before is not None:
            created_at, order_id = parse_order_cursor(before)
            where_parts.append("(created_at < ? OR (created_at = ? AND order_id < ?))")
            values.extend([created_at, created_at, order_id])
        where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
        values.append(max(1, int(count)))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM work_orders{where} ORDER BY created_at DESC, order_id DESC LIMIT ?",
                tuple(values),
            ).fetchall()
        return [self._row_to_order(r) for r in rows]

    def list_orders_page(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        customer_id: str | None = None,
        environment_id: str | None = None,
        limit: int = 200,
        before: str | None = None,
    ) -> tuple[list[WorkOrder], str]:
        """One page of orders and the cursor of the next one (``''`` when there is none) -- 4.7-A08 「查看更早的工单」.

        At most ``limit`` orders (never more than ``ORDER_LIST_HARD_CAP``). One row past the page is read to know
        whether another page exists, so a cursor is handed out only when there really is something older.
        """
        size = max(1, min(int(limit), ORDER_LIST_HARD_CAP))
        clauses, params = self._order_filter(
            project_id=project_id, status=status, customer_id=customer_id, environment_id=environment_id,
        )
        rows = self._paged_orders(clauses, params, before=before, count=size + 1)
        page = rows[:size]
        return page, (order_cursor(page[-1]) if len(rows) > size and page else "")

    def count_orders(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        customer_id: str | None = None,
        environment_id: str | None = None,
    ) -> int:
        """How many orders match -- the whole number, not the length of a capped list."""
        clauses, params = self._order_filter(
            project_id=project_id, status=status, customer_id=customer_id, environment_id=environment_id,
        )
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM work_orders{where}", tuple(params)).fetchone()
        return int(row[0] if row else 0)

    # ── 4.7-A08 批量自动化 · 分发授权单 ──
    def distribution_authorizations_ready(self) -> bool:
        """Whether this database has the authorization table (business schema version 2). Read only: an existing
        database gets the table when the owner runs ``ampuly db-version --apply``, never from here."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (DISTRIBUTION_AUTHORIZATION_TABLE,),
            ).fetchone()
        return row is not None

    @staticmethod
    def distribution_not_ready_text() -> str:
        return ("分发授权单还没启用：这台机器的业务库还是旧的数据格式，系统不会自己去改它。需要你先停掉控制台，"
                "运行数据页上给出的那条命令（改之前会自动备份，出错会自动退回），再重新打开控制台。")

    def _require_distribution_table(self) -> None:
        if not self.distribution_authorizations_ready():
            raise DistributionAuthorizationError(self.distribution_not_ready_text())

    @staticmethod
    def _distribution_digest(row: Any) -> str:
        """What was drafted, as one fingerprint: an approval is of exactly this content, nothing edited in since."""
        def listed(name: str) -> list[str]:
            try:
                value = json.loads(row[name] or "[]")
            except (TypeError, ValueError):
                return []
            return [str(item) for item in value] if isinstance(value, list) else []

        material = {
            "project_id": str(row["project_id"] or ""), "order_id": str(row["order_id"] or ""),
            "batch_ref": str(row["batch_ref"] or ""), "customer_id": str(row["customer_id"] or ""),
            "environment_id": str(row["environment_id"] or ""), "targets": listed("targets_json"),
            "channels": listed("channels_json"), "actions": listed("actions_json"), "valid_days": int(row["valid_days"] or 0),
        }
        return hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _distribution_payload(row: Any, *, now: datetime) -> dict[str, Any]:
        def listed(name: str) -> list[str]:
            try:
                value = json.loads(row[name] or "[]")
            except (TypeError, ValueError):
                return []
            return [str(item) for item in value] if isinstance(value, list) else []

        status = str(row["status"] or "")
        expires = _distribution_when(row["expires_at"])
        shown = expires.strftime("%Y-%m-%d %H:%M") if expires else ""
        state = "expired" if status == "approved" and (expires is None or expires <= now) else status
        state_zh = {
            "draft": "草稿，还没批准",
            "approved": f"已批准，{shown} 前有效",
            "expired": f"已过期（{shown} 到期）" if shown else "已过期",
            "revoked": "已撤销",
        }.get(state, state)
        actions = listed("actions_json")
        channels = listed("channels_json")
        return {
            "authorization_id": str(row["authorization_id"]),
            "project_id": str(row["project_id"] or "default"),
            "order_id": str(row["order_id"] or ""),
            "batch_ref": str(row["batch_ref"] or ""),
            "customer_id": str(row["customer_id"] or ""),
            "environment_id": str(row["environment_id"] or ""),
            "targets": listed("targets_json"),
            "channels": channels,
            # D1: the sites by the names the owner reads (快手); a host without a name, or a pre-D1 word, as it is.
            "channels_zh": [distribution_channel_name(channel) for channel in channels],
            "actions": actions,
            "actions_zh": [DISTRIBUTION_ACTION_LABELS.get(action, action) for action in actions],
            "valid_days": int(row["valid_days"] or 0),
            "expires_at": str(row["expires_at"] or ""),
            "status": status,
            "state": state,
            "state_zh": state_zh,
            "content_sha256": str(row["content_sha256"] or ""),
            "note": str(row["note"] or ""),
            "created_by": str(row["created_by"] or ""),
            "created_at": str(row["created_at"] or ""),
            "approved_by": str(row["approved_by"] or ""),
            "approved_at": str(row["approved_at"] or ""),
            "revoked_by": str(row["revoked_by"] or ""),
            "revoked_at": str(row["revoked_at"] or ""),
            "revoke_reason": str(row["revoke_reason"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def list_distribution_authorizations(
        self, order_id: str, *, project_id: str = "default", now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """This order's authorizations, newest first -- all of them (an order has a handful, never a page's worth)."""
        self._require_distribution_table()
        when = now or datetime.now()
        with self._connect() as conn:
            rows = self._distribution_rows(conn, order_id, project_id)
        return [self._distribution_payload(row, now=when) for row in rows]

    @staticmethod
    def _distribution_rows(conn: sqlite3.Connection, order_id: str, project_id: str) -> list[Any]:
        clean_project = str(project_id or "default").strip() or "default"
        return conn.execute(
            "SELECT * FROM distribution_authorizations WHERE project_id = ? AND order_id = ? "
            "ORDER BY created_at DESC, authorization_id DESC",
            (clean_project, str(order_id or "").strip()),
        ).fetchall()

    def distribution_authorization(
        self, authorization_id: str, *, project_id: str = "default", now: datetime | None = None,
    ) -> dict[str, Any] | None:
        self._require_distribution_table()
        clean_project = str(project_id or "default").strip() or "default"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM distribution_authorizations WHERE authorization_id = ? AND project_id = ?",
                (str(authorization_id or "").strip(), clean_project),
            ).fetchone()
        return self._distribution_payload(row, now=now or datetime.now()) if row is not None else None

    def draft_distribution_authorization(
        self,
        order_id: str,
        *,
        project_id: str = "default",
        targets: Any,
        channels: Any,
        actions: Any,
        valid_days: Any = DISTRIBUTION_AUTHORIZATION_DEFAULT_DAYS,
        batch_ref: str = "",
        note: str = "",
        actor: str = "",
        sites: Any = None,
    ) -> dict[str, Any]:
        """Write a draft. Nothing is authorized until the owner approves it; a draft is never edited, only revoked.

        The order's customer and environment are read from the order, never taken from the request.

        ``sites`` (D1, policy): the hosts the order's batch steps work on, read by the caller from
        the order's flow. The channels are chosen among them and stored as hosts; without them nothing can be chosen,
        so the draft is refused (never a free word such as 「快手」 written down unchecked).
        """
        self._require_distribution_table()
        clean_project = str(project_id or "default").strip() or "default"
        order = self.get_order(str(order_id or "").strip(), project_id=clean_project)
        if order is None:
            raise DistributionAuthorizationError("找不到这张工单（或者它不属于这个项目）。")
        if order.status in ENDED_ORDER_STATUSES:
            raise DistributionAuthorizationError("这张工单已经结束，不能再为它起草分发授权单。")
        from ampuly.computer_use.browser_bridge import ITEM_VALUE_MAX

        cut: list[str] = []
        clean_targets = _clean_distribution_list(targets, what="要发的对象", most=DISTRIBUTION_TARGETS_MAX,
                                                 longest=ITEM_VALUE_MAX, cut=cut)
        clean_channels = _clean_distribution_channels(channels, sites)
        by_label = {label: code for code, label in DISTRIBUTION_ACTION_LABELS.items()}
        named = [by_label.get(item, item) for item in _clean_distribution_list(
            actions, what="允许的动作", most=len(DISTRIBUTION_ACTIONS) * 2, longest=20)]
        unknown = [item for item in named if item not in DISTRIBUTION_ACTIONS]
        if unknown:
            raise DistributionAuthorizationError(f"允许的动作只能是上传、发布、回查，这里有不认识的：{'、'.join(unknown)}。")
        clean_actions = [code for code in DISTRIBUTION_ACTIONS if code in named]
        if isinstance(valid_days, bool):
            raise DistributionAuthorizationError("有效天数要写一个数字。")
        try:
            days = int(str(valid_days).strip())
        except (TypeError, ValueError) as exc:
            raise DistributionAuthorizationError("有效天数要写一个数字。") from exc
        if not 1 <= days <= DISTRIBUTION_AUTHORIZATION_MAX_DAYS:
            raise DistributionAuthorizationError(
                f"有效天数要在 1 到 {DISTRIBUTION_AUTHORIZATION_MAX_DAYS} 天之间；要发更久，到期后再开一张新的。")
        clean_ref = str(batch_ref or "").strip()
        if len(clean_ref) > 120 or any(ch in clean_ref for ch in "\r\n\t"):
            raise DistributionAuthorizationError("批次说明太长或带了换行（最多 120 个字）。")
        clean_note = str(note or "").strip()[:500]
        if cut:
            # LP2-A: said on the authorization itself, so whoever reads it knows why a long name ends early.
            said = f"有 {len(cut)} 个对象超过 {ITEM_VALUE_MAX} 个字，按批量步骤认条目的方式只记了前 {ITEM_VALUE_MAX} 个字。"
            clean_note = f"{clean_note}\n{said}" if clean_note else said
        now = datetime.now().isoformat()
        authorization_id = f"da-{uuid.uuid4().hex[:16]}"
        values = {
            "authorization_id": authorization_id, "project_id": clean_project, "order_id": order.order_id,
            "batch_ref": clean_ref, "customer_id": order.customer_id, "environment_id": order.environment_id,
            "targets_json": json.dumps(clean_targets, ensure_ascii=False),
            "channels_json": json.dumps(clean_channels, ensure_ascii=False),
            "actions_json": json.dumps(clean_actions, ensure_ascii=False), "valid_days": days,
        }
        digest = self._distribution_digest(values)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO distribution_authorizations
                   (authorization_id, project_id, order_id, batch_ref, customer_id, environment_id, targets_json,
                    channels_json, actions_json, valid_days, expires_at, status, content_sha256, note, created_by,
                    created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,'','draft',?,?,?,?,?)""",
                (authorization_id, clean_project, order.order_id, clean_ref, order.customer_id, order.environment_id,
                 values["targets_json"], values["channels_json"], values["actions_json"], days, digest, clean_note,
                 str(actor or "").strip(), now, now),
            )
            self._append_event(
                conn, table="order_events", aggregate_column="order_id", aggregate_id=order.order_id, at=now,
                kind="distribution_authorization.drafted", actor=str(actor or "").strip(),
                note={"authorization_id": authorization_id, "targets": len(clean_targets), "channels": clean_channels,
                      "actions": clean_actions, "valid_days": days, "content_sha256": digest,
                      **({"targets_cut": len(cut)} if cut else {})},
                correlation_id=order.order_id,
            )
        found = self.distribution_authorization(authorization_id, project_id=clean_project)
        assert found is not None
        return found

    def approve_distribution_authorization(
        self, authorization_id: str, *, project_id: str = "default", actor: str = "", now: datetime | None = None,
    ) -> dict[str, Any]:
        """Draft -> approved, valid from now for the days it was drafted with. One live authorization per order:
        a second one is refused until the first is revoked (or has expired), never silently replaced."""
        self._require_distribution_table()
        clean_project = str(project_id or "default").strip() or "default"
        clean_id = str(authorization_id or "").strip()
        when = now or datetime.now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM distribution_authorizations WHERE authorization_id = ? AND project_id = ?",
                (clean_id, clean_project),
            ).fetchone()
            if row is None:
                raise DistributionAuthorizationError("找不到这张分发授权单。")
            if row["status"] == "approved":
                raise DistributionAuthorizationError("这张授权单已经批准过了。")
            if row["status"] == "revoked":
                raise DistributionAuthorizationError("这张授权单已经撤销，不能再批准；要发的话请重新起草一张。")
            order_row = conn.execute("SELECT * FROM work_orders WHERE order_id = ?", (row["order_id"],)).fetchone()
            order = self._row_to_order(order_row) if order_row is not None else None
            if order is None or order.project_id != clean_project:
                raise DistributionAuthorizationError("这张授权单对应的工单已经不在这个项目里了。")
            if order.status in ENDED_ORDER_STATUSES:
                raise DistributionAuthorizationError("这张工单已经结束，不能再批准它的授权单。")
            if self._distribution_digest(row) != str(row["content_sha256"] or ""):
                raise DistributionAuthorizationError("这张授权单的内容和起草时记下的对不上，不能批准；请撤销后重新起草。")
            others = conn.execute(
                "SELECT * FROM distribution_authorizations WHERE project_id = ? AND order_id = ? "
                "AND status = 'approved' AND authorization_id != ?",
                (clean_project, row["order_id"], clean_id),
            ).fetchall()
            for other in others:
                ends = _distribution_when(other["expires_at"])
                if ends is not None and ends > when:
                    raise DistributionAuthorizationError(
                        f"这张工单已经有一张生效中的授权单（{_distribution_drafted(other['created_at'])}，{ends.strftime('%Y-%m-%d %H:%M')} 前有效）；"
                        "先撤销它，再批准新的。")
            expires = (when + timedelta(days=int(row["valid_days"] or 0))).isoformat()
            cursor = conn.execute(
                "UPDATE distribution_authorizations SET status = 'approved', approved_by = ?, approved_at = ?, "
                "expires_at = ?, updated_at = ? WHERE authorization_id = ? AND status = 'draft'",
                (str(actor or "").strip(), when.isoformat(), expires, when.isoformat(), clean_id),
            )
            if cursor.rowcount != 1:
                raise DistributionAuthorizationError("这张授权单刚被处理过，请刷新后再看。")
            self._append_event(
                conn, table="order_events", aggregate_column="order_id", aggregate_id=str(row["order_id"]),
                at=when.isoformat(), kind="distribution_authorization.approved", actor=str(actor or "").strip(),
                note={"authorization_id": clean_id, "expires_at": expires, "content_sha256": str(row["content_sha256"] or "")},
                correlation_id=str(row["order_id"]),
            )
        found = self.distribution_authorization(clean_id, project_id=clean_project, now=when)
        assert found is not None
        return found

    def revoke_distribution_authorization(
        self, authorization_id: str, *, project_id: str = "default", actor: str = "", reason: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Draft or approved -> revoked, at once: the next check refuses it. Revoked is final."""
        self._require_distribution_table()
        clean_project = str(project_id or "default").strip() or "default"
        clean_id = str(authorization_id or "").strip()
        clean_reason = str(reason or "").strip()[:300]
        when = now or datetime.now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM distribution_authorizations WHERE authorization_id = ? AND project_id = ?",
                (clean_id, clean_project),
            ).fetchone()
            if row is None:
                raise DistributionAuthorizationError("找不到这张分发授权单。")
            if row["status"] == "revoked":
                raise DistributionAuthorizationError("这张授权单已经撤销过了。")
            conn.execute(
                "UPDATE distribution_authorizations SET status = 'revoked', revoked_by = ?, revoked_at = ?, "
                "revoke_reason = ?, updated_at = ? WHERE authorization_id = ?",
                (str(actor or "").strip(), when.isoformat(), clean_reason, when.isoformat(), clean_id),
            )
            self._append_event(
                conn, table="order_events", aggregate_column="order_id", aggregate_id=str(row["order_id"]),
                at=when.isoformat(), kind="distribution_authorization.revoked", actor=str(actor or "").strip(),
                note={"authorization_id": clean_id, "was": str(row["status"] or ""), "reason": clean_reason},
                correlation_id=str(row["order_id"]),
            )
        found = self.distribution_authorization(clean_id, project_id=clean_project, now=when)
        assert found is not None
        return found

    def distribution_authorization_gate(
        self, order_id: str, *, project_id: str = "default", targets: list[str] | None = None,
        steps: list[dict[str, Any]] | None = None, now: datetime | None = None,
    ) -> dict[str, Any]:
        """May this order send its list out now? Read only.

        Yes only with an approved authorization that has not expired or been revoked -- and, when the order's current
        list is given as ``targets``, one that names every row of it (a list that grew after the approval is not
        covered by it). ``why_zh`` is the sentence the owner reads either way.

        ``steps`` (D1, policy): the batch steps that will send the list out, each
        ``{"name", "sites", "action"}`` as the step's own connector declares them (api.batch_steps_of at launch,
        browser_connector.authorized_rows on a read of the list). When given, a step that has not said which of
        上传 / 发布 / 回查 it does is refused outright (``action_missing``), and the authorization must name every site
        of every step among its channels (``channel_not_covered``; a channel written before D1 as a word such as 「快手」
        is no site and covers nothing) and every step's action among its actions (``action_not_covered``). These are
        judged before the rows, so an authorization that does not cover the step stops the list -- it never narrows
        it the way ``targets_changed`` does.

        Asked at launch and again by the batch step's item resolver on every read of its list (LP2-A,
        browser_connector.bind_order_inputs), so it is one connection and one short read of this order's rows.
        """
        when = now or datetime.now()
        with self._connect() as conn:
            ready = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (DISTRIBUTION_AUTHORIZATION_TABLE,),
            ).fetchone() is not None
            found = self._distribution_rows(conn, order_id, project_id) if ready else []
        if not ready:
            return {"ok": False, "reason": "not_ready", "why_zh": self.distribution_not_ready_text(), "authorization": None}
        checked = [_distribution_step(step) for step in steps] if steps is not None else None
        for step in checked or []:
            if step["action"] not in DISTRIBUTION_ACTIONS:
                lead = f"「{step['name']}」" if step["name"] else ""
                return {"ok": False, "reason": "action_missing", "why_zh": lead + DISTRIBUTION_ACTION_MISSING_ZH,
                        "authorization": None}
        rows = [self._distribution_payload(row, now=when) for row in found]
        live = [row for row in rows if row["state"] == "approved"]
        if not live:
            if any(row["state"] == "expired" for row in rows):
                reason, why = "expired", "这张工单的分发授权单已经过期了，要接着发请重新起草一张并批准。"
            elif any(row["state"] == "draft" for row in rows):
                reason, why = "draft_only", "这张工单的分发授权单还是草稿，还没批准。"
            elif rows:
                reason, why = "revoked", "这张工单的分发授权单已经撤销了，要发请重新起草一张并批准。"
            else:
                reason, why = "missing", "这张工单还没有分发授权单。"
            return {"ok": False, "reason": reason, "why_zh": why, "authorization": None}
        current = live[0]
        for step in checked or []:
            refused = _distribution_step_not_covered(current, step)
            if refused is not None:
                return {"ok": False, "reason": refused[0], "why_zh": refused[1], "authorization": current}
        if targets is not None:
            named = set(current["targets"])
            missing = [item for item in dict.fromkeys(str(t).strip() for t in targets) if item and item not in named]
            if missing:
                # A row may be named by up to 200 characters; the sentence shows the start of each.
                shown = "、".join(item if len(item) <= 30 else item[:30] + "…" for item in missing[:3]) + (
                    " 等" if len(missing) > 3 else "")
                return {
                    "ok": False, "reason": "targets_changed", "authorization": current,
                    "why_zh": (f"工单里的清单在授权之后变了：有 {len(missing)} 条不在已批准的授权单里（{shown}）。"
                               "请撤销旧的，按现在的清单重新起草并批准。"),
                }
        return {"ok": True, "reason": "approved", "authorization": current,
                "why_zh": f"已有批准的分发授权单（{_distribution_drafted(current['created_at'])}），{current['state_zh'].removeprefix('已批准，')}。"}

    def note_distribution_batch_stop(
        self, order_id: str, *, reason: str, why_zh: str, authorization_id: str = "", rows_left_out: int = 0,
    ) -> None:
        """One line on the order's timeline (LP2-A): a batch step read its list under the order's distribution
        authorization and got fewer rows (``rows_left_out`` the authorization does not name) or none (no live
        authorization, so the step stops at its next row). ``why_zh`` is what the owner reads there.

        The item resolver writes it once per change, not on every read; the stop never waits on this line.
        """
        clean = str(order_id or "").strip()
        if not clean:
            return
        kind = "distribution_authorization.batch_narrowed" if rows_left_out else "distribution_authorization.batch_stopped"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._append_event(
                conn, table="order_events", aggregate_column="order_id", aggregate_id=clean,
                at=datetime.now().isoformat(), kind=kind, actor="system",
                note={"note": str(why_zh or "")[:400], "reason": str(reason or ""),
                      **({"authorization_id": str(authorization_id)} if authorization_id else {}),
                      **({"rows_left_out": int(rows_left_out)} if rows_left_out else {})},
                correlation_id=clean,
            )

    def running_child_lineage(self, *, project_id: str | None = None, lineage_field: str) -> dict[str, list[WorkOrder]]:
        """The orders another order's run started that have not ended, keyed by the order above each -- with the
        orders in between (ended or not), so a walk down from any ancestor reaches every running one.

        ``lineage_field`` names the field in ``fields`` that carries the parent contract (its ``parent_order_id`` is
        the order above). Asked by status and lineage, not read off the newest N: a child older than the newest 500
        that was still running used to be missed by the cancel and by the list's "still going" count (B3,
        2026-09-25). Read only.
        """
        clean_field = str(lineage_field or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", clean_field):
            return {}
        parent_path = f"$.{clean_field}.parent_order_id"
        clauses, params = self._order_filter(project_id=project_id, status=None, customer_id=None, environment_id=None)
        clauses.append(f"status NOT IN ({','.join('?' for _ in ENDED_ORDER_STATUSES)})")
        params.extend(sorted(ENDED_ORDER_STATUSES))
        clauses.append("json_valid(fields_json) = 1 AND COALESCE(json_extract(fields_json, ?), '') <> ''")
        params.append(parent_path)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM work_orders WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, rowid DESC",
                tuple(params),
            ).fetchall()
        running = [self._row_to_order(r) for r in rows]

        def above(order: WorkOrder) -> str:
            parent = order.fields.get(clean_field) if isinstance(order.fields, dict) else None
            return str(parent.get("parent_order_id", "") or "").strip() if isinstance(parent, dict) else ""

        by_parent: dict[str, list[WorkOrder]] = {}
        placed: set[str] = set()
        for child in running:
            # Up the chain until the root or an order already placed; each order is placed once, so a malformed
            # chain that loops cannot keep this going.
            current: WorkOrder | None = child
            while current is not None and current.order_id not in placed:
                placed.add(current.order_id)
                parent_id = above(current)
                if not parent_id:
                    break
                by_parent.setdefault(parent_id, []).append(current)
                current = self.get_order(parent_id, project_id=project_id)
        return by_parent

    def claim_for_launch(
        self,
        order_id: str,
        *,
        actor: str = "",
        project_id: str = "default",
        reviewer_employee_id: str = "",
        note: str = "",
        workflow_version: str = "",
        model_plan: dict[str, Any] | None = None,
        employee_pins: FrozenEmployeePins | None = None,
        _fresh_delivery: Any = None,
    ) -> Any:
        """Atomically take `pending_review -> launching`; False when someone else won.

        Two approvals arriving together used to start the workflow twice: the
        launch ran before the status was written ( audit). The claim
        is the idempotency key; only its winner may launch.

        employee_pins is an internal host-resolved snapshot, never browser input
        or evidence of authorization. Persist it with the claim, not afterwards.
        The launch dispatcher still refuses employee-bound runs until the full
        baseline/runtime adapter is connected. This does not open that path.
        """
        fresh_mode = _fresh_delivery is not None
        fresh_result = None
        if fresh_mode:
            fresh_import_failed = False
            try:
                from .fresh_order_launch import (
                    FreshOrderLaunchError, _FreshOrderLaunchClaimResult,
                    validate_fresh_launch_delivery, fresh_launch_connection,
                    freeze_fresh_launch_result,
                )
            except Exception:
                fresh_import_failed = True
            if fresh_import_failed:
                raise OrderTransitionError("fresh launch source unavailable") from None
            validate_fresh_launch_delivery(_fresh_delivery)
            # This first private slice permits only the explicit legacy caller.
            # Even a valid empty FrozenEmployeePins is outside this mode.
            if employee_pins is not None:
                raise FreshOrderLaunchError("invalid_source")
        frozen_pins: dict[str, Any] = {}
        pins_fingerprint = ""
        if employee_pins is not None:
            from .ai_employee_run_contract import FrozenEmployeePins, restore_employee_pins
            if type(employee_pins) is not FrozenEmployeePins:
                raise OrderTransitionError("employee pins require a frozen server snapshot")
            # Revalidate immutable original bytes; do not resolve current instances.
            snapshot = restore_employee_pins(employee_pins.to_bytes(),
                project_id=employee_pins.project_id, workflow_id=employee_pins.workflow_id,
                expected_fingerprint=employee_pins.fingerprint)
            frozen_pins = snapshot.to_dict()
            pins_fingerprint = snapshot.fingerprint if frozen_pins else ""
            if frozen_pins and (type(workflow_version) is not str or not workflow_version.strip()
                    or workflow_version != workflow_version.strip() or len(workflow_version) > 128
                    or any(ord(char) < 32 or ord(char) == 127 for char in workflow_version)):
                raise OrderTransitionError("employee pins require an explicit workflow version")
        now = datetime.now().isoformat()
        outbox_id = f"business-outbox:{uuid.uuid4()}"
        fresh_failure = None
        try:
            with (fresh_launch_connection(self.path) if fresh_mode else self._connect()) as conn:
                row = conn.execute(
                    "SELECT * FROM work_orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                if row is None:
                    raise OrderTransitionError(f"order {order_id!r} not found")
                order = self._row_to_order(row)
                clean_project_id = str(project_id or "default").strip() or "default"
                if order.project_id != clean_project_id:
                    raise OrderTransitionError("project scope denied")
                if employee_pins is not None and (snapshot.project_id != order.project_id
                        or snapshot.workflow_id != order.workflow_id):
                    raise OrderTransitionError("employee pins do not belong to this project workflow")
                cursor = conn.execute(
                    "UPDATE work_orders SET status = 'launching', updated_at = ? WHERE order_id = ? AND status = 'pending_review'",
                    (now, order_id),
                )
                claimed = cursor.rowcount == 1
                if claimed:
                    payload = {
                        "schema_version": 1,
                        "command": "workflow.launch",
                        "order_id": order.order_id,
                        "project_id": str(project_id or "default").strip() or "default",
                        "workflow_id": order.workflow_id,
                        "workflow_version": str(workflow_version or "").strip(),
                        "org_id": order.org_id,
                        "customer_id": order.customer_id,
                        "environment_id": order.environment_id,
                        "fields": dict(order.fields),
                        "actor": str(actor or "").strip(),
                        "reviewer_employee_id": str(reviewer_employee_id or "").strip(),
                        "note": str(note or "").strip(),
                        # Additive: an older row without this key reads back as {}.
                        "model_plan": dict(model_plan or {}),
                        # Original node snapshots only; full order baseline and live
                        # admission remain separate. Legacy rows may omit both keys.
                        "employee_pins": frozen_pins,
                        "employee_pins_fingerprint": pins_fingerprint,
                    }
                    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                    claim_event_id = self._append_event(
                        conn,
                        table="order_events",
                        aggregate_column="order_id",
                        aggregate_id=order_id,
                        at=now,
                        kind="launching",
                        actor=actor,
                        note={"outbox_id": outbox_id, "anchor_version": 1,
                              "launch_payload_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest()},
                        correlation_id=order_id,
                        causation_id=outbox_id,
                    )
                    if model_plan:
                        # In the same transaction as 「launching」: the settings this order was placed under, written
                        # where the owner can read them back a month later without re-deriving anything.
                        from ampuly.plugins.office_ui.order_model_plan import event_note

                        self._append_event(
                            conn,
                            table="order_events",
                            aggregate_column="order_id",
                            aggregate_id=order_id,
                            at=now,
                            # Written as a literal, like every other kind here, because the console's event-name
                            # guard scrapes these literals: a kind behind a constant would slip past it and reach
                            # the owner's timeline as a raw identifier.
                            kind="model_plan.pinned",
                            actor=actor,
                            note=event_note(dict(model_plan)),
                            correlation_id=order_id,
                            causation_id=outbox_id,
                        )
                    if frozen_pins:
                        self._append_event(
                            conn, table="order_events", aggregate_column="order_id", aggregate_id=order_id,
                            at=now, kind="employee_run.pinned", actor=actor,
                            note={"employee_pins": frozen_pins, "employee_pins_fingerprint": pins_fingerprint,
                                  "workflow_id": order.workflow_id, "workflow_version": workflow_version,
                                  "project_id": clean_project_id, "outbox_id": outbox_id},
                            correlation_id=order_id, causation_id=outbox_id,
                        )
                    conn.execute(
                        """INSERT INTO business_outbox
                       (outbox_id, aggregate_type, aggregate_id, event_type,
                        payload_json, status, attempt_count, last_error,
                        created_at, published_at)
                       VALUES (?, 'work_order', ?, 'workflow_launch_requested',
                               ?, 'pending', 0, '', ?, '')""",
                        (
                            outbox_id,
                            order.order_id,
                            payload_json,
                            now,
                        ),
                    )
                    if fresh_mode:
                        reserved_command = self._reserve_launch_delivery_on_connection(
                            conn, outbox_id, order_id=order_id, project_id=clean_project_id,
                            expected_payload_sha256=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                        )
                        fresh_result = freeze_fresh_launch_result(
                            conn, project_id=clean_project_id, order_id=order_id,
                            outbox_id=outbox_id, workflow_id=order.workflow_id,
                            payload_raw=payload_json, claim_event_id=claim_event_id,
                            sequence=reserved_command.sequence, created_at=reserved_command.created_at,
                        )
                        if type(fresh_result) is not _FreshOrderLaunchClaimResult:
                            raise FreshOrderLaunchError("invalid_source")
                    else:
                        outbox_row = conn.execute(
                            "SELECT * FROM business_outbox WHERE outbox_id = ?",
                            (outbox_id,),
                        ).fetchone()
                        assert outbox_row is not None
                        return self._row_to_outbox(outbox_row)
        except Exception as exc:
            if not fresh_mode:
                raise
            fresh_failure = "invalid_source"
            if (type(exc) is FreshOrderLaunchError and type(exc.code) is str
                    and exc.code in ("unavailable", "invalid_source", "outcome_unknown")):
                fresh_failure = exc.code
        if fresh_failure is not None:
            raise FreshOrderLaunchError(fresh_failure) from None
        if fresh_mode:
            return fresh_result
        return None

    def read_employee_order_claim_snapshot(
        self, *, project_id: str, order_id: str, outbox_id: str,
        workflow_id: str, expected_payload: dict[str, Any],
        include_launch_source: bool = False,
        expected_chain_ancestor: str | None = None,
    ) -> Any:
        """Read original pending bytes in one private query-only transaction.

        By default None means a verified legacy command without employee pins,
        never an unavailable/corrupt employee source. The internal restored
        result is not launch permission and must not be logged or publicized.

        Opt-in legacy transport additionally requires the original claim anchor
        and retains its raw bytes from this same transaction. Historical commands
        without that anchor are not repaired or silently dispatched by this path.
        """
        if expected_chain_ancestor is not None:
            if include_launch_source is not True:
                raise OrderTransitionError('original_claim_chain_check_required')
            import re
            if type(expected_chain_ancestor) is not str or not re.fullmatch('[0-9a-f]{64}',expected_chain_ancestor):
                raise OrderTransitionError('invalid_original_claim_ancestor')
        if type(include_launch_source) is not bool:
            raise OrderTransitionError("include_launch_source must be bool")
        import_failed = False
        try:
            from .ai_employee_contract import canonical_json
            from .employee_order_launch_anchor import (
                OrderLaunchAnchorError, _identity, _ReadBudget, _one_record,
                _json_dict, _MAX_SCALAR_BYTES, _MAX_JSON_BYTES, _rows,
            )
            from .employee_order_claim_pins import read_original_employee_order_claim_pins
            from .employee_order_claim_restore import restore_original_employee_order_claim_pins
            if include_launch_source:
                from .order_launch_claim_source import capture_legacy_order_launch_claim
        except Exception:
            import_failed = True
        if import_failed:
            raise OrderTransitionError("employee launch source unavailable") from None

        project_id = _identity(project_id, scope=True)
        order_id = _identity(order_id, scope=True)
        outbox_id = _identity(outbox_id, scope=True)
        workflow_id = _identity(workflow_id, scope=True)
        failure = None
        result = None
        try:
            if type(expected_payload) is not dict:
                raise OrderLaunchAnchorError("invalid_source")
            expected_bytes = canonical_json(expected_payload)
            with self._connect() as conn:
                conn.execute("PRAGMA query_only = ON").close()
                conn.execute("BEGIN").close()
                budget = _ReadBudget()
                if expected_chain_ancestor is not None:
                    # The full event chain below is verified in THIS same
                    # query-only transaction. Extra valid events may append,
                    # but the earlier observed head must remain its ancestor.
                    ancestors = _rows(conn, 'SELECT 1 FROM order_events WHERE order_id = ? AND event_hash = ? LIMIT 2',
                                      (order_id,expected_chain_ancestor),2)
                    if len(ancestors)!=1:
                        raise OrderLaunchAnchorError('invalid_source')
                status, fields_raw = _one_record(
                    conn, table="work_orders", selector="order_id = ?", params=(order_id,),
                    columns=(("status", _MAX_SCALAR_BYTES), ("fields_json", _MAX_JSON_BYTES)), budget=budget,
                )
                fields = _json_dict(fields_raw, budget)
                # Old unscoped rows belong only to the historical default
                # project. Present malformed scope is never repaired here.
                if fields.get("project_id", "default") != project_id:
                    raise OrderLaunchAnchorError("invalid_source")
                if status != "launching":
                    raise OrderLaunchAnchorError("unavailable")
                stored_id, aggregate_type, aggregate_id, event_type, status, raw = _one_record(
                    conn, table="business_outbox", selector="outbox_id = ?", params=(outbox_id,),
                    columns=tuple((name, _MAX_SCALAR_BYTES) for name in (
                        "outbox_id", "aggregate_type", "aggregate_id", "event_type", "status"
                    )) + (("payload_json", _MAX_JSON_BYTES),), budget=budget,
                )
                if (stored_id != outbox_id or aggregate_type != "work_order" or aggregate_id != order_id
                        or event_type != "workflow_launch_requested"):
                    raise OrderLaunchAnchorError("invalid_source")
                if status != "pending":
                    raise OrderLaunchAnchorError("unavailable")
                payload = _json_dict(raw, budget)
                if (type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
                        or payload.get("command") != "workflow.launch"
                        or payload.get("project_id") != project_id or payload.get("order_id") != order_id
                        or payload.get("workflow_id") != workflow_id
                        or canonical_json(payload) != expected_bytes):
                    raise OrderLaunchAnchorError("invalid_source")
                pins = payload.get("employee_pins", {})
                fingerprint = payload.get("employee_pins_fingerprint", "")
                if type(pins) is not dict or type(fingerprint) is not str:
                    raise OrderLaunchAnchorError("invalid_source")
                if not pins:
                    if fingerprint != "":
                        raise OrderLaunchAnchorError("invalid_source")
                    if include_launch_source:
                        result = capture_legacy_order_launch_claim(
                            conn, budget=budget, project_id=project_id,
                            order_id=order_id, outbox_id=outbox_id,
                            workflow_id=workflow_id, payload_raw=raw, payload=payload,
                        )
                else:
                    source = read_original_employee_order_claim_pins(
                        conn, project_id=project_id, order_id=order_id, outbox_id=outbox_id,
                    )
                    result = restore_original_employee_order_claim_pins(
                        source, project_id=project_id, order_id=order_id, outbox_id=outbox_id,
                    )
                    if include_launch_source:
                        from .employee_launch_source import capture_employee_launch_source
                        result = capture_employee_launch_source(result, raw)
        except OrderLaunchAnchorError as exc:
            failure = exc.code if type(exc) is OrderLaunchAnchorError else "unavailable"
        except Exception:
            failure = "unavailable"
        if failure is not None:
            raise OrderLaunchAnchorError(failure) from None
        return result

    def pending_launch_command(
        self, order_id: str, *, project_id: str | None = None
    ) -> BusinessOutboxRecord | None:
        clean_order_id = str(order_id or "").strip()
        if project_id is not None:
            order = self.get_order(clean_order_id, project_id=project_id)
            if order is None:
                return None
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM business_outbox
                   WHERE aggregate_type = 'work_order' AND aggregate_id = ?
                     AND event_type = 'workflow_launch_requested'
                     AND status = 'pending'
                   ORDER BY sequence LIMIT 1""",
                (clean_order_id,),
            ).fetchone()
        return self._row_to_outbox(row) if row is not None else None

    @staticmethod
    def _check_outbox_decode_diagnostics(value: list[BusinessOutboxDecodeDiagnostic] | None) -> None:
        if value is not None and (type(value) is not list or value):
            raise ValueError("decode_diagnostics must be an explicitly empty list or None")

    def _decode_pending_outbox_rows(
        self, rows: list[sqlite3.Row],
        decode_diagnostics: list[BusinessOutboxDecodeDiagnostic] | None,
    ) -> list[BusinessOutboxRecord]:
        if decode_diagnostics is None:
            return [self._row_to_outbox(row) for row in rows]  # Legacy strict API.
        records: list[BusinessOutboxRecord] = []
        for row in rows:
            try:
                records.append(self._row_to_outbox(row))
            except (OrderTransitionError, ValueError, TypeError, RecursionError, OverflowError):
                # Catch only the materializer's known decoding categories. SQL
                # happened before this loop; I/O, cancellation and system errors
                # are not data corruption and must propagate. Retain the row,
                # never repair, retry its decode, or mark it delivered/failed.
                raw_id, sequence = row["outbox_id"], row["sequence"]
                safe_id = (raw_id if type(raw_id) is str and
                           re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", raw_id) else None)
                safe_sequence = sequence if type(sequence) is int and 0 < sequence <= 2**63 - 1 else None
                decode_diagnostics.append(BusinessOutboxDecodeDiagnostic(
                    code=("outbox_launch_decode_invalid" if row["event_type"] == "workflow_launch_requested"
                          else "outbox_row_decode_invalid"),
                    sequence=safe_sequence, outbox_id=safe_id,
                ))
        return records

    def list_pending_launch_commands(
        self, *, limit: int = 100,
        decode_diagnostics: list[BusinessOutboxDecodeDiagnostic] | None = None,
    ) -> list[BusinessOutboxRecord]:
        """Read one bounded pending window; optionally retain bad rows with diagnoses.

        None keeps strict compatibility. An explicitly empty list opts into
        per-row decoding and receives at most this window's row count. There
        is no refill, offset scan or claim that later pending rows were read.
        """
        self._check_outbox_decode_diagnostics(decode_diagnostics)
        bounded_limit = max(1, min(int(limit or 100), 1000))
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM business_outbox
                   WHERE event_type = 'workflow_launch_requested'
                     AND status = 'pending'
                   ORDER BY sequence LIMIT ?""",
                (bounded_limit,),
            ).fetchall()
        return self._decode_pending_outbox_rows(rows, decode_diagnostics)

    def list_pending_outbox(
        self, *, limit: int = 100,
        decode_diagnostics: list[BusinessOutboxDecodeDiagnostic] | None = None,
    ) -> list[BusinessOutboxRecord]:
        """Every pending row, whatever its type.

        The launch reader asked for one event type by name, so rows of any
        other type -- project lifecycle among them -- were invisible and
        stayed pending for good.

        Default decoding remains strict. An explicitly empty diagnostics list
        allows known per-row decode failures to remain pending while this same
        bounded window's good rows are returned. Never scan further to refill.
        """
        self._check_outbox_decode_diagnostics(decode_diagnostics)
        bounded_limit = max(1, min(int(limit or 100), 1000))
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM business_outbox
                   WHERE status = 'pending'
                   ORDER BY sequence LIMIT ?""",
                (bounded_limit,),
            ).fetchall()
        return self._decode_pending_outbox_rows(rows, decode_diagnostics)

    def mark_outbox_no_consumer(self, outbox_id: str, reason: str) -> bool:
        """Nobody reads this event type; it leaves the pending queue saying so."""
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE business_outbox
                   SET status = 'no_consumer', last_error = ?
                   WHERE outbox_id = ? AND status = 'pending'""",
                (str(reason or "").strip()[:2000], str(outbox_id or "").strip()),
            )
        return cursor.rowcount == 1

    def record_model_plan(self, order_id: str, plan: dict[str, Any] | None, *, actor: str = "system") -> bool:
        """Record which model plan an order runs under, when it was given one after its launch.

        The ordinary path writes this row inside ``claim_for_launch``. This is for an order that was launched
        before there were plans at all: it is given one once, on resume, and the row says so through its
        ``source`` rather than looking like it was planned at launch.
        """
        from ampuly.plugins.office_ui.order_model_plan import event_note

        clean = str(order_id or "").strip()
        if not clean or not plan:
            return False
        with self._connect() as conn:
            self._append_event(
                conn,
                table="order_events",
                aggregate_column="order_id",
                aggregate_id=clean,
                at=datetime.now().isoformat(timespec="seconds"),
                kind="model_plan.pinned",
                actor=str(actor or "system"),
                note=event_note(dict(plan)),
                correlation_id=clean,
            )
        return True

    def record_launch_failure(self, outbox_id: str, error: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE business_outbox
                   SET attempt_count = attempt_count + 1, last_error = ?
                   WHERE outbox_id = ? AND status = 'pending'""",
                (str(error or "").strip()[:2000], str(outbox_id or "").strip()),
            )
        return cursor.rowcount == 1

    @contextmanager
    def _launch_delivery_fixed_connection(self, *, read_only: bool):
        """An owned existing-file transaction, never a Store initializer."""
        if type(read_only) is not bool:
            raise OrderTransitionError("launch delivery request invalid")
        path = Path(self.path)
        if not path.is_absolute():
            raise OrderTransitionError("launch delivery store unavailable")
        conn = sqlite3.connect(
            path.as_uri() + ("?mode=ro" if read_only else "?mode=rw"),
            timeout=5.0,
            uri=True,
        )
        conn.row_factory = sqlite3.Row
        failed = False
        try:
            if read_only:
                conn.execute("PRAGMA query_only = ON").close()
                conn.execute("BEGIN").close()
            else:
                conn.execute("BEGIN IMMEDIATE").close()
            yield conn
            conn.commit()
        except BaseException:
            failed = True
            # Cleanup is not evidence that a commit produced no effects.
            try:
                conn.rollback()
            except BaseException:
                pass
            raise
        finally:
            if failed:
                try:
                    conn.close()
                except BaseException:
                    pass
            else:
                conn.close()

    def _launch_delivery_fixed_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        outbox_id: str,
        order_id: str,
        project_id: str,
        expected_payload_sha256: str,
    ) -> tuple[BusinessOutboxRecord, str, str] | None:
        """Observe only the exact raw request; not an origin or permission proof."""
        for value in (outbox_id, order_id, project_id):
            if (type(value) is not str or not value or value != value.strip()
                    or len(value.encode("utf-8")) > 512
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise OrderTransitionError("launch delivery request invalid")
        if (type(expected_payload_sha256) is not str
                or re.fullmatch(r"[0-9a-f]{64}", expected_payload_sha256) is None):
            raise OrderTransitionError("launch delivery request invalid")
        rows = conn.execute(
            """SELECT CASE WHEN typeof(sequence) = 'integer' THEN sequence ELSE NULL END AS sequence,
                      outbox_id,
                      CASE WHEN aggregate_type = 'work_order' THEN aggregate_type ELSE NULL END AS aggregate_type,
                      CASE WHEN typeof(aggregate_id) = 'text'
                            AND length(CAST(aggregate_id AS BLOB)) <= 512
                           THEN aggregate_id ELSE NULL END AS aggregate_id,
                      CASE WHEN event_type = 'workflow_launch_requested'
                           THEN event_type ELSE NULL END AS event_type,
                      CASE WHEN typeof(payload_json) = 'text'
                            AND length(CAST(payload_json AS BLOB)) <= 131072
                           THEN payload_json ELSE NULL END AS payload_json,
                      CASE WHEN typeof(status) = 'text'
                            AND length(CAST(status AS BLOB)) <= 128
                           THEN status ELSE NULL END AS status,
                      CASE WHEN typeof(attempt_count) = 'integer'
                           THEN attempt_count ELSE NULL END AS attempt_count,
                      CASE WHEN typeof(last_error) = 'text'
                            AND length(CAST(last_error AS BLOB)) <= 8000
                           THEN last_error ELSE NULL END AS last_error,
                      CASE WHEN typeof(created_at) = 'text'
                            AND length(CAST(created_at AS BLOB)) <= 128
                           THEN created_at ELSE NULL END AS created_at,
                      CASE WHEN typeof(published_at) = 'text'
                            AND length(CAST(published_at AS BLOB)) <= 128
                           THEN published_at ELSE NULL END AS published_at
               FROM business_outbox WHERE outbox_id = ? LIMIT 2""",
            (outbox_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise OrderTransitionError("launch delivery source invalid")
        row = rows[0]
        if (row["outbox_id"] != outbox_id or row["aggregate_type"] != "work_order"
                or row["aggregate_id"] != order_id
                or row["event_type"] != "workflow_launch_requested"
                or type(row["sequence"]) is not int or not 0 < row["sequence"] <= 2**63 - 1
                or type(row["attempt_count"]) is not int
                or not 0 <= row["attempt_count"] <= 2**63 - 1
                or type(row["status"]) is not str
                or row["status"] not in {
                    "pending", "inflight", "outcome_unknown", "published", "no_consumer",
                }
                or type(row["payload_json"]) is not str
                or type(row["last_error"]) is not str
                or any(type(row[key]) is not str or len(row[key].encode("utf-8")) > 128
                       for key in ("created_at", "published_at"))):
            raise OrderTransitionError("launch delivery source invalid")
        if hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest() != expected_payload_sha256:
            raise OrderTransitionError("launch delivery source changed")
        command = self._row_to_outbox(row)
        if (command.payload.get("project_id") != project_id
                or command.payload.get("order_id") != order_id):
            raise OrderTransitionError("launch delivery source invalid")
        pins = command.payload.get("employee_pins", {})
        pins_fingerprint = command.payload.get("employee_pins_fingerprint", "")
        if type(pins) is not dict or pins or type(pins_fingerprint) is not str or pins_fingerprint:
            raise OrderTransitionError("employee-bound launch is not connected; original pins retained")
        order_rows = conn.execute(
            """SELECT order_id,
                      CASE WHEN typeof(workflow_id) = 'text'
                            AND length(CAST(workflow_id AS BLOB)) <= 512
                           THEN workflow_id ELSE NULL END AS workflow_id,
                      CASE WHEN typeof(status) = 'text'
                            AND length(CAST(status AS BLOB)) <= 128
                           THEN status ELSE NULL END AS status,
                      CASE WHEN typeof(launched_task_id) = 'text'
                            AND length(CAST(launched_task_id AS BLOB)) <= 512
                           THEN launched_task_id ELSE NULL END AS launched_task_id,
                      CASE WHEN typeof(fields_json) = 'text'
                            AND length(CAST(fields_json AS BLOB)) <= 131072
                           THEN fields_json ELSE NULL END AS fields_json
               FROM work_orders WHERE order_id = ? LIMIT 2""",
            (order_id,),
        ).fetchall()
        if len(order_rows) != 1:
            raise OrderTransitionError("launch delivery source unavailable")
        order = order_rows[0]
        if (order["order_id"] != order_id
                or order["workflow_id"] != command.payload.get("workflow_id")
                or type(order["status"]) is not str
                or type(order["launched_task_id"]) is not str
                or len(order["status"].encode("utf-8")) > 128
                or len(order["launched_task_id"].encode("utf-8")) > 512
                or type(order["fields_json"]) is not str):
            raise OrderTransitionError("launch delivery source invalid")

        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate launch delivery field")
                value[key] = item
            return value

        def finite(_value):
            raise ValueError("non-finite launch delivery field")

        def finite_float(value):
            parsed = float(value)
            if not math.isfinite(parsed):
                raise ValueError("non-finite launch delivery field")
            return parsed

        invalid_fields = False
        try:
            fields = json.loads(order["fields_json"], object_pairs_hook=unique,
                                parse_constant=finite, parse_float=finite_float)
            if type(fields) is not dict or fields.get("project_id", "default") != project_id:
                invalid_fields = True
        except (ValueError, TypeError, RecursionError, OverflowError):
            invalid_fields = True
        if invalid_fields:
            raise OrderTransitionError("launch delivery source invalid") from None
        return command, order["status"], order["launched_task_id"]

    def reserve_launch_delivery_once(
        self, outbox_id: str, *, order_id: str, project_id: str,
        expected_payload_sha256: str,
    ) -> BusinessOutboxRecord:
        """Reserve one fresh command before effects; never launch or acknowledge it.

        Only a trusted new-claim host may opt in. A clean historical pending
        row does not prove that an earlier process never executed its command.
        Returned inflight is reservation state, not runtime acceptance.
        """
        with self._launch_delivery_fixed_connection(read_only=False) as conn:
            result = self._reserve_launch_delivery_on_connection(
                conn, outbox_id, order_id=order_id, project_id=project_id,
                expected_payload_sha256=expected_payload_sha256,
            )
        # Leaving the context confirms commit; an exception is still unknown.
        return result

    def _reserve_launch_delivery_on_connection(
        self, conn: sqlite3.Connection, outbox_id: str, *, order_id: str,
        project_id: str, expected_payload_sha256: str,
    ) -> BusinessOutboxRecord:
        """Original reservation core in an already owned write transaction."""
        snapshot = self._launch_delivery_fixed_snapshot(
            conn, outbox_id=outbox_id, order_id=order_id, project_id=project_id,
            expected_payload_sha256=expected_payload_sha256,
        )
        if snapshot is None:
            raise OrderTransitionError("launch delivery source unavailable")
        command, order_status, launched_task_id = snapshot
        if (command.status != "pending" or order_status != "launching"
                or launched_task_id or command.attempt_count != 0
                or command.last_error or command.published_at):
            raise OrderTransitionError("launch delivery cannot be reserved")
        changed = conn.execute(
            """UPDATE business_outbox SET status = 'inflight'
                   WHERE outbox_id = ? AND aggregate_type = 'work_order'
                     AND aggregate_id = ? AND event_type = 'workflow_launch_requested'
                     AND status = 'pending' AND attempt_count = 0
                     AND last_error = '' AND published_at = ''""",
            (outbox_id, order_id),
        ).rowcount
        if changed != 1:
            raise OrderTransitionError("launch delivery reservation conflict")
        observed = self._launch_delivery_fixed_snapshot(
            conn, outbox_id=outbox_id, order_id=order_id, project_id=project_id,
            expected_payload_sha256=expected_payload_sha256,
        )
        if observed is None or observed[0].status != "inflight":
            raise OrderTransitionError("launch delivery reservation unavailable")
        result = observed[0]
        return result

    def read_launch_delivery_fixed(
        self, outbox_id: str, *, order_id: str, project_id: str,
        expected_payload_sha256: str,
    ) -> BusinessOutboxRecord | None:
        """Read one exact request without claiming, initializing or retrying.

        None is a missing row in this snapshot, not proof of no prior effects.
        Published is an outbox state, not verified Task/Run acceptance.
        """
        with self._launch_delivery_fixed_connection(read_only=True) as conn:
            snapshot = self._launch_delivery_fixed_snapshot(
                conn, outbox_id=outbox_id, order_id=order_id, project_id=project_id,
                expected_payload_sha256=expected_payload_sha256,
            )
            result = None if snapshot is None else snapshot[0]
        return result

    def mark_launch_delivery_outcome_unknown(
        self, outbox_id: str, *, order_id: str, project_id: str,
        expected_payload_sha256: str,
    ) -> BusinessOutboxRecord:
        """Quarantine the reserved exact request, without resetting or resending."""
        with self._launch_delivery_fixed_connection(read_only=False) as conn:
            snapshot = self._launch_delivery_fixed_snapshot(
                conn, outbox_id=outbox_id, order_id=order_id, project_id=project_id,
                expected_payload_sha256=expected_payload_sha256,
            )
            if snapshot is None:
                raise OrderTransitionError("launch delivery source unavailable")
            command = snapshot[0]
            if command.status == "outcome_unknown":
                result = command
            else:
                if command.status != "inflight" or command.published_at:
                    raise OrderTransitionError("launch delivery cannot become unknown")
                changed = conn.execute(
                    """UPDATE business_outbox
                       SET status = 'outcome_unknown', last_error = 'launch_delivery_outcome_unknown'
                       WHERE outbox_id = ? AND aggregate_type = 'work_order'
                         AND aggregate_id = ? AND event_type = 'workflow_launch_requested'
                         AND status = 'inflight' AND published_at = ''""",
                    (outbox_id, order_id),
                ).rowcount
                if changed != 1:
                    raise OrderTransitionError("launch delivery quarantine conflict")
                observed = self._launch_delivery_fixed_snapshot(
                    conn, outbox_id=outbox_id, order_id=order_id, project_id=project_id,
                    expected_payload_sha256=expected_payload_sha256,
                )
                if observed is None or observed[0].status != "outcome_unknown":
                    raise OrderTransitionError("launch delivery quarantine unavailable")
                result = observed[0]
        return result

    def complete_launch(
        self,
        outbox_id: str,
        *,
        task_id: str,
        org_id: str = "",
        project_id: str | None = None,
    ) -> WorkOrder:
        clean_outbox_id = str(outbox_id or "").strip()
        clean_task_id = str(task_id or "").strip()
        if not clean_outbox_id or not clean_task_id:
            raise OrderTransitionError("launch completion requires outbox_id and task_id")
        now = datetime.now().isoformat()
        with self._connect() as conn:
            return BusinessStore._complete_launch_core_on_connection(
                self, conn, clean_outbox_id=clean_outbox_id, clean_task_id=clean_task_id,
                org_id=org_id, project_id=project_id, now=now, expected_status="pending",
            )

    def _complete_launch_core_on_connection(
        self, conn: sqlite3.Connection, *, clean_outbox_id: str, clean_task_id: str,
        org_id: str, project_id: str | None, now: str, expected_status: str,
    ) -> WorkOrder:
        """Original completion work on a caller-owned connection, not authority.

        Legacy public calls retain dynamic self, pending and the original SQL.
        New consumers use the strict source-bound wrapper, never this core.
        """
        outbox_row = conn.execute(
            "SELECT * FROM business_outbox WHERE outbox_id = ?",
            (clean_outbox_id,),
        ).fetchone()
        if outbox_row is None:
            raise OrderTransitionError(f"launch outbox {clean_outbox_id!r} not found")
        command = (BusinessStore._row_to_outbox(outbox_row) if expected_status == "inflight"
                   else self._row_to_outbox(outbox_row))
        order_row = conn.execute(
            "SELECT * FROM work_orders WHERE order_id = ?",
            (command.aggregate_id,),
        ).fetchone()
        if order_row is None:
            raise OrderTransitionError(f"order {command.aggregate_id!r} not found")
        order = (BusinessStore._row_to_order(order_row) if expected_status == "inflight"
                 else self._row_to_order(order_row))
        if project_id is not None:
            clean_project_id = str(project_id or "default").strip() or "default"
            if order.project_id != clean_project_id:
                raise OrderTransitionError("project scope denied")
        if command.status == "published":
            if order.status == "launched" and order.launched_task_id == clean_task_id:
                return order
            raise OrderTransitionError("published launch command conflicts with order state")
        if command.status != expected_status or order.status != "launching":
            raise OrderTransitionError(
                f"launch command cannot complete from {command.status}/{order.status}"
            )
        changed_order = conn.execute(
            """UPDATE work_orders
                   SET status = 'launched', updated_at = ?,
                       reviewer_employee_id = CASE WHEN ? != '' THEN ? ELSE reviewer_employee_id END,
                       review_note = CASE WHEN ? != '' THEN ? ELSE review_note END,
                       launched_task_id = ?
                   WHERE order_id = ? AND status = 'launching'""",
            (
                now,
                str(command.payload.get("reviewer_employee_id") or "").strip(),
                str(command.payload.get("reviewer_employee_id") or "").strip(),
                str(command.payload.get("note") or "").strip(),
                str(command.payload.get("note") or "").strip(),
                clean_task_id,
                order.order_id,
            ),
        )
        if expected_status == "inflight" and changed_order.rowcount != 1:
            raise OrderTransitionError("launch delivery completion conflict")
        append_event = BusinessStore._append_event if expected_status == "inflight" else self._append_event
        append_event(
            conn,
            table="order_events",
            aggregate_column="order_id",
            aggregate_id=order.order_id,
            at=now,
            kind="launched",
            actor=str(command.payload.get("actor") or "").strip(),
            note={
                "note": str(command.payload.get("note") or "").strip(),
                "org_id": str(org_id or command.payload.get("org_id") or "").strip(),
                "outbox_id": clean_outbox_id,
                "anchor_version": 1,
                "order_task_id": clean_task_id,
                "launch_payload_sha256": hashlib.sha256(outbox_row["payload_json"].encode("utf-8")).hexdigest(),
            },
            correlation_id=order.order_id,
            causation_id=clean_outbox_id,
        )
        if expected_status == "pending":
            conn.execute(
                """UPDATE business_outbox
                   SET status = 'published', published_at = ?, last_error = ''
                   WHERE outbox_id = ? AND status = 'pending'""",
                (now, clean_outbox_id),
            )
        elif expected_status == "inflight":
            changed_outbox = conn.execute(
                """UPDATE business_outbox SET status = 'published', published_at = ?, last_error = ''
                   WHERE outbox_id = ? AND status = 'inflight'""",
                (now, clean_outbox_id),
            )
            if changed_outbox.rowcount != 1:
                raise OrderTransitionError("launch delivery completion conflict")
        else:
            raise OrderTransitionError("launch delivery completion invalid")
        refreshed = conn.execute(
            "SELECT * FROM work_orders WHERE order_id = ?",
            (order.order_id,),
        ).fetchone()
        assert refreshed is not None
        return (BusinessStore._row_to_order(refreshed) if expected_status == "inflight"
                else self._row_to_order(refreshed))

    def complete_launch_delivery_fixed(
        self, *, source: Any, task_id: str, org_id: str = "",
    ) -> WorkOrder:
        """Complete one server-fresh inflight source; never start or authorize.

        Published retries are deliberately refused here: the original read-only
        exact receipt path is available, but a launching-only source reader must
        not be weakened to turn historical publication into fresh permission.
        Original complete_launch retains its legacy published receipt behavior.
        """
        from .order_launch_claim_source import OriginalOrderLaunchClaim
        from .launch_stop_source import LaunchStopSourceHeaders, _expected_headers, read_launch_stop_source_headers
        from .employee_order_launch_anchor import _ReadBudget
        from .current_owner_rw import _owned_rw_connection, _source_path

        if type(self) is not BusinessStore or type(source) is not OriginalOrderLaunchClaim:
            raise OrderTransitionError("launch_delivery_source_invalid") from None
        fields = object.__getattribute__(self, "__dict__")
        protected = {"_connect", "_launch_delivery_fixed_connection", "_launch_delivery_fixed_snapshot",
                     "_row_to_order", "_row_to_outbox", "_append_event"}
        if (type(fields) is not dict or len(fields) > 256 or any(type(key) is not str for key in fields)
                or "path" not in fields or protected.intersection(fields)):
            raise OrderTransitionError("launch_delivery_source_invalid") from None
        source_fields = object.__getattribute__(source, "__dict__")
        names = ("project_id", "order_id", "outbox_id", "workflow_id", "workflow_version",
                 "launch_payload_bytes", "launch_payload_sha256", "claim_event_id", "event_chain_head_sha256")
        if (type(source_fields) is not dict or len(source_fields) != len(names)
                or any(type(key) is not str for key in source_fields) or set(source_fields) != set(names)):
            raise OrderTransitionError("launch_delivery_source_invalid") from None
        values = tuple(source_fields[name] for name in names)
        headers = LaunchStopSourceHeaders(project_id=values[0], order_id=values[1], outbox_id=values[2],
            workflow_id=values[3], workflow_version=values[4], launch_payload_sha256=values[6],
            claim_event_id=values[7], event_chain_head_sha256=values[8])
        expected = _expected_headers(headers)
        if (type(values[5]) is not bytes or not 0 < len(values[5]) <= 131072
                or hashlib.sha256(values[5]).hexdigest() != expected[5]):
            raise OrderTransitionError("launch_delivery_source_invalid") from None
        for value, empty in ((task_id, False), (org_id, True)):
            if (type(value) is not str or not value and not empty or len(value) > 512
                    or value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise OrderTransitionError("launch_delivery_completion_invalid") from None
            invalid_text = False
            try:
                invalid_text = len(value.encode("utf-8")) > 512
            except UnicodeError:
                invalid_text = True
            if invalid_text:
                raise OrderTransitionError("launch_delivery_completion_invalid") from None
        captured_path, cwd = fields["path"], Path.cwd()
        expected_main = _source_path(captured_path, cwd)
        with BusinessStore._launch_delivery_fixed_connection(self, read_only=False) as conn:
            if conn.row_factory is not sqlite3.Row:
                raise OrderTransitionError("launch_delivery_source_invalid") from None
            _owned_rw_connection(conn, expected_main=expected_main)
            before = BusinessStore._launch_delivery_fixed_snapshot(self, conn, outbox_id=expected[2],
                order_id=expected[1], project_id=expected[0], expected_payload_sha256=expected[5])
            if (before is None or before[0].status != "inflight" or before[1:] != ("launching", "")
                    or before[0].attempt_count != 0 or before[0].last_error or before[0].published_at):
                raise OrderTransitionError("launch_delivery_completion_conflict") from None
            budget = _ReadBudget()
            observed = read_launch_stop_source_headers(conn, project_id=expected[0], order_id=expected[1],
                outbox_id=expected[2], expected=headers, _budget=budget)
            if _expected_headers(observed) != expected:
                raise OrderTransitionError("launch_delivery_source_invalid") from None
            claim_rows = conn.execute("SELECT id FROM order_events WHERE order_id=? AND event_id=? LIMIT 2",
                                      (expected[1], expected[6])).fetchall()
            if len(claim_rows) != 1 or type(claim_rows[0][0]) is not int:
                raise OrderTransitionError("launch_delivery_source_invalid") from None
            if conn.execute("SELECT id FROM order_events WHERE order_id=? AND kind='launching' AND id>? LIMIT 1",
                            (expected[1], claim_rows[0][0])).fetchone() is not None:
                raise OrderTransitionError("launch_delivery_completion_conflict") from None
            result = BusinessStore._complete_launch_core_on_connection(self, conn,
                clean_outbox_id=expected[2], clean_task_id=task_id, org_id=org_id,
                project_id=expected[0], now=datetime.now().isoformat(), expected_status="inflight")
            after = BusinessStore._launch_delivery_fixed_snapshot(self, conn, outbox_id=expected[2],
                order_id=expected[1], project_id=expected[0], expected_payload_sha256=expected[5])
            if after is None or after[0].status != "published" or after[1:] != ("launched", task_id):
                raise OrderTransitionError("launch_delivery_completion_conflict") from None
            if (type(self) is not BusinessStore or object.__getattribute__(self, "__dict__") is not fields
                    or len(fields) > 256 or any(type(key) is not str for key in fields)
                    or fields.get("path") is not captured_path or Path.cwd() != cwd or protected.intersection(fields)
                    or type(source) is not OriginalOrderLaunchClaim
                    or object.__getattribute__(source, "__dict__") is not source_fields
                    or len(source_fields) != len(names) or any(type(key) is not str for key in source_fields)
                    or set(source_fields) != set(names)):
                raise OrderTransitionError("launch_delivery_source_invalid") from None
            after_values = tuple(source_fields[name] for name in names)
            if (any(type(after) is not type(before) for after, before in zip(after_values, values))
                    or after_values != values):
                raise OrderTransitionError("launch_delivery_source_invalid") from None
            _owned_rw_connection(conn, expected_main=expected_main)
        return result

    def transition(
        self,
        order_id: str,
        to_status: str,
        *,
        actor: str = "",
        note: str = "",
        reviewer_employee_id: str | None = None,
        launched_task_id: str | None = None,
        extra: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> WorkOrder:
        order = self.get_order(order_id, project_id=project_id)
        if order is None:
            raise OrderTransitionError(f"order {order_id!r} not found")
        if to_status not in ORDER_STATUSES:
            raise OrderTransitionError(f"unknown order status {to_status!r}")
        if to_status not in ORDER_TRANSITIONS.get(order.status, frozenset()):
            raise OrderTransitionError(f"order {order_id} cannot go from {order.status} to {to_status}")
        now = datetime.now().isoformat()
        with self._connect() as conn:
            if order.status == "launching" and to_status != "launched":
                conn.execute(
                    """UPDATE business_outbox
                       SET status = 'cancelled', published_at = ?, last_error = ?
                       WHERE aggregate_type = 'work_order' AND aggregate_id = ?
                         AND event_type = 'workflow_launch_requested'
                         AND status = 'pending'""",
                    (
                        now,
                        f"order transitioned from launching to {to_status}",
                        order_id,
                    ),
                )
            conn.execute(
                """UPDATE work_orders SET status = ?, updated_at = ?,
                   reviewer_employee_id = COALESCE(?, reviewer_employee_id),
                   review_note = CASE WHEN ? != '' THEN ? ELSE review_note END,
                   launched_task_id = COALESCE(?, launched_task_id)
                   WHERE order_id = ?""",
                (to_status, now, reviewer_employee_id, note, note, launched_task_id, order_id),
            )
            self._append_event(
                conn,
                table="order_events",
                aggregate_column="order_id",
                aggregate_id=order_id,
                at=now,
                kind=to_status,
                actor=actor,
                note={"note": note, **dict(extra or {})},
                correlation_id=order_id,
            )
        if to_status in {"closed", "rejected"}:
            # An order that is over spends nothing more: its allowance is closed with it (what was only held goes
            # back; what may already have been spent stays for a person to settle). Left open, it would sit on the
            # order row as a live budget and be the one a later grant is refused for.
            try:
                allowance = self.open_budget_for_order(order_id)
                if allowance is not None:
                    self.close_run_budget(str(allowance["budget_id"]), actor=actor or "runtime", reason=f"order {to_status}")
            except Exception:  # noqa: BLE001 - the order's own transition has already been recorded
                pass
        refreshed = self.get_order(order_id, project_id=project_id)
        assert refreshed is not None
        return refreshed

    def events(self, order_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT at, kind, actor, note_json, event_id, correlation_id, causation_id, previous_hash, event_hash "
                "FROM order_events WHERE order_id = ? ORDER BY id",
                (order_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                note = json.loads(row["note_json"] or "{}")
            except ValueError:
                note = {}
            out.append({
                "at": row["at"], "kind": row["kind"], "actor": row["actor"], "note": note,
                "event_id": str(row["event_id"] or ""),
                "correlation_id": str(row["correlation_id"] or ""),
                "causation_id": str(row["causation_id"] or ""),
                "integrity": {
                    "previous_hash": str(row["previous_hash"] or ""),
                    "event_hash": str(row["event_hash"] or ""),
                },
            })
        return out

    def verify_order_event_chain(self, order_id: str) -> bool:
        """Verify persisted order-event hashes without mutating the store."""
        return self._verify_event_chain("order_events", "order_id", order_id)

    def verify_gate_event_chain(self, checkpoint_id: str) -> bool:
        """Verify persisted gate-event hashes without mutating the store."""
        return self._verify_event_chain("gate_events", "checkpoint_id", checkpoint_id)

    def _verify_event_chain(self, table: str, aggregate_column: str, aggregate_id: str) -> bool:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {aggregate_column}, at, kind, actor, note_json, event_id, correlation_id, causation_id, previous_hash, event_hash "
                f"FROM {table} WHERE {aggregate_column} = ? ORDER BY id",
                (str(aggregate_id or ""),),
            ).fetchall()
        previous_hash = ""
        for row in rows:
            event_hash = str(row["event_hash"] or "")
            event_id = str(row["event_id"] or "")
            if not event_hash or not event_id:
                # Legacy rows predate the envelope and are not claimed as
                # tamper-evident until rewritten by a new state transition.
                return False
            try:
                note = json.loads(row["note_json"] or "{}")
            except ValueError:
                return False
            if not isinstance(note, dict) or str(row["previous_hash"] or "") != previous_hash:
                return False
            envelope = {
                "schema_version": 1,
                "event_id": event_id,
                "aggregate_type": "work_order" if table == "order_events" else "gate_card",
                "aggregate_id": str(row[aggregate_column] or ""),
                "at": str(row["at"] or ""),
                "kind": str(row["kind"] or ""),
                "actor": str(row["actor"] or ""),
                "correlation_id": str(row["correlation_id"] or ""),
                "causation_id": str(row["causation_id"] or ""),
                "previous_hash": previous_hash,
                "note": note,
            }
            expected = hashlib.sha256(
                json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if expected != event_hash:
                return False
            previous_hash = event_hash
        return bool(rows)

    # ── contractual acceptance (independent from technical approval) ──

    def fde_control_plane(
        self, order_id: str, *, project_id: str | None = None
    ) -> FDEControlPlane | None:
        """Project traceable FDE objects without creating a second truth store."""
        order = self.get_order(order_id, project_id=project_id)
        if order is None:
            return None
        fields = order.fields if isinstance(order.fields, dict) else {}
        raw = fields.get("fde_control_plane", {})
        context = dict(raw) if isinstance(raw, dict) else {}

        def _text(key: str) -> str:
            value = context.get(key, fields.get(key, ""))
            return str(value or "").strip()

        phase_by_status = {
            "collecting": "discovery", "pending_review": "discovery",
            "launching": "building", "launched": "building",
            "producing": "validating", "delivering": "uat",
            "closed": "closed", "rejected": "paused",
        }
        acceptance = self.acceptance(order_id, project_id=project_id)
        events = self.events(order_id)
        uat = dict(context.get("uat", {}) or {}) if isinstance(context.get("uat"), dict) else {}
        uat.setdefault("status", acceptance.status if acceptance else "not_defined")
        uat.setdefault("criteria", list(acceptance.criteria) if acceptance else [])
        release = dict(context.get("release", {}) or {}) if isinstance(context.get("release"), dict) else {}
        release.setdefault("workflow_id", order.workflow_id)
        release.setdefault("workflow_version", _text("workflow_version") or (acceptance.workflow_version if acceptance else ""))
        release.setdefault("status", "released" if order.status in {"launched", "producing", "delivering", "closed"} else "not_released")
        rollback = dict(context.get("rollback", {}) or {}) if isinstance(context.get("rollback"), dict) else {}
        rollback.setdefault("available", bool(_text("rollback_plan")))
        if _text("rollback_plan"):
            rollback.setdefault("plan", _text("rollback_plan"))
        sla = dict(context.get("sla", {}) or {}) if isinstance(context.get("sla"), dict) else {}
        roi = dict(context.get("roi", {}) or {}) if isinstance(context.get("roi"), dict) else {}
        last_event = events[-1] if events else {}
        trace = {
            "order_event_count": len(events),
            "last_event_id": str(last_event.get("event_id", "") or ""),
            "last_event_kind": str(last_event.get("kind", "") or ""),
            "acceptance_evidence_hash": acceptance.evidence_hash if acceptance else "",
        }
        return FDEControlPlane(
            order_id=order.order_id,
            customer_id=order.customer_id,
            environment_id=order.environment_id,
            phase=phase_by_status.get(order.status, "discovery"),
            as_is=_text("as_is"),
            to_be=_text("to_be"),
            uat=uat,
            sla=sla,
            release=release,
            rollback=rollback,
            roi=roi,
            trace=trace,
        )

    @staticmethod
    def _normalize_text_list(values: list[Any]) -> list[str]:
        return list(
            dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip())
        )

    @staticmethod
    def _digest(value: Any) -> str:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def _acceptance_from_events(
        cls, order_id: str, events: list[dict[str, Any]]
    ) -> AcceptanceRecord | None:
        record: AcceptanceRecord | None = None
        for event in events:
            kind = str(event.get("kind", "") or "")
            if not kind.startswith("acceptance."):
                continue
            note = dict(event.get("note", {}) or {})
            suffix = kind.removeprefix("acceptance.")
            if suffix == "criteria_defined":
                record = AcceptanceRecord(
                    order_id=order_id,
                    status="criteria_defined",
                    criteria=cls._normalize_text_list(list(note.get("criteria", []) or [])),
                    criteria_hash=str(note.get("criteria_hash", "") or ""),
                    workflow_version=str(note.get("workflow_version", "") or ""),
                    round_no=max(1, int(note.get("round_no", 1) or 1)),
                )
                continue
            if record is None:
                continue
            if suffix == "rework_started":
                previous = record.to_payload()
                previous.pop("history", None)
                previous["artifact_snapshot"] = dict(note.get("artifact_snapshot", {}) or {})
                record = AcceptanceRecord(
                    order_id=order_id,
                    status="criteria_defined",
                    criteria=list(record.criteria),
                    criteria_hash=record.criteria_hash,
                    workflow_version=record.workflow_version,
                    round_no=max(record.round_no + 1, int(note.get("round_no", 0) or 0)),
                    rework_target=str(note.get("target_node", "") or ""),
                    rework_feedback=str(note.get("feedback", "") or ""),
                    rework_request_id=str(note.get("request_id", "") or ""),
                    previous_evidence_hash=str(note.get("previous_evidence_hash", "") or ""),
                    previous_artifact_snapshot=dict(note.get("artifact_snapshot", {}) or {}),
                    history=[*record.history, previous],
                )
            elif suffix == "evidence_added":
                record.status = "evidence_collecting"
                evidence = dict(note.get("evidence", {}) or {})
                evidence.update(
                    {
                        "criterion": str(note.get("criterion", "") or ""),
                        "recorded_by": str(event.get("actor", "") or ""),
                        "recorded_at": str(event.get("at", "") or ""),
                    }
                )
                record.evidence.append(evidence)
            elif suffix == "submitted":
                record.status = "submitted"
                record.evidence_hash = str(note.get("evidence_hash", "") or "")
                record.submitted_by = str(event.get("actor", "") or "")
                record.submitted_at = str(event.get("at", "") or "")
            elif suffix in ACCEPTANCE_DECISIONS:
                record.status = suffix
                record.decided_by = str(event.get("actor", "") or "")
                record.decided_at = str(event.get("at", "") or "")
                record.decision_note = str(note.get("note", "") or "")
                record.exceptions = cls._normalize_text_list(
                    list(note.get("exceptions", []) or [])
                )
                record.artifact_snapshot = dict(
                    note.get("artifact_snapshot", {}) or {}
                )
        return record

    def start_acceptance_rework(
        self,
        order_id: str,
        *,
        target_node: str,
        actor: str,
        request_id: str,
        artifact_snapshot: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> AcceptanceRecord:
        """Open the next contractual acceptance round after a rejection.

        The rejection and every old evidence item stay in the signed order
        event chain.  This event only moves the projection to a clean round;
        it never overwrites an artifact or pretends the earlier verdict did
        not happen. ``request_id`` makes a retried UI/API request converge on
        the same round.
        """
        clean_actor = str(actor or "").strip()
        clean_target = str(target_node or "").strip()
        clean_request_id = str(request_id or "").strip()
        clean_snapshot = {
            str(key): value for key, value in dict(artifact_snapshot or {}).items()
            if str(key).strip() and value is not None and value != ""
        }
        if not clean_actor:
            raise OrderTransitionError("acceptance rework actor is required")
        if not clean_target:
            raise OrderTransitionError("acceptance rework target node is required")
        if not clean_request_id:
            raise OrderTransitionError("acceptance rework request_id is required")
        order = self.get_order(order_id, project_id=project_id)
        if order is None:
            raise OrderTransitionError(f"order {order_id!r} not found")
        current = self.acceptance(order_id, project_id=project_id)
        if current is None:
            raise OrderTransitionError("acceptance criteria have not been defined")
        if current.rework_request_id == clean_request_id:
            return current
        for event in self.events(order_id):
            if (
                event.get("kind") == "acceptance.rework_started"
                and str(dict(event.get("note", {}) or {}).get("request_id", "") or "") == clean_request_id
            ):
                raise OrderTransitionError("acceptance rework request_id was already used by an earlier round")
        if current.status != "rejected":
            raise OrderTransitionError(
                f"acceptance rework cannot start in state {current.status}"
            )
        if order.status not in {"delivering", "producing"}:
            raise OrderTransitionError(
                f"order cannot enter acceptance rework from {order.status}"
            )
        now = datetime.now().isoformat()
        next_round = current.round_no + 1
        command_digest = hashlib.sha256(
            f"{order_id}\0{clean_request_id}".encode("utf-8")
        ).hexdigest()
        outbox_id = f"business-outbox:acceptance-rework:{command_digest}"
        with self._connect() as conn:
            if order.status == "delivering":
                changed = conn.execute(
                    "UPDATE work_orders SET status = 'producing', updated_at = ? "
                    "WHERE order_id = ? AND status = 'delivering'",
                    (now, order_id),
                ).rowcount
                if changed != 1:
                    raise OrderTransitionError("order changed while acceptance rework was starting")
                self._append_event(
                    conn,
                    table="order_events",
                    aggregate_column="order_id",
                    aggregate_id=order_id,
                    at=now,
                    kind="producing",
                    actor=clean_actor,
                    note={"note": "contractual acceptance rework", "round_no": next_round},
                    correlation_id=order_id,
                )
            self._append_event(
                conn,
                table="order_events",
                aggregate_column="order_id",
                aggregate_id=order_id,
                at=now,
                kind="acceptance.rework_started",
                actor=clean_actor,
                note={
                    "round_no": next_round,
                    "target_node": clean_target,
                    "feedback": current.decision_note,
                    "request_id": clean_request_id,
                    "previous_evidence_hash": current.evidence_hash,
                    "artifact_snapshot": clean_snapshot,
                },
                correlation_id=order_id,
                causation_id=clean_request_id,
            )
            command_payload = {
                "schema_version": 1,
                "command": "acceptance.rework",
                "order_id": order.order_id,
                "project_id": order.project_id,
                "workflow_id": order.workflow_id,
                "workflow_version": current.workflow_version,
                "launched_task_id": order.launched_task_id,
                "customer_id": order.customer_id,
                "environment_id": order.environment_id,
                "round_no": next_round,
                "target_node": clean_target,
                "feedback": current.decision_note,
                "request_id": clean_request_id,
                "previous_evidence_hash": current.evidence_hash,
            }
            conn.execute(
                """INSERT INTO business_outbox
                   (outbox_id, aggregate_type, aggregate_id, event_type,
                    payload_json, status, attempt_count, last_error,
                    created_at, published_at)
                   VALUES (?, 'work_order', ?, 'acceptance_rework_requested',
                           ?, 'pending', 0, '', ?, '')""",
                (
                    outbox_id,
                    order.order_id,
                    json.dumps(
                        command_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
        refreshed = self.acceptance(order_id, project_id=project_id)
        assert refreshed is not None
        return refreshed

    def pending_acceptance_rework_command(
        self,
        order_id: str,
        *,
        request_id: str = "",
        project_id: str | None = None,
    ) -> BusinessOutboxRecord | None:
        """Return the exact durable runtime command for one acceptance round."""

        clean_order_id = str(order_id or "").strip()
        if project_id is not None and self.get_order(
            clean_order_id, project_id=project_id
        ) is None:
            return None
        clauses = [
            "aggregate_type = 'work_order'",
            "aggregate_id = ?",
            "event_type = 'acceptance_rework_requested'",
            "status = 'pending'",
        ]
        params: list[Any] = [clean_order_id]
        clean_request_id = str(request_id or "").strip()
        if clean_request_id:
            clauses.append("json_extract(payload_json, '$.request_id') = ?")
            params.append(clean_request_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM business_outbox WHERE "
                + " AND ".join(clauses)
                + " ORDER BY sequence LIMIT 1",
                tuple(params),
            ).fetchone()
        return self._row_to_outbox(row) if row is not None else None

    def list_pending_acceptance_rework_commands(
        self, *, limit: int = 100
    ) -> list[BusinessOutboxRecord]:
        bounded_limit = max(1, min(int(limit or 100), 1000))
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM business_outbox
                   WHERE event_type = 'acceptance_rework_requested'
                     AND status = 'pending'
                   ORDER BY sequence LIMIT ?""",
                (bounded_limit,),
            ).fetchall()
        return [self._row_to_outbox(row) for row in rows]

    def record_acceptance_rework_failure(
        self, outbox_id: str, error: str
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE business_outbox
                   SET attempt_count = attempt_count + 1, last_error = ?
                   WHERE outbox_id = ?
                     AND event_type = 'acceptance_rework_requested'
                     AND status = 'pending'""",
                (
                    str(error or "").strip()[:2000],
                    str(outbox_id or "").strip(),
                ),
            )
        return cursor.rowcount == 1

    def complete_acceptance_rework(
        self,
        outbox_id: str,
        *,
        runtime_run_id: str,
        runtime_session_id: str,
        runtime_task_id: str,
        project_id: str | None = None,
    ) -> BusinessOutboxRecord:
        """Acknowledge a runtime reset only after its durable commit."""

        clean_outbox_id = str(outbox_id or "").strip()
        clean_run_id = str(runtime_run_id or "").strip()
        clean_session_id = str(runtime_session_id or "").strip()
        clean_task_id = str(runtime_task_id or "").strip()
        if not all(
            (clean_outbox_id, clean_run_id, clean_session_id, clean_task_id)
        ):
            raise OrderTransitionError(
                "acceptance rework completion requires runtime identity"
            )
        now = datetime.now().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM business_outbox WHERE outbox_id = ?",
                (clean_outbox_id,),
            ).fetchone()
            if row is None:
                raise OrderTransitionError(
                    f"acceptance rework outbox {clean_outbox_id!r} not found"
                )
            command = self._row_to_outbox(row)
            if command.event_type != "acceptance_rework_requested":
                raise OrderTransitionError(
                    "outbox command is not an acceptance rework"
                )
            order = self.get_order(command.aggregate_id, project_id=project_id)
            if order is None:
                raise OrderTransitionError(
                    f"order {command.aggregate_id!r} not found"
                )
            completion = {
                "runtime_run_id": clean_run_id,
                "runtime_session_id": clean_session_id,
                "runtime_task_id": clean_task_id,
            }
            if command.status == "published":
                prior = dict(command.payload.get("runtime", {}) or {})
                if prior == completion:
                    return command
                raise OrderTransitionError(
                    "published acceptance rework conflicts with runtime identity"
                )
            if command.status != "pending":
                raise OrderTransitionError(
                    f"acceptance rework cannot complete from {command.status}"
                )
            payload = {**dict(command.payload), "runtime": completion}
            changed = conn.execute(
                """UPDATE business_outbox
                   SET payload_json = ?, status = 'published',
                       published_at = ?, last_error = ''
                   WHERE outbox_id = ? AND status = 'pending'""",
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now,
                    clean_outbox_id,
                ),
            ).rowcount
            if changed != 1:
                raise OrderTransitionError(
                    "acceptance rework command changed before completion"
                )
            self._append_event(
                conn,
                table="order_events",
                aggregate_column="order_id",
                aggregate_id=order.order_id,
                at=now,
                kind="acceptance.rework_runtime_started",
                actor="system",
                note={
                    "request_id": str(
                        command.payload.get("request_id", "") or ""
                    ),
                    "outbox_id": clean_outbox_id,
                    **completion,
                },
                correlation_id=order.order_id,
                causation_id=clean_outbox_id,
            )
            completed_row = conn.execute(
                "SELECT * FROM business_outbox WHERE outbox_id = ?",
                (clean_outbox_id,),
            ).fetchone()
            assert completed_row is not None
            return self._row_to_outbox(completed_row)

    def acceptance(self, order_id: str, *, project_id: str | None = None) -> AcceptanceRecord | None:
        if self.get_order(order_id, project_id=project_id) is None:
            raise OrderTransitionError(f"order {order_id!r} not found")
        return self._acceptance_from_events(order_id, self.events(order_id))

    def define_acceptance(
        self,
        order_id: str,
        criteria: list[str],
        *,
        actor: str,
        workflow_version: str = "",
        project_id: str | None = None,
    ) -> AcceptanceRecord:
        clean_criteria = self._normalize_text_list(criteria)
        if not clean_criteria:
            raise OrderTransitionError("acceptance criteria cannot be empty")
        if not str(actor or "").strip():
            raise OrderTransitionError("acceptance actor is required")
        existing = self.acceptance(order_id, project_id=project_id)
        if existing is not None:
            if (
                existing.criteria == clean_criteria
                and existing.workflow_version == str(workflow_version or "").strip()
            ):
                return existing
            raise OrderTransitionError("acceptance criteria are already defined differently")
        now = datetime.now().isoformat()
        criteria_hash = self._digest(
            {"criteria": clean_criteria, "workflow_version": workflow_version}
        )
        with self._connect() as conn:
            self._append_event(
                conn,
                table="order_events",
                aggregate_column="order_id",
                aggregate_id=order_id,
                at=now,
                kind="acceptance.criteria_defined",
                actor=str(actor).strip(),
                note={
                    "criteria": clean_criteria,
                    "criteria_hash": criteria_hash,
                    "workflow_version": str(workflow_version or "").strip(),
                },
                correlation_id=order_id,
            )
        record = self.acceptance(order_id, project_id=project_id)
        assert record is not None
        return record

    @classmethod
    def _acceptance_evidence_entry(
        cls, record: AcceptanceRecord, criterion: str, evidence: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """``(criterion, evidence)`` as an evidence event records them, or the refusal: a criterion of this record, an
        immutable run and something to point at (an artifact or an event). The evidence id makes a repeat the same."""
        clean_criterion = str(criterion or "").strip()
        if clean_criterion not in record.criteria:
            raise OrderTransitionError("evidence criterion is not part of this acceptance")
        clean_evidence = {
            str(key): value
            for key, value in dict(evidence or {}).items()
            if str(key).strip() and value is not None and value != ""
        }
        if not str(clean_evidence.get("run_id", "") or "").strip():
            raise OrderTransitionError("acceptance evidence requires an immutable run_id")
        if not any(
            str(clean_evidence.get(key, "") or "").strip()
            for key in ("artifact_id", "artifact_hash", "event_id")
        ):
            raise OrderTransitionError(
                "acceptance evidence requires artifact_id, artifact_hash, or event_id"
            )
        evidence_id = str(clean_evidence.get("evidence_id", "") or "").strip()
        if not evidence_id:
            evidence_id = cls._digest(
                {"criterion": clean_criterion, "evidence": clean_evidence}
            )
        clean_evidence["evidence_id"] = evidence_id
        return clean_criterion, clean_evidence

    def add_acceptance_evidence(
        self,
        order_id: str,
        *,
        criterion: str,
        evidence: dict[str, Any],
        actor: str,
        project_id: str | None = None,
    ) -> AcceptanceRecord:
        record = self.acceptance(order_id, project_id=project_id)
        if record is None:
            raise OrderTransitionError("acceptance criteria have not been defined")
        if record.status not in {"criteria_defined", "evidence_collecting"}:
            raise OrderTransitionError(
                f"acceptance evidence cannot be added in state {record.status}"
            )
        clean_criterion, clean_evidence = self._acceptance_evidence_entry(record, criterion, evidence)
        clean_actor = str(actor or "").strip()
        if not clean_actor:
            raise OrderTransitionError("acceptance evidence actor is required")
        evidence_id = clean_evidence["evidence_id"]
        if any(
            str(item.get("evidence_id", "") or "") == evidence_id
            for item in record.evidence
        ):
            return record
        now = datetime.now().isoformat()
        with self._connect() as conn:
            self._append_event(
                conn,
                table="order_events",
                aggregate_column="order_id",
                aggregate_id=order_id,
                at=now,
                kind="acceptance.evidence_added",
                actor=clean_actor,
                note={"criterion": clean_criterion, "evidence": clean_evidence},
                correlation_id=order_id,
            )
        refreshed = self.acceptance(order_id, project_id=project_id)
        assert refreshed is not None
        return refreshed

    def submit_acceptance(self, order_id: str, *, actor: str, project_id: str | None = None) -> AcceptanceRecord:
        record = self.acceptance(order_id, project_id=project_id)
        if record is not None and record.status in {
            "submitted",
            "accepted",
            "rejected",
            "accepted_with_exceptions",
        }:
            return record
        if record is None or record.status not in {
            "criteria_defined",
            "evidence_collecting",
        }:
            state = record.status if record else "not_started"
            raise OrderTransitionError(f"acceptance cannot be submitted in state {state}")
        covered = {str(item.get("criterion", "") or "") for item in record.evidence}
        missing = [criterion for criterion in record.criteria if criterion not in covered]
        if missing:
            raise OrderTransitionError(
                "acceptance evidence is missing for: " + "; ".join(missing)
            )
        evidence_hash = self._digest(record.evidence)
        now = datetime.now().isoformat()
        with self._connect() as conn:
            self._append_event(
                conn,
                table="order_events",
                aggregate_column="order_id",
                aggregate_id=order_id,
                at=now,
                kind="acceptance.submitted",
                actor=str(actor or "").strip(),
                note={"criteria_hash": record.criteria_hash, "evidence_hash": evidence_hash},
                correlation_id=order_id,
            )
        refreshed = self.acceptance(order_id, project_id=project_id)
        assert refreshed is not None
        return refreshed

    def decide_acceptance(
        self,
        order_id: str,
        *,
        decision: str,
        actor: str,
        note: str = "",
        exceptions: list[str] | None = None,
        artifact_snapshot: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> AcceptanceRecord:
        record = self.acceptance(order_id, project_id=project_id)
        clean_decision = str(decision or "").strip().lower()
        if record is not None and record.status in ACCEPTANCE_DECISIONS:
            if record.status == clean_decision:
                return record
            raise OrderTransitionError(
                f"acceptance is already decided as {record.status}"
            )
        if record is None or record.status != "submitted":
            state = record.status if record else "not_started"
            raise OrderTransitionError(f"acceptance cannot be decided in state {state}")
        if clean_decision not in ACCEPTANCE_DECISIONS:
            raise OrderTransitionError(f"unknown acceptance decision {decision!r}")
        clean_actor = str(actor or "").strip()
        if not clean_actor:
            raise OrderTransitionError("acceptance decision actor is required")
        if clean_decision == "rejected" and not str(note or "").strip():
            raise OrderTransitionError("acceptance rejection feedback required")
        clean_exceptions = self._normalize_text_list(exceptions or [])
        if clean_decision == "accepted_with_exceptions" and not clean_exceptions:
            raise OrderTransitionError("accepted_with_exceptions requires exceptions")
        clean_snapshot = {
            str(key): value
            for key, value in dict(artifact_snapshot or {}).items()
        }
        now = datetime.now().isoformat()
        with self._connect() as conn:
            self._append_event(
                conn,
                table="order_events",
                aggregate_column="order_id",
                aggregate_id=order_id,
                at=now,
                kind=f"acceptance.{clean_decision}",
                actor=clean_actor,
                note={
                    "note": str(note or "").strip(),
                    "exceptions": clean_exceptions,
                    "criteria_hash": record.criteria_hash,
                    "evidence_hash": record.evidence_hash,
                    "artifact_snapshot": clean_snapshot,
                },
                correlation_id=order_id,
            )
        refreshed = self.acceptance(order_id, project_id=project_id)
        assert refreshed is not None
        return refreshed

    def _acceptance_events_on(self, conn: sqlite3.Connection, order_id: str) -> list[dict[str, Any]]:
        """The order's events as ``events`` reads them for the record, on one connection: what an open transaction
        has written counts too."""
        out: list[dict[str, Any]] = []
        for row in conn.execute(
            "SELECT at, kind, actor, note_json, event_id FROM order_events WHERE order_id = ? ORDER BY id",
            (order_id,),
        ).fetchall():
            try:
                note = json.loads(row["note_json"] or "{}")
            except ValueError:
                note = {}
            out.append({"at": row["at"], "kind": row["kind"], "actor": row["actor"], "note": note,
                        "event_id": str(row["event_id"] or "")})
        return out

    def settle_acceptance(
        self,
        order_id: str,
        *,
        decision: str,
        actor: str,
        note: str = "",
        exceptions: list[str] | None = None,
        receipts: list[tuple[str, dict[str, Any]]] | None = None,
        artifact_snapshot: dict[str, Any] | None = None,
        frozen_files: dict[str, str] | None = None,
        snapshot_required: bool = True,
        close_order: bool = False,
        close_note: str = "",
        project_id: str | None = None,
    ) -> tuple[AcceptanceRecord, WorkOrder]:
        """Record the end of an acceptance round in one transaction (C5-2, policy Q14/Q16).

        With ``close_order`` (确认收货并结案) the owner's receipts not yet recorded (``receipts``, as ``(criterion,
        evidence)``), the submission, the decision and the order's close are written together or not at all; without
        it, only the decision of a submitted round. Everything is decided on a fresh read of the order and its record
        under the write lock. A decided record keeps its decision: repeated, nothing is written; with ``close_order`` a
        passing one is only closed, and an order an earlier click already closed is returned as it is.

        Path-backed evidence => a frozen round: when any evidence of the final record names a file, the decision is
        recorded only with ``artifact_snapshot`` and ``frozen_files`` (workplace path -> sha256 of the frozen copy, as
        the caller verified it on disk) naming exactly those files, each with the digest its evidence was bound to.
        ``snapshot_required=False`` lets a rejection whose files could no longer be frozen be recorded without one.
        Refusals are in the owner's words: this is the owner's road. The order's allowance is closed after a close,
        as ``transition`` does.
        """
        from os.path import normcase

        clean_order_id = str(order_id or "").strip()
        clean_decision = str(decision or "").strip().lower()
        clean_actor = str(actor or "").strip()
        clean_note = str(note or "").strip()
        clean_close_note = str(close_note or "").strip()
        clean_exceptions = self._normalize_text_list(exceptions or [])
        clean_snapshot = {str(key): value for key, value in dict(artifact_snapshot or {}).items()}
        frozen = {
            normcase(str(path or "").strip().replace("\\", "/")): str(digest or "").strip().lower()
            for path, digest in dict(frozen_files or {}).items()
            if str(path or "").strip()
        }
        if clean_decision not in ACCEPTANCE_DECISIONS:
            raise OrderTransitionError("不认识这个验收决定")
        if not clean_actor:
            raise OrderTransitionError("验收决定要写明是谁做的")
        if close_order and clean_decision not in ACCEPTANCE_PASSING:
            raise OrderTransitionError("只有验收通过才能结案")
        wanted_project = None if project_id is None else (str(project_id or "default").strip() or "default")
        now = datetime.now().isoformat()

        def rebuilt(conn: sqlite3.Connection) -> AcceptanceRecord | None:
            return self._acceptance_from_events(clean_order_id, self._acceptance_events_on(conn, clean_order_id))

        def append(conn: sqlite3.Connection, kind: str, note_body: dict[str, Any]) -> None:
            self._append_event(
                conn, table="order_events", aggregate_column="order_id", aggregate_id=clean_order_id, at=now,
                kind=kind, actor=clean_actor, note=note_body, correlation_id=clean_order_id,
            )

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM work_orders WHERE order_id = ?", (clean_order_id,)).fetchone()
            order = self._row_to_order(row) if row is not None else None
            if order is None or (wanted_project is not None and order.project_id != wanted_project):
                raise OrderTransitionError("找不到这张工单")
            record = rebuilt(conn)
            if record is None:
                raise OrderTransitionError("这张工单还没有验收标准")
            if close_order and order.status == "closed":
                if record.status not in ACCEPTANCE_PASSING:
                    raise OrderTransitionError("这张工单已经结束了，不能再确认收货")
                # Closed by an earlier click whose answer never arrived: nothing more to record.
            elif close_order and order.status != "delivering":
                raise OrderTransitionError("这张工单不在客户验收中，不能确认收货结案")
            elif record.status in ACCEPTANCE_DECISIONS:
                if close_order and record.status == "rejected":
                    raise OrderTransitionError("这张工单的验收已经打回，要结案得先走下一轮验收")
                if not close_order and record.status != clean_decision:
                    raise OrderTransitionError(
                        f"这一轮验收已经{_ACCEPTANCE_DECISION_WORDS[record.status]}了，"
                        f"不能再改成{_ACCEPTANCE_DECISION_WORDS[clean_decision]}"
                    )
                # A repeated decision records nothing; a round passed on the card is only closed below.
            else:
                if clean_decision == "rejected" and not clean_note:
                    raise OrderTransitionError("打回要写一句原因")
                if clean_decision == "accepted_with_exceptions" and not clean_exceptions:
                    raise OrderTransitionError("带例外通过要写明是哪些例外")
                if close_order and record.status in {"criteria_defined", "evidence_collecting"}:
                    seen = {str(item.get("evidence_id", "") or "") for item in record.evidence}
                    for criterion, evidence in list(receipts or []):
                        entry_criterion, entry = self._acceptance_evidence_entry(record, criterion, evidence)
                        if entry["evidence_id"] in seen:
                            continue
                        seen.add(entry["evidence_id"])
                        append(conn, "acceptance.evidence_added", {"criterion": entry_criterion, "evidence": entry})
                    record = rebuilt(conn)
                    assert record is not None
                    covered = {str(item.get("criterion", "") or "") for item in record.evidence}
                    missing = [criterion for criterion in record.criteria if criterion not in covered]
                    if missing:
                        raise OrderTransitionError("还有验收标准没有证据：" + "；".join(missing))
                    append(conn, "acceptance.submitted",
                           {"criteria_hash": record.criteria_hash, "evidence_hash": self._digest(record.evidence)})
                    record = rebuilt(conn)
                    assert record is not None
                if record.status != "submitted":
                    raise OrderTransitionError("这一轮验收还没提交，还不能做决定")
                named: dict[str, str] = {}
                for item in record.evidence:
                    path = acceptance_evidence_path(item)
                    if not path:
                        continue
                    key = normcase(path)
                    digest = str(item.get("artifact_hash", "") or "").strip().lower()
                    if digest and named.get(key) and named[key] != digest:
                        raise OrderTransitionError(f"交付文件「{path}」在这一轮里记了两个不同的版本")
                    named[key] = digest or named.get(key, "")
                if clean_snapshot:
                    if (
                        not named
                        or set(named) != set(frozen)
                        or any(digest and frozen[key] != digest for key, digest in named.items())
                    ):
                        raise OrderTransitionError("冻结下来的交付文件和验收证据对不上（验收记录刚刚有了变化）")
                elif named and snapshot_required:
                    raise OrderTransitionError("验收证据指向的交付文件还没有冻结，不能记下验收决定")
                append(conn, f"acceptance.{clean_decision}", {
                    "note": clean_note,
                    "exceptions": clean_exceptions,
                    "criteria_hash": record.criteria_hash,
                    "evidence_hash": record.evidence_hash,
                    "artifact_snapshot": clean_snapshot,
                })
                record = rebuilt(conn)
                assert record is not None
            if close_order and order.status == "delivering":
                changed = conn.execute(
                    """UPDATE work_orders SET status = 'closed', updated_at = ?,
                       review_note = CASE WHEN ? != '' THEN ? ELSE review_note END
                       WHERE order_id = ? AND status = 'delivering'""",
                    (now, clean_close_note, clean_close_note, clean_order_id),
                ).rowcount
                if changed != 1:
                    raise OrderTransitionError("这张工单的状态刚刚变了，这次没有结案")
                append(conn, "closed", {"note": clean_close_note})
            refreshed = conn.execute("SELECT * FROM work_orders WHERE order_id = ?", (clean_order_id,)).fetchone()
            assert refreshed is not None
            order = self._row_to_order(refreshed)
        if close_order and order.status == "closed":
            # As ``transition``: an order that is over spends nothing more. Also on a repeated click, so one that
            # stopped before this line is finished by the next.
            try:
                allowance = self.open_budget_for_order(clean_order_id)
                if allowance is not None:
                    self.close_run_budget(str(allowance["budget_id"]), actor=clean_actor, reason="order closed")
            except Exception:  # noqa: BLE001 - the order's own close has already been recorded
                pass
        return record, order

    # ── redistribution authorization: a customer's explicit grant, on their accepted order ──
    REDISTRIBUTION_EVENT = "redistribution.authorized"

    def authorize_redistribution(
        self,
        order_id: str,
        *,
        actor: str,
        workflow_id: str,
        version: str,
        candidate_fingerprint: str,
        manifest_sha256: str,
        note: str = "",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Record that this order's customer authorized redistributing the exact workflow version it accepted.

        Acceptance proves the delivery worked; it grants no intellectual-property right. The grant is an
        immutable event on the accepted order, bound to the exact version, candidate fingerprint and manifest
        digest, so a later version or an edited package is not covered. Only a clean acceptance can carry it.
        """
        clean_actor = str(actor or "").strip()
        if not clean_actor:
            raise OrderTransitionError("redistribution authorization actor is required")
        order = self.get_order(order_id, project_id=project_id)
        if order is None:
            raise OrderTransitionError(f"order {order_id!r} not found")
        clean_workflow = str(workflow_id or "").strip()
        if not clean_workflow or str(order.workflow_id or "") != clean_workflow:
            raise OrderTransitionError("redistribution authorization names a workflow this order did not run")
        record = self.acceptance(order.order_id, project_id=project_id)
        if record is None or record.status != "accepted":
            state = record.status if record else "not_started"
            raise OrderTransitionError(f"only a fully accepted order can authorize redistribution (acceptance is {state})")
        clean_version = str(version or "").strip()
        if not clean_version or record.workflow_version != clean_version:
            raise OrderTransitionError("redistribution authorization must name the workflow version this order accepted")
        fingerprint = str(candidate_fingerprint or "").strip().lower()
        manifest = str(manifest_sha256 or "").strip().lower()
        if not _SHA256_HEX.fullmatch(fingerprint) or not _SHA256_HEX.fullmatch(manifest):
            raise OrderTransitionError("redistribution authorization needs the exact candidate fingerprint and manifest digest")
        for existing in self.redistribution_authorizations(order.order_id):
            if (existing["version"], existing["candidate_fingerprint"], existing["manifest_sha256"]) == (clean_version, fingerprint, manifest):
                return existing  # the same grant twice is one grant
        now = datetime.now().isoformat()
        with self._connect() as conn:
            self._append_event(
                conn,
                table="order_events",
                aggregate_column="order_id",
                aggregate_id=order.order_id,
                at=now,
                kind=self.REDISTRIBUTION_EVENT,
                actor=clean_actor,
                note={
                    "workflow_id": clean_workflow,
                    "version": clean_version,
                    "candidate_fingerprint": fingerprint,
                    "manifest_sha256": manifest,
                    "note": str(note or "").strip()[:500],
                    "criteria_hash": record.criteria_hash,
                    "evidence_hash": record.evidence_hash,
                },
                correlation_id=order.order_id,
            )
        return self.redistribution_authorizations(order.order_id)[-1]

    def redistribution_authorizations(self, order_id: str) -> list[dict[str, Any]]:
        """The grants recorded on one order, oldest first; safe fields only."""
        out: list[dict[str, Any]] = []
        for event in self.events(order_id):
            if event.get("kind") != self.REDISTRIBUTION_EVENT:
                continue
            note = dict(event.get("note", {}) or {})
            out.append({
                "order_id": str(order_id),
                "workflow_id": str(note.get("workflow_id", "") or ""),
                "version": str(note.get("version", "") or ""),
                "candidate_fingerprint": str(note.get("candidate_fingerprint", "") or ""),
                "manifest_sha256": str(note.get("manifest_sha256", "") or ""),
                "authorized_by": str(event.get("actor", "") or ""),
                "authorized_at": str(event.get("at", "") or ""),
                "event_id": str(event.get("event_id", "") or ""),
            })
        return out

    # ── accounts (raw rows; the AccountStore owns hashing and sessions) ──
    @contextmanager
    def _account_principal_ro_connection(self, *, _account_store=None):
        """Own one existing DELETE/WAL RO snapshot; never initialize or repair."""
        dependency_failed = False
        try:
            import os
            import stat
            from .accounts import AccountStore as _AccountStore
            from .current_account_principal import CurrentAccountPrincipalError as _CurrentError
        except Exception:
            dependency_failed = True
        if dependency_failed:
            raise RuntimeError("当前账号记录暂时无法读取。") from None

        def file_snapshot(path):
            parts = path.parts
            if (
                not path.is_absolute() or path.drive.startswith("\\\\")
                or path.name != "business.db" or not 1 <= len(parts) <= 16
            ):
                raise _CurrentError("invalid_source")
            current = type(path)(parts[0])
            info = None
            for index in range(len(parts)):
                if index:
                    current = current / parts[index]
                info = current.lstat()
                attributes = getattr(info, "st_file_attributes", 0)
                if stat.S_ISLNK(info.st_mode) or attributes & 0x400:
                    raise _CurrentError("invalid_source")
                if index < len(parts) - 1:
                    if not stat.S_ISDIR(info.st_mode):
                        raise _CurrentError("invalid_source")
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise _CurrentError("invalid_source")
            return (info.st_dev, info.st_ino, info.st_nlink)

        def safe_sidecars(path):
            for suffix in ("-wal", "-shm", "-journal"):
                try:
                    info = path.with_name(path.name + suffix).lstat()
                except FileNotFoundError:
                    continue
                if suffix == "-journal":
                    # Presence guard only; do not call every journal "hot".
                    raise _CurrentError("unavailable")
                attributes = getattr(info, "st_file_attributes", 0)
                if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or attributes & 0x400 or info.st_nlink != 1):
                    raise _CurrentError("invalid_source")

        def database_header(path, identity):
            stream = None
            header = None
            header_primary = None
            try:
                stream = path.open("rb")
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or (info.st_dev, info.st_ino, info.st_nlink) != identity):
                    raise _CurrentError("invalid_source")
                header = stream.read(20)
            except BaseException as exc:
                header_primary = exc
            finally:
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException as exc:
                        if header_primary is None:
                            header_primary = exc
            if header_primary is not None:
                raise header_primary
            if (type(header) is not bytes or len(header) != 20
                    or header[:16] != b"SQLite format 3\x00"):
                raise _CurrentError("invalid_source")
            versions = (header[18], header[19])
            if versions not in ((1, 1), (2, 2)):
                raise _CurrentError("unavailable")
            if file_snapshot(path) != identity:
                raise _CurrentError("invalid_source")
            return versions

        conn = None
        path = None
        raw_path = None
        raw_home = None
        before = None
        primary = None
        try:
            if type(self) is not BusinessStore:
                raise _CurrentError("invalid_source")
            cwd = Path.cwd().absolute()
            raw_path = getattr(self, "path", None)
            if (
                type(raw_path) is not type(cwd) or raw_path.drive.startswith("\\\\")
                or raw_path.drive and not raw_path.root
            ):
                raise _CurrentError("invalid_source")
            path = type(cwd)(os.path.normpath(os.fspath(
                raw_path if raw_path.is_absolute() else cwd / raw_path
            )))
            if _account_store is not None:
                if (type(_account_store) is not _AccountStore
                        or getattr(_account_store, "business", None) is not self):
                    raise _CurrentError("invalid_source")
                raw_home = getattr(_account_store, "home", None)
                if (type(raw_home) is not type(cwd) or raw_home.drive.startswith("\\\\")
                        or raw_home.drive and not raw_home.root):
                    raise _CurrentError("invalid_source")
                expected = type(cwd)(os.path.normpath(os.fspath(
                    (raw_home if raw_home.is_absolute() else cwd / raw_home) / "business.db"
                )))
                if path != expected:
                    raise _CurrentError("invalid_source")

            def source_unchanged():
                if type(self) is not BusinessStore or self.path is not raw_path:
                    raise _CurrentError("invalid_source")
                if _account_store is not None and (
                    type(_account_store) is not _AccountStore
                    or _account_store.business is not self or _account_store.home is not raw_home
                ):
                    raise _CurrentError("invalid_source")

            source_unchanged()
            before = file_snapshot(path)
            safe_sidecars(path)
            header_versions = database_header(path, before)
            source_unchanged()
            safe_sidecars(path)
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                   timeout=0.0, isolation_level=None)
            if type(conn) is not sqlite3.Connection:
                raise _CurrentError("invalid_source")

            def sql_rows(sql):
                cursor = None
                sql_primary = None
                rows = None
                try:
                    cursor = sqlite3.Connection.execute(conn, sql)
                    rows = sqlite3.Cursor.fetchmany(cursor, 2)
                except BaseException as exc:
                    sql_primary = exc
                finally:
                    if cursor is not None:
                        try:
                            sqlite3.Cursor.close(cursor)
                        except BaseException as exc:
                            if sql_primary is None:
                                sql_primary = exc
                if sql_primary is not None:
                    raise sql_primary
                return rows

            sql_rows("PRAGMA busy_timeout = 0")
            if sql_rows("PRAGMA busy_timeout") != [(0,)]:
                raise _CurrentError("invalid_source")
            sql_rows("PRAGMA query_only = ON")
            if sql_rows("PRAGMA query_only") != [(1,)]:
                raise _CurrentError("invalid_source")
            expected_mode = "delete" if header_versions == (1, 1) else "wal"
            if sql_rows("PRAGMA journal_mode") != [(expected_mode,)]:
                raise _CurrentError("unavailable")
            sql_rows("BEGIN")
            if (
                conn.in_transaction is not True or conn.row_factory is not None
                or conn.text_factory is not str
            ):
                raise _CurrentError("invalid_source")
            source_unchanged()
            yield conn
        except BaseException as exc:
            primary = exc
        finally:
            if conn is not None:
                try:
                    sqlite3.Connection.rollback(conn)
                except BaseException as exc:
                    if primary is None:
                        primary = exc
                try:
                    sqlite3.Connection.close(conn)
                except BaseException as exc:
                    if primary is None:
                        primary = exc
            if before is not None:
                try:
                    source_unchanged()
                    if file_snapshot(path) != before:
                        raise _CurrentError("invalid_source")
                    safe_sidecars(path)
                    database_header(path, before)
                    source_unchanged()
                except BaseException as exc:
                    if primary is None:
                        primary = exc
        if primary is not None:
            if not isinstance(primary, Exception):
                raise primary
            code = "unavailable"
            if (type(primary) is _CurrentError and type(primary.code) is str
                    and primary.code in ("invalid_source", "unavailable")):
                code = primary.code
            raise _CurrentError(code) from None

    def console_session_readiness(self, *, _account_store=None) -> str:
        """Observe only version/schema metadata in one owned, bounded RO view.

        This is a diagnostic, never authentication or permission to migrate.
        Existing incomplete/unknown layouts are not repaired or called ready.
        The context manager rechecks the source identity before returning.
        """
        result = "unavailable"
        try:
            from ...database import schema_version as source_schema

            with BusinessStore._account_principal_ro_connection(
                self, _account_store=_account_store,
            ) as conn:
                versions = source_schema._schema_rows(
                    conn, "PRAGMA main.user_version", limit=1,
                )
                if (len(versions) == 1 and len(versions[0]) == 1
                        and type(versions[0][0]) is int
                        and versions[0][0] in (5, 6)
                        and source_schema._source_schema_state(
                            conn, source_schema.BUSINESS,
                        ) == "complete"):
                    state = source_schema._console_session_schema_state(conn)
                    if versions[0][0] == 5 and state == "absent":
                        result = "schema_required"
                    elif versions[0][0] == 6 and state == "complete":
                        result = "ready"
        except Exception:
            return "unavailable"
        return result

    def _verify_fresh_launch_source_schema(self) -> None:
        """Check the complete existing business bundle in the original RO host.

        No Account, credential, order or budget row is read by this check.
        This is not approval and does not share the runtime database snapshot.
        """
        unavailable = False
        try:
            from ...database import schema_version as _source_schema
            if type(self) is not BusinessStore:
                raise RuntimeError("unavailable")
            raw_path = self.path
            with BusinessStore._account_principal_ro_connection(self) as conn:
                if (type(conn) is not sqlite3.Connection or conn.in_transaction is not True
                        or conn.row_factory is not None or conn.text_factory is not str):
                    raise RuntimeError("unavailable")
                if _source_schema._schema_rows(conn, "PRAGMA user_version", limit=1) != [(_source_schema.current_version(_source_schema.BUSINESS),)]:
                    raise RuntimeError("unavailable")
                if _source_schema._source_schema_state(conn, _source_schema.BUSINESS) != "complete":
                    raise RuntimeError("unavailable")
                if type(self) is not BusinessStore or self.path is not raw_path:
                    raise RuntimeError("unavailable")
            if type(self) is not BusinessStore or self.path is not raw_path:
                raise RuntimeError("unavailable")
        except Exception:
            unavailable = True
        if unavailable:
            raise RuntimeError("fresh_launch_source_schema_unavailable") from None

    def _read_account_principal_current(self, *, login: str, _account_store=None):
        dependency_failed = False
        try:
            from .current_account_principal import (
                CurrentAccountPrincipalError as _CurrentError,
                _PrincipalFault as _CurrentFault,
                _login,
                _read_account_principal_on_connection,
            )
        except Exception:
            dependency_failed = True
        if dependency_failed:
            raise RuntimeError("当前账号记录暂时无法读取。") from None
        failure = None
        result = None
        try:
            # Reuse the original bounded scalar validator BEFORE any connect.
            _login(login)
            with BusinessStore._account_principal_ro_connection(self, _account_store=_account_store) as conn:
                result = _read_account_principal_on_connection(conn, login=login)
        except Exception as exc:
            failure = "unavailable"
            if (type(exc) in (_CurrentError, _CurrentFault) and type(exc.code) is str
                    and exc.code in ("invalid_source", "unavailable")):
                failure = exc.code
        if failure is not None:
            raise _CurrentError(failure) from None
        return result

    def _has_accounts_current(self, *, _account_store=None) -> bool:
        dependency_failed = False
        try:
            from .current_account_principal import (
                CurrentAccountPrincipalError as _CurrentError,
                _PrincipalFault as _CurrentFault,
                _account_schema,
                _owned_read_connection,
                _rows,
            )
        except Exception:
            dependency_failed = True
        if dependency_failed:
            raise RuntimeError("当前账号记录暂时无法读取。") from None
        failure = None
        result = None
        try:
            with BusinessStore._account_principal_ro_connection(self, _account_store=_account_store) as conn:
                _owned_read_connection(conn)
                _account_schema(conn)
                rows = _rows(conn, "SELECT 1 FROM main.accounts "
                             "INDEXED BY sqlite_autoindex_accounts_2 LIMIT 1", (), 1)
                if rows not in ([], [(1,)]) or rows and type(rows[0][0]) is not int:
                    raise _CurrentError("invalid_source")
                result = bool(rows)
                _owned_read_connection(conn)
        except Exception as exc:
            failure = "unavailable"
            if (type(exc) in (_CurrentError, _CurrentFault) and type(exc.code) is str
                    and exc.code in ("invalid_source", "unavailable")):
                failure = exc.code
        if failure is not None:
            raise _CurrentError(failure) from None
        return result

    def issue_console_session(
        self, token_sha256, login_key, issued_at, expires_at,
        *, expected_salt, expected_password_hash,
    ) -> bool:
        from . import console_session_store as sessions
        with sessions.boundary():
            if type(self) is not BusinessStore:
                raise sessions.CurrentAccountPrincipalError("invalid_source")
            sessions.digest(token_sha256)
            sessions.login(login_key)
            sessions.times(issued_at, expires_at)
            sessions.fingerprint(expected_salt, expected_password_hash)
            raw_path = self.path
            before_open = sessions.workspace_path_identity(raw_path)
            with BusinessStore._connect(self) as conn:
                conn.execute("BEGIN IMMEDIATE").close()
                sessions.require_migrated(conn)
                workspace = sessions.workspace_identity(self.path, conn)
                if self.path is not raw_path or workspace != before_open:
                    raise sessions.CurrentAccountPrincipalError("invalid_source")
                issued = sessions.issue(conn, token_sha256, login_key, issued_at, expires_at,
                    expected_salt=expected_salt, expected_password_hash=expected_password_hash,
                    workspace_binding=workspace)
                if self.path is not raw_path or sessions.workspace_identity(self.path, conn) != workspace:
                    raise sessions.CurrentAccountPrincipalError("invalid_source")
                return issued

    def read_console_session_current(self, token_sha256, *, now, _account_store,
                                     _with_principal: bool = False):
        from . import console_session_store as sessions
        with sessions.boundary():
            sessions.digest(token_sha256)
            sessions.moment(now)
            if _account_store is None or type(_with_principal) is not bool:
                raise sessions.CurrentAccountPrincipalError("invalid_source")
            with BusinessStore._account_principal_ro_connection(self, _account_store=_account_store) as conn:
                sessions.require_migrated(conn)
                workspace = sessions.workspace_identity(self.path, conn)
                result = sessions.read(conn, token_sha256, now=now, workspace_binding=workspace)
                if result is not None and _with_principal:
                    from .current_account_principal import _read_account_principal_on_connection
                    result = dict(result)
                    result["principal"] = _read_account_principal_on_connection(
                        conn, login=result["login_key"],
                    )
                if sessions.workspace_identity(self.path, conn) != workspace:
                    raise sessions.CurrentAccountPrincipalError("invalid_source")
                return result

    def revoke_console_session(self, token_sha256, *, now) -> None:
        from . import console_session_store as sessions
        with sessions.boundary():
            if type(self) is not BusinessStore:
                raise sessions.CurrentAccountPrincipalError("invalid_source")
            sessions.digest(token_sha256)
            sessions.moment(now)
            with BusinessStore._connect(self) as conn:
                conn.execute("BEGIN IMMEDIATE").close()
                sessions.require_migrated(conn)
                sessions.revoke_token(conn, token_sha256, now=now)

    def revoke_console_login(self, login_key, *, now) -> None:
        from . import console_session_store as sessions
        with sessions.boundary():
            if type(self) is not BusinessStore:
                raise sessions.CurrentAccountPrincipalError("invalid_source")
            sessions.login(login_key)
            sessions.moment(now)
            with BusinessStore._connect(self) as conn:
                conn.execute("BEGIN IMMEDIATE").close()
                sessions.require_migrated(conn)
                sessions.revoke_login(conn, login_key, now=now)

    def account_rows(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM accounts").fetchall()
        return [dict(r) for r in rows]

    def account_proxy_rows(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT login, proxy_for FROM account_proxies").fetchall()
        return [dict(r) for r in rows]

    def set_account_proxy(self, login: str, proxy_for: str) -> None:
        clean = str(login or "").strip()
        target = str(proxy_for or "").strip()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE").close()
            cursor = conn.execute("SELECT proxy_for FROM main.account_proxies WHERE login=?", (clean,))
            try:
                previous = cursor.fetchone()
            finally:
                cursor.close()
            previous_target = str(previous[0] or "") if previous is not None else ""
            if previous_target != target:
                import time
                from . import console_session_store as sessions
                with sessions.boundary():
                    sessions.revoke_login(conn, clean.lower(), now=time.time())
            if target:
                conn.execute("INSERT INTO account_proxies(login, proxy_for) VALUES(?, ?) "
                             "ON CONFLICT(login) DO UPDATE SET proxy_for=excluded.proxy_for", (clean, target))
            else:
                conn.execute("DELETE FROM account_proxies WHERE login = ?", (clean,))

    def upsert_account(self, row: dict[str, Any]) -> None:
        with self._connect() as conn:
            # Credential/disable changes and durable session revocation share
            # one write transaction. A failed revocation cannot leave a changed
            # account whose old session becomes valid after a restart.
            conn.execute("BEGIN IMMEDIATE").close()
            login_key = str(row.get("login", "")).lower()
            cursor = conn.execute(
                "SELECT salt,password_hash,disabled FROM main.accounts WHERE login_key=?",
                (login_key,),
            )
            try:
                prior = cursor.fetchone()
            finally:
                cursor.close()
            changed = prior is not None and tuple(prior) != (
                str(row.get("salt", "")), str(row.get("password_hash", "")),
                1 if row.get("disabled") else 0,
            )
            if changed:
                import time
                from . import console_session_store as sessions
                with sessions.boundary():
                    sessions.revoke_login(conn, login_key, now=time.time())
            conn.execute(
                """INSERT INTO accounts(login, login_key, role, employee_id, display_name, disabled, salt, password_hash, created_at, last_login_at, customer_ids)
                   VALUES(:login, :login_key, :role, :employee_id, :display_name, :disabled, :salt, :password_hash, :created_at, :last_login_at, :customer_ids)
                   ON CONFLICT(login_key) DO UPDATE SET role=excluded.role, employee_id=excluded.employee_id,
                   display_name=excluded.display_name, disabled=excluded.disabled, salt=excluded.salt,
                   password_hash=excluded.password_hash, last_login_at=excluded.last_login_at,
                   customer_ids=excluded.customer_ids""",
                {
                    "login": str(row.get("login", "")),
                    "login_key": str(row.get("login", "")).lower(),
                    "role": str(row.get("role", "employee")),
                    "employee_id": str(row.get("employee_id", "") or ""),
                    "display_name": str(row.get("display_name", "") or ""),
                    "disabled": 1 if row.get("disabled") else 0,
                    "salt": str(row.get("salt", "")),
                    "password_hash": str(row.get("password_hash", "")),
                    "created_at": str(row.get("created_at", "") or datetime.now().isoformat()),
                    "last_login_at": str(row.get("last_login_at", "") or ""),
                    "customer_ids": ",".join(
                        str(c).strip() for c in list(row.get("customer_ids", []) or []) if str(c).strip()
                    ) if not isinstance(row.get("customer_ids"), str) else str(row.get("customer_ids", "") or ""),
                },
            )

    def account_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])

    # ── gate cards (human gates announced in a chat group) ──
    def record_gate_card(self, *, checkpoint_id: str, message_id: str, chat_id: str, task_id: str, order_id: str, version: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO gate_cards(checkpoint_id, message_id, chat_id, task_id, order_id, version, sent_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(checkpoint_id) DO UPDATE SET message_id=excluded.message_id, chat_id=excluded.chat_id,
                   version=excluded.version, sent_at=excluded.sent_at""",
                (checkpoint_id, message_id, chat_id, task_id, order_id, int(version), datetime.now().isoformat()),
            )

    def gate_card(self, checkpoint_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM gate_cards WHERE checkpoint_id = ?", (checkpoint_id,)).fetchone()
        return dict(row) if row else None

    def gate_card_by_message(self, message_id: str) -> dict[str, Any] | None:
        if not message_id:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM gate_cards WHERE message_id = ?", (message_id,)).fetchone()
        return dict(row) if row else None

    def decide_gate_card(self, checkpoint_id: str, *, decision: str, decided_by: str, note: str = "") -> bool:
        """Record a decision on a card row that has none yet; False when one is already there.

        A gate is decided once.  The update is conditional so a second writer
        (a double click, the group and the console at the same moment) can
        never overwrite who decided and when.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE gate_cards SET decision = ?, decided_by = ?, decided_at = ?, note = ? WHERE checkpoint_id = ? AND decision = ''",
                (decision, decided_by, datetime.now().isoformat(), note, checkpoint_id),
            )
            return cursor.rowcount == 1

    # A decision in flight: taken, but the runtime has not accepted it yet.
    # ``decided_at`` carries this marker (claim time + a token) until the
    # decision is final, so an in-flight claim is told apart from a finished
    # one without a new column, and a claim left behind by a crash can be
    # taken over once it is old.
    GATE_CLAIM_PREFIX = "claimed:"

    def claim_gate_decision(
        self,
        checkpoint_id: str,
        *,
        decision: str,
        decided_by: str,
        note: str = "",
        task_id: str = "",
        order_id: str = "",
        version: int = 1,
        stale_after_seconds: float = 600.0,
    ) -> str:
        """Take the one right to decide this gate; returns a claim token, or "" when it is taken.

        Works whether or not a card was ever sent: a gate answered from the
        console has no card row, so one is added here (no message id) and
        the decision lives in the same table either way.  The conditional
        update is the whole answer to "am I first?": exactly one caller sees
        its row change.  A claim older than ``stale_after_seconds`` that was
        never finished (the process died while the runtime was being told)
        may be taken over; a finished decision never can.
        """
        now = datetime.now()
        token = f"{self.GATE_CLAIM_PREFIX}{now.isoformat(timespec='microseconds')}:{uuid.uuid4().hex}"
        cutoff = now - timedelta(seconds=max(0.0, float(stale_after_seconds)))
        # Tokens compare as text: prefix, a fixed-width timestamp, then ":".
        # Ending the bound with ";" (the character after ":") makes a claim
        # stamped exactly at the cutoff count as old, whatever the clock's
        # resolution.
        stale_before = f"{self.GATE_CLAIM_PREFIX}{cutoff.isoformat(timespec='microseconds')};"
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO gate_cards(checkpoint_id, task_id, order_id, version) VALUES(?,?,?,?)",
                (checkpoint_id, str(task_id or ""), str(order_id or ""), int(version or 1)),
            )
            cursor = conn.execute(
                "UPDATE gate_cards SET decision = ?, decided_by = ?, decided_at = ?, note = ? "
                "WHERE checkpoint_id = ? AND (decision = '' OR (decided_at LIKE ? AND decided_at < ?))",
                (decision, decided_by, token, note, checkpoint_id, self.GATE_CLAIM_PREFIX + "%", stale_before),
            )
            return token if cursor.rowcount == 1 else ""

    def finish_gate_decision(self, checkpoint_id: str, token: str) -> bool:
        """The runtime accepted the claimed decision: stamp it final."""
        if not token:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE gate_cards SET decided_at = ? WHERE checkpoint_id = ? AND decided_at = ?",
                (datetime.now().isoformat(), checkpoint_id, token),
            )
            return cursor.rowcount == 1

    def release_gate_decision(self, checkpoint_id: str, token: str) -> bool:
        """The runtime did not take the claimed decision: give the gate back, as it was.

        A row that exists only because of the claim (no card was ever sent)
        is removed again; a card row keeps its card and loses the decision.
        Only the holder of ``token`` can release.
        """
        if not token:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM gate_cards WHERE checkpoint_id = ? AND decided_at = ? AND message_id = ''",
                (checkpoint_id, token),
            )
            if cursor.rowcount == 1:
                return True
            cursor = conn.execute(
                "UPDATE gate_cards SET decision = '', decided_by = '', decided_at = '', note = '' "
                "WHERE checkpoint_id = ? AND decided_at = ?",
                (checkpoint_id, token),
            )
            return cursor.rowcount == 1

    def gate_claims(self) -> list[dict[str, Any]]:
        """Every gate whose decision is claimed but not final: in flight, left by a crash, or with its outcome not known."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM gate_cards WHERE decided_at LIKE ? ORDER BY rowid",
                (self.GATE_CLAIM_PREFIX + "%",),
            ).fetchall()
        return [dict(row) for row in rows]

    def gate_sent_or_decided(self, checkpoint_id: str) -> bool:
        """Has this gate had its card sent to the group, or a decision made final?

        This is what stops a second card and takes a gate off the duty
        reminders.  A claim -- a decision in flight, one a crash left behind,
        or one whose outcome is not known yet -- is neither: counting it
        would leave a waiting checkpoint with no card and no reminder.
        """
        row = self.gate_card(checkpoint_id)
        if not row:
            return False
        if str(row.get("message_id", "") or ""):
            return True
        decided_at = str(row.get("decided_at", "") or "")
        return bool(row.get("decision")) and not decided_at.startswith(self.GATE_CLAIM_PREFIX)

    # A card whose checkpoint closed with nobody deciding it on the card (C4-A, policy): ``decision``
    # holds this marker and ``decided_at`` a final time, so no click can claim it any more (``claim_gate_decision``
    # needs ``decision = ''``) and the card counts as handled (``gate_sent_or_decided``).
    GATE_RETIRED = "retired"

    def open_gate_cards(self) -> list[dict[str, Any]]:
        """Every card sent to the group that nobody has decided, claimed or retired yet, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM gate_cards WHERE message_id != '' AND decision = '' ORDER BY rowid",
            ).fetchall()
        return [dict(row) for row in rows]

    def retire_gate_card(self, checkpoint_id: str, *, reason: str) -> str:
        """Mark a sent card retired while nobody has decided or claimed it; returns the final stamp, or "" when not.

        One conditional update, like a claim: a click that claimed the card first keeps it, and a card retired once
        is never retired again.
        """
        stamp = datetime.now().isoformat(timespec="microseconds")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE gate_cards SET decision = ?, decided_by = '', decided_at = ?, note = ? "
                "WHERE checkpoint_id = ? AND decision = '' AND message_id != ''",
                (self.GATE_RETIRED, stamp, str(reason or ""), checkpoint_id),
            )
            return stamp if cursor.rowcount == 1 else ""

    def unretire_gate_card(self, checkpoint_id: str, stamp: str) -> bool:
        """Give back the retirement stamped ``stamp`` (its card could not be updated): the card is open for the next sweep."""
        if not stamp:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE gate_cards SET decision = '', decided_by = '', decided_at = '', note = '' "
                "WHERE checkpoint_id = ? AND decision = ? AND decided_at = ?",
                (checkpoint_id, self.GATE_RETIRED, stamp),
            )
            return cursor.rowcount == 1

    def record_gate_event(self, checkpoint_id: str, kind: str, *, actor: str = "", note: dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            self._append_event(
                conn,
                table="gate_events",
                aggregate_column="checkpoint_id",
                aggregate_id=checkpoint_id,
                at=datetime.now().isoformat(),
                kind=kind,
                actor=actor,
                note=dict(note or {}),
                correlation_id=checkpoint_id,
            )

    def gate_cards_for_order(self, order_id: str) -> list[dict[str, Any]]:
        """Every gate card raised for one order, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT checkpoint_id, message_id, chat_id, task_id, order_id, version, sent_at, decision, decided_by, decided_at, note "
                "FROM gate_cards WHERE order_id = ? ORDER BY rowid",
                (str(order_id or ""),),
            ).fetchall()
        return [dict(row) for row in rows]

    def gate_events(self, checkpoint_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT at, kind, actor, note_json, event_id, correlation_id, causation_id, previous_hash, event_hash "
                "FROM gate_events WHERE checkpoint_id = ? ORDER BY id",
                (checkpoint_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                note = json.loads(row["note_json"] or "{}")
            except ValueError:
                note = {}
            out.append({
                "at": row["at"], "kind": row["kind"], "actor": row["actor"], "note": note,
                "event_id": str(row["event_id"] or ""),
                "correlation_id": str(row["correlation_id"] or ""),
                "causation_id": str(row["causation_id"] or ""),
                "integrity": {
                    "previous_hash": str(row["previous_hash"] or ""),
                    "event_hash": str(row["event_hash"] or ""),
                },
            })
        return out
