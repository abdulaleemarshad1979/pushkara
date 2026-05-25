from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger("pushkaralu.chat")

GROQ_API_KEY      = os.getenv("GROQ_API_KEY", "")
GROQ_API_URL      = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL         = "llama-3.3-70b-versatile"
GROQ_MODEL_FALLBACK = "llama-3.1-8b-instant"   # used when primary hits rate limit (429)
MAX_HISTORY_TURNS = 10
CACHE_TTL_SECONDS = 60
RATE_LIMIT        = 20
RATE_WINDOW       = 60

router = APIRouter()

_cache: dict[str, tuple[str, float]] = {}

# ── FIX (A6): cache the heavy system prompt ──────────────────────────────────
# Building the prompt iterates over every ghat / train / bus / facility /
# pooja / hotel / hospital and produces a ~10 KB string. Doing this on EVERY
# request burns ~5–8 ms CPU per chat. Since the underlying data only changes
# slowly (admin updates), we cache the rendered prompt for SYSTEM_PROMPT_TTL
# seconds keyed by a cheap stable signature of the input data.
SYSTEM_PROMPT_TTL = 30   # seconds
_system_prompt_cache: tuple[str, str, float] | None = None  # (signature, prompt, expiry)


def _cache_get(key: str) -> Optional[str]:
    entry = _cache.get(key)
    if entry and (time.time() - entry[1]) < CACHE_TTL_SECONDS:
        return entry[0]
    return None

def _cache_set(key: str, value: str) -> None:
    # FIX (A5): the previous version only evicted *stale* entries — under
    # sustained load (all entries fresh) the dict grew unbounded past 500.
    # If no stale entries exist, fall back to LRU drop of the oldest items
    # so memory stays bounded.
    if len(_cache) > 500:
        cutoff = time.time() - CACHE_TTL_SECONDS
        stale = [k for k, (_, t) in _cache.items() if t < cutoff]
        for k in stale:
            _cache.pop(k, None)
        # If stale cleanup didn't reclaim enough room, drop the oldest entries.
        if len(_cache) > 500:
            oldest = sorted(_cache.items(), key=lambda kv: kv[1][1])[:max(1, len(_cache) - 400)]
            for k, _ in oldest:
                _cache.pop(k, None)
    _cache[key] = (value, time.time())


# ── OFF-TOPIC PRE-FILTER ──────────────────────────────────────────────────────
# These words/phrases are ALLOWED — they relate to Pushkaralu
_ALLOWED_KEYWORDS = {
    # festival core
    "pushkar", "pushkara", "godavari", "ghat", "ganga", "bathing", "ritual",
    "pooja", "puja", "snan", "holy", "sacred", "dip", "pilgrimage", "pilgrim",
    "festival", "mela", "utsav", "2027", "rajahmundry", "rajamahendravaram",
    # transport
    "train", "bus", "auto", "taxi", "cab", "parking", "route", "station",
    "apsrtc", "irctc", "railway", "transport", "travel", "reach", "how to go",
    "journey", "schedule", "timing", "departure", "arrival",
    # facilities & safety
    "toilet", "washroom", "restroom", "food", "water", "medical", "camp",
    "ambulance", "hospital", "doctor", "first aid", "luggage", "cloak",
    "wheelchair", "disabled", "crowd", "safe", "safety", "lost", "found",
    "missing", "child", "police", "help", "emergency", "sos", "helpline",
    "hotel", "accommodation", "stay", "lodge", "dharamshala",
    # Telugu / Hindi terms
    "స్నానం", "ఘాట్", "పూజ", "పుష్కర", "గోదావరి", "రాజమహేంద్రవరం",
    "స్నान", "घाट", "पूजा", "पुष्कर", "गोदावरी",
    # greetings / meta
    "hello", "hi", "namaste", "namaskar", "నమస్కారం", "నమస్తే",
    "helo", "hey", "thank", "thanks", "ok", "okay", "yes", "no",
    "what", "which", "where", "when", "how", "who", "list",
    "tell me", "show me", "give me", "can you", "please",
}

# Topics that are clearly off-topic — checked ONLY if no allowed keyword matched
_BLOCKED_PATTERNS = [
    r"\bcricket\b", r"\bsachin\b", r"\bvirat\b", r"\bipl\b",
    r"\brecipe\b", r"\bcook\b", r"\bchicken\b", r"\bbiriyani\b",
    r"\bpolitics\b", r"\belection\b", r"\bminister\b", r"\bgovernment\b",
    r"\bweather\b",                        # weather elsewhere; festival weather is ok
    r"\bgoogle\b", r"\bbing\b", r"\bgpt\b", r"\bchatgpt\b", r"\bai tool\b",
    r"\bstock\b", r"\bshare price\b", r"\bcrypto\b", r"\bbitcoin\b",
    r"\bmovie\b", r"\bfilm\b", r"\bsong\b", r"\blyric\b",
    r"\bjoke\b", r"\bfunny\b",
    r"\bexam\b", r"\bsyllabus\b", r"\bcollege\b", r"\buniversity\b",
    r"\bjob\b", r"\bsalary\b", r"\bresume\b", r"\binterview\b",
]
_BLOCKED_RE = re.compile("|".join(_BLOCKED_PATTERNS), re.IGNORECASE)

_REFUSAL = (
    "I can only help with Godavari Pushkaralu 2027. "
    "Please ask about ghats, transport, facilities, poojas, or emergencies. "
    "— TourGO Pushkara AI 🕊"
)
_REFUSAL_TE = (
    "నేను కేవలం గోదావరి పుష్కరాలు 2027 గురించి మాత్రమే సహాయం చేయగలను. "
    "దయచేసి ఘాట్లు, రవాణా, సౌకర్యాలు, పూజలు లేదా అత్యవసర పరిస్థితుల గురించి అడగండి. "
    "— TourGO Pushkara AI 🕊"
)
_REFUSAL_HI = (
    "मैं केवल गोदावरी पुष्करालु 2027 के बारे में सहायता कर सकता हूँ। "
    "कृपया घाट, परिवहन, सुविधाएँ, पूजा या आपात स्थिति के बारे में पूछें। "
    "— TourGO Pushkara AI 🕊"
)


def _is_off_topic(msg: str) -> bool:
    """Return True if the message is clearly off-topic."""
    lower = msg.lower()
    # If any allowed keyword appears → let it through
    for kw in _ALLOWED_KEYWORDS:
        if kw in lower:
            return False
    # No allowed keyword found — check blocked patterns
    return bool(_BLOCKED_RE.search(msg))


def _detect_lang(msg: str) -> str:
    """Very rough language detect: te / hi / en."""
    # Telugu Unicode block: 0C00–0C7F
    if re.search(r"[\u0C00-\u0C7F]", msg):
        return "te"
    # Devanagari: 0900–097F
    if re.search(r"[\u0900-\u097F]", msg):
        return "hi"
    return "en"


def _refusal_for_lang(msg: str) -> str:
    lang = _detect_lang(msg)
    if lang == "te":
        return _REFUSAL_TE
    if lang == "hi":
        return _REFUSAL_HI
    return _REFUSAL


# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────────

def _build_system_prompt(db: dict) -> str:
    ghats      = db.get("ghats", [])
    transport  = db.get("transport_routes", [])
    helplines  = db.get("helplines", {})
    hospitals  = db.get("hospitals", [])
    facilities = db.get("facilities", [])
    poojas     = db.get("poojas", [])
    hotels     = db.get("hotels", [])

    # GHATS
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
            f"{crowd_emoji} {crowd.upper()} ({cur:,}/{cap:,} = {pct}%) | "
            f"Timings: {g.get('bathing_timings')} | Near: {g.get('nearest_landmark','')} | "
            f"Special: {special or 'none'} | Facilities: {facs}"
        )
    ghats_block = "\n".join(ghat_lines) or "  Data loading..."

    # TRANSPORT
    trains = [t for t in transport if t.get("type") == "train"]
    buses  = [t for t in transport if t.get("type") == "bus"]
    special_trains = [t for t in trains if t.get("special_pushkaralu")]

    train_lines = []
    for t in trains[:8]:  # trimmed: free tier TPM limit
        arr = t.get("arrival_rjy", "")
        dep = t.get("departure_rjy", "")
        timing = (f"arr {arr}" if arr else "") + (f" / dep {dep}" if dep else "")
        tags = " ".join(filter(None, [
            "🟢 STARTS at RJY" if t.get("originates_rjy") else "",
            "🔴 ENDS at RJY"   if t.get("terminates_rjy") else "",
            "✨ PUSHKARALU SPECIAL" if t.get("special_pushkaralu") else "",
        ]))
        train_lines.append(
            f"  • {t.get('train_number','')} {t.get('train_name','')} | "
            f"{t.get('from','')} → {t.get('to','')} | {timing} {tags}".strip()
        )
    sp_names = ", ".join(f"{t.get('train_number')} {t.get('train_name','')}" for t in special_trains[:5])
    trains_block = (
        f"Total {len(trains)} trains via Rajahmundry.\n"
        f"Special Pushkaralu trains: {sp_names or 'none'}\nSample (8 of {len(trains)}):\n" +
        "\n".join(train_lines)
    )

    bus_lines = []
    for b in buses[:5]:  # trimmed: free tier TPM limit
        times = b.get("departure_times", [])
        time_str = ", ".join(f"{d['time']} ({d.get('service','')})" for d in times[:3])
        freq = f"every {b.get('frequency_mins')} min" if b.get("frequency_mins") else ""
        stops = " → ".join(b.get("stops", []))
        special = "✨ SPECIAL" if b.get("special_pushkaralu") else ""
        bus_lines.append(
            f"  • {b.get('route_number','')} | {b.get('from','')} → {b.get('to','')} | "
            f"{time_str} {freq} | {b.get('operator','')} {special} | Stops: {stops}"
        )
    buses_block = f"Total {len(buses)} APSRTC routes.\n" + "\n".join(bus_lines)

    # FACILITIES
    fac_by_type: dict = {}
    for f in facilities:
        t = f.get("type", "other")
        fac_by_type.setdefault(t, []).append(
            f"{f.get('name','')} | Zone:{f.get('zone','')} | {f.get('status','operational')}"
        )
    fac_block = ""
    for ftype, items in fac_by_type.items():
        fac_block += f"\n  {ftype.upper()} ({len(items)}):\n"
        for item in items[:2]:  # trimmed: free tier TPM limit
            fac_block += f"    - {item}\n"

    # POOJAS
    pooja_lines = [
        f"  • {p.get('name','')} ({p.get('telugu_name','')}) — {p.get('description','')[:80]}"
        for p in poojas[:6]  # trimmed for free-tier TPM limit
    ]
    poojas_block = "\n".join(pooja_lines) or "  Data loading..."

    # HOTELS
    hotel_lines = [
        f"  • {h.get('name')} | {h.get('type')} | {h.get('location')} | Area: {h.get('area')}"
        for h in hotels[:5]  # trimmed for free-tier TPM limit
    ]
    hotels_block = "\n".join(hotel_lines) or "  Data loading..."

    # HOSPITALS
    seen = set()
    hospital_lines = []
    for h in hospitals:
        key = h.get("name", "") + h.get("location", "")
        if key not in seen:
            seen.add(key)
            hospital_lines.append(
                f"  • {h.get('location','')} — {h.get('name','')} | "
                f"Dr. {h.get('doctor','')} | ☎ {h.get('contact','')}"
            )
    hospitals_block = "\n".join(hospital_lines[:6]) or "  Data loading..."  # trimmed

    # HELPLINES
    if isinstance(helplines, dict):
        helpline_block = "\n".join(f"  {k}: {v}" for k, v in helplines.items())
    else:
        helpline_block = "  Police: 100 | Ambulance: 108 | Helpline: 1800-425-0066"

    return f"""You are TourGO Pushkara AI — the official AI assistant for Godavari Pushkaralu 2027.
Festival: June 26 – July 7, 2027 | Location: Rajahmundry (Rajamahendravaram), Andhra Pradesh

SCOPE: Answer ONLY about Godavari Pushkaralu 2027 — ghats, transport, facilities, poojas, hotels, hospitals, emergencies.
For ANYTHING else respond ONLY: "I can only help with Godavari Pushkaralu 2027. Please ask about ghats, transport, facilities, poojas, or emergencies. — TourGO Pushkara AI 🕊"

LANGUAGE: Reply entirely in the user's language (Telugu→Telugu, Hindi→Hindi, English→English).

STYLE: Be specific — use actual names, numbers, timings from data below. Keep answers concise.
For emergencies always include: Police: 100 | Ambulance: 108 | Helpline: 1800-425-0066
End every on-topic reply with: — TourGO Pushkara AI 🕊

════════════ LIVE FESTIVAL DATA ════════════

── GHATS ({len(ghats)} total) ──
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


# ── FIX (A6): cached wrapper for the heavy prompt builder ────────────────────
def _cheap_db_signature(db: dict) -> str:
    """
    Cheap, stable signature of the prompt-relevant DB state.
    Uses lengths only — does not iterate items — so it's O(1) and skips the
    cache only when the admin actually adds/removes a record.
    """
    return (
        f"g{len(db.get('ghats', []))}"
        f":t{len(db.get('transport_routes', []))}"
        f":f{len(db.get('facilities', []))}"
        f":p{len(db.get('poojas', []))}"
        f":h{len(db.get('hotels', []))}"
        f":m{len(db.get('hospitals', []))}"
    )


def _get_cached_system_prompt(db: dict) -> str:
    global _system_prompt_cache
    sig = _cheap_db_signature(db)
    now = time.time()
    if _system_prompt_cache is not None:
        cached_sig, cached_prompt, expiry = _system_prompt_cache
        if cached_sig == sig and now < expiry:
            return cached_prompt
    prompt = _build_system_prompt(db)
    _system_prompt_cache = (sig, prompt, now + SYSTEM_PROMPT_TTL)
    return prompt


def _real_client_ip(request: Request) -> str:
    """
    FIX (A4): in production all chat traffic hits the API through nginx, so
    `request.client.host` returns the nginx container IP and rate-limit triggers
    globally for every user. Honour the X-Forwarded-For chain (left-most entry
    is the original client) when present, with a safe fallback.
    """
    xff = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    real_ip = request.headers.get("x-real-ip") or request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    try:
        return request.client.host or "unknown"
    except Exception:
        return "unknown"


# ── REQUEST / RESPONSE MODELS ─────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


# ── ENDPOINTS ─────────────────────────────────────────────────────────────────

@router.get("/api/health")
async def health():
    """Lightweight ping — call on page load to wake Render from cold sleep."""
    return {"status": "ok"}


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

    # ── PRE-FILTER: block off-topic before hitting Groq ──────────────────────
    if _is_off_topic(msg):
        refusal = _refusal_for_lang(msg)
        logger.info("[Chat] BLOCKED off-topic | q=%s", msg[:60])
        return {"reply": refusal, "cached": False, "filtered": True}

    # Rate limiting
    client_ip = _real_client_ip(request)
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

    # Build system prompt with live DB data (cached, see A6)
    try:
        from main import DB
        system_prompt = _get_cached_system_prompt(DB)
    except ImportError:
        system_prompt = _get_cached_system_prompt({})

    history = req.history[-(MAX_HISTORY_TURNS * 2):]
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        if h.role in ("user", "assistant"):
            messages.append({"role": h.role, "content": h.content[:400]})
    messages.append({"role": "user", "content": msg})

    # Call Groq  (retry up to 3 attempts on 429 with backoff)
    try:
        _groq_headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=25.0) as client:
            response = None
            for attempt in range(3):
                _model = GROQ_MODEL if attempt == 0 else GROQ_MODEL_FALLBACK
                response = await client.post(
                    GROQ_API_URL,
                    headers=_groq_headers,
                    json={
                        "model": _model,
                        "messages": messages,
                        "max_tokens": 400,
                        "temperature": 0.4,
                    },
                )
                if response.status_code == 200:
                    break
                if response.status_code == 429:
                    wait = min(float(response.headers.get("retry-after", 2 * (attempt + 1))), 8)
                    logger.warning("[Chat] 429 rate limit attempt=%d, waiting %.1fs, next model=%s",
                                   attempt + 1, wait, GROQ_MODEL_FALLBACK)
                    await asyncio.sleep(wait)
                    continue
                break  # other error, stop retrying

        if response is None or response.status_code != 200:
            logger.error("[Chat] Groq error status=%s body=%s",
                         response.status_code if response else "none",
                         response.text[:300] if response else "")
            if response and response.status_code == 401:
                raise HTTPException(status_code=503, detail="Chat service not configured. Contact admin.")
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
