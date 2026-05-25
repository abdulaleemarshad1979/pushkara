# ═══════════════════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — Redis Manager  (v6 — Hardened)
#
# HARDENING vs v5:
#   - Circuit breaker: catches TimeoutError + ResponseError (MISCONF)
#     Sets redis_available=False; silent background reconnect loop
#   - _circuit_open flag gates all ops without touching the pool
#   - All v5 helpers (cache, pub/sub, streams, rate-limit, crowd) preserved
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ResponseError, TimeoutError as RedisTimeoutError

logger = logging.getLogger("pushkaralu.redis")

# ── Configuration ────────────────────────────────────────────────────────────
REDIS_URL         = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_PASSWORD    = os.getenv("REDIS_PASSWORD", None)
REDIS_MAX_CONN    = int(os.getenv("REDIS_MAX_CONNECTIONS", "200"))
CACHE_DEFAULT_TTL = int(os.getenv("CACHE_TTL_SECONDS", "3"))
CACHE_STATIC_TTL  = int(os.getenv("CACHE_STATIC_TTL", "30"))
# Crowd history is only ever read with n<=12 (forecast model + ad-hoc queries).
# The previous fixed cap of 60 entries per ghat wasted ~75% of Redis memory
# on data nothing reads. Make it configurable so ops can tune freely.
CROWD_HIST_MAXLEN = int(os.getenv("CROWD_HIST_MAXLEN", "24"))

# Process-local instance identifier — exposed so that ws_manager / main can
# tag pub/sub envelopes with the publisher origin and skip self-echoes.
# Falls back to a stable per-PID value so all importers in the same process
# observe the SAME id (avoids the previous bug where each importer generated
# an independent uuid via os.getenv default).
INSTANCE_ID: str = os.getenv("INSTANCE_ID", f"api-{os.getpid()}")

# ── Circuit breaker state ────────────────────────────────────────────────────
_circuit_open: bool = False          # True → Redis degraded, skip ops
_circuit_tripped_at: float = 0.0
_CIRCUIT_RESET_INTERVAL: float = 10.0  # seconds between reconnect attempts


def is_circuit_open() -> bool:
    """
    Live read of the circuit-breaker state.

    Other modules MUST use this getter (or import the redis_manager module and
    read the attribute via dotted access) instead of `from redis_manager import
    _circuit_open`. The bare-name import binds the value at import time and
    never updates, which silently disables degraded-mode handling everywhere.
    """
    return _circuit_open

# ── Key namespace ─────────────────────────────────────────────────────────────
class Keys:
    CHANNEL_ALL    = "pushkaralu:broadcast"
    CHANNEL_GHAT   = "pushkaralu:ghat:{ghat_id}"
    CHANNEL_ADMIN  = "pushkaralu:admin"
    CHANNEL_ALERTS = "pushkaralu:alerts"

    GHATS_ALL       = "cache:ghats:all"
    GHAT_ONE        = "cache:ghats:{ghat_id}"
    ISSUES_ALL      = "cache:issues:all"
    ISSUES_STATUS   = "cache:issues:status:{status}"
    SOS_ACTIVE      = "cache:sos:active"
    SOS_ALL         = "cache:sos:all"
    STATS           = "cache:stats:main"
    ADMIN_STATS     = "cache:stats:admin"
    FACILITIES      = "cache:facilities:all"
    FACILITIES_TYPE = "cache:facilities:type:{type}"
    TRANSPORT       = "cache:transport:all"
    VOLUNTEERS      = "cache:volunteers:all"
    LOST_ALL        = "cache:lost:all"
    LOST_STATUS     = "cache:lost:status:{status}"
    CONTACTS        = "cache:contacts:all"
    CONTACTS_CAT    = "cache:contacts:cat:{category}"
    MEDICAL         = "cache:medical:all"
    MEDICAL_TYPE    = "cache:medical:type:{type}"
    RISK_ALL        = "risk:all"

    STREAM_CCTV    = "stream:cctv"
    STREAM_TELECOM = "stream:telecom"
    STREAM_EVENTS  = "stream:events"

    RATE_IP  = "rate:ip:{ip}"
    RATE_SOS = "rate:sos:{phone}"

    CROWD_HIST = "crowd:history:{ghat_id}"

    @staticmethod
    def channel_ghat(ghat_id: str) -> str:
        return f"pushkaralu:ghat:{ghat_id}"

    @staticmethod
    def crowd_key(ghat_id: str) -> str:
        return f"crowd:ghat:{ghat_id}"

    @staticmethod
    def risk_key(ghat_id: str) -> str:
        return f"risk:ghat:{ghat_id}"

    @staticmethod
    def crowd_hist(ghat_id: str) -> str:
        return f"crowd:history:{ghat_id}"


# ── Connection pool (singleton, lazy) ────────────────────────────────────────
_pool: Optional[aioredis.Redis] = None
_pubsub_pool: Optional[aioredis.Redis] = None
_pool_lock = asyncio.Lock()
_last_available_check: float = 0.0
_last_available_result: bool = False


def _trip_circuit(exc: Exception):
    """Open the circuit breaker and log once."""
    global _circuit_open, _circuit_tripped_at
    if not _circuit_open:
        logger.warning(
            "[Redis] Circuit breaker OPEN — degraded mode: %s (%s)",
            type(exc).__name__, exc,
        )
    _circuit_open = True
    _circuit_tripped_at = time.monotonic()


def _reset_circuit():
    global _circuit_open
    if _circuit_open:
        logger.info("[Redis] Circuit breaker CLOSED — Redis reconnected")
    _circuit_open = False


async def _reconnect_loop():
    """
    Background task: silently attempt reconnection every _CIRCUIT_RESET_INTERVAL
    seconds while the circuit is open.
    """
    global _pool
    while True:
        await asyncio.sleep(_CIRCUIT_RESET_INTERVAL)
        if not _circuit_open:
            continue
        try:
            client = await _build_pool()
            await client.ping()
            async with _pool_lock:
                if _pool is not None:
                    try:
                        await _pool.aclose()
                    except Exception:
                        pass
                _pool = client
            _reset_circuit()
        except Exception as exc:
            logger.debug("[Redis] Reconnect attempt failed: %s", exc)


async def start_reconnect_loop():
    """Call once at startup to enable automatic circuit-breaker recovery."""
    asyncio.create_task(_reconnect_loop(), name="redis-reconnect-loop")


async def _build_pool(max_connections: int = REDIS_MAX_CONN) -> aioredis.Redis:
    retry = Retry(ExponentialBackoff(cap=10, base=0.5), retries=5)
    client = aioredis.from_url(
        REDIS_URL,
        password=REDIS_PASSWORD,
        encoding="utf-8",
        decode_responses=True,
        max_connections=max_connections,
        retry=retry,
        retry_on_error=[ConnectionError, TimeoutError],
        socket_timeout=2.0,
        socket_connect_timeout=2.0,
        health_check_interval=30,
    )
    try:
        await client.ping()
        logger.info("[Redis] Pool ready  url=%s  max_conn=%d", REDIS_URL, max_connections)
    except Exception as exc:
        logger.warning("[Redis] Could not connect: %s — degraded mode", exc)
    return client


async def get_redis() -> aioredis.Redis:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = await _build_pool()
    return _pool


async def get_pubsub_redis() -> aioredis.Redis:
    global _pubsub_pool
    if _pubsub_pool is None:
        async with _pool_lock:
            if _pubsub_pool is None:
                _pubsub_pool = await _build_pool(max_connections=50)
    return _pubsub_pool


async def close_redis():
    for attr in ("_pool", "_pubsub_pool"):
        client = globals()[attr]
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
            globals()[attr] = None


async def redis_available() -> bool:
    """
    Cached ping — max 1 check per second.
    Also manages circuit breaker state: if ping fails, trip the circuit.
    """
    global _last_available_check, _last_available_result
    if _circuit_open:
        return False
    now = time.monotonic()
    if now - _last_available_check < 1.0:
        return _last_available_result
    try:
        r = await get_redis()
        result = bool(await r.ping())
        if result:
            _reset_circuit()
    except (RedisTimeoutError, ResponseError, ConnectionError) as exc:
        _trip_circuit(exc)
        result = False
    except Exception:
        result = False
    _last_available_check = now
    _last_available_result = result
    return result


# ── Safe executor — wraps every Redis call with circuit-breaker check ─────────

async def _safe(coro, *, default=None):
    """
    Execute a Redis coroutine.
    If the circuit is open → log at debug level and return default immediately.
    If a TimeoutError or ResponseError (MISCONF) is raised → trip circuit, return default.

    FIX (Issue 4): Silent drops made debugging Redis degradation extremely
    difficult. Now logs at debug level when circuit is open so operators can
    observe the degradation pattern in logs. Critical paths (rate limiting)
    should NOT use _safe — they handle circuit state explicitly and fail closed.
    """
    if _circuit_open:
        logger.debug("[Redis._safe] Circuit open — skipping op, returning default")
        return default
    try:
        return await coro
    except (RedisTimeoutError, ResponseError, ConnectionError) as exc:
        _trip_circuit(exc)
        return default
    except Exception as exc:
        logger.debug("[Redis._safe] Unexpected error (non-fatal): %s", exc)
        return default


# ── Cache helpers ──────────────────────────────────────────────────────────────

async def cache_set(key: str, value: Any, ttl: int = CACHE_DEFAULT_TTL) -> bool:
    try:
        r = await get_redis()
        result = await _safe(r.setex(key, ttl, json.dumps(value, default=str)), default=None)
        return result is not None
    except Exception as exc:
        logger.debug("[Cache] SET failed key=%s err=%s", key, exc)
        return False


async def cache_get(key: str) -> Optional[Any]:
    try:
        r = await get_redis()
        raw = await _safe(r.get(key))
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.debug("[Cache] GET failed key=%s err=%s", key, exc)
        return None


async def cache_delete(*keys: str):
    try:
        r = await get_redis()
        if keys:
            await _safe(r.delete(*keys))
    except Exception as exc:
        logger.debug("[Cache] DEL failed err=%s", exc)


async def cache_invalidate_pattern(pattern: str):
    try:
        r = await get_redis()
        cursor = 0
        while True:
            result = await _safe(r.scan(cursor, match=pattern, count=100), default=(0, []))
            cursor, keys = result
            if keys:
                await _safe(r.delete(*keys))
            if cursor == 0:
                break
    except Exception as exc:
        logger.debug("[Cache] SCAN-DEL failed pattern=%s err=%s", pattern, exc)


# ── Pub/Sub publish ────────────────────────────────────────────────────────────

async def publish(channel: str, message: dict) -> int:
    try:
        r = await get_redis()
        result = await _safe(r.publish(channel, json.dumps(message, default=str)), default=0)
        return result or 0
    except Exception as exc:
        logger.debug("[PubSub] PUBLISH failed channel=%s err=%s", channel, exc)
        return 0


async def publish_to_ghat(ghat_id: str, message: dict) -> int:
    """
    Publish to a single per-ghat channel.

    NOTE (perf fix): the previous implementation also re-published the same
    payload to CHANNEL_ALL, which caused every receiver to fan-out the message
    twice — once via the ghat-channel handler (ghat-bucket + "all" bucket)
    and again via the CHANNEL_ALL handler (every bucket including "all").
    The subscriber in ws_manager already fans a per-ghat publish into both
    the ghat bucket and the global "all" bucket, so the second publish is
    redundant. Single publish is correct and ≈2× cheaper.
    """
    return await publish(Keys.channel_ghat(ghat_id), message)


# ── Redis Streams ──────────────────────────────────────────────────────────────

async def stream_publish(stream: str, fields: dict, maxlen: int = 10_000) -> Optional[str]:
    try:
        r = await get_redis()
        return await _safe(r.xadd(stream, fields, maxlen=maxlen, approximate=True))
    except Exception as exc:
        logger.debug("[Stream] XADD failed stream=%s err=%s", stream, exc)
        return None


async def stream_read(stream: str, last_id: str = "0", count: int = 100) -> list:
    try:
        r = await get_redis()
        results = await _safe(r.xread({stream: last_id}, count=count, block=2000), default=[])
        if results:
            _stream, messages = results[0]
            return messages
        return []
    except Exception as exc:
        logger.debug("[Stream] XREAD failed err=%s", exc)
        return []


# ── Crowd data ────────────────────────────────────────────────────────────────

async def set_crowd_data(ghat_id: str, data: dict):
    try:
        r = await get_redis()
        key  = Keys.crowd_key(ghat_id)
        hist = Keys.crowd_hist(ghat_id)
        payload = json.dumps(data, default=str)
        pipe = r.pipeline(transaction=False)
        pipe.set(key, payload)
        pipe.lpush(hist, payload)
        # Keep only the most recent CROWD_HIST_MAXLEN entries — readers ask for
        # at most ~12, the previous 60 was wasted memory.
        pipe.ltrim(hist, 0, CROWD_HIST_MAXLEN - 1)
        await _safe(pipe.execute())
    except Exception as exc:
        logger.debug("[Crowd] set failed ghat=%s err=%s", ghat_id, exc)


async def get_crowd_data(ghat_id: str) -> Optional[dict]:
    try:
        r = await get_redis()
        raw = await _safe(r.get(Keys.crowd_key(ghat_id)))
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def get_crowd_history(ghat_id: str, n: int = 10) -> list:
    try:
        r = await get_redis()
        items = await _safe(r.lrange(Keys.crowd_hist(ghat_id), 0, n - 1), default=[])
        return [json.loads(x) for x in (items or [])]
    except Exception:
        return []


# ── Rate limiting ─────────────────────────────────────────────────────────────

_RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local uid = ARGV[4]
local cutoff = now - window
redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = redis.call('ZCARD', key)
if count < limit then
    redis.call('ZADD', key, now, uid)
    redis.call('EXPIRE', key, window + 1)
    return {1, limit - count - 1}
else
    return {0, 0}
end
"""


async def check_rate_limit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    # ── FIX (Issue 4): Fail CLOSED on Redis degradation ──────────────────────
    # PROBLEM: The old code returned (True, limit) — "allowed, max remaining" —
    # when Redis was unavailable. This means rate limiting silently failed OPEN:
    # any attacker who can cause a Redis blip can then flood the API with no
    # throttling whatsoever, hammering Postgres directly.
    #
    # SOLUTION: Fail CLOSED. When the circuit is open, DENY the request.
    # This is the safe default: it may briefly reject legitimate traffic,
    # but it protects the backend from thundering-herd floods during Redis
    # degradation. Ops can tune HEAL_REDIS_CHECK_S for faster recovery.
    if _circuit_open:
        logger.warning("[RateLimit] Circuit open — denying request key=%s (fail-closed)", key)
        return (False, 0)
    try:
        import uuid as _uuid
        r = await get_redis()
        now = time.time()
        uid = str(_uuid.uuid4())
        result = await _safe(
            r.eval(_RATE_LIMIT_SCRIPT, 1, key, str(now), str(window_seconds), str(limit), uid),
            default=None,
        )
        if result is None:
            # _safe returned None — circuit just tripped; fail closed
            logger.warning("[RateLimit] Redis op failed — denying request key=%s (fail-closed)", key)
            return (False, 0)
        return (bool(result[0]), int(result[1]))
    except Exception as exc:
        logger.warning("[RateLimit] Unexpected error — denying key=%s err=%s (fail-closed)", key, exc)
        return (False, 0)


# ── Health check ──────────────────────────────────────────────────────────────

async def redis_health() -> dict:
    if _circuit_open:
        return {
            "status": "unhealthy",
            "circuit_breaker": "open",
            "tripped_ago_s": round(time.monotonic() - _circuit_tripped_at, 1),
        }
    try:
        r = await get_redis()
        start = time.monotonic()
        await r.ping()
        latency_ms = round((time.monotonic() - start) * 1000, 2)
        info = await r.info("server")
        return {
            "status": "healthy",
            "latency_ms": latency_ms,
            "circuit_breaker": "closed",
            "redis_version": info.get("redis_version", "unknown"),
            "connected_clients": info.get("connected_clients", 0),
            "used_memory_human": info.get("used_memory_human", "unknown"),
        }
    except Exception as exc:
        return {"status": "unhealthy", "error": str(exc)}
