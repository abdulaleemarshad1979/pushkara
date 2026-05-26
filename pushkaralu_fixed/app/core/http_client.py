"""
Godavari Pushkaralu 2027 — Shared HTTP Client Pool

All outbound HTTP traffic must go through this module instead of constructing
a fresh httpx.AsyncClient per call. Each client construction costs:
  - DNS lookup
  - TCP socket setup
  - TLS handshake (1-2 RTTs)
  - HTTP/2 setup if the upstream supports it

Reusing a single client across calls keeps a warm connection pool, which on
a hot path (chat / whatsapp) is the difference between p95 ~150ms and p95
~600ms. It also prevents the "fd exhaustion" failure mode that hit us under
load when 1000 concurrent /api/chat requests each opened a fresh socket.

Design:
  - One client per upstream "category" — groq, whatsapp_meta, whatsapp_twilio,
    whatsapp_mana_mitra, scraperbot. Each gets its own per-host connection
    pool sized to that upstream's documented rate limit.
  - Lazy: clients are created on first call, never on import — keeps startup
    fast and lets tests run without network init.
  - Lifespan-aware: `aclose_all()` is called from FastAPI's shutdown hook so
    sockets are flushed cleanly during deploys.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("pushkaralu.http_client")

# ─────────────────────────────────────────────────────────────────────────────
# Sensible defaults — overridable via env
# ─────────────────────────────────────────────────────────────────────────────
_CONNECT_TIMEOUT = float(os.getenv("HTTP_CONNECT_TIMEOUT", "5.0"))
_READ_TIMEOUT    = float(os.getenv("HTTP_READ_TIMEOUT",    "20.0"))
_POOL_TIMEOUT    = float(os.getenv("HTTP_POOL_TIMEOUT",    "5.0"))

# Per-upstream pool sizes — small enough that one runaway upstream cannot
# drain the entire fd budget, large enough for festival-day burst.
_DEFAULT_KEEPALIVE = int(os.getenv("HTTP_POOL_KEEPALIVE", "20"))
_DEFAULT_MAX_CONNECTIONS = int(os.getenv("HTTP_POOL_MAX_CONN", "100"))

_clients: dict[str, httpx.AsyncClient] = {}
_lock = asyncio.Lock()


def _build_limits(max_conn: int, keepalive: int) -> httpx.Limits:
    return httpx.Limits(
        max_connections=max_conn,
        max_keepalive_connections=keepalive,
        keepalive_expiry=30.0,
    )


def _build_timeout(connect: float = _CONNECT_TIMEOUT,
                   read: float = _READ_TIMEOUT,
                   pool: float = _POOL_TIMEOUT) -> httpx.Timeout:
    return httpx.Timeout(connect=connect, read=read, write=read, pool=pool)


async def get_client(
    name: str,
    *,
    timeout: Optional[httpx.Timeout] = None,
    max_connections: int = _DEFAULT_MAX_CONNECTIONS,
    keepalive: int = _DEFAULT_KEEPALIVE,
    base_url: Optional[str] = None,
    http2: bool = False,
) -> httpx.AsyncClient:
    """
    Return (and lazily create) a singleton AsyncClient for the named category.

    Subsequent calls with the same `name` always return the same client; the
    keyword arguments are honoured ONLY on first creation.
    """
    client = _clients.get(name)
    if client is not None:
        return client

    async with _lock:
        client = _clients.get(name)
        if client is not None:
            return client

        client = httpx.AsyncClient(
            timeout=timeout or _build_timeout(),
            limits=_build_limits(max_connections, keepalive),
            base_url=base_url or "",
            http2=http2,
            follow_redirects=False,
        )
        _clients[name] = client
        logger.info(
            "[HTTP] Client created  name=%s  max_conn=%d  keepalive=%d  http2=%s",
            name, max_connections, keepalive, http2,
        )
        return client


# ─────────────────────────────────────────────────────────────────────────────
# Pre-named clients (typed accessors so callsites read clearly)
# ─────────────────────────────────────────────────────────────────────────────

async def groq_client() -> httpx.AsyncClient:
    """Groq LLM — slower upstream; 25 s read timeout for first-token latency."""
    return await get_client(
        "groq",
        timeout=_build_timeout(read=25.0),
        max_connections=64,
        keepalive=16,
    )


async def whatsapp_client() -> httpx.AsyncClient:
    """Shared client for Twilio / Meta / Mana Mitra HTTP calls."""
    return await get_client(
        "whatsapp",
        timeout=_build_timeout(read=10.0),
        max_connections=64,
        keepalive=16,
    )


async def scraperbot_client() -> httpx.AsyncClient:
    """TourGo SCRAPERBOT_URL — accepts long polls (30 s)."""
    return await get_client(
        "scraperbot",
        timeout=_build_timeout(read=30.0),
        max_connections=16,
        keepalive=4,
    )


async def cctv_ingest_client() -> httpx.AsyncClient:
    """CCTV worker → API ingest. Many short-lived concurrent posts."""
    return await get_client(
        "cctv_ingest",
        timeout=_build_timeout(read=5.0),
        max_connections=32,
        keepalive=8,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle
# ─────────────────────────────────────────────────────────────────────────────

async def aclose_all() -> None:
    """Flush every open client. Call from FastAPI lifespan shutdown."""
    async with _lock:
        for name, client in list(_clients.items()):
            try:
                await client.aclose()
                logger.info("[HTTP] Closed client name=%s", name)
            except Exception as exc:
                logger.debug("[HTTP] aclose failed name=%s err=%s", name, exc)
            _clients.pop(name, None)
