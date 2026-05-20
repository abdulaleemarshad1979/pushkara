"""
Godavari Pushkaralu 2027 — Async PostgreSQL Connection  (v5)
PHASE 2: asyncpg connection pool for DB writer worker.
FastAPI NEVER imports this directly — only the db_writer worker uses it.
"""
import logging
import os
from pathlib import Path

logger = logging.getLogger("pushkaralu.db")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://pushkaralu:change_me@localhost/pushkaralu"
)
DB_MIN_SIZE = int(os.getenv("DB_MIN_POOL", "2"))
DB_MAX_SIZE = int(os.getenv("DB_MAX_POOL", "10"))


class DBPool:
    """Singleton asyncpg pool with auto-schema bootstrap."""

    def __init__(self):
        self._pool = None
        self.available = False

    async def connect(self):
        try:
            import asyncpg
            self._pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=DB_MIN_SIZE,
                max_size=DB_MAX_SIZE,
                command_timeout=10,
                max_inactive_connection_lifetime=300,
            )
            await self._bootstrap_schema()
            self.available = True
            logger.info("[DB] PostgreSQL pool ready  min=%d max=%d", DB_MIN_SIZE, DB_MAX_SIZE)
        except Exception as exc:
            logger.warning("[DB] PostgreSQL unavailable: %s", exc)
            self.available = False

    async def _bootstrap_schema(self):
        """Run schema.sql on first connect (idempotent — all DDL uses IF NOT EXISTS)."""
        schema_path = Path(__file__).parent / "schema.sql"
        if not schema_path.exists():
            logger.warning("[DB] schema.sql not found at %s", schema_path)
            return
        sql = schema_path.read_text()
        async with self._pool.acquire() as conn:
            await conn.execute(sql)
        logger.info("[DB] Schema bootstrap complete")

    def acquire(self):
        """Context manager: `async with db_pool.acquire() as conn:`"""
        if not self.available or not self._pool:
            raise RuntimeError("DB pool not available")
        return self._pool.acquire()

    async def close(self):
        if self._pool:
            await self._pool.close()
            self._pool = None
            self.available = False


# Singleton — imported by db_writer only
db_pool = DBPool()
