"""Handler-owned, bounded independent RO reads; never a permission cache."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import threading


class BoundedPrincipalReader:
    """Only the existing BusinessStore RO query runs on these two workers.

    Admission happens before submit. Cancelling an await does not release a
    worker permit: the concurrent future's real completion does that. Neither
    token/slot mutation nor an event-loop dependency belongs in the worker.
    """

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._owner_thread = threading.get_ident()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ampuly-principal-ro")
        self._slots = asyncio.Semaphore(2)
        self._pending: set[Future] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False

    def _owned(self) -> None:
        if (threading.get_ident() != self._owner_thread
                or asyncio.get_running_loop() is not self._loop):
            raise RuntimeError("principal reader owner changed")

    @staticmethod
    def _query(business, login: str, account_store):
        from .business_store import BusinessStore
        return BusinessStore._read_account_principal_current(
            business, login=login, _account_store=account_store,
        )

    def _finished(self, completed: Future) -> None:
        self._pending.discard(completed)
        self._slots.release()
        if not self._pending:
            self._idle.set()

    async def read(self, business, *, login: str, account_store):
        self._owned()
        if self._closed:
            raise RuntimeError("principal reader closed")
        await self._slots.acquire()
        if self._closed:
            self._slots.release()
            raise RuntimeError("principal reader closed")
        try:
            future = self._executor.submit(self._query, business, login, account_store)
        except BaseException:
            self._slots.release()
            raise
        self._pending.add(future)
        self._idle.clear()

        def completed(done: Future) -> None:
            # No worker waits for this callback. The handler must drain before
            # closing its loop; shutdown timeout is a failure, not a false reap.
            if not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._finished, done)

        future.add_done_callback(completed)
        wrapped = asyncio.wrap_future(future, loop=self._loop)
        # Consume errors even when its original waiter was cancelled.
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            future.cancel()  # Effective only before native work starts.
            raise

    def close(self) -> None:
        self._owned()
        if not self._closed:
            self._closed = True
            self._executor.shutdown(wait=False, cancel_futures=True)

    async def wait_closed(self, timeout: float = 5.0) -> None:
        self.close()
        async with asyncio.timeout(timeout):
            await self._idle.wait()
