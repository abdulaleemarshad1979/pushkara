"""
Godavari Pushkaralu 2027 — DB Writer Worker  (v6 — Hardened)

HARDENING vs v5:
  - asyncpg QueuePool with min/max size, command_timeout, statement_cache
  - Exponential backoff retry on connect AND on every write batch (max 5 attempts)
  - Async memory buffer (asyncio.Queue) collects events; flushes in batches
  - Pool health-check loop reconnects if pool is lost (e.g. Postgres restart)
  - All v5 logic (consumer groups, pending recovery, XACK, audit log) preserved
"""
import asyncio
import json
import logging
import os
import signal
import time
import uuid

logger = logging.getLogger("pushkaralu.db_writer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

DATABASE_URL   = os.getenv("DATABASE_URL", "postgresql://pushkaralu:change_me@localhost/pushkaralu")
FLUSH_INTERVAL = float(os.getenv("DB_FLUSH_INTERVAL", "5.0"))
BATCH_SIZE     = int(os.getenv("DB_BATCH_SIZE", "100"))
BUFFER_MAX     = int(os.getenv("DB_BUFFER_MAX", "5000"))
CONSUMER_GROUP = "db-writers"
CONSUMER_NAME  = f"writer-{os.getenv('HOSTNAME', uuid.uuid4().hex[:8])}"

_CONNECT_BASE_DELAY = 1.0
_CONNECT_MAX_DELAY  = 60.0
_WRITE_BASE_DELAY   = 0.25
_WRITE_MAX_DELAY    = 8.0
_WRITE_MAX_ATTEMPTS = 5

_shutdown = False


def _handle_signal(sig, frame):
    global _shutdown
    logger.info("[DBWriter] Signal %s received — shutting down", sig)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)


class WriteBuffer:
    """asyncio.Queue-backed buffer that absorbs spikes between Redis reads and DB writes."""

    def __init__(self, maxsize: int = BUFFER_MAX):
        self._q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._dropped = 0

    def put_nowait(self, item):
        try:
            self._q.put_nowait(item)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 500 == 1:
                logger.warning("[Buffer] Queue full — dropped %d events so far", self._dropped)

    async def drain(self, max_items: int = BATCH_SIZE) -> list:
        items = []
        while len(items) < max_items:
            try:
                items.append(self._q.get_nowait())
            except asyncio.QueueEmpty:
                break
        return items

    @property
    def size(self) -> int:
        return self._q.qsize()


class DBWriter:
    def __init__(self):
        self._pool = None
        self.available = False
        self._written_total = 0
        self._failed_total  = 0
        self._connect_attempts = 0

    async def connect(self):
        """Retry indefinitely with exponential backoff until Postgres is reachable."""
        import asyncpg
        from pathlib import Path

        delay = _CONNECT_BASE_DELAY
        while not _shutdown:
            self._connect_attempts += 1
            try:
                self._pool = await asyncpg.create_pool(
                    DATABASE_URL,
                    min_size=2,
                    max_size=10,
                    max_inactive_connection_lifetime=300,
                    command_timeout=15,
                    statement_cache_size=100,
                )
                schema_path = Path(__file__).parent.parent.parent / "db" / "schema.sql"
                if schema_path.exists():
                    sql = schema_path.read_text()
                    async with self._pool.acquire() as conn:
                        await conn.execute(sql)
                    logger.info("[DBWriter] Schema bootstrapped")
                self.available = True
                logger.info(
                    "[DBWriter] PostgreSQL pool ready  attempts=%d  min=2 max=10",
                    self._connect_attempts,
                )
                return
            except Exception as exc:
                self.available = False
                logger.warning(
                    "[DBWriter] PostgreSQL unavailable (attempt %d): %s — retry in %.0fs",
                    self._connect_attempts, exc, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _CONNECT_MAX_DELAY)

    async def ensure_connected(self):
        """Health-check the pool; rebuild asynchronously if lost."""
        if self.available and self._pool is not None:
            try:
                async with self._pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
                return
            except Exception:
                logger.warning("[DBWriter] Pool health-check failed — reconnecting")
                self.available = False
                try:
                    await self._pool.close()
                except Exception:
                    pass
                self._pool = None
        asyncio.create_task(self.connect())

    async def write_batch(self, events: list) -> int:
        """Write events to Postgres with per-attempt exponential backoff."""
        if not self.available or not self._pool:
            return 0

        sos_rows, issue_rows, crowd_rows, lost_rows, event_rows = [], [], [], [], []

        for _msg_id, fields in events:
            event_type = fields.get("event", "")
            try:
                payload = json.loads(fields.get("payload", "{}"))
            except Exception:
                continue

            if event_type == "sos_alert":
                sos_rows.append((
                    payload.get("id"),
                    payload.get("user_name"), payload.get("phone"),
                    payload.get("latitude"),  payload.get("longitude"),
                    payload.get("status", "active"),
                    payload.get("assigned_volunteer"),
                    payload.get("assigned_volunteer_name"),
                    payload.get("timestamp"),
                    json.dumps(payload),
                ))
            elif event_type == "new_issue":
                issue_rows.append((
                    payload.get("id"),
                    payload.get("description"), payload.get("category"),
                    payload.get("latitude"),    payload.get("longitude"),
                    payload.get("status", "pending"),
                    payload.get("user_name"),   payload.get("timestamp"),
                    json.dumps(payload),
                ))
            elif event_type == "crowd_snapshot":
                crowd_rows.append((
                    payload.get("ghat_id"),      payload.get("crowd_level"),
                    payload.get("risk_score"),   payload.get("estimated_count"),
                    payload.get("occupancy_pct"),
                    json.dumps(payload.get("sources", {})),
                ))
            elif event_type == "lost_person":
                lost_rows.append((
                    payload.get("id"),   payload.get("name"),   payload.get("age"),
                    payload.get("status", "missing"),
                    payload.get("contact_phone"), payload.get("timestamp"),
                    json.dumps(payload),
                ))

            event_rows.append((event_type, payload.get("id"), json.dumps(payload)))

        attempt = 0
        delay   = _WRITE_BASE_DELAY

        while attempt < _WRITE_MAX_ATTEMPTS:
            attempt += 1
            try:
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        if sos_rows:
                            await conn.executemany(
                                """INSERT INTO sos_alerts
                                   (id,user_name,phone,latitude,longitude,status,
                                    assigned_volunteer,assigned_volunteer_name,created_at,payload)
                                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::timestamptz,$10::jsonb)
                                   ON CONFLICT (id) DO UPDATE SET
                                     status=EXCLUDED.status,
                                     assigned_volunteer=EXCLUDED.assigned_volunteer,
                                     payload=EXCLUDED.payload,
                                     updated_at=NOW()""",
                                sos_rows,
                            )
                        if issue_rows:
                            await conn.executemany(
                                """INSERT INTO issues
                                   (id,description,category,latitude,longitude,
                                    status,user_name,created_at,payload)
                                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8::timestamptz,$9::jsonb)
                                   ON CONFLICT (id) DO UPDATE SET
                                     status=EXCLUDED.status,
                                     payload=EXCLUDED.payload,
                                     updated_at=NOW()""",
                                issue_rows,
                            )
                        if crowd_rows:
                            await conn.executemany(
                                """INSERT INTO crowd_snapshots
                                   (ghat_id,crowd_level,risk_score,estimated_count,occupancy_pct,sources)
                                   VALUES ($1,$2,$3,$4,$5,$6::jsonb)""",
                                crowd_rows,
                            )
                        if lost_rows:
                            await conn.executemany(
                                """INSERT INTO lost_persons
                                   (id,name,age,status,contact_phone,created_at,payload)
                                   VALUES ($1,$2,$3,$4,$5,$6::timestamptz,$7::jsonb)
                                   ON CONFLICT (id) DO UPDATE SET
                                     status=EXCLUDED.status,
                                     payload=EXCLUDED.payload,
                                     updated_at=NOW()""",
                                lost_rows,
                            )
                        if event_rows:
                            await conn.executemany(
                                """INSERT INTO app_events (event_type,entity_id,payload)
                                   VALUES ($1,$2,$3::jsonb)""",
                                event_rows,
                            )

                written = len(sos_rows) + len(issue_rows) + len(crowd_rows) + len(lost_rows)
                self._written_total += written
                return written

            except Exception as exc:
                logger.warning(
                    "[DBWriter] Batch write attempt %d/%d failed: %s — retry in %.2fs",
                    attempt, _WRITE_MAX_ATTEMPTS, exc, delay,
                )
                if attempt >= _WRITE_MAX_ATTEMPTS:
                    logger.error(
                        "[DBWriter] All %d write attempts exhausted — %d events lost",
                        _WRITE_MAX_ATTEMPTS, len(events),
                    )
                    self._failed_total += len(events)
                    self.available = False   # trigger pool rebuild on next health-check
                    return 0
                await asyncio.sleep(delay)
                delay = min(delay * 2, _WRITE_MAX_DELAY)

        return 0

    async def close(self):
        if self._pool:
            try:
                await self._pool.close()
            except Exception:
                pass


async def run():
    from app.core.redis_manager import get_redis, Keys

    writer = DBWriter()
    buffer = WriteBuffer()

    await writer.connect()   # blocks with backoff until Postgres is ready

    r = await get_redis()

    try:
        await r.xgroup_create(Keys.STREAM_EVENTS, CONSUMER_GROUP, id="0", mkstream=True)
        logger.info("[DBWriter] Consumer group created")
    except Exception:
        pass

    logger.info("[DBWriter] Starting  group=%s consumer=%s", CONSUMER_GROUP, CONSUMER_NAME)
    log_counter       = 0
    last_health_check = time.monotonic()

    while not _shutdown:
        try:
            await asyncio.sleep(FLUSH_INTERVAL)

            # Pool health-check every 30 seconds
            now = time.monotonic()
            if now - last_health_check > 30:
                await writer.ensure_connected()
                last_health_check = now

            # Read new messages from Redis Stream into buffer
            results = await r.xreadgroup(
                CONSUMER_GROUP, CONSUMER_NAME,
                {Keys.STREAM_EVENTS: ">"},
                count=BATCH_SIZE, block=1000,
            )
            if results:
                _stream, messages = results[0]
                for msg in messages:
                    buffer.put_nowait(msg)

            # Drain buffer → DB
            batch = await buffer.drain(BATCH_SIZE)
            if batch:
                written = await writer.write_batch(batch)
                if written > 0:
                    msg_ids = [mid for mid, _ in batch]
                    await r.xack(Keys.STREAM_EVENTS, CONSUMER_GROUP, *msg_ids)
                log_counter += 1
                if log_counter % 10 == 0:
                    logger.info(
                        "[DBWriter] written=%d failed=%d buffered=%d",
                        writer._written_total, writer._failed_total, buffer.size,
                    )

            # Recover pending (post-crash redelivery) every ~60s
            if int(time.time()) % 60 < FLUSH_INTERVAL:
                pending = await r.xreadgroup(
                    CONSUMER_GROUP, CONSUMER_NAME,
                    {Keys.STREAM_EVENTS: "0"},
                    count=BATCH_SIZE,
                )
                if pending:
                    _s, pmessages = pending[0]
                    if pmessages:
                        written = await writer.write_batch(pmessages)
                        if written > 0:
                            ids = [mid for mid, _ in pmessages]
                            await r.xack(Keys.STREAM_EVENTS, CONSUMER_GROUP, *ids)
                            logger.info("[DBWriter] Recovered %d pending messages", written)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("[DBWriter] Loop error: %s", exc)
            await asyncio.sleep(5)

    logger.info("[DBWriter] Shutting down  written=%d", writer._written_total)
    await writer.close()


if __name__ == "__main__":
    asyncio.run(run())
