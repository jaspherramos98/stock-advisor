"""
Persistent MCP session — reuse ONE connection for many tool calls.

Opening an MCP session costs ~1.2s (OAuth provider init + HTTP connect + MCP initialize + teardown),
and that used to be paid on EVERY tool call: a dashboard cold load made 7 calls (~10.7s) and an agent
cycle 7 (~8.8s of 11.4s). This keeps one session open on a dedicated background event-loop thread and
routes synchronous calls into it, so only the first call of a burst pays the handshake.

Design constraints (each one is load-bearing):
  - The MCP streamable-HTTP client uses anyio task groups / cancel scopes, which must be ENTERED AND
    EXITED IN THE SAME TASK. So a single long-lived "owner" coroutine opens the session, serves a queue
    of calls, and closes it — the session is never opened in one task and closed in another.
  - Calls are serialized (one at a time). Simple and safe; throughput isn't the bottleneck, latency is.
  - A failed call closes the session (it may be broken: dropped connection, server-side expiry) and is
    retried ONCE on a fresh session — but ONLY for side-effect-free tools (`is_retryable`). Order
    placement is NEVER auto-retried: the first attempt may have reached the broker even though the
    response was lost, and a retry could double-place the order.
  - After `idle_seconds` with no calls the session is closed; the next call reopens it.
  - No `mcp` import here: the opener (an async context manager yielding an object with an async
    `call_tool(name, arguments=...)`) is injected, so this is unit-testable without the optional SDK.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, AsyncContextManager, Callable

IDLE_SECONDS = 300.0
CALL_TIMEOUT_SECONDS = 120.0

_RESET = object()   # queue sentinel: close the open session (e.g. after a fresh login)


def is_retryable(tool_name: str) -> bool:
    """Only read/preview tools may be retried after a failure — never anything that places an order."""
    return tool_name.startswith(("get_", "review_", "list_"))


class PersistentSession:
    def __init__(self, opener: Callable[[], AsyncContextManager[Any]], idle_seconds: float = IDLE_SECONDS):
        self._opener = opener
        self._idle = idle_seconds
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._thread: threading.Thread | None = None
        self._retried: set = set()   # futures already retried once (touched only on the loop thread)
        self.opens = 0               # sessions opened — diagnostics/tests

    # ---- public, synchronous API ---------------------------------------------------------------
    def call(self, name: str, arguments: dict | None = None, timeout: float = CALL_TIMEOUT_SECONDS):
        """Run one tool call on the shared session (opening it if needed). Thread-safe."""
        loop = self._ensure_running()
        return asyncio.run_coroutine_threadsafe(self._submit(name, arguments or {}), loop).result(timeout)

    def reset(self) -> None:
        """Close the open session so the next call reopens it (e.g. with freshly-stored tokens)."""
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                asyncio.run_coroutine_threadsafe(self._queue.put(_RESET), self._loop).result(10)

    # ---- event-loop thread ---------------------------------------------------------------------
    def _ensure_running(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                loop = asyncio.new_event_loop()
                started = threading.Event()

                def run() -> None:
                    asyncio.set_event_loop(loop)
                    self._queue = asyncio.Queue()
                    loop.create_task(self._owner())
                    started.set()
                    loop.run_forever()

                self._thread = threading.Thread(target=run, name="mcp-session", daemon=True)
                self._thread.start()
                started.wait(5)
                self._loop = loop
            return self._loop

    async def _submit(self, name: str, args: dict):
        fut = asyncio.get_running_loop().create_future()
        await self._queue.put((name, args, fut))
        return await fut

    async def _owner(self) -> None:
        """The ONLY task that opens/closes sessions (anyio same-task rule). Loops forever."""
        pending = None
        while True:
            item = pending if pending is not None else await self._queue.get()
            pending = None
            if item is _RESET:
                continue                      # nothing open — nothing to reset
            fut = item[2]
            try:
                async with self._opener() as session:
                    self.opens += 1
                    while True:
                        if item is _RESET:
                            break
                        name, args, fut = item
                        if not await self._run(session, name, args, fut):
                            if not fut.done():  # retryable failure → reopen, then retry this call once
                                pending = item
                            break
                        try:
                            item = await asyncio.wait_for(self._queue.get(), timeout=self._idle)
                        except asyncio.TimeoutError:
                            break             # idle → close; the next call reopens
            except Exception as e:  # opening failed (e.g. NotAuthenticated) or a teardown error
                if not fut.done():
                    fut.set_exception(e)
                if pending is not None and pending[2] is fut:
                    pending = None
            if fut.done():
                self._retried.discard(fut)

    async def _run(self, session, name: str, args: dict, fut: asyncio.Future) -> bool:
        """Execute one call. True = session still healthy. False = close it (fut failed, or is
        left pending when a single retry is allowed)."""
        try:
            result = await session.call_tool(name, arguments=args)
        except Exception as e:
            if is_retryable(name) and fut not in self._retried:
                self._retried.add(fut)        # leave fut pending → owner reopens + retries once
            elif not fut.done():
                fut.set_exception(e)
            return False
        if not fut.done():
            fut.set_result(result)
        self._retried.discard(fut)
        return True
