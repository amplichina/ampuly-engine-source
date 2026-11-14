"""Durable, fail-closed intent storage for the one shop draft submission.

An unreadable record or abandoned update lock requires explicit recovery.
Neither is treated as permission to create the draft again.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
from typing import Any, Iterator, Mapping

INTENT_SCHEMA = "ampuly.shop_listing_intent.v1"
LOCKED_PHASES = frozenset({"submit_started", "unknown", "submitted", "verified"})
_ACTION = "保存草稿"
_MAX_BYTES = 65536


class ListingIntentRefused(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def effect_key(*, business_id: str, shop_id: str, title: str, category: str) -> str:
    raw = json.dumps(
        {"action": _ACTION, "business_id": business_id, "category": category,
         "shop_id": shop_id, "title": title},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return "sled-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _validate(path: Path, data: Any) -> dict[str, Any]:
    if type(data) is not dict or data.get("schema") != INTENT_SCHEMA:
        raise ListingIntentRefused("listing_intent_invalid")
    required = ("idempotency_key", "task_id", "attempt_id", "account", "profile_id",
                "business_id", "shop_id", "title", "category_keyword")
    if any(type(data.get(key)) is not str or not data[key].strip() or len(data[key]) > 4096
           for key in required):
        raise ListingIntentRefused("listing_intent_invalid")
    if (data.get("action") != _ACTION or type(data.get("phase")) is not str
            or data["phase"] not in LOCKED_PHASES
            or type(data.get("target_state")) is not str
            or data["target_state"] not in {"draft", "warehouse"}
            or type(data.get("entity_id")) is not str):
        raise ListingIntentRefused("listing_intent_invalid")
    key = effect_key(business_id=data["business_id"], shop_id=data["shop_id"],
                     title=data["title"], category=data["category_keyword"])
    if (data["idempotency_key"] != key or path.name != f"{key}.json"
            or data["attempt_id"] != f"{key}:submit:1"):
        raise ListingIntentRefused("listing_intent_identity_mismatch")
    if data["phase"] in {"submitted", "verified"} and not data["entity_id"].strip():
        raise ListingIntentRefused("listing_intent_invalid")
    return dict(data)


def load_intent(path: Path) -> dict[str, Any] | None:
    """Only a genuinely absent file means no previous submission."""
    try:
        if path.is_symlink():
            raise ListingIntentRefused("listing_intent_invalid")
        with path.open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise ListingIntentRefused("listing_intent_unreadable") from None
    if len(raw) > _MAX_BYTES:
        raise ListingIntentRefused("listing_intent_invalid")
    try:
        data = json.loads(raw)
    except (UnicodeError, ValueError):
        raise ListingIntentRefused("listing_intent_invalid") from None
    return _validate(path, data)


@contextmanager
def _write_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".json.lock")
    try:
        stream = lock.open("x", encoding="utf-8")
    except FileExistsError:
        raise ListingIntentRefused("listing_intent_busy") from None
    try:
        with stream:
            stream.write("shop-listing-intent-update\n")
            stream.flush()
            yield
    finally:
        # Only this invocation's lock; a process crash leaves it for recovery.
        lock.unlink()


def _write(stream, payload: Mapping[str, Any]) -> None:
    stream.write(json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def claim_intent(path: Path, payload: Mapping[str, Any]) -> None:
    """Exactly one caller may claim the first submit, before sending input."""
    data = _validate(path, dict(payload))
    if data["phase"] != "submit_started" or data["entity_id"]:
        raise ListingIntentRefused("listing_intent_invalid")
    with _write_lock(path):
        try:
            stream = path.open("x", encoding="utf-8", newline="\n")
        except FileExistsError:
            raise ListingIntentRefused("listing_intent_already_exists") from None
        # A failed write deliberately leaves a record. Recovery must not replay.
        with stream:
            _write(stream, data)


def update_intent(path: Path, payload: Mapping[str, Any], *, expected: Mapping[str, Any]) -> None:
    """Compare and replace under an exclusive lock; never silently overwrite."""
    data = _validate(path, dict(payload))
    before = _validate(path, dict(expected))
    immutable = set(before) - {"phase", "entity_id", "target_state"}
    if any(data.get(key) != before[key] for key in immutable):
        raise ListingIntentRefused("listing_intent_identity_mismatch")
    if before["phase"] == "verified" and data != before:
        raise ListingIntentRefused("listing_intent_revision_conflict")
    if data["phase"] == "submit_started" and data != before:
        raise ListingIntentRefused("listing_intent_revision_conflict")
    if before["entity_id"] and (data["entity_id"], data["target_state"]) != (
            before["entity_id"], before["target_state"]):
        raise ListingIntentRefused("listing_intent_identity_mismatch")
    temporary = path.with_suffix(f".json.tmp-{secrets.token_hex(8)}")
    with _write_lock(path):
        if load_intent(path) != before:
            raise ListingIntentRefused("listing_intent_revision_conflict")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                _write(stream, data)
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()
