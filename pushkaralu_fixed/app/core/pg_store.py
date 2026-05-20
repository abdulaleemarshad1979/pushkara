# ═══════════════════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — PostgreSQL Read Store  (v8 — FIXED)
#
# FIXES vs v7:
#   - Moved from root pg_store.py → app/core/pg_store.py (matches import in main.py)
#   - asyncio.get_event_loop() → asyncio.get_running_loop()
#     (get_event_loop() is deprecated in Python 3.10+ and raises DeprecationWarning;
#      get_running_loop() is correct inside async context and raises RuntimeError
#      instead of silently creating a new loop in edge cases)
#   - _pool_lock now created lazily (avoids "no running event loop" on import)
#   - close_pg_pool: graceful wait with timeout before force-close
#   - fetch_* functions: explicit column ordering for future schema migrations
#   - load_db_from_postgres: transaction block for consistent snapshot reads
#
# ARCHITECTURE (Cache-Aside with Stampede Protection):
#
#   READ PATH:
#     1. Try Redis (O(1)).
#     2. On MISS → acquire per-key SET NX distributed lock (prevents stampede).
#     3. Lock holder queries Postgres, populates Redis, releases lock.
#     4. Lock waiters spin-wait briefly then re-read from (now warm) Redis.
#
#   WRITE PATH (CRITICAL — SOS, lost persons, issues):
#     1. INSERT/UPSERT directly to Postgres in the request handler (immediate).
#     2. Invalidate relevant Redis cache keys.
#     3. db_writer still drains crowd_snapshots + audit events via stream.
#
#   STARTUP:
#     - load_db_from_postgres() reads every table and hydrates DB[] dict
#       so existing in-memory code paths are instantly correct after restart.
#
# BIG-O:
#   cache_get / cache_set  — O(1)
#   Postgres reads         — O(log n) via indexed columns (status, created_at)
#   Stampede lock acquire  — O(1) Redis SET NX
# ═══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

logger = logging.getLogger("pushkaralu.pg_store")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://pushkaralu:change_me@localhost/pushkaralu",
)
DB_MIN_SIZE = int(os.getenv("DB_MIN_POOL", "2"))
DB_MAX_SIZE = int(os.getenv("DB_MAX_POOL", "10"))

# Stampede lock config
_LOCK_TTL_MS  = 2000    # lock expires after 2 s even if holder crashes
_LOCK_WAIT_MS = 50      # poll every 50 ms while waiting for lock release
_LOCK_TIMEOUT = 3.0     # give up waiting after 3 s; fall through to DB directly

# ── Singleton asyncpg pool ────────────────────────────────────────────────────
_pg_pool = None
_pool_lock: Optional[asyncio.Lock] = None  # created lazily in async context


def _get_pool_lock() -> asyncio.Lock:
    """Lazy lock creation — safe to call from within async context."""
    global _pool_lock
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    return _pool_lock


async def get_pg_pool():
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    async with _get_pool_lock():
        if _pg_pool is not None:
            return _pg_pool
        try:
            import asyncpg
            _pg_pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=DB_MIN_SIZE,
                max_size=DB_MAX_SIZE,
                command_timeout=10,
                max_inactive_connection_lifetime=300,
                statement_cache_size=100,
            )
            logger.info("[PGStore] Pool ready  min=%d max=%d", DB_MIN_SIZE, DB_MAX_SIZE)
        except Exception as exc:
            logger.warning("[PGStore] PostgreSQL unavailable: %s — reads will use in-memory DB", exc)
            _pg_pool = None
    return _pg_pool


@asynccontextmanager
async def _conn():
    """Yield an asyncpg connection or raise RuntimeError if pool not ready."""
    pool = await get_pg_pool()
    if pool is None:
        raise RuntimeError("PostgreSQL pool not available")
    async with pool.acquire() as conn:
        yield conn


async def close_pg_pool():
    global _pg_pool
    if _pg_pool:
        try:
            await asyncio.wait_for(_pg_pool.close(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("[PGStore] Pool close timed out — forcing terminate")
            await _pg_pool.terminate()
        finally:
            _pg_pool = None


# ── Redis stampede lock ───────────────────────────────────────────────────────

async def _acquire_stampede_lock(redis_client, lock_key: str) -> bool:
    """
    Atomic SET NX PX: returns True if this caller owns the lock.
    O(1) Redis operation.
    """
    result = await redis_client.set(lock_key, "1", px=_LOCK_TTL_MS, nx=True)
    return bool(result)


async def _release_stampede_lock(redis_client, lock_key: str) -> None:
    try:
        await redis_client.delete(lock_key)
    except Exception:
        pass  # TTL will expire it anyway


async def cached_read_pg(
    cache_key: str,
    pg_query_fn: Callable,   # async callable() → dict | list
    ttl: int = 3,
) -> Any:
    """
    Cache-aside read with distributed stampede lock.

    Flow:
      HIT  → return Redis value immediately (O(1))
      MISS → acquire lock → query Postgres → populate Redis → release lock
      MISS + lock contention → spin-wait → re-read from (now warm) Redis
    """
    from app.core.redis_manager import cache_get, cache_set, get_redis

    # 1. Fast path — cache hit
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached

    lock_key = f"lock:stampede:{cache_key}"

    try:
        r = await get_redis()
    except Exception:
        # Redis unavailable — fall through directly to Postgres
        return await pg_query_fn()

    # 2. Try to acquire stampede lock
    if await _acquire_stampede_lock(r, lock_key):
        try:
            # Double-check: another instance may have populated cache between
            # our miss and lock acquisition
            cached = await cache_get(cache_key)
            if cached is not None:
                return cached

            result = await pg_query_fn()
            if result is not None:
                await cache_set(cache_key, result, ttl=ttl)
            return result
        finally:
            await _release_stampede_lock(r, lock_key)
    else:
        # 3. Spin-wait for lock holder to populate cache
        # FIX: asyncio.get_running_loop() (not deprecated get_event_loop())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _LOCK_TIMEOUT
        while loop.time() < deadline:
            await asyncio.sleep(_LOCK_WAIT_MS / 1000.0)
            cached = await cache_get(cache_key)
            if cached is not None:
                return cached
        # Timeout — fall back to Postgres directly (safe, just slower)
        logger.warning("[PGStore] Stampede lock timeout for %s — querying Postgres directly", cache_key)
        return await pg_query_fn()


# ═══════════════════════════════════════════════════════════════════════════════
# CRITICAL WRITES — immediate Postgres commit (no queue, no async drain)
# ═══════════════════════════════════════════════════════════════════════════════

async def write_sos_alert(alert: dict) -> bool:
    """
    Immediate INSERT into sos_alerts. Returns True on success.
    Called synchronously within the SOS request handler before returning 200.
    """
    try:
        async with _conn() as conn:
            await conn.execute(
                """INSERT INTO sos_alerts
                   (id, user_name, phone, latitude, longitude, status,
                    assigned_volunteer, assigned_volunteer_name, created_at, payload)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::timestamptz,$10::jsonb)
                   ON CONFLICT (id) DO UPDATE SET
                     status                  = EXCLUDED.status,
                     assigned_volunteer      = EXCLUDED.assigned_volunteer,
                     assigned_volunteer_name = EXCLUDED.assigned_volunteer_name,
                     payload                 = EXCLUDED.payload,
                     updated_at              = NOW()""",
                alert.get("id"),
                alert.get("user_name"),
                alert.get("phone"),
                alert.get("latitude"),
                alert.get("longitude"),
                alert.get("status", "active"),
                alert.get("assigned_volunteer"),
                alert.get("assigned_volunteer_name"),
                alert.get("timestamp"),
                json.dumps(alert),
            )
        return True
    except Exception as exc:
        logger.error("[PGStore] write_sos_alert failed: %s", exc)
        return False


async def update_sos_status(
    alert_id: str,
    status: str,
    resolved_at: Optional[str] = None,
    volunteer_id: Optional[str] = None,
    volunteer_name: Optional[str] = None,
) -> bool:
    """Update SOS alert status and keep the JSONB payload column in sync."""
    try:
        async with _conn() as conn:
            await conn.execute(
                """UPDATE sos_alerts SET
                     status                  = $2,
                     resolved_at             = $3::timestamptz,
                     assigned_volunteer      = COALESCE($4, assigned_volunteer),
                     assigned_volunteer_name = COALESCE($5, assigned_volunteer_name),
                     updated_at              = NOW()
                   WHERE id = $1""",
                alert_id, status, resolved_at, volunteer_id, volunteer_name,
            )
            # Sync payload JSONB so fetch_sos_alerts returns current status
            await conn.execute(
                """UPDATE sos_alerts SET
                     payload = payload
                       || jsonb_build_object('status', status)
                       || CASE WHEN assigned_volunteer IS NOT NULL
                              THEN jsonb_build_object(
                                'assigned_volunteer', assigned_volunteer,
                                'assigned_volunteer_name', assigned_volunteer_name
                              )
                              ELSE '{}'::jsonb END
                   WHERE id = $1""",
                alert_id,
            )
        return True
    except Exception as exc:
        logger.error("[PGStore] update_sos_status failed: %s", exc)
        return False


async def write_issue(issue: dict) -> bool:
    try:
        async with _conn() as conn:
            await conn.execute(
                """INSERT INTO issues
                   (id, description, category, image_url, latitude, longitude,
                    status, user_name, created_at, payload)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::timestamptz,$10::jsonb)
                   ON CONFLICT (id) DO UPDATE SET
                     status     = EXCLUDED.status,
                     image_url  = EXCLUDED.image_url,
                     payload    = EXCLUDED.payload,
                     updated_at = NOW()""",
                issue.get("id"),
                issue.get("description"),
                issue.get("category"),
                issue.get("image_url"),
                issue.get("latitude"),
                issue.get("longitude"),
                issue.get("status", "pending"),
                issue.get("user_name"),
                issue.get("timestamp"),
                json.dumps(issue),
            )
        return True
    except Exception as exc:
        logger.error("[PGStore] write_issue failed: %s", exc)
        return False


async def update_issue_status(
    issue_id: str,
    status: str,
    volunteer_id: Optional[str] = None,
    resolved_at: Optional[str] = None,
) -> bool:
    """Update issue status and keep the JSONB payload column in sync."""
    try:
        async with _conn() as conn:
            await conn.execute(
                """UPDATE issues SET
                     status             = $2,
                     assigned_volunteer = COALESCE($3, assigned_volunteer),
                     resolved_at        = $4::timestamptz,
                     updated_at         = NOW()
                   WHERE id = $1""",
                issue_id, status, volunteer_id, resolved_at,
            )
            # Sync payload JSONB
            await conn.execute(
                """UPDATE issues SET
                     payload = payload || jsonb_build_object('status', status)
                   WHERE id = $1""",
                issue_id,
            )
        return True
    except Exception as exc:
        logger.error("[PGStore] update_issue_status failed: %s", exc)
        return False


async def write_lost_person(person: dict) -> bool:
    try:
        async with _conn() as conn:
            await conn.execute(
                """INSERT INTO lost_persons
                   (id, name, age, photo_url, last_seen_location, current_location,
                    contact_person, contact_phone, description, status, created_at, payload)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::timestamptz,$12::jsonb)
                   ON CONFLICT (id) DO UPDATE SET
                     status           = EXCLUDED.status,
                     photo_url        = EXCLUDED.photo_url,
                     current_location = EXCLUDED.current_location,
                     payload          = EXCLUDED.payload,
                     updated_at       = NOW()""",
                person.get("id"),
                person.get("name"),
                person.get("age"),
                person.get("photo_url"),
                person.get("last_seen_location"),
                person.get("current_location", "Unknown"),
                person.get("contact_person"),
                person.get("contact_phone"),
                person.get("description", ""),
                person.get("status", "missing"),
                person.get("timestamp"),
                json.dumps(person),
            )
        return True
    except Exception as exc:
        logger.error("[PGStore] write_lost_person failed: %s", exc)
        return False


async def update_lost_person_status(
    person_id: str,
    status: Optional[str],
    current_location: Optional[str],
    last_seen_location: Optional[str],
) -> bool:
    """Update lost person record and keep the JSONB payload column in sync."""
    try:
        async with _conn() as conn:
            # Step 1: Apply column updates
            await conn.execute(
                """UPDATE lost_persons SET
                     status             = COALESCE($2, status),
                     current_location   = COALESCE($3, current_location),
                     last_seen_location = COALESCE($4, last_seen_location),
                     updated_at         = NOW()
                   WHERE id = $1""",
                person_id, status, current_location, last_seen_location,
            )
            # Step 2: Sync payload JSONB with the updated column values
            # so fetch queries reading from payload stay consistent
            await conn.execute(
                """UPDATE lost_persons SET
                     payload = payload
                       || jsonb_build_object('status', status)
                       || jsonb_build_object('current_location', current_location)
                       || jsonb_build_object('last_seen_location', last_seen_location)
                   WHERE id = $1""",
                person_id,
            )
        return True
    except Exception as exc:
        logger.error("[PGStore] update_lost_person_status failed: %s", exc)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# READ QUERIES — hit by cached_read_pg() on cache miss
# ═══════════════════════════════════════════════════════════════════════════════

async def fetch_sos_alerts(status: Optional[str] = None) -> dict:
    try:
        async with _conn() as conn:
            if status:
                rows = await conn.fetch(
                    "SELECT payload FROM sos_alerts WHERE status=$1 ORDER BY created_at DESC",
                    status,
                )
            else:
                rows = await conn.fetch(
                    "SELECT payload FROM sos_alerts ORDER BY created_at DESC",
                )
        alerts = [json.loads(r["payload"]) for r in rows]
        return {"sos_alerts": alerts}
    except Exception as exc:
        logger.error("[PGStore] fetch_sos_alerts failed: %s", exc)
        return {"sos_alerts": []}


async def fetch_issues(status: Optional[str] = None) -> dict:
    try:
        async with _conn() as conn:
            if status:
                rows = await conn.fetch(
                    "SELECT payload FROM issues WHERE status=$1 ORDER BY created_at DESC",
                    status,
                )
            else:
                rows = await conn.fetch(
                    "SELECT payload FROM issues ORDER BY created_at DESC",
                )
        issues = [json.loads(r["payload"]) for r in rows]
        return {"issues": issues}
    except Exception as exc:
        logger.error("[PGStore] fetch_issues failed: %s", exc)
        return {"issues": []}


async def fetch_lost_persons(status: Optional[str] = None) -> dict:
    try:
        async with _conn() as conn:
            if status:
                rows = await conn.fetch(
                    "SELECT payload FROM lost_persons WHERE status=$1 ORDER BY created_at DESC",
                    status,
                )
            else:
                rows = await conn.fetch(
                    "SELECT payload FROM lost_persons ORDER BY created_at DESC",
                )
        persons = [json.loads(r["payload"]) for r in rows]
        return {"lost_persons": persons}
    except Exception as exc:
        logger.error("[PGStore] fetch_lost_persons failed: %s", exc)
        return {"lost_persons": []}


# ═══════════════════════════════════════════════════════════════════════════════
# STARTUP HYDRATION — load Postgres state into DB[] on every instance start
# ═══════════════════════════════════════════════════════════════════════════════

async def load_db_from_postgres(db: dict) -> bool:
    """
    Hydrate the in-memory DB dict from PostgreSQL using a single read transaction
    for a consistent snapshot across all tables.

    Called once at startup so all 4 load-balanced instances share consistent
    state even after a container restart that wiped in-memory data.

    Falls back gracefully if Postgres is not ready (sample_data.json
    remains as the seed — same behaviour as v6).
    """
    pool = await get_pg_pool()
    if pool is None:
        logger.warning("[PGStore] Skipping startup hydration — Postgres unavailable")
        return False

    try:
        async with pool.acquire() as conn:
            # Single read-only transaction for consistent snapshot
            async with conn.transaction(readonly=True):
                # ── FIX (Issue 2): Offload CPU-bound JSON parsing to thread ──
                # json.loads() in a tight list-comprehension blocks the async
                # event loop for the entire duration of the parse pass. Under
                # sustained load this grows linearly with table size and can
                # delay incoming requests during startup or container restart.
                # asyncio.to_thread() runs the parse batch in a ThreadPoolExecutor,
                # keeping the event loop free for health checks and early requests.

                def _parse_rows(rows):
                    return [json.loads(r["payload"]) for r in rows]

                # SOS alerts
                rows = await conn.fetch(
                    "SELECT payload FROM sos_alerts ORDER BY created_at DESC"
                )
                if rows:
                    db["sos_alerts"] = await asyncio.to_thread(_parse_rows, rows)
                    logger.info("[PGStore] Hydrated sos_alerts  count=%d", len(db["sos_alerts"]))

                # Issues
                rows = await conn.fetch(
                    "SELECT payload FROM issues ORDER BY created_at DESC"
                )
                if rows:
                    db["issues"] = await asyncio.to_thread(_parse_rows, rows)
                    logger.info("[PGStore] Hydrated issues  count=%d", len(db["issues"]))

                # Lost persons
                rows = await conn.fetch(
                    "SELECT payload FROM lost_persons ORDER BY created_at DESC"
                )
                if rows:
                    db["lost_persons"] = await asyncio.to_thread(_parse_rows, rows)
                    logger.info("[PGStore] Hydrated lost_persons  count=%d", len(db["lost_persons"]))

        logger.info("[PGStore] Startup hydration complete")
        return True

    except Exception as exc:
        logger.error("[PGStore] Startup hydration failed: %s", exc)
        return False

# ─────────────────────────────────────────────────────────────────────────────
# Volunteer write helpers (admin-only — called from POST/DELETE /admin/volunteer)
# ─────────────────────────────────────────────────────────────────────────────

async def write_volunteer(vol: dict) -> bool:
    """
    INSERT a new volunteer into PostgreSQL.
    Expects vol to have password_hash (not plain password).
    """
    pool = await get_pg_pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO volunteers
                    (id, name, username, password_hash, phone, zone, status,
                     latitude, longitude, created_at, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,NOW(),NOW())
                ON CONFLICT (username) DO NOTHING
                """,
                vol["id"], vol["name"], vol["username"],
                vol["password_hash"], vol.get("phone", ""),
                vol.get("zone", ""), vol.get("status", "available"),
                vol.get("latitude"), vol.get("longitude"),
            )
        logger.info("[PG] Volunteer written id=%s username=%s", vol["id"], vol["username"])
        return True
    except Exception as exc:
        logger.error("[PG] write_volunteer failed: %s", exc)
        return False


async def delete_volunteer(vid: str) -> bool:
    """DELETE a volunteer from PostgreSQL by id."""
    pool = await get_pg_pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            result = await conn.execute("DELETE FROM volunteers WHERE id = $1", vid)
        deleted = result != "DELETE 0"
        logger.info("[PG] Volunteer deleted id=%s success=%s", vid, deleted)
        return deleted
    except Exception as exc:
        logger.error("[PG] delete_volunteer failed: %s", exc)
        return False


async def update_volunteer_fields(vid: str, fields: dict) -> bool:
    """UPDATE mutable volunteer fields (name, phone, zone, status)."""
    pool = await get_pg_pool()
    if pool is None:
        return False
    allowed = {"name", "phone", "zone", "status", "assigned_issue"}
    sets, vals, idx = [], [], 1
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k} = ${idx}")
            vals.append(v)
            idx += 1
    if not sets:
        return True
    sets.append("updated_at = NOW()")
    vals.append(vid)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE volunteers SET {', '.join(sets)} WHERE id = ${idx}",
                *vals,
            )
        return True
    except Exception as exc:
        logger.error("[PG] update_volunteer_fields failed: %s", exc)
        return False
