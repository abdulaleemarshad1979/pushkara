# ═══════════════════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — AI Chatbot Route  (v1.0)
#
# HOW TO INTEGRATE:
#   1. Add to requirements.txt:   groq==0.9.0
#   2. Add to render.yaml envVars: GROQ_API_KEY (sync: false)
#   3. In main.py, add at the top imports section:
#        from chat import router as chat_router
#   4. In main.py, after app = FastAPI(...) lines, add:
#        app.include_router(chat_router)
#
# FEATURES:
#   - Groq (llama3-8b-8192) for fast, free responses
#   - Pilgrim-context system prompt using your live DB data
#   - Per-IP rate limiting (reuses your existing Redis rate limiter)
#   - Conversation history trimming (max 10 turns to avoid token explosion)
#   - Graceful fallback if Groq is down
#   - Short response caching for repeated common questions
# ═══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("pushkaralu.chat")

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_API_KEY      = os.getenv("GROQ_API_KEY", "")
GROQ_API_URL      = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL        = "llama-3.3-70b-versatile"   # better multilingual (Telugu/Hindi/English)
MAX_HISTORY_TURNS = 10          # keep last 10 user+assistant pairs
CACHE_TTL_SECONDS = 60          # cache identical questions for 60s
RATE_LIMIT        = 20          # max requests per IP per minute
RATE_WINDOW       = 60

router = APIRouter()

# ── In-memory response cache (simple dict, TTL-based) ────────────────────────
_cache: dict[str, tuple[str, float]] = {}

def _cache_get(key: str) -> Optional[str]:
    entry = _cache.get(key)
    if entry and (time.time() - entry[1]) < CACHE_TTL_SECONDS:
        return entry[0]
    return None

def _cache_set(key: str, value: str) -> None:
    # Evict old entries if cache grows too large
    if len(_cache) > 500:
        cutoff = time.time() - CACHE_TTL_SECONDS
        stale = [k for k, (_, t) in _cache.items() if t < cutoff]
        for k in stale:
            _cache.pop(k, None)
    _cache[key] = (value, time.time())

# ── System prompt builder — injects live DB context ──────────────────────────
def _build_system_prompt(db: dict) -> str:
    ghats = db.get("ghats", [])
    transport = db.get("transport_routes", [])
    helplines = db.get("helplines", {})
    hospitals = db.get("hospitals", [])
    police = db.get("police_stations", [])
    facilities = db.get("facilities", [])
    poojas = db.get("poojas", [])

    # Summarise ghat names + crowd levels
    ghat_summary = ", ".join(
        f"{g['name']} ({g.get('crowd_level','unknown')} crowd)"
        for g in ghats[:15]
    ) if ghats else "data loading"

    # Summarise transport types available
    transport_types = list({r.get("type","") for r in transport if r.get("type")})
    transport_summary = ", ".join(transport_types) if transport_types else "buses, autos"

    # Helpline numbers
    helpline_lines = ""
    if isinstance(helplines, dict):
        helpline_lines = "\n".join(f"  - {k}: {v}" for k, v in list(helplines.items())[:8])
    elif isinstance(helplines, list):
        helpline_lines = "\n".join(f"  - {h.get('name','')}: {h.get('phone','')}" for h in helplines[:8])

    # Hospital summary
    hospital_summary = ", ".join(h.get("name","") for h in hospitals[:5]) if hospitals else "checking data"

    # Facility types
    fac_types = list({f.get("type","") for f in facilities if f.get("type")})[:8]

    # Pooja schedule
    pooja_summary = "; ".join(
        f"{p.get('name','')} on {p.get('date','')}" for p in poojas[:5]
    ) if poojas else "schedule loading"

    return f"""You are TourGO Pushkara AI, the official pilgrim assistant ONLY for Godavari Pushkaralu 2027. You are powered by TourGO.

STRICT RULES — NON-NEGOTIABLE:
1. You ONLY answer questions about Godavari Pushkaralu 2027 and directly related topics:
   - Ghats (locations, crowd levels, bathing timings, safety)
   - Transport to/from Rajahmundry (buses, trains, autos, parking)
   - Festival facilities (toilets, medical camps, food stalls, drinking water, lost & found)
   - Emergency help (SOS, police, ambulance, helpline numbers)
   - Pooja schedules, rituals, spiritual guidance for the festival
   - Accommodation near the festival
   - General pilgrim safety and navigation during the festival
2. If asked ANYTHING else (cricket, weather, general knowledge, other cities, how to use Google, recipes, politics, technology, etc.) — reply ONLY with:
   "I can only help with Godavari Pushkaralu 2027 questions. Please ask about ghats, transport, facilities, poojas, or emergency help. — TourGO Pushkara AI 🕊"
   Translate that refusal into the user's language but do NOT answer the off-topic question.
3. Language: Detect the user's language and reply ENTIRELY in that language. Telugu→Telugu, Hindi→Hindi, English→English. Never mix.
4. Keep answers short and practical — pilgrims are on mobile in crowds.
5. For ANY emergency, ALWAYS include: Police: 100 | Ambulance: 108 | Helpline: 1800-425-8877
6. End every on-topic response with: — TourGO Pushkara AI 🕊

LIVE FESTIVAL DATA:
Ghats: {ghat_summary}
Transport: {transport_summary}
Facilities: {", ".join(fac_types)}
Pooja schedule: {pooja_summary}
Hospitals: {hospital_summary}
Helplines:
{helpline_lines}

Festival: Godavari Pushkaralu 2027 | Dates: June 26 – July 7, 2027 | Location: Rajahmundry, East Godavari, Andhra Pradesh"""


# ── Request / Response models ─────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: str    # "user" or "assistant"
    content: str

class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


# ── Main chat endpoint ────────────────────────────────────────────────────────
@router.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    # ── 0. Basic validation ──────────────────────────────────────────────────
    msg = req.message.strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(msg) > 500:
        raise HTTPException(status_code=400, detail="Message too long (max 500 chars)")

    if not GROQ_API_KEY:
        logger.error("[Chat] GROQ_API_KEY not set")
        raise HTTPException(status_code=503, detail="Chat service not configured")

    # ── 1. Rate limiting (per IP, reuse Redis if available) ──────────────────
    client_ip = request.client.host
    try:
        from app.core.redis_manager import check_rate_limit
        allowed, _ = await check_rate_limit(
            f"rate:chat:{client_ip}", limit=RATE_LIMIT, window_seconds=RATE_WINDOW
        )
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many messages. Please wait a moment before continuing."
            )
    except ImportError:
        pass  # Redis not available — skip rate limiting

    # ── 2. Check response cache for identical question ────────────────────────
    cache_key = hashlib.md5(msg.lower().encode()).hexdigest()
    cached = _cache_get(cache_key)
    if cached:
        logger.debug("[Chat] Cache hit for: %s", msg[:40])
        return {"reply": cached, "cached": True}

    # ── 3. Build messages array (trim history to avoid token explosion) ───────
    # Import DB from main — late import to avoid circular dependency
    try:
        from main import DB
        system_prompt = _build_system_prompt(DB)
    except ImportError:
        system_prompt = _build_system_prompt({})

    # Keep only last MAX_HISTORY_TURNS pairs
    history = req.history[-(MAX_HISTORY_TURNS * 2):]

    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        if h.role in ("user", "assistant"):
            messages.append({"role": h.role, "content": h.content[:300]})  # trim each msg
    messages.append({"role": "user", "content": msg})

    # ── 4. Call Groq API ──────────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": messages,
                    "max_tokens": 300,
                    "temperature": 0.6,
                },
            )

        if response.status_code != 200:
            logger.error("[Chat] Groq error %s: %s", response.status_code, response.text[:200])
            raise HTTPException(
                status_code=502,
                detail="AI service temporarily unavailable. Please try again shortly."
            )

        data = response.json()
        reply = data["choices"][0]["message"]["content"].strip()

        # Cache the reply
        _cache_set(cache_key, reply)

        logger.info("[Chat] OK | ip=%s | q=%s", client_ip, msg[:40])
        return {"reply": reply, "cached": False}

    except httpx.TimeoutException:
        logger.error("[Chat] Groq timeout for ip=%s", client_ip)
        raise HTTPException(
            status_code=504,
            detail="Response took too long. Please try again."
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[Chat] Unexpected error: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Something went wrong. For emergencies call 112."
        )
