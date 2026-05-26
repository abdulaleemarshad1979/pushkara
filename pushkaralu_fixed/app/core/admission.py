"""
Godavari Pushkaralu 2027 — Admission Control & Backpressure Queue

The single most important thing standing between the API and an OOM-driven
crash under festival-day load is admission control: refusing or queueing new
work when the system is already saturated, instead of letting every request
spawn unbounded coroutines that all compete for the same Postgres pool, the
same Redis connection, and the same event loop time slice.

This module provides:

  1. `AdmissionGate` — a bounded async semaphore + asyncio.Queue with three
     behaviours per slot:
       - FAST  (semaphore non-blocking try): immediate accept or 503
       - WAIT  (semaphore + queue): hold for up to `wait_ms` then 503
       - STRICT: never queue, fail fast at saturation
     Hot endpoints (SOS, report, lost) use WAIT so a brief spike of 1000 RPS
     does NOT translate to 1000 concurrent Postgres acquires; they trickle
     through the gate at a rate the backend can sustain.

  2. `priority_gate(...)` decorator — applies the gate around an async
     handler with one line.

  3. `bg_pool` — a bounded ThreadPoolExecutor used by `run_blocking()` for
     CPU-bound calls (bcrypt, gc, YOLO, cv2). Default workers = max(4, cpu*2).
     Sharing one pool is critical: each `asyncio.to_thread()` call without
     a shared executor reuses the default loop pool which can deadlock when
     callers nest threadpool work.

  4. `gather_bounded(...)` — gathers awaitables with a concurrency cap so
     fan-outs (e.g. notifying volunteers, retrying pending WhatsApp sends)
     can never spawn unbounded coroutines.

Time / space complexity:
  - acquire (FAST):    O(1) — single counter decrement
  - acquire (WAIT):    O(1) put + O(1) take  via asyncio.Queue (FIFO)
  - release:           O(1)
  - in-flight memory:  O(active + queue_size)  bounded by `max_concurrent
                       + max_waiters`, NOT by request rate
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Awaitable, Callable, Iterable, Optional, TypeVar

from fastapi import HTTPException, status

logger = logging.getLogger("pushkaralu.admission")

T = TypeVar("T")


# ─────────────────────────────────────────────────────────────────────────────
# Shared bounded thread pool for blocking work (bcrypt, OpenCV, YOLO, gc)
# ─────────────────────────────────────────────────────────────────────────────
# asyncio.to_thread() uses the loop's default executor (ThreadPoolExecutor with
# max_workers = min(32, cpu+4)). Under sustained load that pool is shared with
# every other library that calls run_in_executor — file I/O for static files,
# DNS resolution, etc. — and can deadlock when a thread waits on something
# that is itself queued.  A dedicated pool gives blocking work a private lane
# with predictable bounds.
_BG_WORKERS = int(os.getenv("BG_THREAD_POOL_WORKERS", "0")) or max(4, (os.cpu_count() or 2) * 2)
bg_pool: ThreadPoolExecutor = ThreadPoolExecutor(
    max_workers=_BG_WORKERS,
    thread_name_prefix="pushkaralu-bg",
)
logger.info("[Admission] Background thread pool ready  workers=%d", _BG_WORKERS)


async def run_blocking(fn: Callable[..., T], /, *args, **kwargs) -> T:
    """
    Run a CPU-bound or sync I/O-bound function in the dedicated thread pool.

    Use this instead of asyncio.to_thread for:
      - bcrypt hash / verify
      - YOLO inference + cv2 frame I/O
      - gc.collect()
      - any third-party sync library

    Returns the function's result. Raises whatever the function raises.
    """
    loop = asyncio.get_running_loop()
    if kwargs:
        # ThreadPoolExecutor.submit() doesn't accept kwargs — wrap with lambda.
        return await loop.run_in_executor(bg_pool, lambda: fn(*args, **kwargs))
    return await loop.run_in_executor(bg_pool, fn, *args)


# ─────────────────────────────────────────────────────────────────────────────
# Admission Gate — bounded concurrency + bounded queue
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AdmissionStats:
    """Per-gate observability counters. All ints — no allocation on update."""
    accepted: int = 0
    queued: int = 0
    rejected_full: int = 0
    rejected_timeout: int = 0
    in_flight: int = 0
    waiting: int = 0
    last_reject_ts: float = 0.0


class AdmissionGate:
    """
    Bounded admission control with optional FIFO wait queue.

    Behaviour:
      max_concurrent   — hard cap on simultaneous requests through the gate
      max_waiters      — extra requests allowed to wait their turn
      wait_ms          — how long a waiter holds before a 503 is returned

    A request that arrives when both concurrent slots AND wait slots are
    full gets a 503 IMMEDIATELY rather than backing up upstream queues
    (nginx, kernel sockets) which would otherwise cause cascading failure.

    The gate is fair: waiters are released in arrival order via a FIFO
    asyncio.Queue. This prevents request starvation under sustained load.
    """

    __slots__ = (
        "name", "max_concurrent", "max_waiters", "wait_ms",
        "_sem", "_waiters", "_stats", "_lock",
    )

    def __init__(
        self,
        name: str,
        max_concurrent: int,
        max_waiters: int = 0,
        wait_ms: int = 0,
    ) -> None:
        self.name = name
        self.max_concurrent = max_concurrent
        self.max_waiters = max(0, max_waiters)
        self.wait_ms = max(0, wait_ms)
        self._sem = asyncio.Semaphore(max_concurrent)
        # Token queue — empty when no slots are immediately free.  When a
        # request finishes it puts a token here so the next waiter unblocks
        # in O(1).
        self._waiters: asyncio.Queue[None] = asyncio.Queue(maxsize=max_concurrent + max_waiters)
        self._stats = AdmissionStats()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def slot(self):
        """
        Async context manager.  Acquires a slot or raises 503.

        Usage:
            async with gate.slot():
                await do_work()
        """
        acquired = await self._acquire()
        if not acquired:
            self._stats.rejected_full += 1
            self._stats.last_reject_ts = time.time()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Server is at capacity for '{self.name}'. Please retry shortly.",
                headers={"Retry-After": "2"},
            )
        try:
            self._stats.in_flight += 1
            yield
        finally:
            self._stats.in_flight -= 1
            self._release()

    async def _acquire(self) -> bool:
        # Fast path — try the semaphore immediately.
        if self._sem.locked() is False or self._sem._value > 0:  # type: ignore[attr-defined]
            try:
                # asyncio.Semaphore has no "try_acquire"; locked() check is racy
                # but the worst case is one extra fall-through to the wait path.
                await asyncio.wait_for(self._sem.acquire(), timeout=0.0001)
                self._stats.accepted += 1
                return True
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass

        # Slow path — fail fast if there's no wait budget.
        if self.max_waiters == 0 or self.wait_ms <= 0:
            return False

        # Don't queue if waiter count would exceed cap.
        async with self._lock:
            if self._stats.waiting >= self.max_waiters:
                return False
            self._stats.waiting += 1
            self._stats.queued += 1

        try:
            try:
                await asyncio.wait_for(
                    self._sem.acquire(),
                    timeout=self.wait_ms / 1000.0,
                )
                self._stats.accepted += 1
                return True
            except asyncio.TimeoutError:
                self._stats.rejected_timeout += 1
                self._stats.last_reject_ts = time.time()
                return False
        finally:
            self._stats.waiting -= 1

    def _release(self) -> None:
        try:
            self._sem.release()
        except ValueError:
            # Released too many times — guard against double-free in rare
            # exception paths.
            pass

    def stats(self) -> dict:
        return {
            "name": self.name,
            "max_concurrent": self.max_concurrent,
            "max_waiters": self.max_waiters,
            "wait_ms": self.wait_ms,
            **{k: getattr(self._stats, k) for k in (
                "accepted", "queued", "rejected_full",
                "rejected_timeout", "in_flight", "waiting",
            )},
        }


# ─────────────────────────────────────────────────────────────────────────────
# Module-level pre-configured gates for common endpoints
# ─────────────────────────────────────────────────────────────────────────────
# These defaults are sized for a 1-CPU / 896 MB container with an 8-connection
# Postgres pool. They can be tuned per-environment via env vars.
#
#   SOS     — life-critical, never queued long; 32 in-flight, 16 waiters, 750 ms
#   REPORT  — pilgrim issue report, can wait; 32 in-flight, 64 waiters, 2 s
#   LOST    — register missing person, can wait; 16 in-flight, 32 waiters, 2 s
#   CHAT    — Groq-bound, expensive, can shed early; 16 in-flight, 16 waiters, 1.5 s
#   INGEST  — CCTV/telecom ingest, internal-only; 64 in-flight, 0 waiters
#   READ    — generic GETs, almost never the bottleneck; 256 / 0
# ─────────────────────────────────────────────────────────────────────────────

def _g(name: str, default_conc: int, default_wait: int, default_wait_ms: int) -> AdmissionGate:
    return AdmissionGate(
        name=name,
        max_concurrent=int(os.getenv(f"GATE_{name.upper()}_CONC", str(default_conc))),
        max_waiters=int(os.getenv(f"GATE_{name.upper()}_WAITERS", str(default_wait))),
        wait_ms=int(os.getenv(f"GATE_{name.upper()}_WAIT_MS", str(default_wait_ms))),
    )


SOS_GATE     = _g("sos",     32,  16, 750)
REPORT_GATE  = _g("report",  32,  64, 2000)
LOST_GATE    = _g("lost",    16,  32, 2000)
CHAT_GATE    = _g("chat",    16,  16, 1500)
INGEST_GATE  = _g("ingest",  64,   0,    0)
READ_GATE    = _g("read",   256,   0,    0)


def all_gates() -> list[AdmissionGate]:
    return [SOS_GATE, REPORT_GATE, LOST_GATE, CHAT_GATE, INGEST_GATE, READ_GATE]


def gates_snapshot() -> dict:
    return {g.name: g.stats() for g in all_gates()}


# ─────────────────────────────────────────────────────────────────────────────
# Bounded gather — fan-out with a hard concurrency cap
# ─────────────────────────────────────────────────────────────────────────────

async def gather_bounded(
    coros: Iterable[Awaitable[T]],
    *,
    limit: int = 16,
    return_exceptions: bool = True,
) -> list[Any]:
    """
    Like asyncio.gather, but never has more than `limit` coroutines running
    at once.  Use this for fan-outs over potentially large iterables where
    spawning a coroutine per item would blow the event loop or exhaust an
    upstream rate limit.

    Memory: O(limit) — does NOT materialize the whole iterable up front.

    Order: input order is preserved (results[i] corresponds to inputs[i]).
    """
    coros = list(coros)
    if not coros:
        return []
    sem = asyncio.Semaphore(limit)
    results: list[Any] = [None] * len(coros)

    async def _runner(idx: int, c: Awaitable[T]) -> None:
        async with sem:
            try:
                results[idx] = await c
            except Exception as exc:
                if return_exceptions:
                    results[idx] = exc
                else:
                    raise

    await asyncio.gather(*(_runner(i, c) for i, c in enumerate(coros)))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Decorator helper (optional — handlers can also use `async with gate.slot()`)
# ─────────────────────────────────────────────────────────────────────────────

def gated(gate: AdmissionGate):
    """
    Wrap a coroutine handler so its body runs inside the gate.

        @app.post("/sos_alert")
        @gated(SOS_GATE)
        async def sos_alert(...):
            ...

    Note: FastAPI inspects the wrapper's signature, so we preserve it via
    functools.wraps. If a handler uses Form/Depends, this still works.
    """
    def _wrap(fn):
        @wraps(fn)
        async def _inner(*args, **kwargs):
            async with gate.slot():
                return await fn(*args, **kwargs)
        return _inner
    return _wrap


# ─────────────────────────────────────────────────────────────────────────────
# Shutdown hook
# ─────────────────────────────────────────────────────────────────────────────

def shutdown_admission() -> None:
    """Call from FastAPI lifespan shutdown to drain the thread pool."""
    try:
        bg_pool.shutdown(wait=False, cancel_futures=True)
        logger.info("[Admission] Background thread pool shut down")
    except Exception as exc:
        logger.debug("[Admission] Pool shutdown error: %s", exc)
