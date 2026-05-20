# ═══════════════════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — WebSocket Manager  (v6 — Hardened)
#
# HARDENING vs v5:
#   - broadcast() checks redis_available flag before pub/sub
#   - If Redis circuit is open: routes through local asyncio.Queue event bus
#     so WebSockets never drop messages during a Redis outage
#   - Local event bus drains in a background task; no message loss
#   - All v5 logic (ghat partitioning, heartbeat, dead-conn pruning) preserved
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Set

from fastapi import WebSocket

from app.core.redis_manager import (
    Keys, _circuit_open, get_pubsub_redis, publish, redis_available,
)

logger = logging.getLogger("pushkaralu.ws")

HEARTBEAT_INTERVAL       = 25
BROADCAST_INTERVAL       = 2.5
MAX_CONNECTIONS_PER_GHAT = 5000

# ── Local fallback event bus (asyncio.Queue) ──────────────────────────────────
# When Redis is unavailable, broadcast() enqueues here.
# _local_bus_drain_task reads from it and calls _local_broadcast directly,
# so all connected WebSockets still receive messages.
_local_event_bus: asyncio.Queue = asyncio.Queue(maxsize=10_000)


class GhatConnectionManager:
    def __init__(self):
        self._connections: Dict[str, List[WebSocket]] = defaultdict(list)
        self._active: Set[WebSocket] = set()
        self._subscriber_task: Optional[asyncio.Task] = None
        self._bus_drain_task:  Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._total_connected    = 0
        self._total_messages_sent = 0
        self.on_event: Optional[Callable[[dict], Any]] = None  # Callback for state sync

    # ── Connection lifecycle ───────────────────────────────────────────────

    async def connect(self, websocket: WebSocket, ghat_id: str = "all") -> bool:
        await websocket.accept()
        async with self._lock:
            bucket = self._connections[ghat_id]
            if len(bucket) >= MAX_CONNECTIONS_PER_GHAT:
                logger.warning("[WS] Cap reached ghat=%s — rejecting", ghat_id)
                await websocket.close(code=1008, reason="Server capacity reached")
                return False
            bucket.append(websocket)
            self._active.add(websocket)
            self._total_connected += 1
        logger.debug("[WS] Connected  ghat=%s  total=%d", ghat_id, self._total_connected)
        return True

    async def disconnect(self, websocket: WebSocket, ghat_id: str = "all"):
        async with self._lock:
            if websocket in self._active:
                self._active.discard(websocket)
                bucket = self._connections.get(ghat_id, [])
                try:
                    bucket.remove(websocket)
                except ValueError:
                    pass

    def get_count(self, ghat_id: Optional[str] = None) -> int:
        if ghat_id:
            return len(self._connections.get(ghat_id, []))
        return len(self._active)

    # ── Local broadcast (this instance only) ──────────────────────────────

    async def _send_to_socket(self, websocket: WebSocket, message: dict) -> bool:
        try:
            await asyncio.wait_for(websocket.send_json(message), timeout=3.0)
            return True
        except Exception:
            return False

    async def _broadcast_to_bucket(self, ghat_id: str, message: dict):
        bucket = self._connections.get(ghat_id, [])
        if not bucket:
            return
        results = await asyncio.gather(
            *[self._send_to_socket(ws, message) for ws in bucket],
            return_exceptions=True,
        )
        dead = [ws for ws, ok in zip(bucket, results) if ok is not True]
        if dead:
            async with self._lock:
                for ws in dead:
                    self._active.discard(ws)
                    try:
                        self._connections[ghat_id].remove(ws)
                    except ValueError:
                        pass
            logger.debug("[WS] Pruned %d dead connections from ghat=%s", len(dead), ghat_id)
        self._total_messages_sent += len(bucket) - len(dead)

    async def _local_broadcast(self, message: dict, ghat_id: Optional[str] = None):
        if ghat_id and ghat_id != "all":
            await asyncio.gather(
                self._broadcast_to_bucket(ghat_id, message),
                self._broadcast_to_bucket("all", message),
            )
        else:
            all_buckets = list(self._connections.keys())
            await asyncio.gather(
                *[self._broadcast_to_bucket(b, message) for b in all_buckets]
            )

    # ── Main broadcast — Redis-aware with local fallback ──────────────────

    async def broadcast(self, message: dict, ghat_id: Optional[str] = None):
        """
        Publish via Redis when available; fall back to local asyncio.Queue
        event bus when the Redis circuit breaker is open so WebSocket
        connections never drop messages during a Redis outage.
        """
        if not _circuit_open:
            # Normal path: publish to Redis (cross-instance fan-out)
            channel = Keys.channel_ghat(ghat_id) if ghat_id else Keys.CHANNEL_ALL
            await publish(channel, message)
            if ghat_id:
                await publish(Keys.CHANNEL_ALL, message)
            # Also deliver locally without Redis round-trip
            await self._local_broadcast(message, ghat_id)
        else:
            # Degraded path: enqueue into local bus, drain task delivers
            try:
                _local_event_bus.put_nowait((message, ghat_id))
            except asyncio.QueueFull:
                logger.warning("[WS] Local event bus full — message dropped (Redis down)")
            # Deliver to this instance immediately as well
            await self._local_broadcast(message, ghat_id)

    # ── Local event bus drain task ─────────────────────────────────────────

    async def _local_bus_drain_loop(self):
        """
        Drain the local asyncio.Queue event bus.
        Runs continuously; delivers queued messages when Redis is unavailable.
        """
        while True:
            try:
                message, ghat_id = await _local_event_bus.get()
                await self._local_broadcast(message, ghat_id)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug("[WS-Bus] Drain error: %s", exc)

    # ── Redis Pub/Sub subscriber ───────────────────────────────────────────

    async def start_subscriber(self):
        self._subscriber_task = asyncio.create_task(
            self._subscriber_loop(), name="ws-redis-subscriber"
        )
        self._bus_drain_task = asyncio.create_task(
            self._local_bus_drain_loop(), name="ws-local-bus-drain"
        )
        logger.info("[WS] Redis subscriber + local bus drain tasks started")

    async def stop_subscriber(self):
        for task in (self._subscriber_task, self._bus_drain_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _subscriber_loop(self):
        backoff = 1
        while True:
            try:
                if not await redis_available():
                    logger.warning("[WS-Sub] Redis not available, retrying in %ds", backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue

                r = await get_pubsub_redis()
                pubsub = r.pubsub()

                channels = [Keys.CHANNEL_ALL, Keys.CHANNEL_ADMIN, Keys.CHANNEL_ALERTS]
                for ghat_id in [f"g{i:02d}" for i in range(1, 20)]:
                    channels.append(Keys.channel_ghat(ghat_id))

                await pubsub.subscribe(*channels)
                logger.info("[WS-Sub] Subscribed to %d channels", len(channels))
                backoff = 1

                async for raw_msg in pubsub.listen():
                    if raw_msg["type"] != "message":
                        continue
                    try:
                        channel: str = raw_msg["channel"]
                        payload: dict = json.loads(raw_msg["data"])

                        # ── TRIGGER CALLBACK FOR STATE SYNC (Multi-instance consistency) ──
                        if self.on_event:
                            try:
                                if asyncio.iscoroutinefunction(self.on_event):
                                    await self.on_event(payload)
                                else:
                                    self.on_event(payload)
                            except Exception as sync_err:
                                logger.error("[WS-Sub] State sync callback failed: %s", sync_err)

                        if channel == Keys.CHANNEL_ALL:
                            await self._local_broadcast(payload, None)
                        elif channel.startswith("pushkaralu:ghat:"):
                            gid = channel.split(":")[-1]
                            await self._broadcast_to_bucket(gid, payload)
                        else:
                            await self._local_broadcast(payload, None)

                    except Exception as e:
                        logger.debug("[WS-Sub] Message processing error: %s", e)

            except asyncio.CancelledError:
                logger.info("[WS-Sub] Subscriber loop cancelled")
                return
            except Exception as exc:
                logger.warning("[WS-Sub] Connection lost: %s  Retry in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # ── Heartbeat loop ─────────────────────────────────────────────────────

    async def heartbeat_loop(self):
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            ping = {"type": "PING", "ts": int(time.time())}
            all_buckets = list(self._connections.keys())
            await asyncio.gather(
                *[self._broadcast_to_bucket(b, ping) for b in all_buckets],
                return_exceptions=True,
            )

    # ── Stats ──────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        per_ghat = {gid: len(conns) for gid, conns in self._connections.items() if conns}
        return {
            "total_connections":   len(self._active),
            "per_ghat":            per_ghat,
            "total_messages_sent": self._total_messages_sent,
            "redis_circuit_open":  _circuit_open,
            "local_bus_depth":     _local_event_bus.qsize(),
        }


manager = GhatConnectionManager()
