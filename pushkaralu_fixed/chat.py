from __future__ import annotations

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


# ── OFF-TOPIC PRE-FILTER ──────────────────────────────────────────────────────
# These words/phrases are ALLOWED — they relate to Pushkaralu
# ── LOCATION INTENT DETECTION ─────────────────────────────────────────────────
_LOCATION_PATTERNS = re.compile(
    r"\b(where is|location of|directions? to|how to (reach|find|get to)|"
    r"map of|navigate to|navigate|show me|address of|coordinates? of|"
    r"ఎక్కడ|స్థానం|దారి|నావిగేట్|"          # Telugu: where, location, route, navigate
    r"कहाँ है|कहां है|स्थान|दिशा|नेविगेट)\b"   # Hindi: where is, location, direction, navigate
    r"|\bghat\b.{0,30}\b(where|location|map|find|reach|address)\b"
    r"|\b(location|map|directions?|navigate)\b.{0,30}\bghat\b",
    re.IGNORECASE,
)

def _wants_location(msg: str) -> bool:
    """Return True if the user is asking for ghat location/directions."""
    return bool(_LOCATION_PATTERNS.search(msg))

def _google_maps_url(lat: float, lng: float, name: str) -> str:
    """Generate a Google Maps link that opens navigation to the ghat."""
    encoded = name.replace(" ", "+")
    return f"https://www.google.com/maps/search/{encoded}/@{lat},{lng},17z"

def _directions_url(lat: float, lng: float) -> str:
    """Generate a Google Maps directions link."""
    return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lng}"

def _build_ghat_location_block(ghats: list) -> str:
    """Build a numbered list of ALL ghats with Google Maps links."""
    valid = [g for g in ghats if g.get("latitude") and g.get("longitude")]
    lines = [f"📍 All {len(valid)} Pushkaralu 2027 Ghats — Locations & Maps:\n"]
    for i, g in enumerate(valid, 1):
        lat  = g["latitude"]
        lng  = g["longitude"]
        name = g.get("name", "")
        telugu   = g.get("telugu_name", "")
        zone     = g.get("zone", "")
        landmark = g.get("nearest_landmark", "")
        crowd    = g.get("crowd_level", "low")
        crowd_emoji = "🔴" if crowd == "critical" else ("🟠" if crowd == "high" else ("🟡" if crowd == "medium" else "🟢"))
        maps_link = _google_maps_url(lat, lng, name)
        nav_link  = _directions_url(lat, lng)
        lines.append(
            f"{i}. {crowd_emoji} {name} ({telugu})\n"
            f"   Zone: {zone} | Near: {landmark}\n"
            f"   🗺 {maps_link}\n"
            f"   🧭 {nav_link}\n"
        )
    lines.append("— TourGO Pushkara AI 🕊")
    return "\n".join(lines)


# ── EMERGENCY FAST-PATH ───────────────────────────────────────────────────────
_EMERGENCY_PATTERNS = re.compile(
    r"(emergency|emergencies|sos|helpline|helplines|help.?line|"
    r"ambulance|police|fire.?(brigade|station|emergency)?|"
    r"hospital|doctor|medical|first.?aid|"
    r"contact|number|phone|call|dial|"
    r"అత్యవసర|సహాయం|పోలీస్|ఆంబులెన్స్|"    # Telugu
    r"आपातकाल|आपातकालीन|पुलिस|एम्बुलेंस)",   # Hindi
    re.IGNORECASE,
)

def _wants_emergency(msg: str) -> bool:
    return bool(_EMERGENCY_PATTERNS.search(msg))

def _build_emergency_reply(db: dict) -> str:
    helplines = db.get("helplines", {})
    lines = [
        "🆘 *Emergency Numbers — Godavari Pushkaralu 2027*\n",
        "📞 *Key Helplines:*",
        "  🚨 National Emergency: *112*",
        "  👮 Police: *100*",
        "  🚑 Ambulance: *108*",
        "  🔥 Fire: *101*",
        "  🏛 NDRF Control Room: *1916*",
        "  🎪 Pushkaralu Festival Helpline: *1800-425-0066* (toll free)",
        "  📋 Collectorate Control Room: *1800-425-3077*",
        "  ☎ State Call Centre: *1100*",
    ]
    if helplines:
        extra = {k: v for k, v in helplines.items()
                 if k not in ("police","ambulance","fire","ndrf","pushkaralu_helpline",
                              "state_call_centre","control_room_collectorate")}
        if extra:
            lines.append("\n📋 *Additional:*")
            for k, v in extra.items():
                label = k.replace("_", " ").title()
                lines.append(f"  • {label}: {v}")
    lines += [
        "\n⚠️ *In any emergency: call 112 immediately.*",
        "\n— TourGO Pushkara AI 🕊",
    ]
    return "\n".join(lines)


def _find_mentioned_ghat(msg: str, ghats: list) -> dict | None:
    """Return the ghat dict if the message mentions a specific ghat name."""
    lower = msg.lower().strip()
    # Try longest match first to avoid partial hits
    for g in sorted(ghats, key=lambda x: len(x.get("name", "")), reverse=True):
        name = g.get("name", "").lower()
        telugu = g.get("telugu_name", "").lower()
        # Match if the ghat name appears anywhere in the message
        if name in lower or (telugu and telugu in lower):
            return g
    return None


def _build_single_ghat_reply(g: dict) -> str:
    """Build a clean, human-readable reply for one specific ghat."""
    name      = g.get("name", "")
    telugu    = g.get("telugu_name", "")
    zone      = g.get("zone", "")
    landmark  = g.get("nearest_landmark", "")
    crowd     = g.get("crowd_level", "low")
    cur       = g.get("current_count", 0)
    cap       = g.get("capacity", 1)
    pct       = int(cur / cap * 100) if cap else 0
    timings   = g.get("bathing_timings", "")
    special   = g.get("special_dates", [])
    facs_raw  = g.get("facilities", [])
    lat       = g.get("latitude")
    lng       = g.get("longitude")

    # Human-readable crowd level
    crowd_map = {
        "low":      "✅ LOW — comfortable, good time to visit",
        "medium":   "🟡 MODERATE — manageable crowds",
        "high":     "🟠 HIGH — very crowded, stay alert",
        "critical": "🔴 CRITICAL — extremely crowded, consider another ghat",
    }
    crowd_text = crowd_map.get(crowd, crowd.upper())
    crowd_line = f"{crowd_text} ({cur:,} of {cap:,} capacity, {pct}% full)"

    # Clean up facility names (remove underscores)
    facs = ", ".join(f.replace("_", " ").title() for f in facs_raw) or "Not listed"

    # Special dates
    special_text = ", ".join(special) if special else "No special dates listed"

    # Maps links
    maps_url = _google_maps_url(lat, lng, name) if lat and lng else None
    nav_url  = _directions_url(lat, lng) if lat and lng else None

    lines = [
        f"🏛 *{name}* ({telugu})",
        f"📍 Zone: {zone} | Near: {landmark}",
        f"",
        f"👥 Crowd: {crowd_line}",
        f"🕐 Bathing Timings: {timings}",
        f"🗓 Special Dates: {special_text}",
        f"🏥 Facilities: {facs}",
    ]
    if maps_url:
        lines += [
            f"",
            f"🗺 View on map: {maps_url}",
            f"🧭 Get directions: {nav_url}",
        ]
    lines.append("")
    lines.append("— TourGO Pushkara AI 🕊")
    return "\n".join(lines)


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
    # location & navigation
    "location", "map", "directions", "direction", "navigate", "navigation",
    "where is", "how to reach", "how to get", "address", "coordinates",
    "ఎక్కడ", "స్థానం", "దారి", "నావిగేట్",
    "कहाँ", "कहां", "स्थान", "दिशा", "नेविगेट",
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
        lat = g.get("latitude")
        lng = g.get("longitude")
        maps_url = _google_maps_url(lat, lng, g["name"]) if lat and lng else "N/A"
        nav_url  = _directions_url(lat, lng) if lat and lng else "N/A"
        ghat_lines.append(
            f"GHAT: {g['name']} | Telugu: {g.get('telugu_name','')} | Zone: {g.get('zone')} | "
            f"Crowd: {crowd_emoji} {crowd.upper()} ({cur:,}/{cap:,}, {pct}%) | "
            f"Bathing: {g.get('bathing_timings')} | Landmark: {g.get('nearest_landmark','')} | "
            f"Special dates: {special or 'none'} | Facilities: {facs} | "
            f"GoogleMaps: {maps_url} | Directions: {nav_url}"
        )
    ghats_block = "\n".join(ghat_lines) or "  Data loading..."

    # TRANSPORT
    trains = [t for t in transport if t.get("type") == "train"]
    buses  = [t for t in transport if t.get("type") == "bus"]
    special_trains = [t for t in trains if t.get("special_pushkaralu")]

    train_lines = []
    for t in trains[:50]:
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
    sp_names = ", ".join(f"{t.get('train_number')} {t.get('train_name','')}" for t in special_trains)
    trains_block = (
        f"Total {len(trains)} trains via Rajahmundry.\n"
        f"Special Pushkaralu trains: {sp_names or 'none'}\nSample:\n" +
        "\n".join(train_lines)
    )

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
        for item in items[:5]:
            fac_block += f"    - {item}\n"

    # POOJAS
    pooja_lines = [
        f"  • {p.get('name','')} ({p.get('telugu_name','')}) — {p.get('description','')[:120]}"
        for p in poojas
    ]
    poojas_block = "\n".join(pooja_lines) or "  Data loading..."

    # HOTELS
    hotel_lines = [
        f"  • {h.get('name')} | {h.get('type')} | {h.get('location')} | Area: {h.get('area')}"
        for h in hotels
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
    hospitals_block = "\n".join(hospital_lines[:20]) or "  Data loading..."

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

RESPONSE FORMAT RULES — FOLLOW STRICTLY:
1. NEVER echo raw data pipe strings. Always write natural, friendly sentences.
2. FACILITIES: always convert underscore_names to plain English (medical_camp → Medical Camp).
3. CROWD: use plain language — "very crowded (90% full)" not "CRITICAL (7200/8000)".
4. MAPS LINKS: Every ghat has a GoogleMaps and Directions link in the data. ALWAYS include both when mentioning any ghat.
5. SINGLE GHAT question — reply format:
   🏛 [Name] ([Telugu name])
   📍 [Zone] | Near: [Landmark]
   👥 Crowd: [plain English crowd status]
   🕐 Bathing: [timings]
   🗓 Special dates: [dates or "none"]
   🏥 Facilities: [plain English list]
   🗺 View on map: [GoogleMaps link]
   🧭 Get directions: [Directions link]
   — TourGO Pushkara AI 🕊
6. ALL GHATS question — list EVERY single ghat (all 15), numbered, each with its GoogleMaps and Directions link. Do not skip any.
7. EMERGENCY question — list all helpline numbers: 112, 100, 108, 101, 1916, 1800-425-0066.
8. Keep single-ghat replies to 10 lines max. For all-ghats list, show all 15 with links.
9. End every on-topic reply with: — TourGO Pushkara AI 🕊

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

    # ── PRE-FILTER: block off-topic before hitting Groq ──────────────────────
    if _is_off_topic(msg):
        refusal = _refusal_for_lang(msg)
        logger.info("[Chat] BLOCKED off-topic | q=%s", msg[:60])
        return {"reply": refusal, "cached": False, "filtered": True}

    # ── API key check ────────────────────────────────────────────────────────
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
                    "max_tokens": 1200,
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
