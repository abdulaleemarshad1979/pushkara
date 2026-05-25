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

from state.emergency_services import EMERGENCY_SERVICES, AMBULANCE_NUMBER, FIRE_NUMBER, POLICE_NUMBER
from services.emergency_service import (
    find_nearest_police, find_nearest_hospital, get_all_services_by_category,
)
from utils.location_utils import haversine as _hvs, nearest_in_list
from app.core.redis_manager import (
    Keys, cache_get, cache_set, cache_delete,
    publish, publish_to_ghat, stream_publish,
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
    create_access_token, authenticate_volunteer, hash_password,
    rebuild_volunteer_index,
    require_admin_key,
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

async def sync_state(payload: dict):
    """
    Synchronizes the in-memory DB dictionary when an event is received from Redis.
    Ensures multi-instance consistency for WS INIT and local HTTP reads.
    """
    try:
        msg_type = payload.get("type")
        data = payload.get("data")
        if not msg_type or not data: return

        if msg_type == "SOS_ALERT":
            if not any(x["id"] == data.get("id") for x in DB["sos_alerts"]):
                DB["sos_alerts"].append(data)
        elif msg_type in ("SOS_RESOLVED", "SOS_ASSIGNED"):
            for x in DB["sos_alerts"]:
                if x["id"] == data.get("id"):
                    x.update(data)
                    break
        elif msg_type == "LOST_REGISTERED":
            if not any(x["id"] == data.get("id") for x in DB["lost_persons"]):
                DB["lost_persons"].append(data)
        elif msg_type == "LOST_UPDATED":
            for x in DB["lost_persons"]:
                if x["id"] == data.get("id"):
                    x.update(data)
                    break
        elif msg_type == "NEW_ISSUE":
            if not any(x["id"] == data.get("id") for x in DB["issues"]):
                DB["issues"].append(data)
        elif msg_type in ("ISSUE_RESOLVED", "ISSUE_ACCEPTED"):
            for x in DB["issues"]:
                if x["id"] == data.get("id"):
                    x.update(data)
                    break
        elif msg_type == "CROWD_UPDATE":
            for g in DB["ghats"]:
                if g["id"] == data.get("ghat_id"):
                    g["crowd_level"] = data.get("level")
                    if "current_count" in data: g["current_count"] = data["current_count"]
                    break
        elif msg_type == "VOLUNTEER_UPDATED":
             for v in DB["volunteers"]:
                if v["id"] == data.get("id"):
                    v.update(data)
                    break
        elif msg_type == "VOLUNTEER_CREATED":
            if not any(v["id"] == data.get("id") for v in DB["volunteers"]):
                DB["volunteers"].append(data)
                rebuild_volunteer_index(DB["volunteers"])
        elif msg_type == "VOLUNTEER_DELETED":
            DB["volunteers"] = [v for v in DB["volunteers"] if v["id"] != data.get("id")]
            rebuild_volunteer_index(DB["volunteers"])

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
    start_all_guardians()
    yield
    await manager.stop_subscriber()
    await stop_all_guardians()
    await close_redis()
    await close_pg_pool()
    logger.info("[Shutdown] Clean  instance=%s", INSTANCE_ID)

app = FastAPI(
    title="Godavari Pushkaralu 2027 API",
    description="Government of Andhra Pradesh — District Administration, East Godavari",
    version="8.0.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
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

async def crowd_broadcast_loop():
    from app.core.risk_engine import RiskEngine
    logger.info("[CrowdLoop] Starting  instance=%s", INSTANCE_ID)
    while True:
        try:
            await asyncio.sleep(CROWD_BROADCAST_INTERVAL)
            if not await _am_leader(): continue
            for ghat in DB["ghats"]:
                ghat_id = ghat["id"]
                try:
                    # Skip auto-evaluation if admin has manually set this ghat's level
                    if _manual_overrides.get(ghat_id, 0) > time.time():
                        # Still broadcast the current manual level so late-joining clients get it
                        await publish_to_ghat(ghat_id, {
                            "type": "CROWD_UPDATE",
                            "data": {
                                "ghat_id":   ghat_id,
                                "level":     ghat["crowd_level"],
                                "name":      ghat.get("name", ""),
                                "risk_score": _prev_scores.get(ghat_id, 0.0),
                                "occupancy_pct": round((ghat.get("current_count", 0) / max(ghat.get("capacity", 1), 1)) * 100, 1),
                                "colour":    {"low": "green", "medium": "orange", "high": "red", "critical": "purple"}.get(ghat["crowd_level"], "grey"),
                                "manual": True,
                            }
                        })
                        continue
                    vision_data  = await get_crowd_data(f"cctv:{ghat_id}")
                    telecom_data = await get_crowd_data(f"telecom:{ghat_id}")
                    history      = await get_crowd_history(ghat_id, 10)
                    result       = evaluate_from_dicts_adaptive(ghat, vision_data, telecom_data, history)
                    ghat["crowd_level"]   = result["crowd_level"]
                    ghat["current_count"] = result["estimated_count"]
                    await set_crowd_data(ghat_id, result)
                    await cache_set(Keys.GHAT_ONE.format(ghat_id=ghat_id), ghat, ttl=3)
                    prev = _prev_scores.get(ghat_id, 0.0)
                    if RiskEngine.should_alert(result["risk_score"], prev):
                        await publish_to_ghat(ghat_id, {
                            "type": "CROWD_ALERT",
                            "data": {
                                "ghat_id":       ghat_id,
                                "name":          ghat.get("name", ""),
                                "crowd_level":   result["crowd_level"],
                                "risk_score":    result["risk_score"],
                                "occupancy_pct": result["occupancy_pct"],
                                "message":       f"⚠️ {ghat.get('name','')} — {result['crowd_level'].upper()} crowd",
                            }
                        })
                    _prev_scores[ghat_id] = result["risk_score"]
                    if result.get("surge_detected"):
                        await publish_to_ghat(ghat_id, {
                            "type": "SURGE_ALERT",
                            "data": {
                                "ghat_id":   ghat_id,
                                "name":      ghat.get("name", ""),
                                "message":   f"🚨 SURGE at {ghat.get('name','')} — crowd rising rapidly",
                                "risk_score": result["risk_score"],
                            }
                        })
                    await publish_to_ghat(ghat_id, {
                        "type": "CROWD_UPDATE",
                        "data": {
                            "ghat_id":       ghat_id,
                            "level":         result["crowd_level"],
                            "name":          ghat.get("name", ""),
                            "risk_score":    result["risk_score"],
                            "occupancy_pct": result["occupancy_pct"],
                            "colour":        result["colour"],
                        }
                    })
                except Exception as exc:
                    logger.debug("[CrowdLoop] ghat=%s error=%s", ghat_id, exc)
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
            "festival_dates": "July 11-22, 2027"}

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
    image: Optional[UploadFile] = File(None)
):
    client_ip = request.client.host
    allowed, _ = await check_rate_limit(f"rate:ip:{client_ip}:report", limit=10, window_seconds=60)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Please wait before reporting again.")
    image_url = await upload_image(image, folder="issues")
    issue = {
        "id": str(uuid.uuid4()), "description": description, "category": category,
        "image_url": image_url, "latitude": latitude, "longitude": longitude,
        "status": "pending", "assigned_volunteer": None, "user_name": user_name,
        "timestamp": _utc_now()
    }
    await write_issue(issue)
    DB["issues"].append(issue)
    msg = {"type": "NEW_ISSUE", "data": issue}
    
    try:
        await cache_delete(Keys.ISSUES_ALL, Keys.ISSUES_STATUS.format(status="pending"), Keys.STATS, Keys.ADMIN_STATS)
        await manager.broadcast(msg)
        await stream_publish(Keys.STREAM_EVENTS, {"event": "new_issue", "payload": json.dumps(issue)})
    except Exception as ws_exc:
        logger.warning("[Issue] Broadcast failed: %s", ws_exc)
        await manager._local_broadcast(msg)

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
        for issue in DB["issues"]:
            if issue["id"] == issue_id:
                issue.update({"status": "resolved", "resolved_at": resolved_at, "assigned_volunteer": volunteer_id})
                if resolution_note:
                    issue["resolution_note"]  = resolution_note.strip()[:1000]
                if photo_url:
                    issue["resolution_photo"] = photo_url
                msg = {"type": "ISSUE_RESOLVED", "data": issue}

                try:
                    await cache_delete(Keys.ISSUES_ALL, Keys.ISSUES_STATUS.format(status="resolved"),
                                       Keys.ISSUES_STATUS.format(status="pending"), Keys.STATS, Keys.ADMIN_STATS)
                    await manager.broadcast(msg)
                    await stream_publish(Keys.STREAM_EVENTS, {"event": "issue_resolved", "payload": json.dumps(issue)})
                except Exception as ws_exc:
                    logger.warning("[Issue] Resolve broadcast failed: %s", ws_exc)
                    await manager._local_broadcast(msg)

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
        for issue in DB["issues"]:
            if issue["id"] == issue_id:
                issue.update({"status": "in_progress", "assigned_volunteer": volunteer_id})
                msg = {"type": "ISSUE_ACCEPTED", "data": issue}
                
                try:
                    await cache_delete(Keys.ISSUES_ALL, Keys.ISSUES_STATUS.format(status="in_progress"),
                                       Keys.ISSUES_STATUS.format(status="pending"), Keys.STATS, Keys.ADMIN_STATS)
                    await manager.broadcast(msg)
                except Exception as ws_exc:
                    logger.warning("[Issue] Accept broadcast failed: %s", ws_exc)
                    await manager._local_broadcast(msg)
                    
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
    msg = {"type": "SOS_ALERT", "data": alert, "priority": "HIGH"}

    # Reliable broadcast (Redis + local fan-out)
    try:
        await cache_delete(Keys.SOS_ACTIVE, Keys.SOS_ALL, Keys.STATS, Keys.ADMIN_STATS)
        await manager.broadcast(msg)
        await stream_publish(
            Keys.STREAM_EVENTS,
            {"event": "sos_alert", "payload": json.dumps(alert)},
        )
    except Exception as ws_exc:
        logger.warning("[SOS] Broadcast failed: %s", ws_exc)
        await manager._local_broadcast(msg)

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
    longitude: float = Form(...), phone: str = Form(default="")
):
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
        for alert in DB["sos_alerts"]:
            if alert["id"] == alert_id:
                alert.update({"status": "resolved", "resolved_at": resolved_at})
                if resolution_note:
                    alert["resolution_note"]  = resolution_note.strip()[:1000]
                if photo_url:
                    alert["resolution_photo"] = photo_url
                if resolution_note or photo_url:
                    alert["resolved_by"] = volunteer_id
                msg = {"type": "SOS_RESOLVED", "data": alert}

                try:
                    await cache_delete(Keys.SOS_ACTIVE, Keys.SOS_ALL, Keys.STATS, Keys.ADMIN_STATS)
                    await manager.broadcast(msg)
                    await stream_publish(Keys.STREAM_EVENTS, {"event": "sos_resolved", "payload": json.dumps(alert)})
                except Exception as ws_exc:
                    logger.warning("[SOS] Resolve broadcast failed: %s", ws_exc)
                    await manager._local_broadcast(msg)

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
        alert = next((a for a in DB["sos_alerts"] if a["id"] == alert_id), None)
        vol   = next((v for v in DB["volunteers"]  if v["id"] == volunteer_id), None)
        if not alert: raise HTTPException(status_code=404, detail="Alert not found")
        if not vol:   raise HTTPException(status_code=404, detail="Volunteer not found")
        
        await update_sos_status(alert_id, "assigned", volunteer_id=volunteer_id, volunteer_name=vol["name"])
        alert.update({"assigned_volunteer": volunteer_id, "assigned_volunteer_name": vol["name"]})
        msg = {"type": "SOS_ASSIGNED", "data": alert}
        
        try:
            await cache_delete(Keys.SOS_ACTIVE, Keys.SOS_ALL)
            await manager.broadcast(msg)
        except Exception as ws_exc:
            logger.warning("[SOS] Assign broadcast failed: %s", ws_exc)
            await manager._local_broadcast(msg)
            
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
    for ghat in DB["ghats"]:
        if ghat["id"] == ghat_id:
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
            await publish_to_ghat(ghat_id, msg)
            await manager._local_broadcast(msg, ghat_id)
            return {"success": True, "locked_until": _manual_overrides[ghat_id], "override_ttl_seconds": MANUAL_OVERRIDE_TTL}
    raise HTTPException(status_code=404, detail="Ghat not found")

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
    for vol in DB["volunteers"]:
        if vol["id"] == vid:
            if status: vol["status"] = status
            if zone:   vol["zone"]   = zone
            if assigned_issue is not None: vol["assigned_issue"] = assigned_issue
            if latitude  is not None: vol["latitude"]  = latitude
            if longitude is not None: vol["longitude"] = longitude
            vol["updated_at"] = _utc_now()
            safe = {k: v for k, v in vol.items() if k != "password"}
            msg = {"type": "VOLUNTEER_UPDATED", "data": safe}
            await cache_delete(Keys.VOLUNTEERS)
            await publish(Keys.CHANNEL_ALL, msg)
            await manager._local_broadcast(msg)
            return {"success": True, "volunteer": safe}
    raise HTTPException(status_code=404, detail="Volunteer not found")

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
    # Check username not already taken
    uname = username.strip().lower()
    if any(v.get("username", "").lower() == uname for v in DB["volunteers"]):
        raise HTTPException(status_code=409, detail=f"Username '{username}' is already taken")

    # FIX (A7): hash once — the previous code called hash_password() twice,
    # producing two DIFFERENT bcrypt hashes (each call uses a fresh random salt)
    # which wasted ~100 ms/CPU per create and caused inconsistency between the
    # `password` (used by authenticate_volunteer) and `password_hash` (used by
    # PostgreSQL persistence) fields.
    pw_hashed = hash_password(password)
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

    # Update in-memory DB and O(1) lookup index
    DB["volunteers"].append(vol)
    rebuild_volunteer_index(DB["volunteers"])

    # Invalidate cache + broadcast
    await cache_delete(Keys.VOLUNTEERS)
    safe = {k: v for k, v in vol.items() if k not in ("password", "password_hash")}
    msg = {"type": "VOLUNTEER_CREATED", "data": safe}
    await publish(Keys.CHANNEL_ALL, msg)
    await manager._local_broadcast(msg)

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
    _auth: None = Depends(require_admin_key),
):
    """
    Update any volunteer field.
    Requires X-Admin-Key header.
    If password is provided it is re-hashed.
    """
    vol = next((v for v in DB["volunteers"] if v["id"] == vid), None)
    if not vol:
        raise HTTPException(status_code=404, detail="Volunteer not found")

    fields: dict = {}
    if name:   vol["name"]   = fields["name"]   = name.strip()
    if phone:  vol["phone"]  = fields["phone"]  = phone.strip()
    if zone:   vol["zone"]   = fields["zone"]   = zone.strip()
    if status: vol["status"] = fields["status"] = status

    if password:
        hashed = hash_password(password)
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
    await publish(Keys.CHANNEL_ALL, msg)
    await manager._local_broadcast(msg)

    logger.info("[Admin] Volunteer updated: %s", vid)
    return {"success": True, "volunteer": safe}


@app.delete("/admin/volunteer/{vid}")
async def admin_delete_volunteer(vid: str, _auth: None = Depends(require_admin_key)):
    """
    Permanently delete a volunteer account.
    Requires X-Admin-Key header.
    """
    idx = next((i for i, v in enumerate(DB["volunteers"]) if v["id"] == vid), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Volunteer not found")

    removed = DB["volunteers"].pop(idx)
    await delete_volunteer(vid)
    rebuild_volunteer_index(DB["volunteers"])

    await cache_delete(Keys.VOLUNTEERS)
    msg = {"type": "VOLUNTEER_DELETED", "data": {"id": vid}}
    await publish(Keys.CHANNEL_ALL, msg)
    await manager._local_broadcast(msg)

    logger.info("[Admin] Volunteer deleted: %s (%s)", removed.get("name"), vid)
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
    result = {
        "total_issues":       len(DB["issues"]),
        "pending_issues":     len([i for i in DB["issues"]    if i["status"] == "pending"]),
        "resolved_issues":    len([i for i in DB["issues"]    if i["status"] == "resolved"]),
        "active_sos":         len([a for a in DB["sos_alerts"] if a["status"] == "active"]),
        "total_volunteers":   len(DB["volunteers"]),
        "active_volunteers":  len([v for v in DB["volunteers"] if v.get("status") == "available"]),
        "ghats":              len(DB["ghats"]),
        "total_facilities":   len(DB["facilities"]),
        "transport_routes":   len(DB["transport_routes"]),
        "lost_persons":       len(DB["lost_persons"]),
        "missing_persons":    len([p for p in DB["lost_persons"] if p.get("status") == "missing"]),
        "emergency_contacts": len(DB["emergency_contacts"]),
        "medical_facilities": len(DB["medical_facilities"]),
        "festival": "Godavari Pushkaralu 2027",
        "location": "Rajahmundry, East Godavari",
    }
    await cache_set(Keys.STATS, result, ttl=3)
    return result

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
    result = {
        "total_issues":       len(DB["issues"]),
        "pending_issues":     len([i for i in DB["issues"]    if i["status"] == "pending"]),
        "in_progress_issues": len([i for i in DB["issues"]    if i["status"] == "in_progress"]),
        "resolved_issues":    len([i for i in DB["issues"]    if i["status"] == "resolved"]),
        "active_sos":         len([a for a in DB["sos_alerts"] if a["status"] == "active"]),
        "resolved_sos":       len([a for a in DB["sos_alerts"] if a["status"] == "resolved"]),
        "total_volunteers":   len(DB["volunteers"]),
        "active_volunteers":  len([v for v in DB["volunteers"] if v.get("status") == "available"]),
        "busy_volunteers":    len([v for v in DB["volunteers"] if v.get("status") == "busy"]),
        "ghats":              len(DB["ghats"]),
        "high_crowd_ghats":   len([g for g in DB["ghats"] if g.get("crowd_level") in ["high", "critical"]]),
        "lost_persons":       len(DB["lost_persons"]),
        "missing_persons":    len([p for p in DB["lost_persons"] if p.get("status") == "missing"]),
        "found_persons":      len([p for p in DB["lost_persons"] if p.get("status") == "found"]),
        "emergency_contacts": len(DB["emergency_contacts"]),
        "medical_facilities": len(DB["medical_facilities"]),
        "festival": "Godavari Pushkaralu 2027",
        "location": "Rajahmundry, East Godavari",
    }
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
    _auth: dict = Depends(require_volunteer_or_admin),
):
    try:
        photo_url = await upload_image(photo, folder="lost-found")
        # Accept either contact_name or contact_person
        resolved_contact = contact_name or contact_person or "Unknown"
        person = {
            "id": str(uuid.uuid4()), "name": name, "age": age, "photo_url": photo_url,
            "gender": gender,
            "last_seen_location": last_seen_location, "current_location": current_location,
            "contact_person": resolved_contact, "contact_phone": contact_phone,
            "description": description, "status": status, "timestamp": _utc_now()
        }
        
        # Persist to PG (non-blocking)
        try:
            await write_lost_person(person)
        except Exception as pg_exc:
            logger.error("[Lost] PG write failed: %s", pg_exc)

        DB["lost_persons"].append(person)
        msg = {"type": "LOST_REGISTERED", "data": person}
        
        # Reliable Broadcast
        try:
            await cache_set(Keys.LOST_ALL, {"lost_persons": DB["lost_persons"]}, ttl=30)
            await cache_delete(Keys.LOST_STATUS.format(status="missing"), Keys.ADMIN_STATS)
            await manager.broadcast(msg)
            await stream_publish(Keys.STREAM_EVENTS, {"event": "lost_person", "payload": json.dumps(person)})
        except Exception as ws_exc:
            logger.warning("[Lost] Broadcast failed: %s", ws_exc)
            await manager._local_broadcast(msg)

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
            
        for p in DB["lost_persons"]:
            if p["id"] == pid:
                if status:             p["status"]             = status
                if current_location:   p["current_location"]   = current_location
                if last_seen_location: p["last_seen_location"] = last_seen_location
                p["updated_at"] = _utc_now()
                
                # Redis & Global Sync
                try:
                    await cache_set(Keys.LOST_ALL, {"lost_persons": DB["lost_persons"]}, ttl=30)
                    await cache_delete(
                        Keys.LOST_STATUS.format(status="missing"),
                        Keys.LOST_STATUS.format(status="found"),
                        Keys.LOST_STATUS.format(status="closed"),
                        Keys.ADMIN_STATS,
                    )
                    msg = {"type": "LOST_UPDATED", "data": p}
                    await manager.broadcast(msg)
                except Exception as ws_exc:
                    logger.warning("[Lost] Broadcast failed: %s", ws_exc)
                    await manager._local_broadcast({"type": "LOST_UPDATED", "data": p})

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
    await cache_delete(Keys.CONTACTS, Keys.CONTACTS_CAT.format(category=category))
    await publish(Keys.CHANNEL_ALL, msg)
    await manager._local_broadcast(msg)
    return {"success": True, "id": contact["id"]}

@app.delete("/contacts/{cid}")
async def delete_contact(cid: str, _auth: dict = Depends(require_volunteer)):
    for i, c in enumerate(DB["emergency_contacts"]):
        if c["id"] == cid:
            DB["emergency_contacts"].pop(i)
            msg = {"type": "CONTACT_DELETED", "data": {"id": cid}}
            await cache_delete(Keys.CONTACTS)
            await publish(Keys.CHANNEL_ALL, msg)
            await manager._local_broadcast(msg)
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
    await cache_delete(Keys.MEDICAL, Keys.MEDICAL_TYPE.format(type=type))
    await publish(Keys.CHANNEL_ALL, msg)
    await manager._local_broadcast(msg)
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
        await websocket.send_json({"type": "INIT", "data": {
            "issues":             DB["issues"],
            "sos_alerts":         DB["sos_alerts"],  # send ALL statuses so admin dashboard shows resolved items too
            "ghats":              DB["ghats"],
            "lost_persons":       DB["lost_persons"],
            "emergency_contacts": DB["emergency_contacts"],
            "medical_facilities": DB["medical_facilities"],
            "volunteers":         safe_volunteers(),
        }})
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "PING"})
    except WebSocketDisconnect:
        await manager.disconnect(websocket, ghat_id="all")

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
                await asyncio.wait_for(websocket.receive_text(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "PING"})
    except WebSocketDisconnect:
        await manager.disconnect(websocket, ghat_id="all")

@app.websocket("/ws/pilgrim/{ghat_id}")
async def websocket_pilgrim(websocket: WebSocket, ghat_id: str):
    ghat = next((g for g in DB["ghats"] if g["id"] == ghat_id), None)
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
                await asyncio.wait_for(websocket.receive_text(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "PING"})
    except WebSocketDisconnect:
        await manager.disconnect(websocket, ghat_id=ghat_id)

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
        async with httpx.AsyncClient(timeout=30.0) as client:
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
async def ingest_cctv(request: Request):
    body = await request.json()
    ghat_id = body.get("ghat_id")
    if not ghat_id: raise HTTPException(status_code=400, detail="ghat_id required")
    person_count = int(body.get("person_count", 0))
    # Auto-derive frame_area_sq_m from ghat capacity if not supplied by sender.
    # At Fruin Level F (stampede), density = 4 persons/m².
    # So "full capacity" area = capacity / 4.  This means occupancy maps linearly to density.
    ghat = next((g for g in DB["ghats"] if g["id"] == ghat_id), None)
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
async def ingest_telecom(request: Request):
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
    # Estimate count from telecom ratio and update ghat current_count
    ghat = next((g for g in DB["ghats"] if g["id"] == ghat_id), None)
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
    result = []
    for ghat in DB["ghats"]:
        risk_data = await get_crowd_data(ghat["id"])
        if not risk_data: risk_data = evaluate_from_dicts(ghat)
        result.append(risk_data)
    payload = {"risks": result, "timestamp": time.time()}
    await cache_set(Keys.RISK_ALL, payload, ttl=3)
    return payload

@app.get("/crowd/risk/{ghat_id}")
async def get_ghat_risk(ghat_id: str):
    risk_data = await get_crowd_data(ghat_id)
    if risk_data: return risk_data
    ghat = next((g for g in DB["ghats"] if g["id"] == ghat_id), None)
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
