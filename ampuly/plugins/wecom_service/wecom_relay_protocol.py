"""The envelope between the console and the 微信客服 relay -- pure, no socket.

The relay (a later batch) is a small server with a public callback URL and
an enterprise trusted IP; it holds the 企业微信 secrets. The console dials OUT
to it over one websocket, so the owner's PC never needs an inbound port. Four
kinds of envelope travel on that socket:

* ``event``     relay -> console: a callback arrived (today: kf_msg_or_event);
* ``command``   console -> relay: make one of the six 微信客服 calls, with a request id;
* ``result``    relay -> console: the answer to one command, by request id;
* ``heartbeat`` both ways, to keep the socket and notice a dead peer.

Every envelope is signed with HMAC-SHA256 under a shared relay token, over
its canonical JSON (sorted keys, no spaces, UTF-8) behind a fixed domain
tag, and carries a unix-seconds timestamp. The receiver refuses a bad
signature, a timestamp more than 5 minutes off, an envelope id it has
already seen in that window, a kind it does not take from that peer, and
any command that is not one of the six. Nothing here reads configuration or
the environment: the token, the clock and the replay memory are passed in.

Wire shape (JSON object; ``sig`` covers every other key)::

    {"v": 1, "kind": "command", "id": "<id>", "ts": 1727500000,
     "request_id": "<id>", "command": "send_msg", "payload": {...}, "sig": "<hex>"}
    {"v": 1, "kind": "result", "id": ..., "ts": ..., "request_id": "<id>", "ok": true, "payload": {...}}
    {"v": 1, "kind": "result", ..., "ok": false,
     "error": {"errcode": 95018, "errmsg": "...", "outcome_unknown": false}}
    {"v": 1, "kind": "event", "id": ..., "ts": ..., "payload": {"source": "kf_callback", ...}}
    {"v": 1, "kind": "heartbeat", "id": ..., "ts": ...}
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from ampuly.plugins.wecom_service.wecom_kf import (
    KF_COMMANDS,
    KF_MSG_OR_EVENT,
    KfApiError,
    KfCallback,
    MediaUploadRequest,
    describe_media_upload,
)

PROTOCOL_VERSION = 1
SIGNATURE_DOMAIN = b"ampuly-wecom-relay/v1\n"
KIND_EVENT = "event"
KIND_COMMAND = "command"
KIND_RESULT = "result"
KIND_HEARTBEAT = "heartbeat"
ENVELOPE_KINDS = frozenset({KIND_EVENT, KIND_COMMAND, KIND_RESULT, KIND_HEARTBEAT})
# What each side takes from the other: a command reflected back at the console,
# or an event pushed at the relay, is refused even when correctly signed.
CONSOLE_ACCEPTS = frozenset({KIND_EVENT, KIND_RESULT, KIND_HEARTBEAT})
RELAY_ACCEPTS = frozenset({KIND_COMMAND, KIND_HEARTBEAT})
ALLOWED_COMMANDS = frozenset(KF_COMMANDS)
CLOCK_WINDOW_SECONDS = 300
HEARTBEAT_INTERVAL_SECONDS = 30
MIN_RELAY_TOKEN_BYTES = 32
# The largest command is upload_media: a 20MB file as base64 plus its description.
MAX_ENVELOPE_BYTES = 28 * 1024 * 1024
# Unix seconds; 2**53 is the largest integer a float holds exactly, far beyond any real clock.
MAX_ENVELOPE_TS = 2**53
CALLBACK_EVENT_SOURCE = "kf_callback"

_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
_SIG = re.compile(r"[0-9a-f]{64}")
_KEYS = {
    KIND_EVENT: ({"payload"}, set()),
    KIND_COMMAND: ({"request_id", "command", "payload"}, set()),
    KIND_RESULT: ({"request_id", "ok"}, {"payload", "error"}),
    KIND_HEARTBEAT: (set(), {"payload"}),
}
_COMMON_KEYS = {"v", "kind", "id", "ts", "sig"}


class RelayProtocolError(Exception):
    """An envelope was refused. ``code`` is stable; the text is plain Chinese for people."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RelayOutcomeUnknown(RelayProtocolError):
    """The relay could not tell whether 企业微信 acted on the command (e.g. it timed out after sending)."""


@dataclass(frozen=True)
class RelayEnvelope:
    kind: str
    envelope_id: str
    ts: int
    request_id: str = ""
    command: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    ok: bool | None = None
    error: Mapping[str, Any] | None = None


class ReplayGuard:
    """Envelope ids seen inside the clock window. In memory; one per connection side."""

    def __init__(self, window_seconds: int = CLOCK_WINDOW_SECONDS) -> None:
        self._window = window_seconds
        self._seen: dict[str, int] = {}

    def check_and_remember(self, envelope_id: str, ts: int, now: float) -> bool:
        """True the first time an id is seen; False for a repeat."""
        # An id whose timestamp has left the window would be refused as stale
        # anyway, so it no longer needs remembering.
        horizon = now - self._window
        for seen_id in [key for key, seen_ts in self._seen.items() if seen_ts < horizon]:
            del self._seen[seen_id]
        if envelope_id in self._seen:
            return False
        self._seen[envelope_id] = ts
        return True

    @property
    def size(self) -> int:
        # A property, not __len__: an empty guard must not read as false in `guard or ReplayGuard()`.
        return len(self._seen)


def _token_bytes(relay_token: str | bytes) -> bytes:
    if isinstance(relay_token, str):
        raw = relay_token.encode("utf-8")
    elif isinstance(relay_token, (bytes, bytearray)):
        raw = bytes(relay_token)
    else:
        raw = b""
    if len(raw) < MIN_RELAY_TOKEN_BYTES:
        raise RelayProtocolError("weak_token", "中转服务的共享口令太短（至少 32 个字节），请重新生成")
    return raw


def canonical_bytes(fields: Mapping[str, Any]) -> bytes:
    # The encode sits inside the try: a lone surrogate (JSON "\ud800") decodes
    # fine but cannot be UTF-8 encoded, and UnicodeEncodeError is a ValueError.
    # Nesting deep enough to exhaust the recursion limit is refused the same way.
    try:
        text = json.dumps(dict(fields), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return text.encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise RelayProtocolError("bad_shape", "消息里有不能签名的内容") from None


def compute_envelope_signature(fields: Mapping[str, Any], relay_token: str | bytes) -> str:
    body = {key: value for key, value in fields.items() if key != "sig"}
    return hmac.new(_token_bytes(relay_token), SIGNATURE_DOMAIN + canonical_bytes(body), hashlib.sha256).hexdigest()


def sign_envelope(fields: Mapping[str, Any], *, relay_token: str | bytes) -> dict[str, Any]:
    body = {key: value for key, value in fields.items() if key != "sig"}
    body["sig"] = compute_envelope_signature(body, relay_token)
    return body


def new_id() -> str:
    return os.urandom(16).hex()


def _checked_id(value: str | None, what: str) -> str:
    chosen = new_id() if value is None else value
    if not isinstance(chosen, str) or not _ID.fullmatch(chosen):
        raise RelayProtocolError("bad_shape", f"{what}只能是 8 到 64 个字母、数字、下划线或短横线")
    return chosen


def _base(kind: str, now: float, envelope_id: str | None) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "kind": kind, "id": _checked_id(envelope_id, "消息编号"), "ts": int(now)}


def _checked_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RelayProtocolError("bad_shape", "消息内容必须是 JSON 对象")
    return dict(payload)


def _checked_error(error: Any) -> dict[str, Any]:
    if not isinstance(error, Mapping):
        raise RelayProtocolError("bad_shape", "失败结果必须带错误码和说明")
    errcode, errmsg = error.get("errcode"), error.get("errmsg")
    unknown = error.get("outcome_unknown", False)
    if (
        isinstance(errcode, bool) or not isinstance(errcode, int) or not isinstance(errmsg, str)
        or not isinstance(unknown, bool) or set(error) - {"errcode", "errmsg", "outcome_unknown"}
    ):
        raise RelayProtocolError("bad_shape", "失败结果必须带错误码和说明")
    checked: dict[str, Any] = {"errcode": errcode, "errmsg": errmsg}
    if unknown:
        checked["outcome_unknown"] = True
    return checked


def _checked_command(command: Any) -> str:
    if not isinstance(command, str) or command not in ALLOWED_COMMANDS:
        raise RelayProtocolError("command_not_allowed", "中转服务只做微信客服的六种操作，这个请求不在其中，已拒绝")
    return command


def build_event(payload: Mapping[str, Any], *, relay_token: str | bytes, now: float, envelope_id: str | None = None) -> dict[str, Any]:
    fields = _base(KIND_EVENT, now, envelope_id)
    fields["payload"] = _checked_payload(payload)
    return sign_envelope(fields, relay_token=relay_token)


def build_command(
    command: str, payload: Mapping[str, Any], *, relay_token: str | bytes, now: float,
    request_id: str | None = None, envelope_id: str | None = None,
) -> dict[str, Any]:
    fields = _base(KIND_COMMAND, now, envelope_id)
    fields["request_id"] = _checked_id(request_id, "请求编号")
    fields["command"] = _checked_command(command)
    fields["payload"] = _checked_payload(payload)
    return sign_envelope(fields, relay_token=relay_token)


def build_result(
    request_id: str, *, relay_token: str | bytes, now: float, data: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None, envelope_id: str | None = None,
) -> dict[str, Any]:
    """Exactly one of ``data`` (企业微信's errcode-0 answer) and ``error``."""
    if (data is None) == (error is None):
        raise RelayProtocolError("bad_shape", "结果要么是成功的内容，要么是错误，只能二选一")
    fields = _base(KIND_RESULT, now, envelope_id)
    fields["request_id"] = _checked_id(request_id, "请求编号")
    if error is None:
        fields["ok"] = True
        fields["payload"] = _checked_payload(data)
    else:
        fields["ok"] = False
        fields["error"] = _checked_error(error)
    return sign_envelope(fields, relay_token=relay_token)


def build_heartbeat(*, relay_token: str | bytes, now: float, envelope_id: str | None = None) -> dict[str, Any]:
    return sign_envelope(_base(KIND_HEARTBEAT, now, envelope_id), relay_token=relay_token)


def encode_envelope(envelope: Mapping[str, Any]) -> str:
    return json.dumps(dict(envelope), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _load(raw: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > MAX_ENVELOPE_BYTES:
            raise RelayProtocolError("too_large", "消息太大，已拒收")
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise RelayProtocolError("bad_json", "消息不是 UTF-8 文字，已拒收") from None
    if not isinstance(raw, str):
        raise RelayProtocolError("bad_json", "消息不是文字，已拒收")
    try:
        size = len(raw.encode("utf-8"))
    except UnicodeEncodeError:
        raise RelayProtocolError("bad_json", "消息不是合法的 UTF-8 文字，已拒收") from None
    if size > MAX_ENVELOPE_BYTES:
        raise RelayProtocolError("too_large", "消息太大，已拒收")
    try:
        data = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except RecursionError:
        raise RelayProtocolError("bad_json", "消息的层级嵌套太深，已拒收") from None
    except ValueError:
        raise RelayProtocolError("bad_json", "消息不是合法的 JSON，已拒收") from None
    if not isinstance(data, dict):
        raise RelayProtocolError("bad_json", "消息不是 JSON 对象，已拒收")
    return data


def open_envelope(
    raw: str | bytes | Mapping[str, Any], *, relay_token: str | bytes, now: float,
    accept: frozenset[str], replay_guard: ReplayGuard,
) -> RelayEnvelope:
    """Check one received envelope and return it; every refusal raises ``RelayProtocolError``.

    Order: shape, signature, clock window, replay, kind accepted from this
    peer, then the kind's own fields (the command whitelist among them).
    """
    data = _load(raw)
    version, kind, envelope_id, ts, sig = (data.get(key) for key in ("v", "kind", "id", "ts", "sig"))
    if isinstance(version, bool) or version != PROTOCOL_VERSION:
        raise RelayProtocolError("bad_version", "消息的协议版本不对，已拒收")
    # isinstance first: a list or dict kind is unhashable and would raise TypeError on the set lookup.
    if not isinstance(kind, str) or kind not in ENVELOPE_KINDS:
        raise RelayProtocolError("bad_kind", "不认识的消息类型，已拒收")
    required, optional = _KEYS[kind]
    keys = set(data)
    if not (_COMMON_KEYS | required) <= keys or keys - _COMMON_KEYS - required - optional:
        raise RelayProtocolError("bad_shape", "消息的字段不对，已拒收")
    if not isinstance(envelope_id, str) or not _ID.fullmatch(envelope_id):
        raise RelayProtocolError("bad_shape", "消息编号不对，已拒收")
    # Bounded so `now - ts` below cannot raise OverflowError against a float clock.
    if isinstance(ts, bool) or not isinstance(ts, int) or not 0 <= ts <= MAX_ENVELOPE_TS:
        raise RelayProtocolError("bad_shape", "消息时间不对，已拒收")
    if not isinstance(sig, str) or not _SIG.fullmatch(sig):
        raise RelayProtocolError("bad_signature", "消息签名对不上，已拒收")
    expected = compute_envelope_signature(data, relay_token)
    if not hmac.compare_digest(sig.encode("ascii"), expected.encode("ascii")):
        raise RelayProtocolError("bad_signature", "消息签名对不上，已拒收")
    if abs(now - ts) > CLOCK_WINDOW_SECONDS:
        raise RelayProtocolError("stale", "消息时间和本机相差超过 5 分钟，按防重放规则拒收")
    if not replay_guard.check_and_remember(envelope_id, ts, now):
        raise RelayProtocolError("replayed", "这条消息已经收到过，按防重放规则拒收")
    if kind not in accept:
        raise RelayProtocolError("kind_not_accepted", "这一端不接收这种消息，已拒收")

    request_id = data.get("request_id", "")
    if kind in (KIND_COMMAND, KIND_RESULT) and (not isinstance(request_id, str) or not _ID.fullmatch(request_id)):
        raise RelayProtocolError("bad_shape", "请求编号不对，已拒收")
    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        raise RelayProtocolError("bad_shape", "消息内容必须是 JSON 对象，已拒收")
    command = ""
    ok: bool | None = None
    error: dict[str, Any] | None = None
    if kind == KIND_COMMAND:
        command = _checked_command(data.get("command"))
    elif kind == KIND_RESULT:
        ok = data.get("ok")
        if not isinstance(ok, bool):
            raise RelayProtocolError("bad_shape", "结果里没有说成功还是失败，已拒收")
        if ok and ("payload" not in data or "error" in data):
            raise RelayProtocolError("bad_shape", "成功的结果必须带内容、不能带错误，已拒收")
        if not ok:
            if "error" not in data or "payload" in data:
                raise RelayProtocolError("bad_shape", "失败的结果必须带错误、不能带内容，已拒收")
            error = _checked_error(data.get("error"))
    return RelayEnvelope(
        kind=kind, envelope_id=envelope_id, ts=ts, request_id=request_id if kind in (KIND_COMMAND, KIND_RESULT) else "",
        command=command, payload=payload, ok=ok, error=error,
    )


def unwrap_result(envelope: RelayEnvelope) -> dict[str, Any]:
    """A result envelope -> 企业微信's answer; ``KfApiError`` when it refused; ``RelayOutcomeUnknown`` when nobody knows.

    ``RelayOutcomeUnknown`` is not a ``KfApiError`` on purpose: ``send_checked``
    counts an unknown outcome as a sent message.
    """
    if envelope.kind != KIND_RESULT:
        raise RelayProtocolError("bad_kind", "这不是结果消息")
    if envelope.ok:
        return dict(envelope.payload)
    error = envelope.error or {}
    if error.get("outcome_unknown"):
        raise RelayOutcomeUnknown(
            "outcome_unknown", "中转服务没法确定企业微信是否已经执行，请到企业微信里核对",
        )
    raise KfApiError(int(error.get("errcode", -1)), str(error.get("errmsg", "")))


def callback_event_payload(callback: KfCallback) -> dict[str, Any]:
    """What the relay pushes after it has checked and decrypted one callback."""
    return {
        "source": CALLBACK_EVENT_SOURCE,
        "event": callback.event,
        "to_user_name": callback.to_user_name,
        "create_time": callback.create_time,
        "token": callback.token,
        "open_kfid": callback.open_kfid,
    }


def callback_from_event_payload(payload: Mapping[str, Any]) -> KfCallback:
    if not isinstance(payload, Mapping) or payload.get("source") != CALLBACK_EVENT_SOURCE:
        raise RelayProtocolError("bad_event", "这不是微信客服回调的转发，已拒收")
    event = payload.get("event")
    create_time = payload.get("create_time", 0)
    texts = [payload.get(key, "") for key in ("to_user_name", "token", "open_kfid")]
    if (
        not isinstance(event, str) or not event or isinstance(create_time, bool) or not isinstance(create_time, int)
        or not all(isinstance(text, str) for text in texts)
    ):
        raise RelayProtocolError("bad_event", "微信客服回调的转发缺字段，已拒收")
    to_user_name, token, open_kfid = texts
    if event == KF_MSG_OR_EVENT and (not token or not open_kfid):
        raise RelayProtocolError("bad_event", "微信客服回调的转发缺少凭证或客服账号，已拒收")
    return KfCallback(event=event, to_user_name=to_user_name, create_time=create_time, token=token, open_kfid=open_kfid)


def _check_upload_content(request: MediaUploadRequest, content: bytes) -> None:
    if len(content) != request.size:
        raise RelayProtocolError("upload_mismatch", "文件内容和说明对不上（大小不同），没有发出")
    if request.sha256 and hashlib.sha256(content).hexdigest() != request.sha256:
        raise RelayProtocolError("upload_mismatch", "文件内容和说明对不上（校验值不同），没有发出")


def upload_command_payload(request: MediaUploadRequest, content: bytes) -> dict[str, Any]:
    """The upload_media command's payload: the description plus the file as base64."""
    _check_upload_content(request, content)
    return {"media": request.as_payload(), "content_b64": base64.b64encode(content).decode("ascii")}


def read_upload_command_payload(payload: Mapping[str, Any]) -> tuple[MediaUploadRequest, bytes]:
    """The relay's side: re-check the description against the documented limits and the bytes against it."""
    media = payload.get("media") if isinstance(payload, Mapping) else None
    encoded = payload.get("content_b64") if isinstance(payload, Mapping) else None
    if not isinstance(media, Mapping) or not isinstance(encoded, str):
        raise RelayProtocolError("bad_shape", "上传请求缺少文件说明或文件内容")
    request = describe_media_upload(
        media_type=media.get("type", ""), filename=media.get("filename", ""), size=media.get("size", 0),
        content_type=media.get("content_type", ""), sha256=media.get("sha256", ""),
    )
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise RelayProtocolError("bad_shape", "上传的文件内容不是合法的 Base64") from None
    _check_upload_content(request, content)
    return request, content
