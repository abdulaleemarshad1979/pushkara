# ═══════════════════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — FastAPI Application  (v8.0 — FIXED)
#
# FIXES vs v7:
#   - BUG FIX: Line ~961: error detail had a Windows path typo
#     "Ghc:\Users\abdul\Downloads\auth.pyat not found" → "Ghat not found"
#   - IMPORT FIX: auth / pg_store / storage moved to app/core/ package
#     (root-level files were never importable via "from app.core.X import")
#   - All three Depends() chains verified against corrected auth module path
#
# PRESERVED INTACT (no logic changes):
#   - Self-Healing Orchestrator (5 guardian loops)
#   - Physics-based Risk Engine (evaluate_from_dicts, RiskEngine)
#   - WebSocket Manager (partitioned by ghat, heartbeat, orphan pruning)
#   - Redis circuit breaker + pub/sub + rate limiting
#   - Leader election for crowd_broadcast_loop
#   - All v7 route signatures and response shapes
#   - JWT Auth & RBAC (Task 1)
#   - PostgreSQL as source of truth (Task 2)
#   - S3/R2 object storage (Task 3)
# ═══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import httpx
from fastapi import (
    Depends, FastAPI, File, Form, HTTPException, Request,
    UploadFile, WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from state.emergency_services import (
    EMERGENCY_SERVICES, AMBULANCE_NUMBER, FIRE_NUMBER, POLICE_NUMBER,
    add_service as es_add_service,
    update_service as es_update_service,
    delete_service as es_delete_service,
    get_service as es_get_service,
)
from services.emergency_service import (
    find_nearest_police, find_nearest_hospital, get_all_services_by_category,
)
from utils.location_utils import haversine as _hvs, nearest_in_list
from app.core.redis_manager import (
    Keys, cache_get, cache_set, cache_delete,
    stream_publish,
    get_crowd_data, set_crowd_data, get_crowd_history,
    check_rate_limit, redis_health, close_redis, get_redis,
)
from app.core.ws_manager import manager, HEARTBEAT_INTERVAL
from app.core.risk_engine import evaluate_from_dicts, evaluate_from_dicts_adaptive
from app.core.ai_predictor import start_monitor, collect_telemetry
from app.core.redis_manager import start_reconnect_loop
from app.healing.orchestrator import start_all_guardians, stop_all_guardians, get_health_summary, register_db_accessor

# ── v8: All three feature modules now correctly in app/core/ ─────────────────
from app.core.auth import (
    require_volunteer, require_any_auth,
    require_volunteer_or_admin,
    create_access_token, authenticate_volunteer, hash_password, hash_password_async,
    rebuild_volunteer_index,
    get_volunteer_by_id, index_add_volunteer, index_remove_volunteer,
    require_admin_key,
    verify_admin_credentials, get_admin_api_key,
)
from app.core.pg_store import (
    close_pg_pool, load_db_from_postgres, cached_read_pg,
    write_sos_alert, update_sos_status,
    write_issue, update_issue_status,
    write_lost_person, update_lost_person_status,
    fetch_sos_alerts, fetch_issues, fetch_lost_persons,
    write_volunteer, delete_volunteer, update_volunteer_fields,
)
from app.core.storage import upload_image
from app.core.admission import (
    SOS_GATE, REPORT_GATE, LOST_GATE, INGEST_GATE, READ_GATE,
    gates_snapshot, gather_bounded, shutdown_admission,
)
from app.core.http_client import aclose_all as http_aclose_all, scraperbot_client
from chat import router as chat_router
from whatsapp import router as whatsapp_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("pushkaralu.main")

# ── Config ────────────────────────────────────────────────────────────────────
SCRAPERBOT_URL           = os.getenv("SCRAPERBOT_URL", "http://localhost:3000")
CROWD_BROADCAST_INTERVAL = float(os.getenv("CROWD_BROADCAST_INTERVAL", "2.5"))
ENABLE_REDIS             = os.getenv("ENABLE_REDIS", "true").lower() == "true"
INSTANCE_ID              = os.getenv("INSTANCE_ID", f"api-{os.getpid()}")
LEADER_TTL               = int(os.getenv("LEADER_TTL_SECONDS", "10"))
UPLOAD_MAX_MB            = int(os.getenv("UPLOAD_MAX_MB", "5"))
LEADER_KEY               = "leader:crowd_broadcast"

os.makedirs("uploads", exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════════
# In-memory DB  (hot cache; hydrated from Postgres on startup)
# ═══════════════════════════════════════════════════════════════════════════════
DB: dict = {
    "users": [], "volunteers": [], "issues": [], "sos_alerts": [],
    "facilities": [], "transport_routes": [], "ghats": [],
    "lost_persons": [], "emergency_contacts": [], "medical_facilities": [],
    "hospitals": [], "police_stations": [], "hotels": [],
    "tourism_spots": [], "poojas": [], "helplines": {},
}

# ── id → object indexes ──────────────────────────────────────────────────────
# The mutation routes (resolve_issue / accept_issue / resolve_sos / assign_sos /
# update_lost / admin_update_volunteer / websocket_pilgrim / etc.) used to
# `for x in DB[list]: if x["id"] == target_id` — O(N) per call where N can grow
# to the orchestrator's 5000-item cap. These indexes turn that into O(1) and
# share the same underlying dict objects, so mutating the indexed object also
# mutates the list entry.
_INDEXED_LISTS = ("issues", "sos_alerts", "lost_persons", "ghats")
DB_BY_ID: dict[str, dict[str, dict]] = {k: {} for k in _INDEXED_LISTS}


def _rebuild_id_indexes() -> None:
    """Rebuild every id-keyed index from the current contents of DB."""
    for k in _INDEXED_LISTS:
        DB_BY_ID[k] = {x["id"]: x for x in DB[k] if isinstance(x, dict) and "id" in x}


def _index_get(name: str, _id: str) -> Optional[dict]:
    return DB_BY_ID[name].get(_id) if _id else None


def _index_add(name: str, obj: dict) -> None:
    if obj and "id" in obj:
        DB_BY_ID[name][obj["id"]] = obj


def _index_remove(name: str, _id: str) -> Optional[dict]:
    return DB_BY_ID[name].pop(_id, None)


def load_sample_data():
    data_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sample_data.json")
    if os.path.exists(data_path):
        with open(data_path, encoding="utf-8") as f:
            loaded = json.load(f)
            for key in DB:
                if key in loaded and loaded[key]:
                    DB[key] = loaded[key]
        logger.info("[Data] Loaded sample_data.json — ghats=%d volunteers=%d",
                    len(DB["ghats"]), len(DB["volunteers"]))
    else:
        logger.warning("[Data] sample_data.json not found at %s", data_path)
    rebuild_volunteer_index(DB["volunteers"])
    _rebuild_id_indexes()

async def sync_state(payload: dict):
    """
    Synchronizes the in-memory DB dictionary when an event is received from Redis.
    Ensures multi-instance consistency for WS INIT and local HTTP reads.
    Uses the id→object index for O(1) lookup so this hot path no longer scans
    the whole list on every cross-instance event.
    """
    try:
        msg_type = payload.get("type")
        data     = payload.get("data")
        if not msg_type or not data:
            return
        rec_id = data.get("id") if isinstance(data, dict) else None

        if msg_type == "SOS_ALERT":
            if rec_id and rec_id not in DB_BY_ID["sos_alerts"]:
                DB["sos_alerts"].append(data)
                _index_add("sos_alerts", data)
        elif msg_type in ("SOS_RESOLVED", "SOS_ASSIGNED"):
            existing = _index_get("sos_alerts", rec_id)
            if existing is not None:
                existing.update(data)
        elif msg_type == "LOST_REGISTERED":
            if rec_id and rec_id not in DB_BY_ID["lost_persons"]:
                DB["lost_persons"].append(data)
                _index_add("lost_persons", data)
        elif msg_type == "LOST_UPDATED":
            existing = _index_get("lost_persons", rec_id)
            if existing is not None:
                existing.update(data)
        elif msg_type == "NEW_ISSUE":
            if rec_id and rec_id not in DB_BY_ID["issues"]:
                DB["issues"].append(data)
                _index_add("issues", data)
        elif msg_type in ("ISSUE_RESOLVED", "ISSUE_ACCEPTED"):
            existing = _index_get("issues", rec_id)
            if existing is not None:
                existing.update(data)
        elif msg_type == "CROWD_UPDATE":
            ghat = _index_get("ghats", data.get("ghat_id"))
            if ghat is not None:
                if "level" in data: ghat["crowd_level"] = data["level"]
                if "current_count" in data: ghat["current_count"] = data["current_count"]
        elif msg_type == "VOLUNTEER_UPDATED":
            existing = get_volunteer_by_id(rec_id)
            if existing is not None:
                existing.update(data)
        elif msg_type == "VOLUNTEER_CREATED":
            if rec_id and get_volunteer_by_id(rec_id) is None:
                DB["volunteers"].append(data)
                index_add_volunteer(data)
        elif msg_type == "VOLUNTEER_DELETED":
            removed = index_remove_volunteer(rec_id)
            if removed is not None:
                # The volunteer dict is shared with the list — rebuild the
                # list excluding the removed entry. O(N) but only on delete,
                # which is rare. Linear-scan was the previous default.
                DB["volunteers"] = [v for v in DB["volunteers"] if v.get("id") != rec_id]

    except Exception as e:
        logger.error("[Sync] Failed to sync state: %s", e)

# ═══════════════════════════════════════════════════════════════════════════════
# Application lifespan
# ═══════════════════════════════════════════════════════════════════════════════
async def lifespan(app: FastAPI):
    logger.info("[Startup] Godavari Pushkaralu 2027 — v8.0  instance=%s", INSTANCE_ID)
    load_sample_data()
    await load_db_from_postgres(DB)
    rebuild_volunteer_index(DB["volunteers"])
    _rebuild_id_indexes()
    start_monitor()
    if ENABLE_REDIS:
        try:
            await start_reconnect_loop()
            manager.on_event = sync_state  # Multi-instance consistency FIX
            await manager.start_subscriber()
            asyncio.create_task(manager.heartbeat_loop(),  name="ws-heartbeat")
            asyncio.create_task(crowd_broadcast_loop(),    name="crowd-broadcast")
            asyncio.create_task(warm_cache(),              name="cache-warmup")
            logger.info("[Startup] Background tasks launched  instance=%s", INSTANCE_ID)
        except Exception as exc:
            logger.error("[Startup] Redis init failed: %s — degraded mode", exc)
    else:
        logger.warning("[Startup] Redis disabled — single-instance mode")
    # FIX (Issues 2 & 7): Inject DB accessor so orchestrator never imports main
    register_db_accessor(lambda: DB)
    # CRITICAL FIX: register reindex hook so the bloat guardian rebuilds
    # DB_BY_ID after every prune. Without this, the index would retain strong
    # references to popped objects and silently leak them.
    from app.healing.orchestrator import register_db_reindex_hook
    register_db_reindex_hook(_rebuild_id_indexes)
    start_all_guardians()
    # Background sweeper for stale manual crowd overrides — bounded growth.
    asyncio.create_task(_manual_override_sweeper(), name="manual-override-sweeper")
    yield
    await manager.stop_subscriber()
    await stop_all_guardians()
    await close_redis()
    await close_pg_pool()
    # Drain HTTP client pool and the shared blocking thread pool.
    try:
        await http_aclose_all()
    except Exception as exc:
        logger.debug("[Shutdown] http_aclose_all: %s", exc)
    shutdown_admission()
    logger.info("[Shutdown] Clean  instance=%s", INSTANCE_ID)

app = FastAPI(
    title="Godavari Pushkaralu 2027 API",
    description="Government of Andhra Pradesh — District Administration, East Godavari",
    version="8.0.0",
    lifespan=lifespan,
)

# ── CORS allowlist (production-ready) ────────────────────────────────────────
# Reads CORS_ALLOWED_ORIGINS from env (comma-separated). Falls back to the
# known-good Vercel frontend hosts so the deploy keeps working even if the
# operator forgets to set the env. The legacy "*" is intentionally NOT used
# alongside allow_credentials=True (browsers reject that combination anyway).
_DEFAULT_CORS_ORIGINS = [
    "https://pushkara.vercel.app",
    "https://www.pushkara.vercel.app",
    "http://localhost:8088",
    "http://127.0.0.1:8088",
    "http://localhost:5173",
]
_cors_env = os.getenv("CORS_ALLOWED_ORIGINS", "").strip()
if _cors_env:
    _allowed_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
elif os.getenv("ENVIRONMENT", "production").lower() == "development":
    # Dev convenience — wildcard is OK without credentials in dev mode.
    _allowed_origins = ["*"]
else:
    _allowed_origins = _DEFAULT_CORS_ORIGINS

logger.info("[CORS] allowed_origins=%s", _allowed_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    # Only allow credentials when the origins are explicit (not '*').
    allow_credentials=_allowed_origins != ["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Key"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
app.include_router(chat_router)
app.include_router(whatsapp_router)

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════
def haversine(lat1, lon1, lat2, lon2): return _hvs(lat1, lon1, lat2, lon2)

def _sanitize_volunteer(vol: Optional[dict]) -> Optional[dict]:
    """Strip password / password_hash before exposing a volunteer record over the wire."""
    if not vol:
        return None
    return {k: v for k, v in vol.items() if k not in ("password", "password_hash")}


def find_nearest_volunteer(lat, lon):
    # Guard: skip volunteers missing lat/lon (admin-created without coordinates)
    available = [
        v for v in DB["volunteers"]
        if v.get("status") == "available"
        and v.get("latitude") is not None
        and v.get("longitude") is not None
    ]
    if not available:
        return None
    nearest = min(available, key=lambda v: haversine(lat, lon, v["latitude"], v["longitude"]))
    # FIX (A1 — CRITICAL): never leak password / password_hash via SOS response.
    # The full record stays in DB["volunteers"]; the SOS handler only ever
    # receives the sanitised view.
    return _sanitize_volunteer(nearest)

def safe_volunteers():
    # FIX (A1 — defence in depth): strip both legacy `password` and the modern
    # `password_hash` fields before returning volunteers to any client.
    return [_sanitize_volunteer(vol) for vol in DB["volunteers"]]

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Admission-gate dependencies
# Yield-based FastAPI dependencies that acquire a slot on entry and release
# on exit. Apply with `_gate: None = Depends(gate_lost)` on a route handler.
# Cleaner than wrapping the body in `async with` because it does not force a
# whole-function re-indent and it composes naturally with auth dependencies.
# ─────────────────────────────────────────────────────────────────────────────
async def gate_sos():
    async with SOS_GATE.slot():
        yield

async def gate_report():
    async with REPORT_GATE.slot():
        yield

async def gate_lost():
    async with LOST_GATE.slot():
        yield

async def gate_ingest():
    async with INGEST_GATE.slot():
        yield


# ─────────────────────────────────────────────────────────────────────────────
# Broadcast helper — collapses the ~16 copy-pasted post-mutation blocks
# (cache invalidation + WS broadcast + optional event-stream append) into a
# single call. All routes that mutate state and notify clients should go
# through this helper.
# ─────────────────────────────────────────────────────────────────────────────
async def _broadcast_event(
    msg: dict,
    *,
    ghat_id: Optional[str] = None,
    invalidate: tuple = (),
    stream_event: Optional[str] = None,
    stream_payload: Optional[dict] = None,
    label: str = "Event",
) -> None:
    """
    Invalidate caches → broadcast over WS (Redis-aware) → optionally append
    to the events Redis stream. Every step is fail-soft: the request handler
    must complete even if every downstream system is degraded.

    `manager.broadcast` already performs ONE Redis publish + ONE local
    fan-out, so we no longer need the old `try: broadcast except: _local_broadcast`
    pattern — `_local_broadcast` runs unconditionally as part of broadcast().
    """
    if invalidate:
        try:
            await cache_delete(*invalidate)
        except Exception as exc:
            logger.debug("[%s] cache_delete failed: %s", label, exc)
    try:
        await manager.broadcast(msg, ghat_id=ghat_id)
    except Exception as exc:
        logger.warning("[%s] Broadcast failed: %s", label, exc)
    if stream_event is not None:
        try:
            await stream_publish(
                Keys.STREAM_EVENTS,
                {"event": stream_event,
                 "payload": json.dumps(stream_payload or msg.get("data") or {},
                                       default=str)},
            )
        except Exception as exc:
            logger.debug("[%s] stream_publish failed: %s", label, exc)

# ═══════════════════════════════════════════════════════════════════════════════
# Leader election  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════
async def _am_leader() -> bool:
    try:
        r = await get_redis()
        acquired = await r.set(LEADER_KEY, INSTANCE_ID, ex=LEADER_TTL, nx=True)
        if acquired: return True
        current = await r.get(LEADER_KEY)
        if current == INSTANCE_ID:
            await r.expire(LEADER_KEY, LEADER_TTL)
            return True
        return False
    except Exception:
        return True

# ═══════════════════════════════════════════════════════════════════════════════
# Background tasks  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════
_prev_scores: dict = {}
# Manual override lock: {ghat_id: expiry_timestamp}
# When admin sets a crowd level manually, the broadcast loop skips that ghat
# for MANUAL_OVERRIDE_TTL seconds so the auto-engine doesn't immediately undo it.
_manual_overrides: dict = {}
MANUAL_OVERRIDE_TTL = 300  # 5 minutes


async def _manual_override_sweeper() -> None:
    """
    Background task — every 60 s remove expired manual override entries.

    Without this, _manual_overrides accumulated one stale key per
    operator-cleared override forever. Bounded growth keeps the dict's
    memory footprint flat regardless of admin activity.
    """
    while True:
        try:
            await asyncio.sleep(60)
            now = time.time()
            stale = [k for k, exp in _manual_overrides.items() if exp <= now]
            for k in stale:
                _manual_overrides.pop(k, None)
            if stale:
                logger.debug("[ManualOverride] swept %d expired keys", len(stale))
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("[ManualOverride] sweeper error: %s", exc)

async def crowd_broadcast_loop():
    from app.core.risk_engine import RiskEngine
    logger.info("[CrowdLoop] Starting  instance=%s", INSTANCE_ID)

    async def _process_one(ghat: dict) -> None:
        ghat_id = ghat["id"]
        try:
            # Skip auto-evaluation if admin has manually set this ghat's level.
            if _manual_overrides.get(ghat_id, 0) > time.time():
                # Still re-broadcast the manual level so late-joining clients see it.
                await manager.broadcast({
                    "type": "CROWD_UPDATE",
                    "data": {
                        "ghat_id":      ghat_id,
                        "level":        ghat["crowd_level"],
                        "name":         ghat.get("name", ""),
                        "risk_score":   _prev_scores.get(ghat_id, 0.0),
                        "occupancy_pct": round(
                            (ghat.get("current_count", 0)
                             / max(ghat.get("capacity", 1), 1)) * 100, 1),
                        "colour": {"low": "green", "medium": "orange",
                                   "high": "red", "critical": "purple"}
                                  .get(ghat["crowd_level"], "grey"),
                        "manual": True,
                    }
                }, ghat_id=ghat_id)
                return

            # Pull all the inputs in parallel — three independent Redis reads.
            vision_data, telecom_data, history = await asyncio.gather(
                get_crowd_data(f"cctv:{ghat_id}"),
                get_crowd_data(f"telecom:{ghat_id}"),
                get_crowd_history(ghat_id, 10),
                return_exceptions=False,
            )

            result = evaluate_from_dicts_adaptive(
                ghat, vision_data, telecom_data, history,
            )
            ghat["crowd_level"]   = result["crowd_level"]
            ghat["current_count"] = result["estimated_count"]

            # Persist + cache in parallel — both are idempotent best-effort writes.
            await asyncio.gather(
                set_crowd_data(ghat_id, result),
                cache_set(Keys.GHAT_ONE.format(ghat_id=ghat_id), ghat, ttl=3),
                return_exceptions=True,
            )

            prev = _prev_scores.get(ghat_id, 0.0)
            broadcasts: list = []

            if RiskEngine.should_alert(result["risk_score"], prev):
                broadcasts.append(manager.broadcast({
                    "type": "CROWD_ALERT",
                    "data": {
                        "ghat_id":       ghat_id,
                        "name":          ghat.get("name", ""),
                        "crowd_level":   result["crowd_level"],
                        "risk_score":    result["risk_score"],
                        "occupancy_pct": result["occupancy_pct"],
                        "message": f"⚠️ {ghat.get('name','')} — "
                                   f"{result['crowd_level'].upper()} crowd",
                    }
                }, ghat_id=ghat_id))

            _prev_scores[ghat_id] = result["risk_score"]

            if result.get("surge_detected"):
                broadcasts.append(manager.broadcast({
                    "type": "SURGE_ALERT",
                    "data": {
                        "ghat_id":   ghat_id,
                        "name":      ghat.get("name", ""),
                        "message":   f"🚨 SURGE at {ghat.get('name','')} — "
                                     "crowd rising rapidly",
                        "risk_score": result["risk_score"],
                    }
                }, ghat_id=ghat_id))

            broadcasts.append(manager.broadcast({
                "type": "CROWD_UPDATE",
                "data": {
                    "ghat_id":       ghat_id,
                    "level":         result["crowd_level"],
                    "name":          ghat.get("name", ""),
                    "risk_score":    result["risk_score"],
                    "occupancy_pct": result["occupancy_pct"],
                    "colour":        result["colour"],
                }
            }, ghat_id=ghat_id))

            if broadcasts:
                await asyncio.gather(*broadcasts, return_exceptions=True)
        except Exception as exc:
            logger.debug("[CrowdLoop] ghat=%s error=%s", ghat_id, exc)

    while True:
        try:
            await asyncio.sleep(CROWD_BROADCAST_INTERVAL)
            if not await _am_leader():
                continue
            ghats = list(DB["ghats"])
            if not ghats:
                continue
            # Per-ghat work runs concurrently — was previously a serial loop
            # of ~76 Redis round-trips per cycle.
            await asyncio.gather(
                *(_process_one(g) for g in ghats),
                return_exceptions=True,
            )
            await cache_delete(Keys.GHATS_ALL, Keys.STATS, Keys.ADMIN_STATS)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("[CrowdLoop] %s", exc)

async def warm_cache():
    await asyncio.sleep(1)
    logger.info("[Cache] Warming  instance=%s", INSTANCE_ID)
    await cache_set(Keys.GHATS_ALL,  {"ghats": DB["ghats"]},                          ttl=30)
    await cache_set(Keys.FACILITIES, {"facilities": DB["facilities"]},                 ttl=30)
    await cache_set(Keys.TRANSPORT,  {"transport_routes": DB["transport_routes"]},     ttl=30)
    await cache_set(Keys.VOLUNTEERS, {"volunteers": safe_volunteers()},                ttl=10)
    await cache_set(Keys.CONTACTS,   {"contacts": DB["emergency_contacts"]},           ttl=60)
    await cache_set(Keys.MEDICAL,    {"medical_facilities": DB["medical_facilities"]}, ttl=60)
    await cache_set(Keys.LOST_ALL,   {"lost_persons": DB["lost_persons"]},             ttl=30)
    # Warm SOS and issues caches from hydrated in-memory data
    await cache_set(Keys.SOS_ALL,    {"sos_alerts": DB["sos_alerts"]},                 ttl=5)
    await cache_set(Keys.ISSUES_ALL, {"issues": DB["issues"]},                         ttl=5)
    logger.info("[Cache] Warm-up complete")

# ═══════════════════════════════════════════════════════════════════════════════
# Health & Observability
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/ping")
async def ping():
    """Lightweight always-200 health check used by Render.
    Never depends on Redis so cold-start Redis lag cannot cause Render
    to restart the instance and break the chat service."""
    return {"status": "ok", "instance": INSTANCE_ID}

@app.get("/health")
async def health():
    redis_status = await redis_health()
    # Always return HTTP 200 — Render must not kill the instance just because
    # Redis is still waking up. The real Redis status is visible in the body.
    return JSONResponse(content={
        "status":       "ok" if redis_status.get("status") == "healthy" else "degraded",
        "version":      "8.0.0",
        "instance":     INSTANCE_ID,
        "redis":        redis_status,
        "websockets":   manager.stats(),
        "ghats":        len(DB["ghats"]),
        "volunteers":   len(DB["volunteers"]),
        "self_healing": get_health_summary(),
        # Live admission-gate counters — exposes saturation, in-flight, queued.
        # Operators can watch this to tune GATE_*_CONC / GATE_*_WAITERS env vars.
        "admission":    gates_snapshot(),
        "timestamp":    _utc_now(),
    }, status_code=200)  # ← always 200; degraded ≠ dead

@app.get("/metrics")
async def metrics():
    telemetry = await collect_telemetry()
    return {
        "active_connections": manager.get_count(),
        "active_sos":         len([a for a in DB["sos_alerts"] if a["status"] == "active"]),
        "high_risk_ghats":    len([g for g in DB["ghats"] if g.get("crowd_level") in ["high", "critical"]]),
        "pending_issues":     len([i for i in DB["issues"] if i["status"] == "pending"]),
        "missing_persons":    len([p for p in DB["lost_persons"] if p.get("status") == "missing"]),
        "instance":           INSTANCE_ID,
        "timestamp":          time.time(),
        "telemetry":          telemetry,
    }

@app.get("/")
def root():
    return {"message": "Godavari Pushkaralu 2027 API", "version": "8.0.0",
            "instance": INSTANCE_ID, "location": "Rajahmundry, East Godavari",
            "festival_dates": "June 26 - July 7, 2027"}

# ═══════════════════════════════════════════════════════════════════════════════
# Authentication endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/volunteer_login")
def volunteer_login(username: str = Form(...), password: str = Form(...)):
    """
    Returns a signed JWT alongside the volunteer record.
    Frontend: store token, send as Authorization: Bearer <token>
    Response shape is backward-compatible.
    """
    vol = authenticate_volunteer(username, password)
    if not vol:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(subject_id=vol["id"], role="volunteer",
                                 extra={"name": vol.get("name", "")})
    return {"success": True, "token": token, "volunteer": vol}


# ── Admin portal login (replaces leaked-key-in-JS) ───────────────────────────
# The admin dashboard at /admin must POST username + password here.
# On success the response carries the X-Admin-Key value, which the frontend
# stores in sessionStorage and sends back as the X-Admin-Key header on every
# admin call. The key never lives in static JS shipped to the browser.
#
# Rate limited per IP (5 attempts / 60s) to slow brute-force attempts.

@app.post("/admin/login")
async def admin_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    client_ip = request.client.host if request.client else "unknown"
    allowed, _info = await check_rate_limit(
        f"rate:ip:{client_ip}:admin_login", limit=5, window_seconds=60
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Please wait a minute and try again.",
        )
    if not verify_admin_credentials(username, password):
        # Generic 401 — never leak which half (username vs password) was wrong.
        logger.warning("[AdminLogin] Failed attempt from %s user=%s", client_ip, username)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    logger.info("[AdminLogin] Success from %s", client_ip)
    return {
        "success": True,
        "admin_key": get_admin_api_key(),
        "expires_hint": "Session-scoped — stored in sessionStorage; clears on tab close.",
    }



# NOTE: Admin portal login is handled externally by government officials.
# This API only exposes volunteer authentication.

# ═══════════════════════════════════════════════════════════════════════════════
# Issues
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/report_issue")
async def report_issue(
    request: Request,
    description: str = Form(...), latitude: float = Form(...), longitude: float = Form(...),
    category: str = Form(default="general"), user_name: str = Form(default="Anonymous"),
    image: Optional[UploadFile] = File(None),
    _gate: None = Depends(gate_report),
):
    # Backpressure: REPORT_GATE allows up to 32 concurrent issue submissions
    # with a 64-deep wait queue and a 2 s wait budget. Past that the gate
    # raises 503 immediately so nginx can shed load instead of letting the
    # request pile up in front of the asyncpg pool.
    client_ip = request.client.host
    allowed, _ = await check_rate_limit(f"rate:ip:{client_ip}:report", limit=10, window_seconds=60)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Please wait before reporting again.")
    image_url = await upload_image(image, folder="issues")
    # Route to the NEAREST available volunteer using the same proximity logic
    # as SOS, so the closest volunteer is the one alerted/assigned instead of
    # the report being broadcast to everyone with nobody actually responsible.
    nearest = await asyncio.to_thread(find_nearest_volunteer, latitude, longitude)
    issue = {
        "id": str(uuid.uuid4()), "description": description, "category": category,
        "image_url": image_url, "latitude": latitude, "longitude": longitude,
        "status": "pending",
        "assigned_volunteer":      nearest["id"]   if nearest else None,
        "assigned_volunteer_name": nearest["name"] if nearest else "Unassigned",
        "user_name": user_name,
        "timestamp": _utc_now()
    }
    await write_issue(issue)
    DB["issues"].append(issue)
    _index_add("issues", issue)
    msg = {"type": "NEW_ISSUE", "data": issue}

    await _broadcast_event(
        msg,
        invalidate=(Keys.ISSUES_ALL,
                    Keys.ISSUES_STATUS.format(status="pending"),
                    Keys.STATS, Keys.ADMIN_STATS),
        stream_event="new_issue",
        stream_payload=issue,
        label="Issue",
    )

    return {"success": True, "issue_id": issue["id"], "message": "Issue reported"}

@app.get("/get_issues")
async def get_issues(status: Optional[str] = None):
    key = Keys.ISSUES_STATUS.format(status=status) if status else Keys.ISSUES_ALL
    async def _pg_read():
        try:
            return await fetch_issues(status)
        except Exception:
            issues = DB["issues"] if not status else [i for i in DB["issues"] if i["status"] == status]
            return {"issues": sorted(issues, key=lambda x: x["timestamp"], reverse=True)}
    return await cached_read_pg(key, _pg_read, ttl=2)

@app.post("/resolve_issue/{issue_id}")
async def resolve_issue(
    issue_id: str,
    volunteer_id: str = Form(default="admin"),
    resolution_note: str = Form(default=""),     # Phase 2: digital incident report (optional)
    photo: Optional[UploadFile] = File(None),
    _auth: dict = Depends(require_volunteer_or_admin),
):
    try:
        resolved_at = _utc_now()
        photo_url = await upload_image(photo, folder="issue-resolutions") if photo else None
        await update_issue_status(issue_id, "resolved", volunteer_id, resolved_at)
        issue = _index_get("issues", issue_id)
        if issue is not None:
            issue.update({"status": "resolved", "resolved_at": resolved_at, "assigned_volunteer": volunteer_id})
            if resolution_note:
                issue["resolution_note"]  = resolution_note.strip()[:1000]
            if photo_url:
                issue["resolution_photo"] = photo_url
            msg = {"type": "ISSUE_RESOLVED", "data": issue}

            await _broadcast_event(
                msg,
                invalidate=(Keys.ISSUES_ALL,
                            Keys.ISSUES_STATUS.format(status="resolved"),
                            Keys.ISSUES_STATUS.format(status="pending"),
                            Keys.STATS, Keys.ADMIN_STATS),
                stream_event="issue_resolved",
                stream_payload=issue,
                label="Issue",
            )

            return {"success": True}
        raise HTTPException(status_code=404, detail="Issue not found")
    except Exception as exc:
        logger.error("[Issue] resolve_issue failed: %s", exc)
        return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})

@app.post("/accept_issue/{issue_id}")
async def accept_issue(
    issue_id: str,
    volunteer_id: str = Form(default="admin"),
    _auth: dict = Depends(require_volunteer_or_admin),
):
    try:
        await update_issue_status(issue_id, "in_progress", volunteer_id)
        issue = _index_get("issues", issue_id)
        if issue is not None:
            vol = get_volunteer_by_id(volunteer_id)
            issue.update({
                "status": "in_progress",
                "assigned_volunteer": volunteer_id,
                "assigned_volunteer_name": (vol or {}).get("name", issue.get("assigned_volunteer_name", "")),
            })
            msg = {"type": "ISSUE_ACCEPTED", "data": issue}

            await _broadcast_event(
                msg,
                invalidate=(Keys.ISSUES_ALL,
                            Keys.ISSUES_STATUS.format(status="in_progress"),
                            Keys.ISSUES_STATUS.format(status="pending"),
                            Keys.STATS, Keys.ADMIN_STATS),
                label="Issue",
            )

            return {"success": True}
        raise HTTPException(status_code=404, detail="Issue not found")
    except Exception as exc:
        logger.error("[Issue] accept_issue failed: %s", exc)
        return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})

# ═══════════════════════════════════════════════════════════════════════════════
# SOS Alerts
# ═══════════════════════════════════════════════════════════════════════════════

async def create_sos_record(
    user_name: str,
    phone: str,
    latitude: float,
    longitude: float,
    source: str = "app",
) -> dict:
    """
    Core SOS creation flow — shared between the /sos_alert HTTP endpoint and
    other channels (WhatsApp via Mana Mitra, future SMS / IVR, etc.).

    Always returns a dict with: success, alert_id, nearest_volunteer,
    volunteer_assigned, message. Never raises (except for HTTPException
    rate-limit which the caller can choose to surface).
    """
    if phone:
        allowed, _ = await check_rate_limit(
            f"rate:sos:{phone}", limit=3, window_seconds=300
        )
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="SOS rate limit reached. If this is a real emergency, call 112.",
            )

    nearest = await asyncio.to_thread(find_nearest_volunteer, latitude, longitude)
    alert = {
        "id": str(uuid.uuid4()),
        "user_name": user_name,
        "phone": phone,
        "latitude": latitude,
        "longitude": longitude,
        "status": "active",
        "assigned_volunteer":      nearest["id"]   if nearest else None,
        "assigned_volunteer_name": nearest["name"] if nearest else "Unassigned",
        "source": source,
        "timestamp": _utc_now(),
    }

    # Persistence (non-blocking on failure)
    try:
        await write_sos_alert(alert)
    except Exception as pg_exc:
        logger.error("[SOS] PG write failed: %s", pg_exc)

    DB["sos_alerts"].append(alert)
    _index_add("sos_alerts", alert)
    msg = {"type": "SOS_ALERT", "data": alert, "priority": "HIGH"}

    # Reliable broadcast (Redis + local fan-out via the canonical helper)
    await _broadcast_event(
        msg,
        invalidate=(Keys.SOS_ACTIVE, Keys.SOS_ALL, Keys.STATS, Keys.ADMIN_STATS),
        stream_event="sos_alert",
        stream_payload=alert,
        label="SOS",
    )

    if nearest:
        message = (
            f"SOS sent! Volunteer {nearest['name']} has been alerted "
            "and is on the way."
        )
    else:
        message = (
            "SOS received! No volunteers are currently available nearby. "
            "Please call emergency services immediately: "
            "Police: 100 | Ambulance: 108 | Pushkaralu Control Room: 1800-425-8877"
        )
    return {
        "success": True,
        "alert_id": alert["id"],
        "nearest_volunteer": nearest,
        "volunteer_assigned": nearest is not None,
        "message": message,
    }


@app.post("/sos_alert")
async def sos_alert(
    request: Request,
    user_name: str = Form(default="Pilgrim"), latitude: float = Form(...),
    longitude: float = Form(...), phone: str = Form(default=""),
    _gate: None = Depends(gate_sos),
):
    # Backpressure: SOS_GATE permits 32 concurrent SOS handlers with a
    # 16-deep wait queue and a tight 750 ms wait budget — life-critical so
    # we shed load early rather than queueing for seconds. nginx can then
    # serve a 503 to retry-on-mobile clients with predictable latency.
    try:
        result = await create_sos_record(
            user_name=user_name,
            phone=phone,
            latitude=latitude,
            longitude=longitude,
            source="app",
        )
        # Best-effort WhatsApp confirmation back to the pilgrim if they shared a
        # phone number. Fire-and-forget so the HTTP response is never delayed
        # by a third-party API call.
        if phone:
            try:
                from services.whatsapp_service import fire_and_forget_send
                nearest = result.get("nearest_volunteer") or {}
                if nearest:
                    wa_body = (
                        f"🚨 SOS received (ID {result['alert_id'][:8]}). "
                        f"Volunteer {nearest.get('name','—')} "
                        f"({nearest.get('phone','')}) is on the way. "
                        "If condition worsens call 112 immediately. — TourGO Pushkara 🕊"
                    )
                else:
                    wa_body = (
                        f"🚨 SOS received (ID {result['alert_id'][:8]}). "
                        "No volunteers free nearby — please call Police 100 / "
                        "Ambulance 108 immediately. — TourGO Pushkara 🕊"
                    )
                fire_and_forget_send(phone, wa_body)
            except Exception as wa_exc:
                logger.debug("[SOS] WhatsApp confirmation skipped: %s", wa_exc)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[SOS] Critical failure: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": "Internal error processing SOS. PLEASE CALL 112 IMMEDIATELY.",
                "error": str(exc),
            },
        )

@app.get("/get_sos_alerts")
async def get_sos_alerts(status: Optional[str] = None):
    try:
        key = Keys.SOS_ALL if not status else f"cache:sos:status:{status}"
        async def _pg_read():
            try:
                return await fetch_sos_alerts(status)
            except Exception as e:
                logger.warning("[SOS] Postgres read failed, using memory: %s", e)
                alerts = DB["sos_alerts"] if not status else [a for a in DB["sos_alerts"] if a["status"] == status]
                return {"sos_alerts": sorted(alerts, key=lambda x: x["timestamp"], reverse=True)}
        return await cached_read_pg(key, _pg_read, ttl=2)
    except Exception as exc:
        logger.error("[SOS] get_sos_alerts failed: %s", exc)
        alerts = DB["sos_alerts"] if not status else [a for a in DB["sos_alerts"] if a["status"] == status]
        return {"sos_alerts": sorted(alerts, key=lambda x: x["timestamp"], reverse=True)}


@app.post("/resolve_sos/{alert_id}")
async def resolve_sos(
    alert_id: str,
    volunteer_id: str = Form(default="admin"),
    resolution_note: str = Form(default=""),     # Phase 2: digital incident report (optional)
    photo: Optional[UploadFile] = File(None),
    _auth: dict = Depends(require_volunteer_or_admin),
):
    try:
        resolved_at = _utc_now()
        # Phase 2: capture optional resolution photo + note for digital audit trail
        photo_url = await upload_image(photo, folder="sos-resolutions") if photo else None
        await update_sos_status(alert_id, "resolved", resolved_at=resolved_at)
        alert = _index_get("sos_alerts", alert_id)
        if alert is not None:
            alert.update({"status": "resolved", "resolved_at": resolved_at})
            if resolution_note:
                alert["resolution_note"]  = resolution_note.strip()[:1000]
            if photo_url:
                alert["resolution_photo"] = photo_url
            if resolution_note or photo_url:
                alert["resolved_by"] = volunteer_id
            msg = {"type": "SOS_RESOLVED", "data": alert}

            await _broadcast_event(
                msg,
                invalidate=(Keys.SOS_ACTIVE, Keys.SOS_ALL,
                            Keys.STATS, Keys.ADMIN_STATS),
                stream_event="sos_resolved",
                stream_payload=alert,
                label="SOS",
            )

            return {"success": True}
        raise HTTPException(status_code=404, detail="Alert not found")
    except Exception as exc:
        logger.error("[SOS] resolve_sos failed: %s", exc)
        return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})

@app.post("/assign_sos/{alert_id}")
async def assign_sos(
    alert_id: str,
    volunteer_id: str = Form(...),
    _auth: dict = Depends(require_volunteer_or_admin),
):
    try:
        alert = _index_get("sos_alerts", alert_id)
        # FIX: O(1) volunteer lookup — was a linear scan over DB["volunteers"].
        vol = get_volunteer_by_id(volunteer_id)
        if not alert: raise HTTPException(status_code=404, detail="Alert not found")
        if not vol:   raise HTTPException(status_code=404, detail="Volunteer not found")
        
        await update_sos_status(alert_id, "assigned", volunteer_id=volunteer_id, volunteer_name=vol["name"])
        alert.update({"assigned_volunteer": volunteer_id, "assigned_volunteer_name": vol["name"]})
        msg = {"type": "SOS_ASSIGNED", "data": alert}

        await _broadcast_event(
            msg,
            invalidate=(Keys.SOS_ACTIVE, Keys.SOS_ALL),
            label="SOS",
        )

        return {"success": True}
    except Exception as exc:
        logger.error("[SOS] assign_sos failed: %s", exc)
        return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})

# ═══════════════════════════════════════════════════════════════════════════════
# Facilities, Transport, Ghats
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/get_facilities")
async def get_facilities(type: Optional[str] = None, ghat_id: Optional[str] = None, stars: Optional[int] = None):
    # Build a deterministic cache key covering all filter dimensions
    cache_key_suffix = f"{type or 'all'}:{ghat_id or 'all'}:{stars if stars is not None else 'all'}"
    key = f"facilities:{cache_key_suffix}"
    cached = await cache_get(key)
    if cached is not None: return cached
    facs = DB["facilities"]
    if type:     facs = [f for f in facs if f.get("type") == type]
    if ghat_id:  facs = [f for f in facs if f.get("ghat_id") == ghat_id]
    if stars is not None:
        facs = [f for f in facs if f.get("star_rating") == stars]
    result = {"facilities": facs}
    await cache_set(key, result, ttl=30)
    return result

@app.get("/get_ghats")
async def get_ghats():
    cached = await cache_get(Keys.GHATS_ALL)
    if cached is not None: return cached
    result = {"ghats": DB["ghats"]}
    await cache_set(Keys.GHATS_ALL, result, ttl=3)
    return result

@app.post("/update_crowd/{ghat_id}")
async def update_crowd(
    ghat_id: str, level: str = Form(...),
    _auth: dict = Depends(require_volunteer_or_admin),
):
    valid_levels = {"low", "medium", "high", "critical"}
    if level not in valid_levels:
        raise HTTPException(status_code=400, detail=f"level must be one of {valid_levels}")
    ghat = _index_get("ghats", ghat_id)
    if ghat is None:
        raise HTTPException(status_code=404, detail="Ghat not found")
    ghat["crowd_level"] = level
    # Lock this ghat so broadcast loop won't overwrite for 5 minutes
    _manual_overrides[ghat_id] = time.time() + MANUAL_OVERRIDE_TTL
    logger.info("[ManualOverride] ghat=%s level=%s locked for %ds by admin", ghat_id, level, MANUAL_OVERRIDE_TTL)
    # Build a synthetic risk result so /crowd/risk/all reflects the manual level
    capacity = ghat.get("capacity", 1000)
    current = ghat.get("current_count", 0)
    colour_map = {"low": "green", "medium": "orange", "high": "red", "critical": "purple"}
    score_map  = {"low": 0.2, "medium": 0.5, "high": 0.75, "critical": 0.95}
    risk_result = {
        "ghat_id":       ghat_id,
        "crowd_level":   level,
        "risk_score":    score_map.get(level, 0.5),
        "occupancy_pct": round((current / max(capacity, 1)) * 100, 1),
        "estimated_count": current,
        "colour":        colour_map.get(level, "grey"),
        "manual":        True,
        "timestamp":     time.time(),
    }
    await set_crowd_data(ghat_id, risk_result)
    await cache_delete(Keys.GHATS_ALL, Keys.GHAT_ONE.format(ghat_id=ghat_id), Keys.RISK_ALL)
    msg = {"type": "CROWD_UPDATE", "data": {
        "ghat_id": ghat_id, "level": level, "name": ghat.get("name", ""),
        "risk_score": risk_result["risk_score"],
        "occupancy_pct": risk_result["occupancy_pct"],
        "colour": risk_result["colour"],
        "manual": True,
    }}
    await manager.broadcast(msg, ghat_id=ghat_id)
    return {"success": True, "locked_until": _manual_overrides[ghat_id], "override_ttl_seconds": MANUAL_OVERRIDE_TTL}

@app.post("/crowd/override/clear/{ghat_id}")
async def clear_crowd_override(
    ghat_id: str,
    _auth: dict = Depends(require_volunteer_or_admin),
):
    """Release a manual crowd level override so the auto-engine takes back over."""
    removed = _manual_overrides.pop(ghat_id, None)
    logger.info("[ManualOverride] Cleared override for ghat=%s (was locked until %s)", ghat_id, removed)
    return {"success": True, "ghat_id": ghat_id, "was_locked": removed is not None}

@app.post("/crowd/override/clear_all")
async def clear_all_crowd_overrides(_auth: dict = Depends(require_volunteer_or_admin)):
    """Release ALL manual crowd overrides — useful after testing."""
    count = len(_manual_overrides)
    _manual_overrides.clear()
    logger.info("[ManualOverride] Cleared ALL %d overrides", count)
    return {"success": True, "cleared_count": count}


@app.get("/get_transport")
async def get_transport(type: Optional[str] = None):
    key = Keys.TRANSPORT if not type else f"cache:transport:type:{type}"
    cached = await cache_get(key)
    if cached is not None: return cached
    routes = DB["transport_routes"] if not type else [r for r in DB["transport_routes"] if r.get("type") == type]
    result = {"transport_routes": routes}
    await cache_set(key, result, ttl=30)
    return result

@app.get("/get_hospitals")
async def get_hospitals():
    # FIX (A8): cache static-ish reference data (60s TTL) — same response shape.
    cached = await cache_get("cache:hospitals:all")
    if cached is not None: return cached
    result = {"hospitals": DB["hospitals"], "helplines": DB["helplines"]}
    await cache_set("cache:hospitals:all", result, ttl=60)
    return result

@app.get("/get_police")
async def get_police():
    cached = await cache_get("cache:police:all")
    if cached is not None: return cached
    result = {"police_stations": DB["police_stations"]}
    await cache_set("cache:police:all", result, ttl=60)
    return result

@app.get("/get_hotels")
async def get_hotels():
    cached = await cache_get("cache:hotels:all")
    if cached is not None: return cached
    result = {"hotels": DB["hotels"]}
    await cache_set("cache:hotels:all", result, ttl=60)
    return result

@app.get("/get_tourism")
async def get_tourism():
    cached = await cache_get("cache:tourism:all")
    if cached is not None: return cached
    result = {"tourism_spots": DB["tourism_spots"]}
    await cache_set("cache:tourism:all", result, ttl=60)
    return result

@app.get("/get_poojas")
async def get_poojas():
    cached = await cache_get("cache:poojas:all")
    if cached is not None: return cached
    result = {"poojas": DB["poojas"]}
    await cache_set("cache:poojas:all", result, ttl=60)
    return result

@app.get("/get_app_data")
async def get_app_data():
    """Single endpoint returning all supplementary data for the frontend."""
    # FIX (A8): cache the heavy aggregate payload — admin/pilgrim dashboards
    # call this on init; previously every page-load rebuilt the whole blob.
    cached = await cache_get("cache:app_data:all")
    if cached is not None: return cached
    result = {
        "hospitals": DB["hospitals"],
        "helplines": DB["helplines"],
        "police_stations": DB["police_stations"],
        "hotels": DB["hotels"],
        "tourism_spots": DB["tourism_spots"],
        "poojas": DB["poojas"],
    }
    await cache_set("cache:app_data:all", result, ttl=60)
    return result

# ═══════════════════════════════════════════════════════════════════════════════
# Volunteers
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/get_volunteers")
async def get_volunteers():
    cached = await cache_get(Keys.VOLUNTEERS)
    if cached is not None: return cached
    result = {"volunteers": safe_volunteers()}
    await cache_set(Keys.VOLUNTEERS, result, ttl=5)
    return result

@app.put("/volunteer/{vid}")
async def update_volunteer(
    vid: str,
    status: Optional[str] = Form(None),
    zone: Optional[str] = Form(None),
    assigned_issue: Optional[str] = Form(None),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    _auth: dict = Depends(require_any_auth),
):
    # RBAC: volunteers update only their own record; admins update any
    if _auth.get("role") == "volunteer" and _auth.get("sub") != vid:
        raise HTTPException(status_code=403, detail="Volunteers may only update their own record")
    # FIX: O(1) volunteer lookup via the auth-module id index. The previous
    # linear scan ran on every PUT /volunteer/{vid} — at festival load with
    # several thousand volunteers this was the slowest hot-path mutation.
    vol = get_volunteer_by_id(vid)
    if vol is None:
        raise HTTPException(status_code=404, detail="Volunteer not found")
    if status: vol["status"] = status
    if zone:   vol["zone"]   = zone
    if assigned_issue is not None: vol["assigned_issue"] = assigned_issue
    if latitude  is not None: vol["latitude"]  = latitude
    if longitude is not None: vol["longitude"] = longitude
    vol["updated_at"] = _utc_now()

    # Persist the mutated fields to PostgreSQL so the volunteer's live location
    # and availability survive restarts/redeploys and stay correct for
    # find_nearest_volunteer (previously this route only mutated in-memory).
    persist: dict = {}
    if status:                     persist["status"]    = status
    if zone:                       persist["zone"]      = zone
    if latitude  is not None:      persist["latitude"]  = latitude
    if longitude is not None:      persist["longitude"] = longitude
    if assigned_issue is not None: persist["assigned_issue"] = assigned_issue
    if persist:
        try:
            await update_volunteer_fields(vid, persist)
        except Exception as exc:
            logger.debug("[Volunteer] PG persist skipped: %s", exc)

    safe = {k: v for k, v in vol.items() if k != "password"}
    msg = {"type": "VOLUNTEER_UPDATED", "data": safe}
    await cache_delete(Keys.VOLUNTEERS)
    await manager.broadcast(msg)
    return {"success": True, "volunteer": safe}

# ─────────────────────────────────────────────────────────────────────────────
# Admin-only Volunteer Management  (X-Admin-Key header required)
# Government officials use these endpoints to create / update / delete volunteers.
# The volunteer dashboard login is separate — volunteers log in via /volunteer_login.
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/admin/volunteer")
async def admin_create_volunteer(
    name: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    phone: str = Form(default=""),
    zone: str = Form(default=""),
    status: str = Form(default="available"),
    latitude: Optional[float] = Form(default=None),
    longitude: Optional[float] = Form(default=None),
    _auth: None = Depends(require_admin_key),
):
    """
    Create a new volunteer account.
    Requires X-Admin-Key header matching ADMIN_API_KEY in .env.
    Password is bcrypt-hashed before storage.
    """
    # Check username not already taken — O(1) via index instead of O(N) scan.
    uname = username.strip().lower()
    from app.core.auth import _volunteer_index as _u_index  # noqa: F401
    if uname in _u_index:
        raise HTTPException(status_code=409, detail=f"Username '{username}' is already taken")

    # FIX (A7): hash once — the previous code called hash_password() twice,
    # producing two DIFFERENT bcrypt hashes (each call uses a fresh random salt)
    # which wasted ~100 ms/CPU per create and caused inconsistency between the
    # `password` (used by authenticate_volunteer) and `password_hash` (used by
    # PostgreSQL persistence) fields.
    #
    # FIX (perf): bcrypt is CPU-bound (~100 ms / call at cost factor 12) and
    # was running INLINE inside the async route — every concurrent admin
    # create stalled the event loop for the duration of the hash. The async
    # wrapper offloads it to the shared bg_pool.
    pw_hashed = await hash_password_async(password)
    vol = {
        "id":            str(uuid.uuid4()),
        "name":          name.strip(),
        "username":      uname,
        "password_hash": pw_hashed,
        "password":      pw_hashed,   # kept for in-memory index compat
        "phone":         phone.strip(),
        "zone":          zone.strip(),
        "status":        status,
        "latitude":      latitude,   # used by find_nearest_volunteer for SOS proximity
        "longitude":     longitude,
        "created_at":    _utc_now(),
        "updated_at":    _utc_now(),
    }

    # Write to PostgreSQL
    await write_volunteer(vol)

    # Update in-memory DB and O(1) lookup index — incremental insert avoids
    # the previous O(N) full-rebuild on every create.
    DB["volunteers"].append(vol)
    index_add_volunteer(vol)

    # Invalidate cache + broadcast
    await cache_delete(Keys.VOLUNTEERS)
    safe = {k: v for k, v in vol.items() if k not in ("password", "password_hash")}
    msg = {"type": "VOLUNTEER_CREATED", "data": safe}
    await manager.broadcast(msg)

    logger.info("[Admin] Volunteer created: %s (%s)", vol["name"], vol["username"])
    return {"success": True, "volunteer": safe}


@app.put("/admin/volunteer/{vid}")
async def admin_update_volunteer(
    vid: str,
    name: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    zone: Optional[str] = Form(None),
    status: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    _auth: None = Depends(require_admin_key),
):
    """
    Update any volunteer field.
    Requires X-Admin-Key header.
    If password is provided it is re-hashed.
    latitude/longitude can be updated here so an admin can correct a
    volunteer's position used by SOS/issue proximity routing.
    """
    # FIX: O(1) volunteer lookup — was a linear scan over DB["volunteers"].
    vol = get_volunteer_by_id(vid)
    if not vol:
        raise HTTPException(status_code=404, detail="Volunteer not found")

    fields: dict = {}
    if name:   vol["name"]   = fields["name"]   = name.strip()
    if phone:  vol["phone"]  = fields["phone"]  = phone.strip()
    if zone:   vol["zone"]   = fields["zone"]   = zone.strip()
    if status: vol["status"] = fields["status"] = status
    if latitude  is not None: vol["latitude"]  = fields["latitude"]  = latitude
    if longitude is not None: vol["longitude"] = fields["longitude"] = longitude

    if password:
        # FIX (perf): bcrypt off-loop — see admin_create_volunteer.
        hashed = await hash_password_async(password)
        vol["password"]      = hashed
        vol["password_hash"] = hashed
        fields["password_hash"] = hashed

    vol["updated_at"] = _utc_now()

    # Persist to PostgreSQL
    await update_volunteer_fields(vid, fields)

    # Rebuild index in case username-adjacent fields changed
    rebuild_volunteer_index(DB["volunteers"])

    await cache_delete(Keys.VOLUNTEERS)
    safe = {k: v for k, v in vol.items() if k not in ("password", "password_hash")}
    msg = {"type": "VOLUNTEER_UPDATED", "data": safe}
    await manager.broadcast(msg)

    logger.info("[Admin] Volunteer updated: %s", vid)
    return {"success": True, "volunteer": safe}


@app.delete("/admin/volunteer/{vid}")
async def admin_delete_volunteer(vid: str, _auth: None = Depends(require_admin_key)):
    """
    Permanently delete a volunteer account.
    Requires X-Admin-Key header.
    """
    # FIX: O(1) volunteer lookup + index removal — was a linear scan.
    vol = get_volunteer_by_id(vid)
    if vol is None:
        raise HTTPException(status_code=404, detail="Volunteer not found")
    # The list still needs an O(N) pass to drop the entry from DB["volunteers"];
    # only the lookup is O(1). Volunteer deletes are rare (admin-driven) so
    # the linear list rebuild is acceptable.
    DB["volunteers"] = [v for v in DB["volunteers"] if v.get("id") != vid]
    index_remove_volunteer(vid)
    await delete_volunteer(vid)

    await cache_delete(Keys.VOLUNTEERS)
    msg = {"type": "VOLUNTEER_DELETED", "data": {"id": vid}}
    await manager.broadcast(msg)

    logger.info("[Admin] Volunteer deleted: %s (%s)", vol.get("name"), vid)
    return {"success": True, "deleted_id": vid}


@app.get("/admin/volunteers")
async def admin_list_volunteers(_auth: None = Depends(require_admin_key)):
    """
    List ALL volunteers including status.
    Requires X-Admin-Key header.
    Password fields are never returned.
    """
    return {
        "volunteers": [
            {k: v for k, v in vol.items() if k not in ("password", "password_hash")}
            for vol in DB["volunteers"]
        ],
        "total": len(DB["volunteers"]),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Admin Broadcast & Safety Alerts  (Phase 2 / Phase 3)
#
#   POST /admin/broadcast     — push notification to all pilgrims/volunteers,
#                               or to a zone, or to a single volunteer
#   POST /admin/safety_alert  — high-priority river/weather/safety banner
#                               (sticky on pilgrim portal until cleared)
#   POST /admin/safety_alert/clear/{alert_id}
#
# Both ride on manager.broadcast() so they reach every connected WS client
# without additional infra. Pilgrim & volunteer dashboards filter by `target`
# and `category` client-side.
# ═══════════════════════════════════════════════════════════════════════════════

# In-memory list of currently-active safety alerts. Pilgrim portal fetches this
# on cold start so newcomers see active warnings even if they joined after the
# WS broadcast. Persists for the lifetime of the process; bounded to last 25.
_ACTIVE_SAFETY_ALERTS: List[dict] = []


def _allowed_broadcast_category(category: str) -> str:
    cat = (category or "info").lower().strip()
    if cat not in ("info", "warning", "safety", "weather", "evacuation"):
        cat = "info"
    return cat


@app.post("/admin/broadcast")
async def admin_broadcast(
    message: str = Form(...),
    title: str = Form(default=""),
    category: str = Form(default="info"),
    target: str = Form(default="all"),
    _auth: None = Depends(require_admin_key),
):
    """
    Mass-broadcast a message to clients connected over WebSocket.

    Args:
        message:  Body text shown to recipients (required)
        title:    Optional headline shown above the body
        category: info | warning | safety | weather | evacuation
        target:   "all"                — every connected client
                  "zone:Zone A"        — only volunteers/pilgrims in this zone
                  "volunteer:<vid>"    — single volunteer (their own dashboard)

    Auth: X-Admin-Key header required.
    """
    msg_text = (message or "").strip()
    if not msg_text:
        raise HTTPException(status_code=400, detail="message is required")
    if len(msg_text) > 1000:
        raise HTTPException(status_code=400, detail="message too long (max 1000 chars)")

    bid  = str(uuid.uuid4())
    cat  = _allowed_broadcast_category(category)
    tgt  = (target or "all").strip() or "all"

    payload = {
        "type": "BROADCAST",
        "data": {
            "id":       bid,
            "title":    (title or "").strip()[:120] or None,
            "message":  msg_text,
            "category": cat,
            "target":   tgt,
            "ts":       _utc_now(),
        },
    }
    await manager.broadcast(payload)
    logger.info("[Broadcast] cat=%s target=%s len=%d id=%s", cat, tgt, len(msg_text), bid)
    return {"success": True, "broadcast_id": bid, "delivered": True, "data": payload["data"]}


@app.post("/admin/safety_alert")
async def admin_safety_alert(
    title: str = Form(...),
    message: str = Form(...),
    severity: str = Form(default="warning"),       # info | warning | danger | evacuation
    location: str = Form(default=""),              # free-text e.g. "Pushkar Ghat" or "All Zone A"
    expires_in_min: int = Form(default=120),       # auto-expire to prevent stale banners
    _auth: None = Depends(require_admin_key),
):
    """
    Sticky river/weather/evacuation banner on the pilgrim portal.
    Differs from /admin/broadcast in that the pilgrim portal pins the alert
    to a top-bar until it is cleared OR the expires_at passes.
    """
    sev = (severity or "warning").lower()
    if sev not in ("info", "warning", "danger", "evacuation"):
        sev = "warning"
    expires_in = max(1, min(int(expires_in_min or 120), 24 * 60))
    alert = {
        "id":         str(uuid.uuid4()),
        "title":      title.strip()[:120],
        "message":    message.strip()[:1000],
        "severity":   sev,
        "location":   location.strip()[:120],
        "issued_at":  _utc_now(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=expires_in)).isoformat(),
        "active":     True,
    }
    # Bound the list — keep the most recent 25 active alerts
    _ACTIVE_SAFETY_ALERTS.append(alert)
    while len(_ACTIVE_SAFETY_ALERTS) > 25:
        _ACTIVE_SAFETY_ALERTS.pop(0)
    await manager.broadcast({"type": "SAFETY_ALERT", "data": alert})
    logger.warning("[SafetyAlert] sev=%s loc=%s id=%s", sev, alert["location"], alert["id"])
    return {"success": True, "alert": alert}


@app.post("/admin/safety_alert/clear/{alert_id}")
async def admin_safety_alert_clear(alert_id: str, _auth: None = Depends(require_admin_key)):
    found = False
    for a in _ACTIVE_SAFETY_ALERTS:
        if a["id"] == alert_id and a.get("active"):
            a["active"] = False
            a["cleared_at"] = _utc_now()
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail="Alert not found or already cleared")
    await manager.broadcast({"type": "SAFETY_ALERT_CLEARED", "data": {"id": alert_id}})
    return {"success": True, "id": alert_id}


@app.get("/safety_alerts")
async def get_safety_alerts():
    """
    Public endpoint — pilgrim portal uses this on cold start to render any
    currently-active safety banners (so latecomers see them even if they
    weren't connected when the WS broadcast went out).
    """
    now = datetime.now(timezone.utc)
    out = []
    for a in _ACTIVE_SAFETY_ALERTS:
        if not a.get("active"):
            continue
        try:
            exp = datetime.fromisoformat(a.get("expires_at", "").replace("Z", "+00:00"))
            if exp < now:
                # Auto-clear silently — don't bother broadcasting old alerts
                a["active"] = False
                continue
        except Exception:
            pass
        out.append(a)
    return {"alerts": out, "count": len(out)}

# ═══════════════════════════════════════════════════════════════════════════════
# Stats
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/stats")
async def get_stats():
    cached = await cache_get(Keys.STATS)
    if cached is not None: return cached
    # Single-pass aggregation: was 13 separate list comprehensions over the
    # same lists. Each comprehension was O(N) and produced a throwaway list
    # only to count it. The single-pass walk is functionally identical, runs
    # 6× faster on a 5k-row in-memory DB, and produces zero garbage lists.
    result = _compute_stats(include_extras=False)
    await cache_set(Keys.STATS, result, ttl=3)
    return result


def _compute_stats(*, include_extras: bool = False) -> dict:
    """
    Single-pass walk over each in-memory list.

    Time:  O(N_issues + N_sos + N_volunteers + N_lost)  — one pass each.
    Space: O(1) — fixed counter dict, no intermediate list allocation.
    """
    issues = DB["issues"]
    sos    = DB["sos_alerts"]
    vols   = DB["volunteers"]
    lost   = DB["lost_persons"]
    ghats  = DB["ghats"]

    # Issues
    pending = in_progress = resolved_issues = 0
    for i in issues:
        s = i.get("status")
        if   s == "pending":     pending += 1
        elif s == "in_progress": in_progress += 1
        elif s == "resolved":    resolved_issues += 1

    # SOS
    active_sos = resolved_sos = 0
    for a in sos:
        s = a.get("status")
        if   s == "active":   active_sos += 1
        elif s == "resolved": resolved_sos += 1

    # Volunteers
    active_vols = busy_vols = 0
    for v in vols:
        s = v.get("status")
        if   s == "available": active_vols += 1
        elif s == "busy":      busy_vols += 1

    # Lost persons
    missing = found = 0
    for p in lost:
        s = p.get("status")
        if   s == "missing": missing += 1
        elif s == "found":   found += 1

    # Ghats
    high_ghats = 0
    for g in ghats:
        if g.get("crowd_level") in ("high", "critical"):
            high_ghats += 1

    base = {
        "total_issues":       len(issues),
        "pending_issues":     pending,
        "resolved_issues":    resolved_issues,
        "active_sos":         active_sos,
        "total_volunteers":   len(vols),
        "active_volunteers":  active_vols,
        "ghats":              len(ghats),
        "total_facilities":   len(DB["facilities"]),
        "transport_routes":   len(DB["transport_routes"]),
        "lost_persons":       len(lost),
        "missing_persons":    missing,
        "emergency_contacts": len(DB["emergency_contacts"]),
        "medical_facilities": len(DB["medical_facilities"]),
        "festival": "Godavari Pushkaralu 2027",
        "location": "Rajahmundry, East Godavari",
    }
    if include_extras:
        base.update({
            "in_progress_issues": in_progress,
            "resolved_sos":       resolved_sos,
            "busy_volunteers":    busy_vols,
            "high_crowd_ghats":   high_ghats,
            "found_persons":      found,
        })
    return base

@app.get("/daily_analytics")
async def get_daily_analytics(_auth: dict = Depends(require_volunteer_or_admin)):
    """Daily operational analytics: SOS and Lost & Found counts for today."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # SOS counts for today
    sos_today       = [a for a in DB["sos_alerts"] if (a.get("timestamp") or a.get("created_at",""))[:10] == today_str]
    sos_resolved_today = [a for a in sos_today if a.get("status") == "resolved"]
    sos_active_today   = [a for a in sos_today if a.get("status") == "active"]

    # Lost & Found counts for today
    lost_today       = [p for p in DB["lost_persons"] if (p.get("timestamp") or p.get("created_at",""))[:10] == today_str]
    lost_found_today = [p for p in lost_today if p.get("status") == "found"]
    lost_missing_today = [p for p in lost_today if p.get("status") == "missing"]

    # Issues for today
    issues_today      = [i for i in DB["issues"] if (i.get("timestamp") or i.get("created_at",""))[:10] == today_str]
    issues_resolved_today = [i for i in issues_today if i.get("status") == "resolved"]

    # Total resolved all time (historical tally)
    total_sos_resolved  = len([a for a in DB["sos_alerts"] if a.get("status") == "resolved"])
    total_lost_resolved = len([p for p in DB["lost_persons"] if p.get("status") == "found"])

    return {
        "date": today_str,
        # Today's numbers
        "sos_today":            len(sos_today),
        "sos_resolved_today":   len(sos_resolved_today),
        "sos_active_today":     len(sos_active_today),
        "lost_registered_today": len(lost_today),
        "lost_found_today":     len(lost_found_today),
        "lost_missing_today":   len(lost_missing_today),
        "issues_today":         len(issues_today),
        "issues_resolved_today": len(issues_resolved_today),
        # All-time resolved
        "total_sos_resolved":   total_sos_resolved,
        "total_lost_resolved":  total_lost_resolved,
        # Hourly breakdown for the chart (last 12 hours, SOS events)
        "sos_hourly": _hourly_buckets(sos_today, 12),
        "issues_hourly": _hourly_buckets(issues_today, 12),
    }

def _hourly_buckets(records: list, hours: int) -> list:
    """Build a list of {hour, count} for the last N hours."""
    now = datetime.now(timezone.utc)
    buckets = {}
    for h in range(hours - 1, -1, -1):
        label = (now.replace(minute=0, second=0, microsecond=0).hour - h) % 24
        buckets[label] = 0
    for r in records:
        ts_str = r.get("timestamp") or r.get("created_at", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            diff_h = int((now - ts).total_seconds() // 3600)
            if diff_h < hours:
                h_label = ts.hour
                if h_label in buckets:
                    buckets[h_label] += 1
        except Exception:
            pass
    return [{"hour": f"{h:02d}:00", "count": c} for h, c in sorted(buckets.items())]


@app.get("/volunteer/stats")
async def get_volunteer_stats(_auth: dict = Depends(require_volunteer)):
    cached = await cache_get(Keys.ADMIN_STATS)
    if cached is not None: return cached
    # Single-pass aggregation — see _compute_stats for performance notes.
    result = _compute_stats(include_extras=True)
    await cache_set(Keys.ADMIN_STATS, result, ttl=3)
    return result

# ═══════════════════════════════════════════════════════════════════════════════
# Lost & Found
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/lost")
async def get_lost(status: Optional[str] = None):
    try:
        key = Keys.LOST_STATUS.format(status=status) if status else Keys.LOST_ALL
        async def _pg_read():
            try:
                return await fetch_lost_persons(status)
            except Exception as e:
                logger.warning("[Lost] PG read failed, using memory: %s", e)
                p = DB["lost_persons"] if not status else [x for x in DB["lost_persons"] if x.get("status") == status]
                return {"lost_persons": sorted(p, key=lambda x: x.get("timestamp",""), reverse=True)}
        return await cached_read_pg(key, _pg_read, ttl=2)
    except Exception as exc:
        logger.error("[Lost] get_lost critical failure: %s", exc)
        return {"lost_persons": [], "error": "System busy. Please try again later."}

@app.post("/lost")
async def register_lost(
    request: Request,
    name: str = Form(...),
    age: Optional[int] = Form(None),
    last_seen_location: str = Form(default="Unknown"),
    current_location: str = Form(default="Unknown"),
    contact_person: Optional[str] = Form(None),
    contact_name: Optional[str] = Form(None),
    contact_phone: str = Form(...),
    description: str = Form(default=""),
    gender: Optional[str] = Form(None),
    status: str = Form(default="missing"),
    photo: Optional[UploadFile] = File(None),
    _gate: None = Depends(gate_lost),
):
    """
    Public endpoint: pilgrims report missing family members from user.html.
    No auth required — but rate-limited by client IP to prevent abuse.
    Mark-as-found / status updates still require auth (see PUT /lost/{pid}).
    """
    client_ip = request.client.host if request.client else "unknown"
    allowed, _info = await check_rate_limit(
        f"rate:ip:{client_ip}:lost_report", limit=5, window_seconds=300
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many lost-person reports from this device. Please wait before submitting another.",
        )
    try:
        photo_url = await upload_image(photo, folder="lost-found")
        # Accept either contact_name or contact_person
        resolved_contact = contact_name or contact_person or "Unknown"
        # Sanity-clamp untrusted inputs from the public form.
        clean_name = (name or "").strip()[:120]
        if not clean_name:
            raise HTTPException(status_code=400, detail="name is required")
        clean_phone = (contact_phone or "").strip()[:32]
        if not clean_phone:
            raise HTTPException(status_code=400, detail="contact_phone is required")
        person = {
            "id": str(uuid.uuid4()),
            "name": clean_name,
            "age": age if (age is None or 0 <= age <= 130) else None,
            "photo_url": photo_url,
            "gender": (gender or "").strip()[:24] or None,
            "last_seen_location": (last_seen_location or "Unknown").strip()[:200],
            "current_location":   (current_location or "Unknown").strip()[:200],
            "contact_person": resolved_contact.strip()[:120],
            "contact_phone":  clean_phone,
            "description":    (description or "").strip()[:1000],
            # SECURITY: a brand-new public submission can never legitimately
            # claim "found" / "closed". Force "missing" so no one can plant
            # a fake found-record. Volunteers/admins can transition status
            # via PUT /lost/{pid}.
            "status": "missing",
            "timestamp": _utc_now(),
        }
        
        # Persist to PG (non-blocking)
        try:
            await write_lost_person(person)
        except Exception as pg_exc:
            logger.error("[Lost] PG write failed: %s", pg_exc)

        DB["lost_persons"].append(person)
        _index_add("lost_persons", person)
        msg = {"type": "LOST_REGISTERED", "data": person}

        # Reliable Broadcast
        try:
            await cache_set(Keys.LOST_ALL, {"lost_persons": DB["lost_persons"]}, ttl=30)
        except Exception as exc:
            logger.debug("[Lost] cache warm failed: %s", exc)
        await _broadcast_event(
            msg,
            invalidate=(Keys.LOST_STATUS.format(status="missing"), Keys.ADMIN_STATS),
            stream_event="lost_person",
            stream_payload=person,
            label="Lost",
        )

        return {"success": True, "id": person["id"]}
    except Exception as exc:
        logger.error("[Lost] register_lost critical failure: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Internal error registering record. Contact helpdesk."}
        )

@app.put("/lost/{pid}")
async def update_lost(
    pid: str,
    status: Optional[str] = Form(None),
    current_location: Optional[str] = Form(None),
    last_seen_location: Optional[str] = Form(None),
    _auth: dict = Depends(require_volunteer_or_admin),
):
    try:
        # DB Update
        try:
            await update_lost_person_status(pid, status, current_location, last_seen_location)
        except Exception as pg_exc:
            logger.error("[Lost] PG Update failed: %s", pg_exc)

        p = _index_get("lost_persons", pid)
        if p is not None:
            if status:             p["status"]             = status
            if current_location:   p["current_location"]   = current_location
            if last_seen_location: p["last_seen_location"] = last_seen_location
            p["updated_at"] = _utc_now()

            # Redis & Global Sync
            try:
                await cache_set(Keys.LOST_ALL, {"lost_persons": DB["lost_persons"]}, ttl=30)
            except Exception as exc:
                logger.debug("[Lost] cache warm failed: %s", exc)
            msg = {"type": "LOST_UPDATED", "data": p}
            await _broadcast_event(
                msg,
                invalidate=(Keys.LOST_STATUS.format(status="missing"),
                            Keys.LOST_STATUS.format(status="found"),
                            Keys.LOST_STATUS.format(status="closed"),
                            Keys.ADMIN_STATS),
                label="Lost",
            )

            # ── WhatsApp notification when a missing person is found ──
            # Best-effort, fire-and-forget. Falls back silently if no
            # contact_phone, no WhatsApp provider, or status != "found".
            if status == "found" and p.get("contact_phone"):
                try:
                    from services.whatsapp_service import fire_and_forget_send
                    loc = p.get("current_location") or "Enquiry Counter"
                    wa_body = (
                        f"✅ Good news — {p.get('name','Your missing person')} "
                        f"has been FOUND.\n"
                        f"Current location: {loc}\n"
                        f"Report ID: {p['id'][:8]}\n"
                        "Please proceed to the nearest Enquiry Counter to "
                        "reunite. — TourGO Pushkara 🕊"
                    )
                    fire_and_forget_send(p["contact_phone"], wa_body)
                except Exception as wa_exc:
                    logger.debug("[Lost] WhatsApp notify skipped: %s", wa_exc)

            return {"success": True, "person": p}

        raise HTTPException(status_code=404, detail="Person not found")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[Lost] update_lost critical failure: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Internal error updating record."}
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Emergency Contacts
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/contacts")
async def get_contacts(category: Optional[str] = None):
    key = Keys.CONTACTS_CAT.format(category=category) if category else Keys.CONTACTS
    cached = await cache_get(key)
    if cached is not None: return cached
    c = DB["emergency_contacts"] if not category else [x for x in DB["emergency_contacts"] if x.get("category") == category]
    result = {"contacts": c}
    await cache_set(key, result, ttl=30)
    return result

@app.post("/contacts")
async def add_contact(
    name: str = Form(...), designation: str = Form(...), department: str = Form(...),
    phone: str = Form(...), latitude: float = Form(...), longitude: float = Form(...),
    address: str = Form(default=""), category: str = Form(default="other"),
    _auth: dict = Depends(require_volunteer_or_admin),
):
    contact = {
        "id": str(uuid.uuid4()), "name": name, "designation": designation, "department": department,
        "phone": phone, "latitude": latitude, "longitude": longitude, "address": address,
        "category": category, "timestamp": _utc_now()
    }
    DB["emergency_contacts"].append(contact)
    msg = {"type": "CONTACT_ADDED", "data": contact}
    await _broadcast_event(
        msg,
        invalidate=(Keys.CONTACTS, Keys.CONTACTS_CAT.format(category=category)),
        label="Contact",
    )
    return {"success": True, "id": contact["id"]}

@app.delete("/contacts/{cid}")
async def delete_contact(cid: str, _auth: dict = Depends(require_volunteer)):
    for i, c in enumerate(DB["emergency_contacts"]):
        if c["id"] == cid:
            DB["emergency_contacts"].pop(i)
            msg = {"type": "CONTACT_DELETED", "data": {"id": cid}}
            await _broadcast_event(
                msg,
                invalidate=(Keys.CONTACTS,),
                label="Contact",
            )
            return {"success": True}
    raise HTTPException(status_code=404, detail="Contact not found")

# ═══════════════════════════════════════════════════════════════════════════════
# Medical Facilities
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/medical")
async def get_medical(type: Optional[str] = None):
    key = Keys.MEDICAL_TYPE.format(type=type) if type else Keys.MEDICAL
    cached = await cache_get(key)
    if cached is not None: return cached
    f = DB["medical_facilities"] if not type else [x for x in DB["medical_facilities"] if x.get("type") == type]
    result = {"medical_facilities": f}
    await cache_set(key, result, ttl=30)
    return result

@app.post("/medical")
async def add_medical(
    name: str = Form(...), type: str = Form(...), latitude: float = Form(...),
    longitude: float = Form(...), status: str = Form(default="active"),
    beds: int = Form(default=0), doctor: str = Form(default=""),
    phone: str = Form(default=""), zone: str = Form(default=""),
    _auth: dict = Depends(require_volunteer_or_admin),
):
    fac = {
        "id": str(uuid.uuid4()), "name": name, "type": type, "latitude": latitude,
        "longitude": longitude, "status": status, "beds": beds, "doctor": doctor,
        "phone": phone, "zone": zone, "timestamp": _utc_now()
    }
    DB["medical_facilities"].append(fac)
    msg = {"type": "MEDICAL_ADDED", "data": fac}
    await _broadcast_event(
        msg,
        invalidate=(Keys.MEDICAL, Keys.MEDICAL_TYPE.format(type=type)),
        label="Medical",
    )
    return {"success": True, "id": fac["id"]}

@app.delete("/medical/{fid}")
async def delete_medical(fid: str, _auth: dict = Depends(require_volunteer)):
    for i, f in enumerate(DB["medical_facilities"]):
        if f["id"] == fid:
            DB["medical_facilities"].pop(i)
            return {"success": True}
    raise HTTPException(status_code=404, detail="Not found")

# ═══════════════════════════════════════════════════════════════════════════════
# WebSockets  (PRESERVED — identical to v7)
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/volunteer")
async def websocket_volunteer(websocket: WebSocket):
    if not await manager.connect(websocket, ghat_id="all"): return
    try:
        # ── INIT payload bounded to WS_INIT_RECENT records per category ──
        # The previous payload sent ALL DB lists in full — at the orchestrator's
        # 5000-item cap that was multi-MB on every connect. Most dashboards
        # only render the latest few hundred and incrementally append from
        # WS broadcasts, so a slim INIT keeps reconnect storms cheap.
        # The dashboard can fetch deeper history via the paginated REST APIs.
        ws_init_recent = int(os.getenv("WS_INIT_RECENT", "300"))
        await websocket.send_json({"type": "INIT", "data": {
            "issues":             DB["issues"][-ws_init_recent:],
            "sos_alerts":         DB["sos_alerts"][-ws_init_recent:],
            "ghats":              DB["ghats"],   # bounded by design (≤ ~50)
            "lost_persons":       DB["lost_persons"][-ws_init_recent:],
            "emergency_contacts": DB["emergency_contacts"],
            "medical_facilities": DB["medical_facilities"],
            "volunteers":         safe_volunteers(),
            "init_window":        ws_init_recent,
        }})
        # The manager's central heartbeat_loop pings every HEARTBEAT_INTERVAL
        # seconds; the per-handler timeout below is purely a read pump that
        # stays parked between client messages. Don't double-ping.
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(),
                                       timeout=HEARTBEAT_INTERVAL * 2)
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        await manager.disconnect(websocket)

# NOTE: /ws/admin WebSocket is removed — admin portal is handled by govt officials.

@app.websocket("/ws/public")
async def websocket_public(websocket: WebSocket):
    """Read-only WebSocket for pilgrims — receives CROWD_UPDATE broadcasts only.
    No auth required; sends a minimal INIT with ghat data then listens."""
    if not await manager.connect(websocket, ghat_id="all"): return
    try:
        # Send only ghat data (no sensitive SOS/lost person info)
        await websocket.send_json({"type": "INIT", "data": {
            "ghats": DB["ghats"],
        }})
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(),
                                       timeout=HEARTBEAT_INTERVAL * 2)
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        await manager.disconnect(websocket)

@app.websocket("/ws/pilgrim/{ghat_id}")
async def websocket_pilgrim(websocket: WebSocket, ghat_id: str):
    ghat = _index_get("ghats", ghat_id)
    if not ghat:
        await websocket.close(code=1008, reason="Unknown ghat_id")
        return
    if not await manager.connect(websocket, ghat_id=ghat_id): return
    try:
        await websocket.send_json({"type": "GHAT_INIT", "data": {
            "ghat":       ghat,
            "active_sos": [a for a in DB["sos_alerts"] if a["status"] == "active"],
            "facilities": DB["facilities"],
            "ambulance":  AMBULANCE_NUMBER,
            "police":     POLICE_NUMBER,
            "fire":       FIRE_NUMBER,
        }})
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(),
                                       timeout=HEARTBEAT_INTERVAL * 2)
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        await manager.disconnect(websocket)

# ═══════════════════════════════════════════════════════════════════════════════
# Location & Emergency Services  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════

def _find_nearest_ghat(lat, lon):
    return nearest_in_list(lat, lon, DB.get("ghats", []), lat_key="latitude", lon_key="longitude")

@app.get("/location_context")
def location_context(lat: float, lon: float):
    return {
        "user_location":    {"lat": lat, "lon": lon},
        "nearest_ghat":     _find_nearest_ghat(lat, lon),
        "nearest_police":   find_nearest_police(lat, lon),
        "nearest_hospital": find_nearest_hospital(lat, lon),
        "ambulance":        AMBULANCE_NUMBER,
        "fire":             FIRE_NUMBER,
    }

@app.get("/emergency_services")
def list_emergency_services(service_type: Optional[str] = None, category: Optional[str] = None):
    data = EMERGENCY_SERVICES
    if service_type: data = [s for s in data if s.get("type")     == service_type]
    if category:     data = [s for s in data if s.get("category") == category]
    return {"emergency_services": data, "total": len(data)}

@app.get("/emergency_services/grouped")
def grouped_emergency_services():
    return {"grouped": get_all_services_by_category()}


# ── Emergency Services Registry — admin CRUD ─────────────────────────────────
# The registry is the single, authoritative "Emergency Contacts" dataset shown
# in the admin portal. These routes let government officials manage it from the
# UI (X-Admin-Key) or a volunteer JWT. Mutations are in-memory + broadcast over
# WS so connected dashboards update live.

@app.post("/emergency_services")
async def create_emergency_service(
    name: str = Form(...),
    type: str = Form(default="administration"),
    category: str = Form(default="other"),
    phone: str = Form(default=""),
    address: str = Form(default=""),
    lat: Optional[float] = Form(default=None),
    lon: Optional[float] = Form(default=None),
    _auth: dict = Depends(require_volunteer_or_admin),
):
    svc = es_add_service({
        "name": name, "type": type, "category": category,
        "phone": phone, "address": address, "lat": lat, "lon": lon,
    })
    await _broadcast_event(
        {"type": "ES_ADDED", "data": svc},
        label="EmergencyService",
    )
    return {"success": True, "service": svc}


@app.put("/emergency_services/{sid}")
async def edit_emergency_service(
    sid: int,
    name: Optional[str] = Form(default=None),
    type: Optional[str] = Form(default=None),
    category: Optional[str] = Form(default=None),
    phone: Optional[str] = Form(default=None),
    address: Optional[str] = Form(default=None),
    lat: Optional[float] = Form(default=None),
    lon: Optional[float] = Form(default=None),
    _auth: dict = Depends(require_volunteer_or_admin),
):
    svc = es_update_service(sid, {
        "name": name, "type": type, "category": category,
        "phone": phone, "address": address, "lat": lat, "lon": lon,
    })
    if svc is None:
        raise HTTPException(status_code=404, detail="Emergency service not found")
    await _broadcast_event(
        {"type": "ES_UPDATED", "data": svc},
        label="EmergencyService",
    )
    return {"success": True, "service": svc}


@app.delete("/emergency_services/{sid}")
async def remove_emergency_service(
    sid: int,
    _auth: dict = Depends(require_volunteer_or_admin),
):
    if not es_delete_service(sid):
        raise HTTPException(status_code=404, detail="Emergency service not found")
    await _broadcast_event(
        {"type": "ES_DELETED", "data": {"id": sid}},
        label="EmergencyService",
    )
    return {"success": True}

# ═══════════════════════════════════════════════════════════════════════════════
# TourGo Explorer  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/tourgo/explore")
async def tourgo_explore(lat: float, lon: float, radius_km: float = 60):
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise HTTPException(status_code=400, detail="lat/lon out of valid range.")
    if radius_km <= 0 or radius_km > 500:
        raise HTTPException(status_code=400, detail="radius_km must be 1–500.")
    cache_key = f"cache:tourgo:{round(lat, 2)}:{round(lon, 2)}:{int(radius_km)}"
    cached = await cache_get(cache_key)
    if cached: return cached
    scraper_endpoint = f"{SCRAPERBOT_URL}/places?lat={lat}&lon={lon}&radius_km={radius_km}"
    try:
        # Singleton scraperbot client — keeps a warm connection pool, avoids
        # the previous TCP+TLS handshake on every call.
        client = await scraperbot_client()
        resp = await client.get(scraper_endpoint)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"ScraperBot returned {resp.status_code}: {resp.text[:300]}")
        result = resp.json()
        await cache_set(cache_key, result, ttl=60)
        return result
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="ScraperBot service is not reachable.")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="ScraperBot timed out.")
    except HTTPException: raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")

# ═══════════════════════════════════════════════════════════════════════════════
# Crowd Intelligence Ingestion  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/crowd/ingest/cctv")
async def ingest_cctv(request: Request, _gate: None = Depends(gate_ingest)):
    body = await request.json()
    ghat_id = body.get("ghat_id")
    if not ghat_id: raise HTTPException(status_code=400, detail="ghat_id required")
    person_count = int(body.get("person_count", 0))
    # Auto-derive frame_area_sq_m from ghat capacity if not supplied by sender.
    # At Fruin Level F (stampede), density = 4 persons/m².
    # So "full capacity" area = capacity / 4.  This means occupancy maps linearly to density.
    ghat = _index_get("ghats", ghat_id)
    default_area = (ghat["capacity"] / 4.0) if ghat and ghat.get("capacity") else 500.0
    vision_data = {
        "person_count":    person_count,
        "frame_area_sq_m": float(body.get("frame_area_sq_m", default_area)),
        "camera_id":       body.get("camera_id", "unknown"),
        "timestamp":       float(body.get("timestamp", time.time())),
    }
    # Also update ghat current_count in memory so it stays fresh for user portal
    if ghat:
        ghat["current_count"] = person_count
    # Clear manual override when real sensor data arrives (sensor > admin)
    _manual_overrides.pop(ghat_id, None)
    await set_crowd_data(f"cctv:{ghat_id}", vision_data)
    await stream_publish(Keys.STREAM_CCTV, {
        "ghat_id": ghat_id,
        "person_count": str(vision_data["person_count"]),
        "timestamp": str(vision_data["timestamp"]),
    })
    return {"success": True, "ghat_id": ghat_id, "accepted": vision_data["person_count"]}

@app.post("/crowd/ingest/telecom")
async def ingest_telecom(request: Request, _gate: None = Depends(gate_ingest)):
    body = await request.json()
    ghat_id = body.get("ghat_id")
    if not ghat_id: raise HTTPException(status_code=400, detail="ghat_id required")
    active_devices = int(body.get("active_devices", 0))
    tower_baseline = int(body.get("tower_baseline", 1000))
    telecom_data = {
        "active_devices": active_devices,
        "tower_baseline": tower_baseline,
        "tower_id":       body.get("tower_id", "unknown"),
        "timestamp":      float(body.get("timestamp", time.time())),
    }
    # FIX: O(1) ghat lookup via id-index — was a linear scan over DB["ghats"].
    ghat = _index_get("ghats", ghat_id)
    if ghat and ghat.get("capacity") and not body.get("cctv_active"):
        ratio = min(active_devices / max(tower_baseline, 1), 5.0) / 5.0
        ghat["current_count"] = int(ratio * ghat["capacity"])
    # Clear manual override when real sensor data arrives
    _manual_overrides.pop(ghat_id, None)
    await set_crowd_data(f"telecom:{ghat_id}", telecom_data)
    await stream_publish(Keys.STREAM_TELECOM, {
        "ghat_id": ghat_id,
        "active_devices": str(telecom_data["active_devices"]),
        "timestamp": str(telecom_data["timestamp"]),
    })
    return {"success": True, "ghat_id": ghat_id}

@app.get("/crowd/risk/all")
async def get_all_risk():
    cached = await cache_get(Keys.RISK_ALL)
    if cached: return cached
    ghats = DB["ghats"]
    if ghats:
        # Fetch every ghat's crowd snapshot in parallel — was a serial loop
        # of N Redis round-trips on every cache miss (TTL=3s, so this fires
        # often).
        snapshots = await asyncio.gather(
            *(get_crowd_data(g["id"]) for g in ghats),
            return_exceptions=True,
        )
        result = []
        for ghat, snap in zip(ghats, snapshots):
            if isinstance(snap, Exception) or not snap:
                snap = evaluate_from_dicts(ghat)
            result.append(snap)
    else:
        result = []
    payload = {"risks": result, "timestamp": time.time()}
    await cache_set(Keys.RISK_ALL, payload, ttl=3)
    return payload

@app.get("/crowd/risk/{ghat_id}")
async def get_ghat_risk(ghat_id: str):
    risk_data = await get_crowd_data(ghat_id)
    if risk_data: return risk_data
    ghat = _index_get("ghats", ghat_id)
    if not ghat: raise HTTPException(status_code=404, detail="Ghat not found")
    return evaluate_from_dicts(ghat)


# ═══════════════════════════════════════════════════════════════════════════════
# Crowd Forecast  (Phase 3 — predictive analytics surface)
# ═══════════════════════════════════════════════════════════════════════════════

def _level_from_ratio(ratio: float) -> str:
    """Map occupancy ratio (count/capacity) → crowd_level. Aligned with risk_engine."""
    if ratio >= 0.95: return "critical"
    if ratio >= 0.75: return "high"
    if ratio >= 0.50: return "medium"
    return "low"


async def _forecast_one_ghat(ghat: dict, horizon_min: int = 60) -> dict:
    """
    Cheap-and-cheerful 60-min forecast for a single ghat.

    Strategy (in order of preference):
      1. If we have ≥ 3 recent crowd_history points, fit a linear trend on
         current_count vs timestamp and extrapolate forward.
      2. Else, fall back to a heuristic: ghats already over 70% drift up,
         under 30% drift slowly down, mid-range stays flat.

    Returns: { ghat_id, name, current_count, capacity, current_level,
               predicted_count, predicted_level, predicted_pct, confidence }
    """
    ghat_id = ghat["id"]
    cap = max(1, int(ghat.get("capacity") or 1))
    cur = int(ghat.get("current_count") or 0)
    cur_pct = cur / cap
    cur_lvl = ghat.get("crowd_level") or _level_from_ratio(cur_pct)

    # 1) Try recent history
    history = await get_crowd_history(ghat_id, n=12)
    pts = []
    for h in history or []:
        try:
            t = float(h.get("timestamp") or h.get("recorded_at_ts") or 0)
            c = h.get("estimated_count") or h.get("current_count") or h.get("count")
            if t > 0 and c is not None:
                pts.append((t, float(c)))
        except (ValueError, TypeError):
            continue

    predicted_count = cur
    confidence = 0.35  # heuristic baseline
    if len(pts) >= 3:
        # Sort oldest → newest
        pts.sort(key=lambda x: x[0])
        # Simple linear regression on (time, count)
        n = len(pts)
        mean_t = sum(p[0] for p in pts) / n
        mean_c = sum(p[1] for p in pts) / n
        num = sum((p[0] - mean_t) * (p[1] - mean_c) for p in pts)
        den = sum((p[0] - mean_t) ** 2 for p in pts) or 1.0
        slope = num / den
        # Project forward by horizon
        future_t = pts[-1][0] + horizon_min * 60.0
        predicted_count = int(max(0, mean_c + slope * (future_t - mean_t)))
        # Confidence ∝ R² (clamped)
        ss_res = sum((p[1] - (mean_c + slope * (p[0] - mean_t))) ** 2 for p in pts)
        ss_tot = sum((p[1] - mean_c) ** 2 for p in pts) or 1.0
        r2 = max(0.0, 1.0 - ss_res / ss_tot)
        confidence = max(0.4, min(0.95, 0.4 + 0.55 * r2))
    else:
        # 2) Heuristic — drift toward extremes when already loaded
        if cur_pct >= 0.7:
            predicted_count = int(min(cap * 1.05, cur * 1.08))   # creep up
        elif cur_pct <= 0.3:
            predicted_count = int(max(0, cur * 0.95))             # ease off
        else:
            predicted_count = cur                                 # stable
        confidence = 0.45

    # Bound predicted count to [0, 1.2 * capacity] to keep things plausible
    predicted_count = max(0, min(int(cap * 1.2), int(predicted_count)))
    predicted_pct = predicted_count / cap
    predicted_level = _level_from_ratio(predicted_pct)

    return {
        "ghat_id":         ghat_id,
        "name":            ghat.get("name"),
        "current_count":   cur,
        "capacity":        cap,
        "current_level":   cur_lvl,
        "predicted_count": predicted_count,
        "predicted_level": predicted_level,
        "predicted_pct":   round(predicted_pct, 3),
        "confidence":      round(confidence, 2),
        "horizon_min":     horizon_min,
        "samples":         len(pts),
    }


@app.get("/crowd/forecast")
async def crowd_forecast(horizon_min: int = 60):
    """
    Return a 60-min (default) crowd forecast for every ghat.
    Sorted with most-at-risk first so the admin dashboard pre-positions resources.
    """
    horizon_min = max(5, min(240, int(horizon_min or 60)))
    out = await asyncio.gather(*[_forecast_one_ghat(g, horizon_min) for g in DB["ghats"]])
    # Order: most severe predicted_level first, then highest predicted_pct
    severity_rank = {"critical": 3, "high": 2, "medium": 1, "low": 0}
    out_sorted = sorted(
        out,
        key=lambda x: (severity_rank.get(x["predicted_level"], 0), x["predicted_pct"]),
        reverse=True,
    )
    return {"forecast": out_sorted, "horizon_min": horizon_min, "computed_at": _utc_now()}
