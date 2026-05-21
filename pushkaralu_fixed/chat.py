from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger("pushkaralu.chat")

GROQ_API_KEY      = os.getenv("GROQ_API_KEY", "")
GROQ_API_URL      = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL        = "llama-3.3-70b-versatile"
MAX_HISTORY_TURNS = 10
CACHE_TTL_SECONDS = 60
RATE_LIMIT        = 20
RATE_WINDOW       = 60

router = APIRouter()

_cache: dict[str, tuple[str, float]] = {}

def _cache_get(key: str) -> Optional[str]:
    entry = _cache.get(key)
    if entry and (time.time() - entry[1]) < CACHE_TTL_SECONDS:
        return entry[0]
    return None

def _cache_set(key: str, value: str) -> None:
    if len(_cache) > 500:
        cutoff = time.time() - CACHE_TTL_SECONDS
        stale = [k for k, (_, t) in _cache.items() if t < cutoff]
        for k in stale:
            _cache.pop(k, None)
    _cache[key] = (value, time.time())


def _build_system_prompt(db: dict) -> str:
    ghats      = db.get("ghats", [])
    transport  = db.get("transport_routes", [])
    helplines  = db.get("helplines", {})
    hospitals  = db.get("hospitals", [])
    facilities = db.get("facilities", [])
    poojas     = db.get("poojas", [])
    hotels     = db.get("hotels", [])
    police     = db.get("police_stations", [])

    # ── GHATS — full detail ───────────────────────────────────────────────────
    ghat_lines = []
    for g in ghats:
        crowd = g.get("crowd_level", "unknown")
        crowd_emoji = "🔴" if crowd == "high" else ("🟡" if crowd == "medium" else "🟢")
        cur = g.get("current_count", 0)
        cap = g.get("capacity", 0)
        pct = int(cur / cap * 100) if cap else 0
        special = ", ".join(g.get("special_dates", []))
        facs = ", ".join(g.get("facilities", []))
        ghat_lines.append(
            f"  • {g['name']} ({g.get('telugu_name','')}) | Zone:{g.get('zone')} | "
            f"{crowd_emoji} {crowd.upper()} crowd ({cur:,}/{cap:,} = {pct}%) | "
            f"Timings: {g.get('bathing_timings')} | "
            f"Near: {g.get('nearest_landmark','')} | "
            f"Special dates: {special or 'none'} | Facilities: {facs}"
        )
    ghats_block = "\n".join(ghat_lines) if ghat_lines else "  Data loading..."

    # ── TRANSPORT — trains and buses separately ───────────────────────────────
    trains = [t for t in transport if t.get("type") == "train"]
    buses  = [t for t in transport if t.get("type") == "bus"]
    special_trains = [t for t in trains if t.get("special_pushkaralu")]

    train_lines = []
    for t in trains[:50]:  # first 50 trains in prompt
        arr = t.get("arrival_rjy", "")
        dep = t.get("departure_rjy", "")
        timing = f"arr {arr}" if arr else ""
        timing += f" / dep {dep}" if dep else ""
        orig = "🟢 STARTS at RJY" if t.get("originates_rjy") else ""
        term = "🔴 ENDS at RJY" if t.get("terminates_rjy") else ""
        special = "✨ PUSHKARALU SPECIAL" if t.get("special_pushkaralu") else ""
        train_lines.append(
            f"  • {t.get('train_number','')} {t.get('train_name','')} | "
            f"{t.get('from','')} → {t.get('to','')} | "
            f"{timing} {orig}{term} {special}".strip()
        )
    trains_block = f"Total {len(trains)} trains via Rajahmundry.\nSpecial Pushkaralu trains: " + \
        ", ".join(f"{t.get('train_number')} {t.get('train_name','')}" for t in special_trains) + \
        "\nSample listing:\n" + "\n".join(train_lines)

    bus_lines = []
    for b in buses:
        times = b.get("departure_times", [])
        time_str = ", ".join(f"{d['time']} ({d.get('service','')})" for d in times[:3])
        freq = f"every {b.get('frequency_mins')} min" if b.get("frequency_mins") else ""
        stops = " → ".join(b.get("stops", []))
        special = "✨ SPECIAL" if b.get("special_pushkaralu") else ""
        bus_lines.append(
            f"  • {b.get('route_number','')} | {b.get('from','')} → {b.get('to','')} | "
            f"{time_str} {freq} | {b.get('operator','')} {special} | Stops: {stops}"
        )
    buses_block = f"Total {len(buses)} APSRTC bus routes.\n" + "\n".join(bus_lines)

    # ── FACILITIES ────────────────────────────────────────────────────────────
    fac_by_type: dict = {}
    for f in facilities:
        t = f.get("type", "other")
        fac_by_type.setdefault(t, []).append(
            f"{f.get('name','')} | Zone:{f.get('zone','')} | {f.get('status','operational')}"
        )
    fac_block = ""
    for ftype, items in fac_by_type.items():
        fac_block += f"\n  {ftype.upper()} ({len(items)}):\n"
        for item in items[:5]:
            fac_block += f"    - {item}\n"

    # ── POOJAS ────────────────────────────────────────────────────────────────
    pooja_lines = []
    for p in poojas:
        pooja_lines.append(
            f"  • {p.get('name','')} ({p.get('telugu_name','')}) — {p.get('description','')[:120]}"
        )
    poojas_block = "\n".join(pooja_lines) if pooja_lines else "  Data loading..."

    # ── HOTELS ────────────────────────────────────────────────────────────────
    hotel_lines = [
        f"  • {h.get('name')} | {h.get('type')} | {h.get('location')} | Area: {h.get('area')}"
        for h in hotels
    ]
    hotels_block = "\n".join(hotel_lines) if hotel_lines else "  Data loading..."

    # ── HOSPITALS ─────────────────────────────────────────────────────────────
    seen_hospitals = set()
    hospital_lines = []
    for h in hospitals:
        key = h.get("name", "") + h.get("location", "")
        if key not in seen_hospitals:
            seen_hospitals.add(key)
            hospital_lines.append(
                f"  • {h.get('location','')} — {h.get('name','')} | Dr. {h.get('doctor','')} | ☎ {h.get('contact','')}"
            )
    hospitals_block = "\n".join(hospital_lines[:20]) if hospital_lines else "  Data loading..."

    # ── HELPLINES ─────────────────────────────────────────────────────────────
    if isinstance(helplines, dict):
        helpline_block = "\n".join(f"  {k}: {v}" for k, v in helplines.items())
    else:
        helpline_block = "  Police: 100 | Ambulance: 108 | Helpline: 1800-425-0066"

    return f"""You are TourGO Pushkara AI — the ONLY official AI assistant for Godavari Pushkaralu 2027.
Festival: June 26 – July 7, 2027 | Location: Rajahmundry (Rajamahendravaram), East Godavari, Andhra Pradesh

═══ STRICT SCOPE RULES ═══
You ONLY answer questions about Godavari Pushkaralu 2027:
  ✅ Ghats, crowd levels, bathing timings, safety
  ✅ Transport (trains, buses, autos, parking) to/from Rajahmundry
  ✅ Facilities (toilets, medical camps, food, water, parking, luggage)
  ✅ Emergency help, SOS, police, ambulance, helplines
  ✅ Poojas, rituals, spiritual guidance for the festival
  ✅ Hotels and accommodation near the festival
  ✅ Lost & found, pilgrim safety

If asked ANYTHING outside this scope (cricket, Google, weather elsewhere, general knowledge, tech, politics, recipes, etc.) respond ONLY with:
"I can only help with Godavari Pushkaralu 2027. Please ask about ghats, transport, facilities, poojas, or emergencies. — TourGO Pushkara AI 🕊"
(Translate refusal to user's language but do NOT answer the off-topic question.)

═══ LANGUAGE RULE ═══
Detect language from user's message. Reply ENTIRELY in that language. Telugu→Telugu. Hindi→Hindi. English→English. Never mix.

═══ RESPONSE STYLE ═══
- Be specific — use ACTUAL names, numbers, timings from the data below
- Keep answers concise and mobile-friendly
- For emergencies ALWAYS include: Police: 100 | Ambulance: 108 | Helpline: 1800-425-0066
- End every on-topic response with: — TourGO Pushkara AI 🕊

════════════════════════════════════════════
LIVE FESTIVAL DATA (use this to answer questions)
════════════════════════════════════════════

── GHATS (15 total) ──
{ghats_block}

── TRAINS ({len(trains)} total via Rajahmundry) ──
{trains_block}

── APSRTC BUSES ──
{buses_block}

── FACILITIES ──
{fac_block}

── POOJAS & RITUALS ──
{poojas_block}

── HOTELS & ACCOMMODATION ──
{hotels_block}

── HOSPITALS ──
{hospitals_block}

── HELPLINES ──
{helpline_block}
"""


class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


@router.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    msg = req.message.strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(msg) > 500:
        raise HTTPException(status_code=400, detail="Message too long (max 500 chars)")

    if not GROQ_API_KEY:
        logger.error("[Chat] GROQ_API_KEY not set")
        raise HTTPException(status_code=503, detail="Chat service not configured. Contact admin.")

    # Rate limiting
    client_ip = request.client.host
    try:
        from app.core.redis_manager import check_rate_limit
        allowed, _ = await check_rate_limit(
            f"rate:chat:{client_ip}", limit=RATE_LIMIT, window_seconds=RATE_WINDOW
        )
        if not allowed:
            raise HTTPException(status_code=429, detail="Too many messages. Please wait a moment.")
    except ImportError:
        pass

    # Cache check
    cache_key = hashlib.md5(msg.lower().encode()).hexdigest()
    cached = _cache_get(cache_key)
    if cached:
        return {"reply": cached, "cached": True}

    # Build system prompt with live DB data
    try:
        from main import DB
        system_prompt = _build_system_prompt(DB)
    except ImportError:
        system_prompt = _build_system_prompt({})

    history = req.history[-(MAX_HISTORY_TURNS * 2):]
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        if h.role in ("user", "assistant"):
            messages.append({"role": h.role, "content": h.content[:400]})
    messages.append({"role": "user", "content": msg})

    # Call Groq
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": messages,
                    "max_tokens": 400,
                    "temperature": 0.4,
                },
            )

        if response.status_code != 200:
            logger.error("[Chat] Groq error %s: %s", response.status_code, response.text[:300])
            raise HTTPException(
                status_code=502,
                detail="AI service temporarily unavailable. Please try again shortly."
            )

        data = response.json()
        reply = data["choices"][0]["message"]["content"].strip()
        _cache_set(cache_key, reply)
        logger.info("[Chat] OK | ip=%s | q=%s", client_ip, msg[:40])
        return {"reply": reply, "cached": False}

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Response took too long. Please try again.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[Chat] Unexpected error: %s", exc)
        raise HTTPException(status_code=500, detail="Something went wrong. For emergencies call 112.")
