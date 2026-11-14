"""Tests for the runtime-status reconciliation broadcast (issue #11).

The sync tick re-broadcasts the authoritative status of every task with a
live runtime so any dropped live delta converges within one interval.
"""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from ampuly.plugins.office_ui.event_adapter import AgentAnimState, EventAdapter
from ampuly.plugins.office_ui.ws_handler import WSHandler, _EventLoopWatchdog, _WSInbox


@dataclass
class FakeEvent:
    event_type: str
    payload: dict[str, Any]
    timestamp: float = 0.0
    event_id: str = "test"


@dataclass
class _FakeTask:
    status: str


@dataclass
class _FakeStore:
    tasks: dict[str, _FakeTask] = field(default_factory=dict)

    async def get_task(self, task_id: str) -> _FakeTask | None:
        return self.tasks.get(task_id)


class _FakeBgTask:
    """Hashable stand-in for an asyncio.Task in the bg-context registry."""

    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def _fake_bg_task(done: bool = False) -> _FakeBgTask:
    return _FakeBgTask(done)


def _make_handler(tasks: dict[str, _FakeTask]) -> WSHandler:
    engine = SimpleNamespace(project_id="p1", store=_FakeStore(tasks))
    handler = WSHandler(engine, SimpleNamespace(), SimpleNamespace(), EventAdapter())
    handler._engine_for_project = AsyncMock(return_value=engine)  # type: ignore[method-assign]
    handler.broadcast = AsyncMock()  # type: ignore[method-assign]
    handler._clients = {object()}  # type: ignore[assignment]
    return handler


class RuntimeStatusSyncTickTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_broadcasts_authoritative_status_with_tracker_state(self) -> None:
        handler = _make_handler({"task-1": _FakeTask(status="running")})
        handler._task_bg_context[_fake_bg_task()] = {"task_id": "task-1", "project_id": "p1"}
        tracker = handler.event_adapter._get_tracker("agent-1")
        tracker.task_id = "task-1"
        tracker.state = AgentAnimState.REFLECTING
        tracker.current_tool = None

        await handler._runtime_status_sync_tick()

        handler.broadcast.assert_awaited_once()
        envelope = handler.broadcast.await_args.args[0]
        self.assertEqual(envelope["type"], "runtime_status_sync")
        self.assertEqual(envelope["payload"]["project_id"], "p1")
        self.assertEqual(envelope["payload"]["sessions"], [{
            "task_id": "task-1",
            "status": "running",
            "agent_status": "reflecting",
            "current_tool": None,
        }])

    async def test_tick_without_clients_or_candidates_is_silent(self) -> None:
        handler = _make_handler({"task-1": _FakeTask(status="running")})

        # No candidates at all → nothing to broadcast.
        await handler._runtime_status_sync_tick()
        handler.broadcast.assert_not_awaited()

        # Candidates but no clients → still silent (snapshot covers reconnect).
        handler._task_bg_context[_fake_bg_task()] = {"task_id": "task-1", "project_id": "p1"}
        handler._clients = set()  # type: ignore[assignment]
        await handler._runtime_status_sync_tick()
        handler.broadcast.assert_not_awaited()

    async def test_departed_task_gets_one_final_tick(self) -> None:
        handler = _make_handler({"task-1": _FakeTask(status="done")})
        bg = _fake_bg_task()
        handler._task_bg_context[bg] = {"task_id": "task-1", "project_id": "p1"}

        await handler._runtime_status_sync_tick()
        self.assertEqual(handler.broadcast.await_count, 1)

        # Runtime finished: the task leaves the registry, one final sync fires.
        del handler._task_bg_context[bg]
        await handler._runtime_status_sync_tick()
        self.assertEqual(handler.broadcast.await_count, 2)
        envelope = handler.broadcast.await_args.args[0]
        self.assertEqual(envelope["payload"]["sessions"][0]["status"], "done")

        # After the final tick the task is forgotten entirely.
        await handler._runtime_status_sync_tick()
        self.assertEqual(handler.broadcast.await_count, 2)

    async def test_company_child_inherits_root_project(self) -> None:
        handler = _make_handler({
            "root-1": _FakeTask(status="running"),
            "child-1": _FakeTask(status="running"),
        })
        handler._task_bg_context[_fake_bg_task()] = {"task_id": "root-1", "project_id": "p1"}
        handler._active_runtime_children["root-1"] = "root-1"
        handler._active_runtime_children["child-1"] = "root-1"

        await handler._runtime_status_sync_tick()

        envelope = handler.broadcast.await_args.args[0]
        task_ids = [s["task_id"] for s in envelope["payload"]["sessions"]]
        self.assertEqual(task_ids, ["child-1", "root-1"])
        self.assertEqual(envelope["payload"]["project_id"], "p1")

    async def test_done_bg_tasks_are_not_candidates(self) -> None:
        handler = _make_handler({"task-1": _FakeTask(status="running")})
        handler._task_bg_context[_fake_bg_task(done=True)] = {"task_id": "task-1", "project_id": "p1"}

        await handler._runtime_status_sync_tick()
        handler.broadcast.assert_not_awaited()


class TrackerTurnLifecycleTests(unittest.TestCase):
    """The tracker must treat tool boundaries as reasoning, and turn ends as idle."""

    def _runtime_event(self, runtime_type: str, **extra: Any) -> FakeEvent:
        return FakeEvent("runtime_event", {"type": runtime_type, "task_id": "task-1", **extra})

    def test_tool_completed_returns_to_reflecting(self) -> None:
        adapter = EventAdapter()
        adapter.translate(self._runtime_event("tool_started", tool_name="file_read", agent_id="a1"))
        adapter.translate(self._runtime_event("tool_completed", agent_id="a1"))
        tracker = adapter._get_tracker("a1")
        self.assertEqual(tracker.state, AgentAnimState.REFLECTING)
        self.assertIsNone(tracker.current_tool)

    def test_turn_end_goes_idle_and_emits_runtime_update(self) -> None:
        adapter = EventAdapter()
        adapter.translate(self._runtime_event("turn_started", agent_id="a1"))
        for terminal in ("turn_completed", "turn_failed"):
            adapter.translate(self._runtime_event("turn_started", agent_id="a1"))
            events = adapter.translate(self._runtime_event(terminal, agent_id="a1"))
            tracker = adapter._get_tracker("a1")
            self.assertEqual(tracker.state, AgentAnimState.IDLE)
            updates = [e for e in events if e["type"] == "agent_runtime_update"]
            self.assertEqual(len(updates), 1)
            self.assertEqual(updates[0]["data"]["status"], "idle")


# ── Connection health (2026-09-26) ─────────────────────────────────────────
# The console kept one core at 100%; the page's connection dropped every minute or two because its ping waited behind
# a backlog of slow requests on the same connection, and every reconnect added more requests to the backlog.


class _RecordingSocket:
    closed = False
    closing = False

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


def _connection_handler() -> WSHandler:
    handler = _make_handler({})
    # Never the real account store: without an ampuly_home it would open the working directory's .ampuly.
    handler._accounts = lambda: SimpleNamespace(auth_required=lambda: False, business=None)  # type: ignore[method-assign]
    return handler


def _types(socket: _RecordingSocket) -> list[str]:
    return [str(message.get("type")) for message in socket.sent]


class ConnectionInboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_ping_is_answered_while_a_slow_request_holds_the_connection(self) -> None:
        import json
        from unittest import mock

        handler = _connection_handler()
        socket = _RecordingSocket()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_detail(self_: WSHandler, ws: Any, data: dict) -> None:
            started.set()
            await release.wait()
            await self_._send_ack(ws, ok=True, action="session_detail", task_id=data.get("task_id"))

        with mock.patch.dict(WSHandler._HANDLERS, {"session_detail": slow_detail}):
            inbox = _WSInbox(handler, socket)
            await inbox.accept(json.dumps({"type": "session_detail", "project_id": "p1", "task_id": "t1"}))
            await asyncio.wait_for(started.wait(), 2)
            await inbox.accept(json.dumps({"type": "ping"}))
            self.assertEqual(_types(socket), ["pong"], "the pong does not wait for the request ahead of it")
            release.set()
            await inbox.finish()
        self.assertEqual(_types(socket), ["pong", "ack"])

    async def test_identical_waiting_requests_merge_and_each_detail_request_still_gets_its_answer(self) -> None:
        import json
        from unittest import mock

        handler = _connection_handler()
        socket = _RecordingSocket()
        started = asyncio.Event()
        release = asyncio.Event()
        runs: list[str] = []

        async def detail(self_: WSHandler, ws: Any, data: dict) -> None:
            runs.append(f"detail:{data.get('task_id')}")
            if data.get("task_id") == "blocker":
                started.set()
                await release.wait()
            await self_._send_ack(ws, ok=True, action="session_detail", task_id=data.get("task_id"))

        async def collab(self_: WSHandler, ws: Any, data: dict) -> None:
            runs.append("collab")
            await ws.send_json({"type": "ack", "payload": {"ok": True, "channels": []}})

        same = {"type": "session_detail", "project_id": "p1", "task_id": "t2", "limit": 200, "detail_level": "full"}
        with mock.patch.dict(WSHandler._HANDLERS, {"session_detail": detail, "collab_sync": collab}):
            inbox = _WSInbox(handler, socket)
            await inbox.accept(json.dumps({**same, "task_id": "blocker"}))
            await asyncio.wait_for(started.wait(), 2)
            # While the connection is busy the page asks again and again: identical waiting requests fold together.
            for _ in range(3):
                await inbox.accept(json.dumps(same))
            await inbox.accept(json.dumps({**same, "task_id": "t3"}))
            await inbox.accept(json.dumps({"type": "collab_sync", "project_id": "p1"}))
            await inbox.accept(json.dumps({"type": "collab_sync", "project_id": "p1"}))
            await inbox.accept(json.dumps({**same, "detail_level": "summary"}))
            self.assertEqual(inbox.waiting, 4, "t2 full, t3, one sync, t2 summary")
            release.set()
            await inbox.finish()

        self.assertEqual(runs, ["detail:blocker", "detail:t2", "detail:t3", "collab", "detail:t2"],
                         "arrival order kept; each distinct request handled once")
        detail_acks = [m["payload"]["task_id"] for m in socket.sent if m["payload"].get("action") == "session_detail"]
        self.assertEqual(detail_acks, ["blocker", "t2", "t2", "t2", "t3", "t2"],
                         "three merged t2 requests, three answers: the page pairs them one to one")
        sync_acks = [m for m in socket.sent if "channels" in m["payload"]]
        self.assertEqual(len(sync_acks), 1, "the page pairs nothing with sync answers: one serves both")

    async def test_a_request_already_running_is_not_merged_into(self) -> None:
        import json
        from unittest import mock

        handler = _connection_handler()
        socket = _RecordingSocket()
        started = asyncio.Event()
        release = asyncio.Event()
        runs = 0

        async def detail(self_: WSHandler, ws: Any, data: dict) -> None:
            nonlocal runs
            runs += 1
            if runs == 1:
                started.set()
                await release.wait()
            await self_._send_ack(ws, ok=True, action="session_detail", task_id=data.get("task_id"))

        request = json.dumps({"type": "session_detail", "project_id": "p1", "task_id": "t1"})
        with mock.patch.dict(WSHandler._HANDLERS, {"session_detail": detail}):
            inbox = _WSInbox(handler, socket)
            await inbox.accept(request)
            await asyncio.wait_for(started.wait(), 2)
            await inbox.accept(request)
            release.set()
            await inbox.finish()
        self.assertEqual(runs, 2, "a request made after the first began gets an answer built after it asked")
        self.assertEqual(_types(socket), ["ack", "ack"])

    async def test_handle_ws_answers_the_ping_itself_and_still_handles_what_arrived_before_the_close(self) -> None:
        import json
        from unittest import mock

        from aiohttp.test_utils import make_mocked_request

        handler = _connection_handler()
        handler._ensure_progress_flush_loop = lambda: None  # type: ignore[method-assign]
        handler._ensure_runtime_status_sync_loop = lambda: None  # type: ignore[method-assign]
        handler._ensure_duty_watch_loop = lambda: None  # type: ignore[method-assign]
        handler._ensure_loop_watchdog = lambda: None  # type: ignore[method-assign]
        handler._console_api = lambda: SimpleNamespace(recover_pending_launches=AsyncMock(return_value=[]))  # type: ignore[method-assign]
        handler._sync_role_map = AsyncMock()  # type: ignore[method-assign]
        handler._send_auth_state = AsyncMock()  # type: ignore[method-assign]
        handler._send_welcome = AsyncMock(return_value=True)  # type: ignore[method-assign]
        frames: asyncio.Queue[str | None] = asyncio.Queue()
        sockets: list[Any] = []

        class _Socket(_RecordingSocket):
            def __init__(self) -> None:
                super().__init__()
                sockets.append(self)

            async def prepare(self, _request: Any) -> None:
                return None

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                frame = await frames.get()
                if frame is None:
                    raise StopAsyncIteration
                return SimpleNamespace(type=1, data=frame)

        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_detail(self_: WSHandler, ws: Any, data: dict) -> None:
            started.set()
            await release.wait()
            await self_._send_ack(ws, ok=True, action="session_detail", task_id=data.get("task_id"))

        request = make_mocked_request("GET", "/ws", headers={"Host": "127.0.0.1:8766"})
        with mock.patch("aiohttp.web.WebSocketResponse", _Socket), \
                mock.patch("ampuly.plugins.office_ui.file_serving.bootstrap_request_allowed", lambda *_a, **_k: True), \
                mock.patch.dict(WSHandler._HANDLERS, {"session_detail": slow_detail}):
            connection = asyncio.create_task(handler.handle_ws(request))
            frames.put_nowait(json.dumps({"type": "session_detail", "project_id": "p1", "task_id": "t1"}))
            await asyncio.wait_for(started.wait(), 2)
            frames.put_nowait(json.dumps({"type": "ping"}))
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertEqual(_types(sockets[0]), ["pong"], "answered by the reader while the request still runs")
            frames.put_nowait(None)  # the page closes the connection
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertFalse(connection.done(), "what arrived before the close is still being handled")
            release.set()
            await asyncio.wait_for(connection, 2)
        self.assertEqual(_types(sockets[0]), ["pong", "ack"])
        self.assertNotIn(sockets[0], handler._clients)


class EventLoopWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_loop_held_for_three_seconds_is_logged_once_with_its_stack_and_the_request_in_hand(self) -> None:
        import time

        from loguru import logger

        handler = _connection_handler()
        handler._ws_actions_in_progress()[object()] = ("session_detail", time.monotonic())
        lines: list[str] = []
        sink = logger.add(lambda message: lines.append(str(message)), level="WARNING", format="{message}")
        watchdog = _EventLoopWatchdog(
            asyncio.get_running_loop(),
            threshold=2.0,
            interval=0.1,
            describe_actions=handler._describe_ws_actions_in_progress,
        )
        try:
            watchdog.start()
            await asyncio.sleep(0.3)

            def _hold_the_loop_for_three_seconds() -> None:
                time.sleep(3.0)

            _hold_the_loop_for_three_seconds()
            await asyncio.sleep(0.3)
        finally:
            watchdog.stop()
            logger.remove(sink)
        held = [line for line in lines if "Event loop held for" in line]
        self.assertEqual(len(held), 1, "one warning per stall, at most one a minute")
        self.assertIn("session_detail", held[0], "names the request being handled")
        self.assertIn("_hold_the_loop_for_three_seconds", held[0], "carries the loop thread's stack")
        self.assertEqual(watchdog.stalls_reported, 1)

    async def test_the_watchdog_starts_once_and_stops_with_the_console(self) -> None:
        handler = _connection_handler()
        handler._ensure_loop_watchdog()
        first = handler._loop_watchdog
        handler._ensure_loop_watchdog()
        self.assertIs(handler._loop_watchdog, first)
        self.assertTrue(first.alive)
        handler._shutting_down = True
        for _ in range(40):
            if not first.alive:
                break
            await asyncio.sleep(0.05)
        self.assertFalse(first.alive, "the thread ends when the console shuts down")


class _SteppingClock:
    """A clock that moves ``step`` seconds each time it is read (thread-safe enough for one reader)."""

    def __init__(self, step: float) -> None:
        import threading

        self._lock = threading.Lock()
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        with self._lock:
            self._now += self._step
            return self._now


class ProcessCpuReportTests(unittest.TestCase):
    # AV (2026-09-26): this used to need half a core within a real 0.3 s window and failed on a busy machine. The
    # watchdog now takes its wall and CPU clocks from the caller, so the test decides what the process "used".

    def _run_window(self, *, cpu_step: float) -> tuple[_EventLoopWatchdog, list[str], list[str]]:
        import time

        from loguru import logger

        loop = asyncio.new_event_loop()
        warnings: list[str] = []
        infos: list[str] = []
        warn_sink = logger.add(lambda message: warnings.append(str(message)), level="WARNING", format="{message}")
        info_sink = logger.add(
            lambda message: infos.append(str(message)),
            level="INFO",
            format="{message}",
            filter=lambda record: record["level"].name == "INFO",
        )
        watchdog = _EventLoopWatchdog(
            loop,
            threshold=1e9,
            interval=0.001,
            cpu_window=0.3,
            cpu_warn_ratio=0.5,
            clock=_SteppingClock(0.1),
            cpu_clock=_SteppingClock(cpu_step),
        )
        try:
            watchdog.start()
            deadline = time.monotonic() + 5.0
            while watchdog.cpu_reports < 1 and time.monotonic() < deadline:
                time.sleep(0.005)
        finally:
            watchdog.stop()
            logger.remove(warn_sink)
            logger.remove(info_sink)
            loop.close()
        return watchdog, warnings, infos

    def test_a_busy_window_is_logged_as_a_warning(self) -> None:
        # The window is 0.3 s of the fake wall clock; the CPU clock is read once per window and moves 1 s: over a core.
        watchdog, warnings, _infos = self._run_window(cpu_step=1.0)
        self.assertGreaterEqual(watchdog.cpu_reports, 1)
        self.assertTrue(any("Console process used" in line for line in warnings), warnings)

    def test_a_quiet_window_is_logged_as_info_only(self) -> None:
        watchdog, warnings, infos = self._run_window(cpu_step=0.001)
        self.assertGreaterEqual(watchdog.cpu_reports, 1)
        self.assertFalse(any("Console process used" in line for line in warnings), warnings)
        self.assertTrue(any("Console process used" in line for line in infos), infos)


# ── AV (2026-09-26): the owner's click goes first; a closed page's reads are dropped ───────────────────────────────


class OwnerActionsGoFirstTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _handlers(runs: list[str], started: asyncio.Event, release: asyncio.Event) -> dict[str, Any]:
        async def detail(self_: WSHandler, ws: Any, data: dict) -> None:
            runs.append(f"detail:{data.get('task_id')}")
            if data.get("task_id") == "blocker":
                started.set()
                await release.wait()
            await self_._send_ack(ws, ok=True, action="session_detail", task_id=data.get("task_id"))

        async def collab(self_: WSHandler, ws: Any, data: dict) -> None:
            runs.append("collab")
            await ws.send_json({"type": "ack", "payload": {"ok": True, "channels": []}})

        async def reply(self_: WSHandler, ws: Any, data: dict) -> None:
            runs.append(f"reply:{data.get('checkpoint_id')}")
            await self_._send_ack(ws, ok=True, action="interaction_reply", checkpoint_id=data.get("checkpoint_id"))

        return {"session_detail": detail, "collab_sync": collab, "interaction_reply": reply}

    async def test_an_answer_is_handled_before_five_slow_reads_already_waiting(self) -> None:
        import json
        from unittest import mock

        handler = _connection_handler()
        socket = _RecordingSocket()
        started, release = asyncio.Event(), asyncio.Event()
        runs: list[str] = []
        with mock.patch.dict(WSHandler._HANDLERS, self._handlers(runs, started, release)):
            inbox = _WSInbox(handler, socket)
            await inbox.accept(json.dumps({"type": "session_detail", "project_id": "p1", "task_id": "blocker"}))
            await asyncio.wait_for(started.wait(), 2)
            for n in range(5):
                await inbox.accept(json.dumps({"type": "session_detail", "project_id": "p1", "task_id": f"slow-{n}"}))
            await inbox.accept(json.dumps({"type": "interaction_reply", "project_id": "p1", "checkpoint_id": "cp-1"}))
            self.assertEqual(inbox.waiting, 6)
            release.set()
            await inbox.finish()
        self.assertEqual(
            runs,
            ["detail:blocker", "reply:cp-1", *[f"detail:slow-{n}" for n in range(5)]],
            "the answer waits only for the request already running; reads keep their order after it",
        )
        acks = [m["payload"].get("action") for m in socket.sent if m.get("type") == "ack"]
        self.assertEqual(acks[:2], ["session_detail", "interaction_reply"], "the receipt goes out right after")

    async def test_a_read_is_not_folded_into_a_copy_queued_before_an_action(self) -> None:
        import json
        from unittest import mock

        handler = _connection_handler()
        socket = _RecordingSocket()
        started, release = asyncio.Event(), asyncio.Event()
        runs: list[str] = []
        sync = json.dumps({"type": "collab_sync", "project_id": "p1"})
        with mock.patch.dict(WSHandler._HANDLERS, self._handlers(runs, started, release)):
            inbox = _WSInbox(handler, socket)
            await inbox.accept(json.dumps({"type": "session_detail", "project_id": "p1", "task_id": "blocker"}))
            await asyncio.wait_for(started.wait(), 2)
            await inbox.accept(sync)
            await inbox.accept(json.dumps({"type": "interaction_reply", "project_id": "p1", "checkpoint_id": "cp-1"}))
            await inbox.accept(sync)
            self.assertEqual(inbox.waiting, 3, "the sync asked after the answer is not merged into the one before it")
            await inbox.accept(sync)
            self.assertEqual(inbox.waiting, 3, "two identical reads with no action between them still merge")
            release.set()
            await inbox.finish()
        self.assertEqual(runs, ["detail:blocker", "reply:cp-1", "collab", "collab"])

    async def test_reads_waiting_when_the_page_closes_are_dropped_and_actions_still_run(self) -> None:
        import json
        from unittest import mock

        handler = _connection_handler()
        socket = _RecordingSocket()
        started, release = asyncio.Event(), asyncio.Event()
        runs: list[str] = []
        with mock.patch.dict(WSHandler._HANDLERS, self._handlers(runs, started, release)):
            inbox = _WSInbox(handler, socket)
            await inbox.accept(json.dumps({"type": "session_detail", "project_id": "p1", "task_id": "blocker"}))
            await asyncio.wait_for(started.wait(), 2)
            for n in range(3):
                await inbox.accept(json.dumps({"type": "session_detail", "project_id": "p1", "task_id": f"r-{n}"}))
            await inbox.accept(json.dumps({"type": "interaction_reply", "project_id": "p1", "checkpoint_id": "cp-9"}))
            await inbox.accept(json.dumps({"type": "collab_sync", "project_id": "p1"}))
            socket.closed = True  # the page went away
            release.set()
            await inbox.finish()
        self.assertEqual(runs, ["detail:blocker", "reply:cp-9"], "the answer is still handled; nobody reads the rest")
        self.assertEqual(inbox.reads_dropped, 4)

    async def test_the_classification_is_conservative(self) -> None:
        # A read that leaves an audit record, and anything unknown, is an action: kept, and never dropped.
        for message_type in ("order_list", "interaction_reply", "session_send", "switch_project", "brand_new_thing"):
            self.assertFalse(_WSInbox._is_pure_read({"type": message_type}), message_type)
        for message_type in ("session_detail", "collab_sync", "org_info", "list_projects"):
            self.assertTrue(_WSInbox._is_pure_read({"type": message_type}), message_type)


class LoopWatchdogStartsWithTheConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_console_start_starts_the_watchdog_before_any_browser(self) -> None:
        from ampuly.plugins.office_ui import server

        handler = _connection_handler()
        handler._ensure_duty_watch_loop = lambda: None  # type: ignore[method-assign]
        self.assertIsNone(getattr(handler, "_loop_watchdog", None))
        await server._on_startup({"ws_handler": handler})  # type: ignore[arg-type]
        watchdog = handler._loop_watchdog
        try:
            self.assertTrue(watchdog.alive, "running with no socket connected")
            handler._ensure_loop_watchdog()  # what a connecting browser does
            self.assertIs(handler._loop_watchdog, watchdog, "no second watchdog")
        finally:
            handler._shutting_down = True
            watchdog.stop()
        self.assertFalse(watchdog.alive)

    async def test_a_watchdog_that_cannot_start_never_stops_the_console_starting(self) -> None:
        from ampuly.plugins.office_ui import server

        started: list[str] = []

        def broken() -> None:
            raise RuntimeError("no thread for you")

        fake = SimpleNamespace(_ensure_duty_watch_loop=lambda: started.append("duty"), _ensure_loop_watchdog=broken)
        await server._on_startup({"ws_handler": fake})  # type: ignore[arg-type]
        self.assertEqual(started, ["duty"])


# ── AV (2026-09-26): a page is sent an owner card again only when it is new or has changed ─────────────────────────


def _owner_card(checkpoint_id: str, *, status: str = "pending", prompt: str = "这一步需要你确认") -> dict[str, Any]:
    return {
        "message_id": f"checkpoint::{checkpoint_id}",
        "channel_id": "session:anchor",
        "sender": "assistant",
        "sender_name": "需要输入",
        "content": prompt,
        "created_at": 1_790_000_000.0,
        "reply_to_id": None,
        "mentions": [],
        "metadata": {"checkpoint_id": checkpoint_id, "checkpoint_status": status},
        "project_id": "p1",
    }


class _PushSocket(_RecordingSocket):
    async def send_str(self, data: str) -> None:
        import json

        self.sent.append(json.loads(data))


def _card_ids(socket: _RecordingSocket) -> list[str]:
    return [str(m["payload"]["message_id"]) for m in socket.sent if m.get("type") == "session_message"]


class OwnerCardReplayTests(unittest.IsolatedAsyncioTestCase):
    def _handler(self, cards: list[dict[str, Any]]) -> WSHandler:
        handler = _connection_handler()
        handler._engine_for_request = AsyncMock(return_value=(handler.engine, "p1"))  # type: ignore[method-assign]
        handler._owner_interaction_baseline_cards = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda **_kwargs: [dict(card) for card in cards],
        )
        return handler

    async def test_a_second_sync_on_the_same_connection_sends_only_new_or_changed_cards(self) -> None:
        from unittest import mock

        cards = [_owner_card("cp-1"), _owner_card("cp-2")]
        handler = self._handler(cards)
        socket, other = _RecordingSocket(), _RecordingSocket()
        handler._clients.update({socket, other})
        handler._client_project_ids[socket] = "p1"
        handler._client_project_ids[other] = "p1"
        sync = {"type": "collab_sync", "project_id": "p1"}
        with mock.patch(
            "ampuly.plugins.office_ui.ws_handler.build_collab_sync",
            new=AsyncMock(return_value={"channels": [], "messages": []}),
        ):
            await handler._handle_collab_sync(socket, dict(sync))
            self.assertEqual(_card_ids(socket), ["checkpoint::cp-1", "checkpoint::cp-2"], "the first sync sends every card")
            socket.sent.clear()
            await handler._handle_collab_sync(socket, dict(sync))
            self.assertEqual(_card_ids(socket), [], "nothing changed, nothing sent again")
            # Identical syncs merged by the inbox are answered through a recorder: same page, same record.
            await handler._answer_merged_request(socket, dict(sync), WSHandler._handle_collab_sync, 1)
            self.assertEqual(_card_ids(socket), [])

            cards[1] = _owner_card("cp-2", status="answered")
            cards.append(_owner_card("cp-3"))
            await handler._handle_collab_sync(socket, dict(sync))
            self.assertEqual(_card_ids(socket), ["checkpoint::cp-2", "checkpoint::cp-3"], "the changed card and the new one")

            socket.sent.clear()
            await handler._handle_collab_sync(socket, {**sync, "full_cards": True})
            self.assertEqual(len(_card_ids(socket)), 3, "the page asked for every card")

            await handler._handle_collab_sync(other, dict(sync))
            self.assertEqual(len(_card_ids(other)), 3, "another tab keeps its own record")

            # The page went to another project and came back: its message list was replaced, it gets every card.
            socket.sent.clear()
            handler._client_project_ids[socket] = "p2"
            await handler._send_owner_interaction_baseline_for_client(socket, engine=handler.engine, project_id="p2")
            handler._client_project_ids[socket] = "p1"
            socket.sent.clear()
            await handler._handle_collab_sync(socket, dict(sync))
            self.assertEqual(len(_card_ids(socket)), 3)

            # A card that closed and later opens again is news again.
            closed = cards.pop(0)
            socket.sent.clear()
            await handler._handle_collab_sync(socket, dict(sync))
            self.assertEqual(_card_ids(socket), [])
            cards.insert(0, closed)
            await handler._handle_collab_sync(socket, dict(sync))
            self.assertEqual(_card_ids(socket), ["checkpoint::cp-1"])

    async def test_pushed_cards_leave_out_a_page_that_already_shows_them(self) -> None:
        cards = [_owner_card("cp-1"), _owner_card("cp-2")]
        handler = self._handler(cards)
        del handler.broadcast  # the real broadcast, not _make_handler's stand-in
        socket = _PushSocket()
        handler._clients = {socket}  # type: ignore[assignment]
        handler._client_project_ids[socket] = "p1"

        await handler._broadcast_owner_interaction_baseline(engine=handler.engine, project_id="p1")
        self.assertEqual(_card_ids(socket), ["checkpoint::cp-1", "checkpoint::cp-2"])
        socket.sent.clear()
        await handler._broadcast_owner_interaction_baseline(engine=handler.engine, project_id="p1")
        self.assertEqual(_card_ids(socket), [], "on_kanban_changed, a snapshot or a run finishing: no replay of the same cards")
        cards[0] = _owner_card("cp-1", prompt="改过的问题")
        await handler._broadcast_owner_interaction_baseline(engine=handler.engine, project_id="p1")
        self.assertEqual(_card_ids(socket), ["checkpoint::cp-1"])

        # One exact change pushed from the runtime reaches the page once.
        socket.sent.clear()
        changed = [_owner_card("cp-2", status="answered")]
        await handler._broadcast_owner_interaction_cards(changed, project_id="p1")
        await handler._broadcast_owner_interaction_cards(changed, project_id="p1")
        self.assertEqual(_card_ids(socket), ["checkpoint::cp-2"])

    async def test_a_card_the_sync_itself_carried_is_sent_again_after_it(self) -> None:
        """AV re-review: the sync carries its own copy of a session's newest open card under the same id, and the
        page merges it over the card it shows. The replay after that sync must put the authoritative card back."""
        from unittest import mock

        cards = [_owner_card("cp-1"), _owner_card("cp-2")]
        handler = self._handler(cards)
        socket = _RecordingSocket()
        handler._clients.add(socket)
        handler._client_project_ids[socket] = "p1"
        sync = {"type": "collab_sync", "project_id": "p1"}
        carried = {"channels": [], "messages": [dict(_owner_card("cp-2"), sender_name="anchor-1")]}
        with mock.patch(
            "ampuly.plugins.office_ui.ws_handler.build_collab_sync",
            new=AsyncMock(return_value=carried),
        ):
            await handler._handle_collab_sync(socket, dict(sync))
            self.assertEqual(_card_ids(socket), ["checkpoint::cp-1", "checkpoint::cp-2"])
            socket.sent.clear()
            await handler._handle_collab_sync(socket, dict(sync))
            self.assertEqual(_card_ids(socket), ["checkpoint::cp-2"], "the card the sync carried goes again, the other does not")
            sent_card = [m["payload"] for m in socket.sent if m.get("type") == "session_message"][0]
            self.assertEqual(sent_card["sender_name"], "需要输入", "the authoritative card, not the sync's copy")

    async def test_a_pushed_sync_is_followed_by_the_cards_it_carried(self) -> None:
        cards = [_owner_card("cp-1"), _owner_card("cp-2")]
        handler = self._handler(cards)
        del handler.broadcast  # the real broadcast, not _make_handler's stand-in
        socket = _PushSocket()
        handler._clients = {socket}  # type: ignore[assignment]
        handler._client_project_ids[socket] = "p1"
        await handler._broadcast_owner_interaction_baseline(engine=handler.engine, project_id="p1")
        socket.sent.clear()
        pushed = {"messages": [dict(_owner_card("cp-1"), sender_name="anchor-1")]}
        await handler._broadcast_owner_interaction_baseline(engine=handler.engine, project_id="p1", after_sync=pushed)
        self.assertEqual(_card_ids(socket), ["checkpoint::cp-1"])

    async def test_a_sync_for_a_view_the_page_moved_to_replays_every_card(self) -> None:
        """The page threw the answer for view 2 away and may replace its list on view 3's answer: every card again."""
        from unittest import mock

        cards = [_owner_card("cp-1"), _owner_card("cp-2")]
        handler = self._handler(cards)
        socket = _RecordingSocket()
        handler._clients.add(socket)
        handler._client_project_ids[socket] = "p1"
        sync = {"type": "collab_sync", "project_id": "p1"}
        with mock.patch(
            "ampuly.plugins.office_ui.ws_handler.build_collab_sync",
            new=AsyncMock(return_value={"channels": [], "messages": []}),
        ):
            # The page clicked the same project twice while its sync for view 2 waited: the server
            # answers view 2 (every card) and the page drops that answer.
            await handler._handle_collab_sync(socket, {**sync, "view_generation": 2})
            self.assertEqual(len(_card_ids(socket)), 2)
            socket.sent.clear()
            await handler._handle_collab_sync(socket, {**sync, "view_generation": 3})
            self.assertEqual(len(_card_ids(socket)), 2, "a sync for another view gets every card")
            socket.sent.clear()
            await handler._handle_collab_sync(socket, {**sync, "view_generation": 3})
            self.assertEqual(_card_ids(socket), [], "the same view again: nothing unchanged is resent")
            await handler._handle_collab_sync(socket, {**sync, "view_generation": "3"})
            self.assertEqual(_card_ids(socket), [], "the page's number and its text are the same view")
            await handler._answer_merged_request(
                socket, {**sync, "view_generation": 3}, WSHandler._handle_collab_sync, 1,
            )
            self.assertEqual(_card_ids(socket), [], "a merged sync for the same view: same record")
            await handler._answer_merged_request(
                socket, {**sync, "view_generation": 4}, WSHandler._handle_collab_sync, 1,
            )
            self.assertEqual(len(_card_ids(socket)), 2, "a merged sync for a new view gets every card")

    async def test_a_stand_in_socket_is_never_remembered(self) -> None:
        """A bare recorder (auto-resume, a retry) and a socket that left get every card and leave no record."""
        from unittest import mock

        from ampuly.plugins.office_ui.ws_handler import _AckRecorder

        cards = [_owner_card("cp-1"), _owner_card("cp-2")]
        handler = self._handler(cards)
        recorder = _AckRecorder()
        project = handler._client_active_project_id(recorder)
        with mock.patch(
            "ampuly.plugins.office_ui.ws_handler.build_collab_sync",
            new=AsyncMock(return_value={"channels": [], "messages": []}),
        ):
            for _ in range(2):
                await handler._refresh_runtime_control_for_client(recorder, engine=handler.engine, project_id=project)
        self.assertEqual(
            [m["payload"]["message_id"] for m in recorder.messages if m.get("type") == "session_message"],
            ["checkpoint::cp-1", "checkpoint::cp-2"] * 2,
        )
        memory = getattr(handler, "_owner_cards_sent", None) or {}
        self.assertNotIn(recorder, memory, "a recorder never closes: remembering it would keep it alive")

        # A page that has left: every card, no record.
        gone = _RecordingSocket()
        handler._client_project_ids[gone] = "p1"
        await handler._send_owner_interaction_baseline_for_client(gone, engine=handler.engine, project_id="p1")
        await handler._send_owner_interaction_baseline_for_client(gone, engine=handler.engine, project_id="p1")
        self.assertEqual(len(_card_ids(gone)), 4)
        self.assertNotIn(gone, getattr(handler, "_owner_cards_sent", None) or {})

        # A connected page is remembered until it leaves; the next page's call drops its record.
        page, next_page = _RecordingSocket(), _RecordingSocket()
        handler._clients.update({page, next_page})
        handler._client_project_ids[page] = "p1"
        handler._client_project_ids[next_page] = "p1"
        await handler._send_owner_interaction_baseline_for_client(page, engine=handler.engine, project_id="p1")
        self.assertIn(page, handler._owner_cards_sent)
        handler._clients.discard(page)
        await handler._send_owner_interaction_baseline_for_client(next_page, engine=handler.engine, project_id="p1")
        self.assertNotIn(page, handler._owner_cards_sent)
        self.assertIn(next_page, handler._owner_cards_sent)


class RequestReadScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_task_started_inside_a_request_reads_the_store_itself(self) -> None:
        from ampuly.plugins.office_ui import request_reads

        loads: list[str] = []

        class _CountingStore:
            async def get_tasks(self, project_id: str | None = None, **_kwargs: Any) -> list[str]:
                loads.append(str(project_id))
                return [f"read {len(loads)}"]

        store = _CountingStore()
        with request_reads.request_read_scope() as scope:
            first = await request_reads.project_tasks(store, "p1")
            self.assertEqual(await request_reads.project_tasks(store, "p1"), first)
            self.assertEqual(len(loads), 1, "the request's own task reads once")
            # A task the request starts may run on after a write the request makes.
            child = await asyncio.create_task(request_reads.project_tasks(store, "p1"))
            self.assertEqual(child, ["read 2"])

            async def scope_seen_by_child() -> Any:
                return request_reads.current_request_reads()

            self.assertIsNone(await asyncio.create_task(scope_seen_by_child()))

            async def child_scope() -> int:
                with request_reads.request_read_scope() as own:
                    await request_reads.project_tasks(store, "p1")
                    await request_reads.project_tasks(store, "p1")
                    return own.task_loads

            self.assertEqual(await asyncio.create_task(child_scope()), 1, "a child opens a scope of its own")
            self.assertIs(request_reads.current_request_reads(), scope, "the request's scope is untouched")
            self.assertEqual(await request_reads.project_tasks(store, "p1"), first)
        self.assertEqual(len(loads), 3)
        self.assertIsNone(request_reads.current_request_reads())


# ── AV (2026-09-26): one request reads the project's tasks once; one sync shares one identity index ──────────────


async def _company_runtime_console(tmp_path: Any) -> SimpleNamespace:
    """A real store and engine with two company runtimes, a task-mode session and four owner cards."""
    import json
    from datetime import datetime, timedelta
    from unittest.mock import MagicMock

    from ampuly.core.models import ExecutionCheckpoint, Task, TaskStatus
    from ampuly.database.store import AmpulyStore, _SQLiteConnectionAdapter
    from ampuly.engine import AmpulyEngine
    from ampuly.plugins.office_ui.chat_store import ChatStore

    store = AmpulyStore(tmp_path / "tasks.db")
    await store.initialize()
    base = datetime.now() - timedelta(hours=1)
    note = [{"seq": n, "text": f"note {n}"} for n in range(50)]

    async def save(task_id: str, session_id: str, status: TaskStatus, offset: int, **fields: Any) -> None:
        metadata = dict(fields.pop("metadata", {}))
        metadata["notes"] = note
        await store.save_task(Task(
            id=task_id, title=task_id, project_id="av", session_id=session_id, status=status,
            created_at=base + timedelta(seconds=offset), metadata=metadata, **fields,
        ))

    company = {"execution_mode": "company_mode", "company_profile": "corporate", "delegation_run_id": "run-1"}
    await save("anchor-1", "rs-1", TaskStatus.RUNNING, 0, metadata={"exec_mode": "company", "company_profile": "corporate"})
    for i in range(1, 9):
        await save(f"wi-{i}", f"s-{i}", TaskStatus.IDLE, i, parent_session_id="rs-1",
                   metadata={**company, "work_item_projection_id": f"role::{i}"})
    for i in range(9, 13):
        await save(f"wi-{i}", f"s-{i}", TaskStatus.DONE, i, parent_session_id="rs-1",
                   metadata={**company, "work_item_projection_id": f"role::{i}"})
    await save("anchor-2", "rs-2", TaskStatus.IDLE, 20, metadata={"exec_mode": "company", "company_profile": "corporate"})
    for i in (1, 2):
        await save(f"wi-2-{i}", f"s-2-{i}", TaskStatus.IDLE, 20 + i, parent_session_id="rs-2",
                   metadata={**company, "delegation_run_id": "run-2", "work_item_projection_id": f"other::{i}"})
    await save("plain", "plain-session", TaskStatus.IDLE, 30, metadata={"mode": "task"})
    await store.save_execution_checkpoint(ExecutionCheckpoint(
        checkpoint_id="suspend-2", project_id="av", session_id="rs-2", checkpoint_type="company_runtime_suspended",
        status="pending", payload={"parent_session_id": "rs-2"},
        created_at=base + timedelta(minutes=5), updated_at=base + timedelta(minutes=5),
    ))
    # Owner cards are published through the coordinator in real runs; test rows are written directly.
    for i in (1, 2, 3, 4):
        await store._db.execute(
            "INSERT INTO execution_checkpoints (checkpoint_id, project_id, session_id, checkpoint_type, status, "
            "task_id, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"cp-{i}", "av", f"s-{i}", "task_user_input", "pending", f"wi-{i}", json.dumps({
                "task_id": f"wi-{i}", "prompt": f"第 {i} 步需要你确认",
                "runtime_v2": {"runtime_session_id": f"rt-{i}"},
                "pause_request": {"reason": "需要确认", "questions": ["继续吗？"]},
                "interaction": {"ownership": {
                    "ui_anchor_task_id": "anchor-1", "ui_anchor_session_id": "rs-1",
                    "waiting_task_id": f"wi-{i}", "waiting_session_id": f"s-{i}",
                    "company_runtime_session_id": "rs-1",
                }},
            }), (base + timedelta(minutes=i)).isoformat(), (base + timedelta(minutes=i)).isoformat()),
        )
    await store._db.commit()

    loads: list[str] = []
    real_get_tasks = store.get_tasks

    async def counting_get_tasks(*args: Any, **kwargs: Any) -> Any:
        loads.append(str(kwargs.get("project_id") or (args[0] if args else "")))
        return await real_get_tasks(*args, **kwargs)

    store.get_tasks = counting_get_tasks  # type: ignore[method-assign]
    engine = AmpulyEngine.__new__(AmpulyEngine)
    engine.store = store
    engine.project_id = "av"
    engine.config = SimpleNamespace(
        org=SimpleNamespace(company_profile="corporate"),
        autonomy=SimpleNamespace(native_approval_level="auto"),
    )
    engine.interaction_coordinator = None
    engine._project_owner_actor_token = ""
    engine.llm = None
    engine._task_runtime_is_live = lambda _task: False

    chat_db = _SQLiteConnectionAdapter(":memory:")
    chat_store = ChatStore(chat_db)  # type: ignore[arg-type]
    await chat_store.initialize()
    stand_in = MagicMock()
    stand_in.project_id = "av"
    stand_in.ampuly_home = tmp_path / "home"
    handler = WSHandler(stand_in, MagicMock(), chat_store, EventAdapter())
    handler.engine = engine
    handler._engine_for_request = AsyncMock(return_value=(engine, "av"))  # type: ignore[method-assign]
    handler._exec_mode = "company"
    handler._accounts = lambda: SimpleNamespace(auth_required=lambda: False, business=None)  # type: ignore[method-assign]
    socket = _PushSocket()
    handler._clients.add(socket)
    handler._client_project_ids[socket] = "av"

    async def close() -> None:
        await chat_db.close()
        await store.close()

    return SimpleNamespace(store=store, engine=engine, handler=handler, socket=socket, loads=loads, close=close)


def test_one_session_detail_reads_the_project_tasks_once(tmp_path: Any) -> None:
    async def scenario() -> None:
        console = await _company_runtime_console(tmp_path)
        try:
            for task_id, cards in (("anchor-1", 4), ("wi-1", 0), ("anchor-2", 0)):
                console.loads.clear()
                console.socket.sent.clear()
                await console.handler._handle_session_detail(
                    console.socket, {"project_id": "av", "task_id": task_id, "detail_level": "full"},
                )
                ack = next(m["payload"] for m in console.socket.sent if m.get("type") == "ack")
                assert ack["ok"] is True, ack
                shown = [m["message_id"] for m in ack["messages"] if str(m.get("message_id", "")).startswith("checkpoint::")]
                assert len(shown) == cards, (task_id, shown)
                # Before AV: 6 loads for the anchor (4 cards + runtime config + runtime controls), 2 for a work item.
                assert console.loads == ["av"], (task_id, console.loads)
        finally:
            await console.close()

    asyncio.run(scenario())


def test_one_sync_shares_one_identity_index_across_every_session_and_card(tmp_path: Any) -> None:
    from unittest import mock

    from ampuly.plugins.office_ui import request_reads

    async def scenario() -> None:
        console = await _company_runtime_console(tmp_path)
        engine = console.engine
        real_latest = engine.get_latest_pending_checkpoint_for_session
        indexes_seen: list[Any] = []

        async def latest_spy(
            session_id: str | None = None,
            *,
            persist_runtime_v2_migration: bool = True,
            identity_index: Any | None = None,
        ) -> Any:
            indexes_seen.append(identity_index)
            return await real_latest(
                session_id,
                persist_runtime_v2_migration=persist_runtime_v2_migration,
                identity_index=identity_index,
            )

        engine.get_latest_pending_checkpoint_for_session = latest_spy
        request_index_builds: list[str] = []
        real_load = request_reads.load_company_runtime_identity_index

        async def counting_load(store: Any, project_id: str) -> Any:
            request_index_builds.append(project_id)
            return await real_load(store, project_id)

        try:
            with mock.patch.object(request_reads, "load_company_runtime_identity_index", counting_load):
                await console.handler._handle_collab_sync(console.socket, {"project_id": "av"})
            assert request_index_builds == ["av"], "the sync, its sessions and its card replay share one index"
            live_sessions = 1 + 8 + 1 + 2 + 1  # both anchors, their idle work items, the task-mode session
            assert len(indexes_seen) == live_sessions, len(indexes_seen)
            assert indexes_seen[0] is not None and all(index is indexes_seen[0] for index in indexes_seen)
            # Before AV: 1 + one per session that reaches its runtime's snapshot + one per card (14 here).
            # AV: the sync's own read, and each runtime's snapshot once (two runtimes).
            # AZ: each runtime's snapshot is read from the sync's index too, so only the sync's own read.
            assert console.loads == ["av"], console.loads
            assert _card_ids(console.socket) == [f"checkpoint::cp-{i}" for i in (1, 2, 3, 4)]

            # Same answers as asking each session on its own, with a fresh index every time.
            shared = indexes_seen[0]
            answers: dict[str, Any] = {}
            for task in await console.store.get_tasks(project_id="av"):
                alone = await real_latest(task.session_id, persist_runtime_v2_migration=False)
                together = await real_latest(task.session_id, persist_runtime_v2_migration=False, identity_index=shared)
                assert getattr(alone, "checkpoint_id", None) == getattr(together, "checkpoint_id", None), task.session_id
                answers[task.id] = getattr(together, "checkpoint_id", None)
            # Not a vacuous comparison: own cards, the runtime's newest card, a suspended runtime, and nothing.
            assert (answers["wi-1"], answers["anchor-1"], answers["wi-6"]) == ("cp-1", "cp-4", "cp-4"), answers
            assert (answers["anchor-2"], answers["wi-2-1"], answers["plain"]) == ("suspend-2", "suspend-2", None), answers
            # Each runtime's snapshot read from the shared index is the one it rebuilds on its own.
            for runtime_session_id, run_id in (("rs-1", None), ("rs-2", None), ("rs-1", "run-1"), ("rs-1", "run-2"),
                                               ("s-1", None), ("plain-session", None)):
                console.loads.clear()
                alone = await engine._load_company_runtime_snapshot(runtime_session_id, delegation_run_id=run_id)
                assert console.loads == ["av"], (runtime_session_id, console.loads)
                console.loads.clear()
                together = await engine._load_company_runtime_snapshot(
                    runtime_session_id, delegation_run_id=run_id, identity_index=shared,
                )
                assert console.loads == [], (runtime_session_id, console.loads)
                shape = lambda snap: None if snap is None else (  # noqa: E731
                    [spec.projection_id for spec in snap[0].projections], dict(snap[0].metadata),
                    snap[0].profile, [task.id for task in snap[1]],
                )
                assert shape(alone) == shape(together), (runtime_session_id, run_id)
            first = await engine._load_company_runtime_snapshot("rs-1", identity_index=shared)
            assert first is not None and [task.id for task in first[1]] == [f"wi-{i}" for i in range(1, 13)], first
            assert await engine._load_company_runtime_snapshot("rs-1", delegation_run_id="run-2", identity_index=shared) is None
            assert await engine._load_company_runtime_snapshot("plain-session", identity_index=shared) is None
            for checkpoint in await console.store.get_execution_checkpoints(project_id="av"):
                if checkpoint.checkpoint_type != "task_user_input":
                    continue
                for requester in ({"requester_task_id": "anchor-1"}, {"requester_session_id": "rs-1"},
                                  {"requester_task_id": "wi-1"}, {"requester_session_id": "rs-2"}):
                    alone = await engine.can_answer_checkpoint(checkpoint, **requester)
                    together = await engine.can_answer_checkpoint(checkpoint, identity_index=shared, **requester)
                    assert alone == together, (checkpoint.checkpoint_id, requester)
        finally:
            await console.close()

    asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
