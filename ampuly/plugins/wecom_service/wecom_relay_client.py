"""The console's one outbound link to the 微信客服 relay (policy, W3a).

The relay (W2) sits on a server with a public callback URL and an enterprise trusted IP and holds the 企业微信
secrets. The console never listens for it: it dials OUT over one websocket, and from then on both sides speak the
signed envelope of ``wecom_relay_protocol`` -- events come in, commands go out, each command's result comes back by
request id, heartbeats both ways.

The hello rides on the websocket upgrade request itself (``HELLO_HEADER``, a signed heartbeat envelope), the way the
relay (``scripts/wecom_relay/server.py``) checks it before it opens any websocket. A refused hello comes back as an
HTTP answer with the reason in ``REFUSAL_HEADER``; ``RelayHelloRefused`` turns it into the owner's words. The relay's
first frame on an accepted link (its signed welcome) is the answer that proves both ends hold the same token.

What this module promises:

* **one link, bounded retries.** A dropped or refused link is dialled again after 1, 2, 5, 10, 30, then every 60
  seconds -- never faster, never a busy loop. A successful hello resets the wait.
* **a dead peer is noticed.** A heartbeat goes out every 30 seconds; a link that stays silent for three of those is
  closed and dialled again.
* **nothing is sent twice.** A command whose answer never came -- the link dropped, the wait ran out, the relay said
  it could not tell -- raises an outcome-unknown error to the caller and is never re-sent from here. A command asked
  while there is no link raises ``RelayNotConnected``: nothing left the machine.
* **the shared token is a secret.** It comes from the environment variable ``AMPULY_WECOM_RELAY_TOKEN`` (the same user-
  level variables the settings page writes for channel keys; this extension declares both names to the console,
  ``console.extension.secret_env_names``, and they are defined here and nowhere else), is used only to sign
  and check envelopes, and is never logged, stored in the business database, or put in any reply. The relay's address
  (``AMPULY_WECOM_RELAY_URL``) must be ``wss://`` -- plain ``ws://`` only to this machine, for local testing.

``WecomRelayClient`` implements ``wecom_kf.KfTransport``. The socket itself is behind ``Connect`` (url -> link), so
tests drive the client over an in-memory link; ``websockets_connect`` is the real one.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from hashlib import sha256
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from loguru import logger

from ampuly.plugins.wecom_service.wecom_kf import KfApiError, KfCallback, MediaUploadRequest
from ampuly.plugins.wecom_service.wecom_relay_protocol import (
    CONSOLE_ACCEPTS,
    HEARTBEAT_INTERVAL_SECONDS,
    KIND_EVENT,
    KIND_HEARTBEAT,
    KIND_RESULT,
    MAX_ENVELOPE_BYTES,
    MIN_RELAY_TOKEN_BYTES,
    PROTOCOL_VERSION,
    RelayOutcomeUnknown,
    RelayProtocolError,
    ReplayGuard,
    build_command,
    build_heartbeat,
    callback_from_event_payload,
    encode_envelope,
    new_id,
    open_envelope,
    sign_envelope,
    unwrap_result,
    upload_command_payload,
)

RELAY_URL_ENV = "AMPULY_WECOM_RELAY_URL"
RELAY_TOKEN_ENV = "AMPULY_WECOM_RELAY_TOKEN"
# The relay's names (scripts/wecom_relay/server.py): the hello on the upgrade request, the reason of a refusal.
HELLO_HEADER = "X-Ampuly-Relay-Hello"
REFUSAL_HEADER = "X-Ampuly-Relay-Refused"
# The relay's close code for a second console while one is connected.
CLOSE_ANOTHER_CONSOLE = 4409

# Seconds to wait before dialling again after the n-th failure in a row; the last one repeats.
BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
HELLO_TIMEOUT_SECONDS = 15.0
COMMAND_TIMEOUT_SECONDS = 20.0
UPLOAD_TIMEOUT_SECONDS = 120.0
# A link that says nothing (not even a heartbeat) for this many heartbeat intervals is taken as dead.
SILENT_INTERVALS = 3
# Refused envelopes one link may bring before it is dropped: a peer that keeps sending unsigned or stale envelopes is
# not the relay we know.
MAX_REFUSALS_PER_LINK = 10
# In-flight event handlers; one more is dropped with a log line (each event only says "go and sync": the next one, or
# the next hello, catches up).
MAX_EVENT_TASKS = 32
ATTEMPTS_KEPT = 20
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class RelayConfigError(ValueError):
    """The relay is not configured in a usable way. The text is for the owner."""


class RelayNotConnected(RelayProtocolError):
    """There is no link to the relay right now: nothing was sent."""


class RelayLinkDropped(RelayOutcomeUnknown):
    """The link went while a command was on its way or waiting for its answer: whether it ran is unknown."""


# Why the relay turned the hello down (its REFUSAL_HEADER), in the owner's words.
_HELLO_REFUSAL_WORDS = {
    "bad_signature": "两边的共享口令不一致",
    "weak_token": "两边的共享口令不一致",
    "stale": "本机和中转站的时间相差超过 5 分钟，请校准两边的时钟",
    "replayed": "这次握手被当成了重复的，稍后会自动再连",
    "missing": "中转站没收到握手信息，多半是中转站前面的网关把它拦掉了",
    "too_large": "握手信息太长，两边的程序版本可能对不上",
    "bad_json": "两边的程序版本对不上",
    "bad_version": "两边的程序版本对不上",
    "bad_shape": "两边的程序版本对不上",
    "bad_kind": "两边的程序版本对不上",
    "kind_not_accepted": "两边的程序版本对不上",
}
_HTTP_REFUSAL_WORDS = {
    400: "这个地址不接受这种连接，请核对中转站的地址",
    401: "中转站要求另外的登录，请核对地址是不是中转站本身",
    404: "这个地址上没有中转站，请核对地址的路径",
    502: "中转站前面的网关连不上中转站，请看中转站有没有在运行",
    503: "中转站正在停止或暂时不可用，稍后会自动再连",
}


class RelayHelloRefused(ConnectionError):
    """The relay answered the upgrade with an HTTP status instead of a websocket: nothing was opened or sent."""

    def __init__(self, status: int, reason: str = "") -> None:
        self.status = int(status)
        self.reason = str(reason or "")[:40]
        super().__init__(f"relay refused the upgrade (HTTP {self.status}, {self.reason or 'no reason'})")

    @property
    def said(self) -> str:
        """For the owner's page: what went wrong, never the token or the address."""
        if self.status == 403:
            words = _HELLO_REFUSAL_WORDS.get(self.reason, "")
            if words:
                return "中转站拒绝了握手：" + words
            return "中转站拒绝了连接（状态码 403）"
        words = _HTTP_REFUSAL_WORDS.get(self.status, "")
        return f"中转站拒绝了连接（状态码 {self.status}）" + (f"：{words}" if words else "")


class RelayLink(Protocol):
    async def send(self, text: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


# (url, hello) -> link: ``hello`` is the encoded signed hello that goes on the upgrade request as HELLO_HEADER.
Connect = Callable[[str, str], Awaitable[RelayLink]]
OnEvent = Callable[[KfCallback], Awaitable[Any]]


def check_relay_url(url: Any) -> str:
    """The relay's address, checked: ``wss://host[:port][/path]``; ``ws://`` only to this machine."""
    text = str(url or "").strip()
    if not text:
        raise RelayConfigError("还没有填中转站的地址")
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        _ = parts.port
    except ValueError:
        raise RelayConfigError("中转站的地址格式不对") from None
    if parts.scheme not in ("wss", "ws") or not host:
        raise RelayConfigError("中转站的地址要以 wss:// 开头")
    if parts.scheme == "ws" and host not in _LOCAL_HOSTS:
        raise RelayConfigError("中转站的地址要以 wss:// 开头（不加密的 ws:// 只能连本机，用于本机测试）")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise RelayConfigError("中转站的地址里不要带用户名、口令或问号后面的参数，共享口令单独填")
    return text


def relay_host(url: Any) -> str:
    """Only the host, for the owner's page (never the whole address, never the token)."""
    try:
        return (urlsplit(str(url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""


def check_relay_token(token: Any) -> str:
    raw = str(token or "")
    if not raw.strip():
        raise RelayConfigError("还没有填中转站的共享口令")
    if len(raw.encode("utf-8")) < MIN_RELAY_TOKEN_BYTES:
        raise RelayConfigError(f"中转站的共享口令太短（至少 {MIN_RELAY_TOKEN_BYTES} 个字节），请重新生成")
    return raw


class _WebsocketsLink:
    def __init__(self, socket: Any) -> None:
        self._socket = socket

    async def send(self, text: str) -> None:
        await self._socket.send(text)

    async def recv(self) -> str | bytes:
        return await self._socket.recv()

    async def close(self) -> None:
        await self._socket.close()


def _accepts_keyword(function: Any, name: str) -> bool:
    """Whether ``function`` (a callable or a class) takes the keyword ``name`` by name; False when it cannot be told."""
    import inspect

    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    found = parameters.get(name)
    return found is not None and found.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)


def websockets_client() -> tuple[Any, str]:
    """(connect, the keyword its extra request headers go under) of the installed ``websockets``.

    pyproject declares ``websockets>=12``. The asyncio client this module was written for (``websockets.asyncio``)
    arrived in 13 and takes ``additional_headers``; 12 has only the legacy client, which takes ``extra_headers``.
    """
    try:
        from websockets.asyncio.client import connect
    except ImportError:  # websockets 12: the legacy client only
        from websockets.client import connect  # type: ignore[no-redef,attr-defined]

        return connect, "extra_headers"
    return connect, "additional_headers"


def relay_connect_options(connect: Any, header_keyword: str, hello: str) -> dict[str, Any]:
    """The keyword arguments one dial passes to ``connect`` -- only what that version of ``websockets`` accepts.

    Straight to the relay: no system proxy in between (a ws:// test link to this machine must never leave it). Only 15
    and later read a system proxy at all, and only they know ``proxy``: it is passed as ``None`` when accepted, and an
    older client (13, 14, the legacy 12), which never goes through a proxy, is not handed a keyword it would refuse. No
    compression on a link that carries signed envelopes.
    """
    options: dict[str, Any] = {
        header_keyword: {HELLO_HEADER: str(hello)},
        "max_size": MAX_ENVELOPE_BYTES, "open_timeout": HELLO_TIMEOUT_SECONDS, "close_timeout": 5, "compression": None,
    }
    if _accepts_keyword(connect, "proxy"):
        options["proxy"] = None
    return options


def _refusal_of(exc: BaseException, *, legacy_client: bool = False) -> RelayHelloRefused | None:
    """The relay's HTTP refusal of the upgrade, from either client's exception (``InvalidStatus`` with its response
    in 13+, ``InvalidStatusCode`` with status and headers in the legacy client of 12); None for anything else. The legacy
    name is looked up only for the legacy client (newer versions warn when it is touched)."""
    import websockets.exceptions as ws_errors

    new_style = getattr(ws_errors, "InvalidStatus", None)
    legacy = getattr(ws_errors, "InvalidStatusCode", None) if legacy_client else None
    if new_style is not None and isinstance(exc, new_style):
        response = getattr(exc, "response", None)
        status, headers = getattr(response, "status_code", 0), getattr(response, "headers", None)
    elif legacy is not None and isinstance(exc, legacy):
        status, headers = getattr(exc, "status_code", 0), getattr(exc, "headers", None)
    else:
        return None
    reason = str(headers.get(REFUSAL_HEADER, "") or "") if headers is not None else ""
    return RelayHelloRefused(int(status or 0), reason)


async def websockets_connect(url: str, hello: str) -> RelayLink:
    """The real link: one websocket to the relay, sized for the largest envelope (an upload). The signed hello goes on
    the upgrade request (``HELLO_HEADER``); a refusal raises ``RelayHelloRefused`` with the relay's reason."""
    connect, header_keyword = websockets_client()
    try:
        socket = await connect(check_relay_url(url), **relay_connect_options(connect, header_keyword, hello))
    except Exception as exc:
        refused = _refusal_of(exc, legacy_client=header_keyword == "extra_headers")
        if refused is None:
            raise
        raise refused from None
    return _WebsocketsLink(socket)


@dataclass(frozen=True)
class RelayAttempt:
    """One real exchange with the relay: ok True / False / None (outcome unknown), when, and in the owner's words."""

    ok: bool | None
    at: float
    said: str


class WecomRelayClient:
    """One outbound link, kept up while running; the six 微信客服 calls over it."""

    def __init__(
        self, *, url: str, token: str, on_event: OnEvent | None = None, connect: Connect = websockets_connect,
        clock: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        heartbeat_seconds: float = HEARTBEAT_INTERVAL_SECONDS, backoff: tuple[float, ...] = BACKOFF_SECONDS,
        on_hello: Callable[[], Any] | None = None,
    ) -> None:
        self._url = check_relay_url(url)
        self._token = check_relay_token(token)
        self._on_event = on_event
        self._on_hello = on_hello
        self._connect = connect
        self._clock = clock
        self._sleep = sleep
        self._heartbeat_seconds = float(heartbeat_seconds)
        self._backoff = tuple(float(item) for item in backoff) or BACKOFF_SECONDS
        self._link: RelayLink | None = None
        self._guard: ReplayGuard | None = None
        self._connected = False
        self._stopping = False
        self._pending: dict[str, asyncio.Future] = {}
        self._event_tasks: set[asyncio.Task] = set()
        self._task: asyncio.Task | None = None
        self.attempts: list[RelayAttempt] = []
        self.dial_count = 0
        self.last_error = ""

    # ── what the owner's page reads ──

    @property
    def connected(self) -> bool:
        return self._connected and self._link is not None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _note(self, ok: bool | None, said: str) -> None:
        self.attempts.append(RelayAttempt(ok, float(self._clock()), said))
        del self.attempts[:-ATTEMPTS_KEPT]

    # ── lifecycle ──

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.get_running_loop().create_task(self.run(), name="wecom-relay-link")
        return self._task

    def stop_soon(self) -> None:
        """Stop from synchronous code (shutdown, a switch turned off): no new dial, the link task cancelled."""
        self._stopping = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def stop(self) -> None:
        self.stop_soon()
        task = self._task
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - stopping; the reason was logged where it happened
                pass
        await self._drop_link()

    async def run(self) -> None:
        failures = 0
        try:
            while not self._stopping:
                self.dial_count += 1
                # A fresh hello for every dial (the relay remembers each one it has seen); its id is remembered here
                # too, so the relay cannot be handed our own envelope back as its answer.
                guard = ReplayGuard()
                hello = self._hello()
                guard.check_and_remember(str(hello["id"]), int(hello["ts"]), self._clock())
                try:
                    link = await self._connect(self._url, encode_envelope(hello))
                except asyncio.CancelledError:
                    raise
                except RelayHelloRefused as exc:
                    self.last_error = f"hello_refused:{exc.status}:{exc.reason}"
                    self._note(False, exc.said)
                    logger.warning("wecom relay: the relay refused the hello (HTTP {}, {})", exc.status, exc.reason or "-")
                    await self._wait_before_dialling(failures)
                    failures += 1
                    continue
                except Exception as exc:  # noqa: BLE001 - a failed dial is noted and dialled again later
                    self.last_error = type(exc).__name__
                    self._note(False, "连不上中转站（" + type(exc).__name__ + "）")
                    logger.warning("wecom relay: dial failed ({})", type(exc).__name__)
                    await self._wait_before_dialling(failures)
                    failures += 1
                    continue
                greeted = False
                try:
                    greeted = await self._session(link, guard)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the link is dropped and dialled again
                    # Not an attempt of its own: the dial that follows says whether the relay is there.
                    self.last_error = type(exc).__name__
                    logger.warning("wecom relay: link ended ({})", type(exc).__name__)
                finally:
                    await self._drop_link()
                if self._stopping:
                    break
                if greeted:
                    failures = 0
                await self._wait_before_dialling(failures)
                if not greeted:
                    failures += 1
        finally:
            await self._drop_link()

    async def _wait_before_dialling(self, failures: int) -> None:
        if self._stopping:
            return
        await self._sleep(self._backoff[min(failures, len(self._backoff) - 1)])

    def _hello(self) -> dict[str, Any]:
        # The console's hello is a heartbeat that says who it is, sent on the upgrade request (HELLO_HEADER); the relay
        # answers with a signed envelope of its own (its welcome), which proves both ends hold the same token.
        fields = {"v": PROTOCOL_VERSION, "kind": KIND_HEARTBEAT, "id": new_id(), "ts": int(self._clock()),
                  "payload": {"hello": "console"}}
        return sign_envelope(fields, relay_token=self._token)

    def _remember_sent(self, envelope: Mapping[str, Any]) -> None:
        # Both ends sign with the same token: an envelope of ours handed back must not pass as the relay's.
        guard = self._guard
        if guard is not None:
            guard.check_and_remember(str(envelope["id"]), int(envelope["ts"]), self._clock())

    async def _session(self, link: RelayLink, guard: ReplayGuard | None = None) -> bool:
        """One link from the relay's welcome to its end. True when the hello was answered.

        The hello itself already went with the upgrade request (``Connect``); the relay's first frame is its answer."""
        guard = guard if guard is not None else ReplayGuard()
        self._guard = guard
        self._link = link
        try:
            first = await asyncio.wait_for(link.recv(), HELLO_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self._note(False, "中转站没有回应握手")
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - closed before its welcome: a failed hello, dialled again later
            received = getattr(exc, "rcvd", None)
            if getattr(received, "code", None) == CLOSE_ANOTHER_CONSOLE:
                self._note(False, "中转站已经连着另一个控制台（一个中转站只接一个），这边稍后再试")
            else:
                self._note(False, "中转站接受了连接，但没发回应就断开了")
            logger.warning("wecom relay: the link closed before the relay's welcome ({})", type(exc).__name__)
            return False
        try:
            envelope = open_envelope(first, relay_token=self._token, now=self._clock(), accept=CONSOLE_ACCEPTS,
                                     replay_guard=guard)
        except RelayProtocolError as exc:
            self._note(False, "中转站的回应没通过核对（" + exc.message + "），多半是两边的共享口令不一致")
            logger.warning("wecom relay: hello answer refused ({})", exc.code)
            return False
        self._connected = True
        self._note(True, "和中转站握手成功")
        logger.info("wecom relay: link up")
        self._handle(envelope)
        if self._on_hello is not None:
            try:
                self._on_hello()
            except Exception:  # noqa: BLE001 - catching up is best effort; the next event does it too
                logger.opt(exception=True).warning("wecom relay: catch-up after hello failed")
        beat = asyncio.get_running_loop().create_task(self._heartbeats(link), name="wecom-relay-heartbeat")
        refusals = 0
        try:
            while not self._stopping:
                try:
                    raw = await asyncio.wait_for(link.recv(), self._heartbeat_seconds * SILENT_INTERVALS)
                except asyncio.TimeoutError:
                    logger.warning("wecom relay: the link went silent; dialling again")
                    return True
                try:
                    envelope = open_envelope(raw, relay_token=self._token, now=self._clock(), accept=CONSOLE_ACCEPTS,
                                             replay_guard=guard)
                except RelayProtocolError as exc:
                    refusals += 1
                    logger.warning("wecom relay: envelope refused ({})", exc.code)
                    if refusals >= MAX_REFUSALS_PER_LINK:
                        self._note(False, "中转站发来的消息多次没通过核对，已断开重连")
                        return True
                    continue
                self._handle(envelope)
        finally:
            beat.cancel()
        return True

    async def _heartbeats(self, link: RelayLink) -> None:
        while True:
            # The real clock on purpose: the injected ``sleep`` paces the dialling only.
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                beat = build_heartbeat(relay_token=self._token, now=self._clock())
                self._remember_sent(beat)
                await link.send(encode_envelope(beat))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the receive loop notices the dead link
                return

    def _handle(self, envelope: Any) -> None:
        if envelope.kind == KIND_RESULT:
            future = self._pending.get(envelope.request_id)
            if future is not None and not future.done():
                future.set_result(envelope)
            else:
                logger.info("wecom relay: a result came for a request nobody waits on any more")
        elif envelope.kind == KIND_EVENT:
            try:
                callback = callback_from_event_payload(envelope.payload)
            except RelayProtocolError as exc:
                logger.warning("wecom relay: event refused ({})", exc.code)
                return
            if self._on_event is None:
                return
            if len(self._event_tasks) >= MAX_EVENT_TASKS:
                logger.warning("wecom relay: too many events in flight; this one is dropped (the next one catches up)")
                return
            task = asyncio.get_running_loop().create_task(self._run_event(callback), name="wecom-relay-event")
            self._event_tasks.add(task)
            task.add_done_callback(self._event_tasks.discard)

    async def _run_event(self, callback: KfCallback) -> None:
        try:
            await self._on_event(callback)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - one event's failure must not take the link down
            logger.opt(exception=True).warning("wecom relay: handling an event failed")

    async def _drop_link(self) -> None:
        link, self._link = self._link, None
        self._guard = None
        self._connected = False
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(RelayLinkDropped(
                    "outcome_unknown", "和中转站的连接在等回音时断了，不知道这次操作有没有执行；没有重发"))
            self._pending.pop(request_id, None)
        if link is not None:
            try:
                await link.close()
            except Exception:  # noqa: BLE001 - closing a dead link
                pass

    # ── the six calls (wecom_kf.KfTransport) ──

    async def _call(self, command: str, payload: Mapping[str, Any], *, timeout: float = COMMAND_TIMEOUT_SECONDS,
                    _observe: bool = False, _before_send: Any = None) -> dict[str, Any]:
        link = self._link
        if link is None or not self._connected or (_observe and self._stopping):
            raise RelayNotConnected("not_connected", "和中转站的连接还没建立，这次没有发出")
        frozen_payload = deepcopy(dict(payload)) if _observe else dict(payload)
        envelope = build_command(command, frozen_payload, relay_token=self._token, now=self._clock())
        encoded = encode_envelope(envelope)
        request_sha256 = None
        if _observe:
            from ampuly.components.customer_service.core.contracts import canonical_json
            request_sha256 = sha256(canonical_json(frozen_payload)).hexdigest()
        request_id = str(envelope["request_id"])
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._remember_sent(envelope)
        try:
            if _before_send is not None:
                # Final host fence after all preparatory awaits, immediately
                # before the existing wire send. Not a public payload option.
                checked = await _before_send()
                if checked is not None or self._link is not link or not self._connected or self._stopping:
                    raise RelayNotConnected("not_connected", "发送前授权或连接发生变化；没有发出")
            try:
                await link.send(encoded)
            except Exception as exc:  # noqa: BLE001 - it may or may not have left
                self._note(None, "发给中转站时连接断了")
                raise RelayLinkDropped("outcome_unknown", "发给中转站时连接断了，不知道它有没有收到；没有重发") from exc
            try:
                answer = await asyncio.wait_for(asyncio.shield(future), timeout)
            except asyncio.TimeoutError:
                self._note(None, f"中转站没有在 {int(timeout)} 秒内回答")
                raise RelayOutcomeUnknown(
                    "outcome_unknown", f"中转站没有在 {int(timeout)} 秒内回答，不知道这次操作有没有执行；没有重发") from None
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
        try:
            result = unwrap_result(answer)
        except KfApiError:
            self._note(True, "中转站回答了（企业微信拒绝了这次请求）")
            raise
        except RelayOutcomeUnknown:
            self._note(None, "中转站说不清企业微信有没有执行")
            raise
        self._note(True, "中转站处理了一次请求")
        if _observe:
            from ampuly.components.customer_service.core.contracts import canonical_json
            if answer.kind != KIND_RESULT or answer.request_id != request_id:
                raise RelayOutcomeUnknown("outcome_unknown", "回执与本次请求不匹配；没有重发")
            # The receive loop verified this envelope before _handle matched
            # its request_id. Persist digests, never a token/signature/secret.
            return {"response": result, "relay_request_id": request_id,
                    "request_sha256": request_sha256,
                    "response_sha256": sha256(canonical_json(result)).hexdigest(),
                    "response_envelope_sha256": sha256(canonical_json({
                        "kind": answer.kind, "id": answer.envelope_id, "ts": answer.ts,
                        "request_id": answer.request_id, "ok": answer.ok,
                        "payload": dict(answer.payload), "error": answer.error})).hexdigest()}
        return result

    async def sync_msg(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._call("sync_msg", payload)

    async def send_msg(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._call("send_msg", payload)

    async def send_msg_observed(self, payload: Mapping[str, Any], *, before_send: Any) -> dict[str, Any]:
        """Host-only bounded send retaining correlated API acceptance evidence.

        Successful API acceptance is not proof of delivery or read. Existing
        send_msg callers keep their original result and behavior.
        """
        if not callable(before_send):
            raise ValueError("customer_service_send_guard_required")
        return await self._call("send_msg", payload, _observe=True, _before_send=before_send)

    async def send_msg_on_event(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._call("send_msg_on_event", payload)

    async def upload_media(self, request: MediaUploadRequest, content: bytes) -> dict[str, Any]:
        return await self._call("upload_media", upload_command_payload(request, content), timeout=UPLOAD_TIMEOUT_SECONDS)

    async def service_state_get(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._call("service_state_get", payload)

    async def service_state_trans(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._call("service_state_trans", payload)
