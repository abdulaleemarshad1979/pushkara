# ═══════════════════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — WebSocket Manager  (v7 — Optimized)
#
# Optimizations vs v6:
#   1. Buckets are sets, not lists       → O(1) add / remove / membership.
#   2. Reverse `ws → ghat_id` index      → disconnect(ws) needs no extra arg
#                                          and never leaks bucket entries on
#                                          mismatched callers.
#   3. Pre-serialize payload ONCE        → one json.dumps per broadcast,
#                                          bytes shared across all recipients
#                                          via send_text() instead of N×
#                                          send_json() re-encoding.
#   4. Single publish + single local fan-out per broadcast. Subscriber drops
#      self-originating echoes via an `_origin` envelope key, eliminating the
#      old 3–4× duplicate-delivery on the publishing instance.
#   5. Pattern-subscribe `pushkaralu:ghat:*` instead of hard-coding g01..g19,
#      so any ghat id is routed correctly.
#   6. Live circuit-breaker read via redis_manager.is_circuit_open() — the
#      previous `from redis_manager import _circuit_open` captured the bool
#      at import time and never updated, silently disabling degraded mode.
#   7. Centralised heartbeat: WS handlers no longer send their own PING on
#      receive_text timeout — manager.heartbeat_loop is the single source of
#      keep-alives.
#   8. Removed the dead local-event-bus drain task (was unreachable due to
#      bug #6 above).
# ═══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any, Callable, Dict, Optional, Set

from fastapi import WebSocket

from app.core import redis_manager
from app.core.redis_manager import (
    INSTANCE_ID,
    Keys,
    get_pubsub_redis,
    publish,
    redis_available,
)

logger = logging.getLogger("pushkaralu.ws")

HEARTBEAT_INTERVAL       = 25
MAX_CONNECTIONS_PER_GHAT = 5000
SEND_TIMEOUT_SECONDS     = 3.0
SUBSCRIBER_BACKOFF_MAX   = 60

# Envelope key that tags every broadcast with the originating instance id so
# the local subscriber can drop the message it just published itself.
ORIGIN_KEY = "_origin"


class GhatConnectionManager:
    """
    Per-ghat WebSocket connection manager with a Redis pub/sub fan-out for
    cross-instance delivery.

    Connections are bucketed by `ghat_id` (use "all" for unscoped clients).
    A single broadcast performs ONE Redis publish (cross-instance) plus ONE
    local fan-out (this instance) — the subscriber on this same instance
    drops its own echo via the `_origin` envelope tag.
    """

    def __init__(self) -> None:
        self._connections: Dict[str, Set[WebSocket]] = defaultdict(set)
        self._ws_to_ghat:  Dict[WebSocket, str]      = {}
        self._active:      Set[WebSocket]            = set()
        self._lock = asyncio.Lock()

        self._subscriber_task: Optional[asyncio.Task] = None

        self._total_connected      = 0
        self._total_messages_sent  = 0
        self._total_dead_pruned    = 0

        # Optional callback invoked for every Redis-received payload (used by
        # main.sync_state to mirror DB across instances).
        self.on_event: Optional[Callable[[dict], Any]] = None

    # ── Connection lifecycle ────────────────────────────────────────────────

    async def connect(self, websocket: WebSocket, ghat_id: str = "all") -> bool:
        await websocket.accept()
        async with self._lock:
            bucket = self._connections[ghat_id]
            if len(bucket) >= MAX_CONNECTIONS_PER_GHAT:
                logger.warning("[WS] Cap reached ghat=%s — rejecting", ghat_id)
                # close happens outside the lock (next line) so we don't hold
                # the manager-wide lock through a network round-trip.
                full = True
            else:
                bucket.add(websocket)
                self._active.add(websocket)
                self._ws_to_ghat[websocket] = ghat_id
                self._total_connected += 1
                full = False
        if full:
            try:
                await websocket.close(code=1008, reason="Server capacity reached")
            except Exception:
                pass
            return False
        logger.debug("[WS] Connected ghat=%s total=%d", ghat_id, self._total_connected)
        return True

    async def disconnect(self, websocket: WebSocket, ghat_id: Optional[str] = None) -> None:
        """
        Remove a websocket from all manager indexes.

        `ghat_id` is accepted for backwards compatibility but ignored — the
        reverse `_ws_to_ghat` map is the single source of truth.
        """
        async with self._lock:
            self._active.discard(websocket)
            gid = self._ws_to_ghat.pop(websocket, None)
            if gid is not None:
                bucket = self._connections.get(gid)
                if bucket is not None:
                    bucket.discard(websocket)
                    if not bucket:
                        # Drop empty bucket so heartbeat / fan-out skips it.
                        self._connections.pop(gid, None)

    def get_count(self, ghat_id: Optional[str] = None) -> int:
        if ghat_id:
            return len(self._connections.get(ghat_id, ()))
        return len(self._active)

    # ── Local fan-out (single instance) ─────────────────────────────────────

    async def _send_text(self, websocket: WebSocket, text: str) -> bool:
        try:
            await asyncio.wait_for(websocket.send_text(text), timeout=SEND_TIMEOUT_SECONDS)
            return True
        except Exception:
            return False

    async def _broadcast_to_bucket_text(self, ghat_id: str, text: str) -> int:
        """
        Send a pre-serialised text payload to every websocket in `ghat_id`.

        Returns the number of successful deliveries. Dead sockets are pruned
        in a single locked pass.
        """
        bucket = self._connections.get(ghat_id)
        if not bucket:
            return 0
        # Snapshot to avoid mutation-during-iteration; the gather may take a
        # while if a socket is slow.
        snapshot = tuple(bucket)
        results = await asyncio.gather(
            *(self._send_text(ws, text) for ws in snapshot),
            return_exceptions=True,
        )
        dead = [ws for ws, ok in zip(snapshot, results) if ok is not True]
        if dead:
            await self._prune(dead)
        sent = len(snapshot) - len(dead)
        self._total_messages_sent += sent
        return sent

    async def _prune(self, dead) -> None:
        async with self._lock:
            for ws in dead:
                self._active.discard(ws)
                gid = self._ws_to_ghat.pop(ws, None)
                if gid is not None:
                    bucket = self._connections.get(gid)
                    if bucket is not None:
                        bucket.discard(ws)
                        if not bucket:
                            self._connections.pop(gid, None)
            self._total_dead_pruned += len(dead)
        logger.debug("[WS] Pruned %d dead connections", len(dead))

    async def _local_broadcast(self, message: dict, ghat_id: Optional[str] = None) -> None:
        """
        Pre-serialize once, then fan out to every interested bucket.

        - ghat_id None → every bucket.
        - ghat_id "all" → only the "all" bucket.
        - any other ghat_id → that ghat's bucket plus the "all" bucket
          (so dashboards subscribed to "all" still receive per-ghat events).
        """
        # Strip the cross-instance origin tag before pushing to clients —
        # it's an internal protocol detail.
        if ORIGIN_KEY in message:
            message = {k: v for k, v in message.items() if k != ORIGIN_KEY}
        try:
            text = json.dumps(message, default=str)
        except Exception as exc:
            logger.warning("[WS] Failed to serialize broadcast: %s", exc)
            return

        if ghat_id and ghat_id != "all":
            await asyncio.gather(
                self._broadcast_to_bucket_text(ghat_id, text),
                self._broadcast_to_bucket_text("all", text),
                return_exceptions=True,
            )
        elif ghat_id == "all":
            await self._broadcast_to_bucket_text("all", text)
        else:
            keys = list(self._connections.keys())
            if not keys:
                return
            await asyncio.gather(
                *(self._broadcast_to_bucket_text(k, text) for k in keys),
                return_exceptions=True,
            )

    # ── Public broadcast entry point ────────────────────────────────────────

    async def broadcast(self, message: dict, ghat_id: Optional[str] = None) -> None:
        """
        Canonical broadcast: ONE Redis publish + ONE local fan-out.

        - When Redis is up: cross-instance recipients see the payload via
          the subscriber loop on each instance; the originating instance's
          subscriber drops its own echo via the `_origin` tag.
        - When the circuit breaker is open: the publish becomes a no-op via
          the safe wrapper in redis_manager — local clients still receive
          the payload through the local fan-out.
        """
        # Tag a copy so mutation never leaks into caller-owned dicts.
        envelope = dict(message)
        envelope[ORIGIN_KEY] = INSTANCE_ID

        channel = Keys.channel_ghat(ghat_id) if ghat_id else Keys.CHANNEL_ALL
        # publish() is wrapped in the redis _safe() helper — already swallows
        # transient errors and returns 0 when the circuit is open.
        try:
            await publish(channel, envelope)
        except Exception as exc:  # belt-and-braces
            logger.debug("[WS] publish() failed (degraded): %s", exc)

        # Local fan-out is independent of Redis state; clients on this
        # instance always see the message.
        await self._local_broadcast(message, ghat_id)

    # ── Redis pub/sub subscriber ────────────────────────────────────────────

    async def start_subscriber(self) -> None:
        if self._subscriber_task and not self._subscriber_task.done():
            return
        self._subscriber_task = asyncio.create_task(
            self._subscriber_loop(), name="ws-redis-subscriber",
        )
        logger.info("[WS] Redis subscriber started")

    async def stop_subscriber(self) -> None:
        task = self._subscriber_task
        if not task:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._subscriber_task = None

    async def _subscriber_loop(self) -> None:
        backoff = 1
        while True:
            try:
                if not await redis_available():
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, SUBSCRIBER_BACKOFF_MAX)
                    continue

                r = await get_pubsub_redis()
                pubsub = r.pubsub()
                # Pattern-subscribe so ANY ghat id (g01, g42, alphanumeric, …)
                # is routed correctly without the hard-coded g01..g19 range.
                await pubsub.subscribe(
                    Keys.CHANNEL_ALL, Keys.CHANNEL_ADMIN, Keys.CHANNEL_ALERTS,
                )
                await pubsub.psubscribe("pushkaralu:ghat:*")
                logger.info("[WS-Sub] Subscribed: 3 channels + 1 pattern")
                backoff = 1

                async for raw in pubsub.listen():
                    msg_type = raw.get("type")
                    if msg_type not in ("message", "pmessage"):
                        continue
                    try:
                        await self._handle_pubsub_message(raw)
                    except Exception as exc:
                        logger.debug("[WS-Sub] dispatch error: %s", exc)

            except asyncio.CancelledError:
                logger.info("[WS-Sub] Subscriber loop cancelled")
                return
            except Exception as exc:
                logger.warning("[WS-Sub] Connection lost: %s — retry in %ds",
                               exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, SUBSCRIBER_BACKOFF_MAX)

    async def _handle_pubsub_message(self, raw: dict) -> None:
        data = raw.get("data")
        try:
            payload = json.loads(data) if isinstance(data, (str, bytes)) else data
        except Exception:
            logger.debug("[WS-Sub] Bad payload (not JSON), dropped")
            return
        if not isinstance(payload, dict):
            return

        # Drop self-originating echoes — we already fanned out locally.
        if payload.get(ORIGIN_KEY) == INSTANCE_ID:
            return

        # Multi-instance state-sync hook.
        cb = self.on_event
        if cb is not None:
            try:
                res = cb(payload)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.error("[WS-Sub] state sync callback failed: %s", exc)

        channel = raw.get("channel") or ""
        if channel == Keys.CHANNEL_ALL:
            await self._local_broadcast(payload, None)
        elif channel.startswith("pushkaralu:ghat:"):
            gid = channel.rsplit(":", 1)[-1]
            await self._local_broadcast(payload, gid)
        else:
            # Admin / alerts channels also fan to everyone.
            await self._local_broadcast(payload, None)

    # ── Heartbeat ───────────────────────────────────────────────────────────

    async def heartbeat_loop(self) -> None:
        """
        Single source of WebSocket keep-alives. Pre-serialises the PING once
        per cycle and reuses the bytes across every connected client.
        """
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if not self._active:
                    continue
                ping_text = json.dumps({"type": "PING", "ts": int(time.time())})
                keys = list(self._connections.keys())
                if not keys:
                    continue
                await asyncio.gather(
                    *(self._broadcast_to_bucket_text(k, ping_text) for k in keys),
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug("[WS] heartbeat error: %s", exc)

    # ── Stats / introspection ───────────────────────────────────────────────

    def stats(self) -> dict:
        per_ghat = {gid: len(conns) for gid, conns in self._connections.items() if conns}
        return {
            "total_connections":   len(self._active),
            "per_ghat":            per_ghat,
            "total_messages_sent": self._total_messages_sent,
            "total_dead_pruned":   self._total_dead_pruned,
            # Live read — not the stale import-time copy.
            "redis_circuit_open":  redis_manager.is_circuit_open(),
            "instance":            INSTANCE_ID,
        }

    # ── Backwards-compat shims used by the orchestrator orphan pruner ───────

    async def prune_dead(self, dead) -> int:
        """Public wrapper around _prune for external callers (orchestrator)."""
        if not dead:
            return 0
        await self._prune(dead)
        return len(dead)


# Module-level singleton consumed by main.py and the orchestrator.
manager = GhatConnectionManager()
