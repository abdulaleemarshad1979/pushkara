# ═══════════════════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — WhatsApp Intent Router
#
# Parses an incoming WhatsApp message and dispatches it to the Pushkaralu
# internal logic — SOS, ghat status, nearest help, lost & found, helplines.
#
# Design rules:
#   1. NEVER crash on bad input — every handler returns a friendly text reply.
#   2. NEVER make a SOS path depend on a successful WhatsApp confirmation;
#      the SOS itself is the source of truth, the WA reply is best-effort.
#   3. Language: replies are bilingual (English + Telugu summary) for short
#      operational replies; long replies stay English.
#   4. No HTTP self-calls — we reach into main.DB / helpers in-process.
# ═══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("pushkaralu.whatsapp.router")

# Minimal local copy of haversine to avoid pulling main into module-load
# (location_utils is fine, no circulars there).
from utils.location_utils import haversine, nearest_in_list


# ── Incoming message canonical shape ────────────────────────────────────────
@dataclass
class IncomingMessage:
    """Normalized inbound message — provider-agnostic."""

    from_phone: str                  # E.164-ish without leading '+', e.g. '919876543210'
    body: str = ""                   # text content; '' if location-only
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    media_url: Optional[str] = None  # first attached media (photo)
    contact_name: Optional[str] = None  # WhatsApp profile name if provider sent it
    raw: dict = field(default_factory=dict)


@dataclass
class RouterReply:
    text: str
    intent: str
    handled: bool = True
    side_effects: list = field(default_factory=list)  # e.g. ['sos_created:<id>']


# ── Helpers ────────────────────────────────────────────────────────────────
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _detect_lang(msg: str) -> str:
    if not msg:
        return "en"
    if re.search(r"[\u0C00-\u0C7F]", msg):
        return "te"
    if re.search(r"[\u0900-\u097F]", msg):
        return "hi"
    return "en"


def _first_token(body: str) -> str:
    if not body:
        return ""
    return body.strip().split()[0].upper() if body.strip() else ""


def _has_location(m: IncomingMessage) -> bool:
    return m.latitude is not None and m.longitude is not None


def _crowd_emoji(level: str) -> str:
    return {
        "low": "🟢",
        "medium": "🟡",
        "high": "🔴",
        "critical": "🟣",
    }.get(level or "", "⚪")


SIGNATURE = "— TourGO Pushkara 🕊"


# ── Menu / help text ───────────────────────────────────────────────────────
def _menu_text(lang: str = "en") -> str:
    en = (
        "🙏 Welcome to Godavari Pushkaralu 2027.\n"
        "Reply with one of:\n\n"
        "• *SOS* + share your live location → get the nearest volunteer\n"
        "• *GHATS* → live crowd levels at all 12 ghats\n"
        "• *GHAT <name>* → status of one ghat\n"
        "• *NEAREST* + share location → nearest hospital, police, ghat\n"
        "• *HELPLINE* → emergency phone numbers\n"
        "• *LOST <name, age, last seen>* → register a missing person\n\n"
        "For any other question, just type your query in plain English, "
        "Telugu or Hindi.\n" + SIGNATURE
    )
    te = (
        "🙏 గోదావరి పుష్కరాలు 2027 కి స్వాగతం.\n"
        "ఈ ఆదేశాలను పంపండి:\n\n"
        "• *SOS* + మీ లైవ్ లొకేషన్ షేర్ చేయండి → దగ్గరి వాలంటీర్\n"
        "• *GHATS* → 12 ఘాట్ల ప్రస్తుత రద్దీ\n"
        "• *GHAT <పేరు>* → ఒక ఘాట్ స్థితి\n"
        "• *NEAREST* + లొకేషన్ → దగ్గర హాస్పిటల్, పోలీస్, ఘాట్\n"
        "• *HELPLINE* → అత్యవసర నంబర్లు\n"
        "• *LOST <వివరాలు>* → తప్పిపోయిన వ్యక్తి నమోదు\n\n" + SIGNATURE
    )
    return te if lang == "te" else en


# ═══════════════════════════════════════════════════════════════════════════
# Intent: HELPLINE
# ═══════════════════════════════════════════════════════════════════════════
def _handle_helpline() -> RouterReply:
    text = (
        "🚨 *Pushkaralu Emergency Helplines*\n"
        "• Police: *100*\n"
        "• Ambulance: *108*\n"
        "• Fire: *101*\n"
        "• National Emergency: *112*\n"
        "• Pushkaralu Festival: *1800-425-0066*\n"
        "• District Disaster Control: *1800-425-3077*\n\n"
        f"{SIGNATURE}"
    )
    return RouterReply(text=text, intent="helpline")


# ═══════════════════════════════════════════════════════════════════════════
# Intent: GHATS / GHAT <name>
# ═══════════════════════════════════════════════════════════════════════════
def _handle_ghats(arg: str = "") -> RouterReply:
    try:
        from main import DB  # lazy to avoid circular import on module load
    except Exception as exc:
        logger.warning("[WA-Router] DB not loaded yet: %s", exc)
        return RouterReply(
            text="System is starting up. Please retry in a minute.\n" + SIGNATURE,
            intent="ghats",
            handled=False,
        )
    ghats = DB.get("ghats", [])
    if not ghats:
        return RouterReply(
            text="Ghat data is loading. Please try again shortly.\n" + SIGNATURE,
            intent="ghats",
        )

    if arg:
        # fuzzy match by substring (case-insensitive)
        q = arg.strip().lower()
        match = next(
            (g for g in ghats if q in g.get("name", "").lower()),
            None,
        )
        if not match:
            return RouterReply(
                text=(
                    f"No ghat named *{arg}* found. Reply *GHATS* for the full list.\n"
                    + SIGNATURE
                ),
                intent="ghat_unknown",
            )
        cap = match.get("capacity", 0) or 0
        cur = match.get("current_count", 0) or 0
        pct = int(cur / cap * 100) if cap else 0
        text = (
            f"{_crowd_emoji(match.get('crowd_level',''))} "
            f"*{match.get('name','')}* "
            f"({match.get('telugu_name','')})\n"
            f"Crowd: *{(match.get('crowd_level') or 'unknown').upper()}*  "
            f"({cur:,}/{cap:,} = {pct}%)\n"
            f"Timings: {match.get('bathing_timings','—')}\n"
            f"Zone: {match.get('zone','—')}\n"
            f"Near: {match.get('nearest_landmark','—')}\n\n"
            + SIGNATURE
        )
        return RouterReply(text=text, intent="ghat_detail")

    # ALL ghats — short list
    lines = ["📍 *Live Ghat Status — Godavari Pushkaralu 2027*\n"]
    # Sort by crowd severity for usefulness: critical → high → medium → low
    severity = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for g in sorted(ghats, key=lambda x: severity.get(x.get("crowd_level",""), 9)):
        cap = g.get("capacity", 0) or 0
        cur = g.get("current_count", 0) or 0
        pct = int(cur / cap * 100) if cap else 0
        lines.append(
            f"{_crowd_emoji(g.get('crowd_level',''))} "
            f"{g.get('name','—')}: "
            f"{(g.get('crowd_level') or '?').upper()} ({pct}%)"
        )
    lines.append("\nReply *GHAT <name>* for details.")
    lines.append(SIGNATURE)
    return RouterReply(text="\n".join(lines), intent="ghats")


# ═══════════════════════════════════════════════════════════════════════════
# Intent: NEAREST (requires location)
# ═══════════════════════════════════════════════════════════════════════════
def _handle_nearest(m: IncomingMessage) -> RouterReply:
    if not _has_location(m):
        return RouterReply(
            text=(
                "Please share your live location with this message so I can "
                "find the nearest help. (WhatsApp → 📎 → Location → Send your "
                "current location)\n" + SIGNATURE
            ),
            intent="nearest_no_loc",
        )
    try:
        from main import DB
        from services.emergency_service import find_nearest_police, find_nearest_hospital
        from state.emergency_services import AMBULANCE_NUMBER, POLICE_NUMBER, FIRE_NUMBER
    except Exception as exc:
        logger.warning("[WA-Router] nearest deps not loaded: %s", exc)
        return RouterReply(
            text="System is starting up. Please retry shortly.\n" + SIGNATURE,
            intent="nearest",
            handled=False,
        )

    lat, lon = m.latitude, m.longitude
    ghat = nearest_in_list(lat, lon, DB.get("ghats", []), lat_key="latitude", lon_key="longitude")
    police = find_nearest_police(lat, lon)
    hospital = find_nearest_hospital(lat, lon)

    def _fmt(label: str, item: Optional[dict], lat_key: str = "lat", lon_key: str = "lon") -> str:
        if not item:
            return f"• {label}: not found nearby"
        plat = item.get(lat_key) or item.get("latitude")
        plon = item.get(lon_key) or item.get("longitude")
        dist = (
            f"{haversine(lat, lon, plat, plon):.1f} km"
            if (plat is not None and plon is not None)
            else "?"
        )
        phone = item.get("phone", "")
        phone_str = f" ☎ {phone}" if phone else ""
        return f"• {label}: *{item.get('name','—')}* ({dist}){phone_str}"

    lines = [
        "📍 *Nearest help to your location*",
        _fmt("Ghat",     ghat,     lat_key="latitude", lon_key="longitude"),
        _fmt("Hospital", hospital),
        _fmt("Police",   police),
        "",
        f"Helplines — Ambulance: *{AMBULANCE_NUMBER}*  Police: *{POLICE_NUMBER}*  Fire: *{FIRE_NUMBER}*",
        SIGNATURE,
    ]
    return RouterReply(text="\n".join(lines), intent="nearest")


# ═══════════════════════════════════════════════════════════════════════════
# Intent: SOS — needs location.
# Performs full SOS creation flow (PG write, broadcast, cache invalidation).
# ═══════════════════════════════════════════════════════════════════════════
async def _handle_sos(m: IncomingMessage) -> RouterReply:
    if not _has_location(m):
        return RouterReply(
            text=(
                "🚨 To send an SOS, please share your *live location* with this "
                "message. WhatsApp → 📎 → Location → *Send your current location*.\n"
                "If this is a real emergency RIGHT NOW, also call *112* immediately.\n"
                + SIGNATURE
            ),
            intent="sos_no_loc",
        )
    try:
        # Reuse the helper that the /sos_alert HTTP endpoint also calls.
        from main import create_sos_record
    except Exception as exc:
        logger.error("[WA-Router] create_sos_record not importable: %s", exc)
        return RouterReply(
            text=(
                "🚨 SOS could not be filed automatically. Please call *112* now.\n"
                + SIGNATURE
            ),
            intent="sos_failed",
            handled=False,
        )

    user_name = m.contact_name or "WhatsApp Pilgrim"
    try:
        result = await create_sos_record(
            user_name=user_name,
            phone=m.from_phone,
            latitude=float(m.latitude),
            longitude=float(m.longitude),
            source="whatsapp",
        )
    except Exception as exc:
        logger.error("[WA-Router] create_sos_record failed: %s", exc)
        return RouterReply(
            text=(
                "🚨 SOS could not be processed. Please call *112* immediately.\n"
                + SIGNATURE
            ),
            intent="sos_failed",
            handled=False,
        )

    nearest = result.get("nearest_volunteer")
    alert_id = result.get("alert_id", "")
    if nearest:
        text = (
            f"🚨 *SOS RECEIVED*\n"
            f"Alert ID: {alert_id[:8]}\n"
            f"Volunteer *{nearest.get('name','—')}* "
            f"({nearest.get('phone','no phone')}) has been alerted and is on the way.\n\n"
            f"Stay where you are if safe. If condition worsens call *112* immediately.\n"
            + SIGNATURE
        )
    else:
        text = (
            f"🚨 *SOS RECEIVED*\n"
            f"Alert ID: {alert_id[:8]}\n"
            "No volunteers are currently free nearby. Please call:\n"
            "• Police: *100*\n• Ambulance: *108*\n• Festival Helpline: *1800-425-0066*\n"
            + SIGNATURE
        )
    return RouterReply(
        text=text,
        intent="sos",
        side_effects=[f"sos_created:{alert_id}"],
    )


# ═══════════════════════════════════════════════════════════════════════════
# Intent: LOST <details>
# Lightweight registration — pilgrim sends free text describing the missing
# person; we register with status=missing and acknowledge.
# ═══════════════════════════════════════════════════════════════════════════
async def _handle_lost(m: IncomingMessage, details: str) -> RouterReply:
    if not details or len(details.strip()) < 3:
        return RouterReply(
            text=(
                "To register a missing person, send:\n"
                "*LOST <name>, <age>, <last seen location>*\n"
                "Example: *LOST Ramu, 7yr, Pushkar Ghat near food stall*\n"
                "Attach a photo if you can.\n" + SIGNATURE
            ),
            intent="lost_help",
        )
    try:
        from main import DB
        from app.core.pg_store import write_lost_person
        from app.core.ws_manager import manager
        from app.core.redis_manager import cache_delete, cache_set, Keys
    except Exception as exc:
        logger.error("[WA-Router] lost deps not importable: %s", exc)
        return RouterReply(
            text="System busy. Please try again or visit nearest enquiry counter.\n"
            + SIGNATURE,
            intent="lost_failed",
            handled=False,
        )

    parts = [p.strip() for p in details.split(",", 2)]
    name = parts[0] if parts else "Unknown"
    age: Optional[int] = None
    last_seen = "Reported via WhatsApp"
    if len(parts) >= 2:
        age_match = re.search(r"\d+", parts[1])
        if age_match:
            try:
                age = int(age_match.group())
            except ValueError:
                age = None
    if len(parts) >= 3:
        last_seen = parts[2]

    person = {
        "id":                  str(uuid.uuid4()),
        "name":                name,
        "age":                 age,
        "photo_url":           m.media_url,
        "gender":              None,
        "last_seen_location":  last_seen,
        "current_location":    "Unknown",
        "contact_person":      m.contact_name or "WhatsApp Reporter",
        "contact_phone":       m.from_phone,
        "description":         details,
        "status":              "missing",
        "source":              "whatsapp",
        "timestamp":           _utc_now(),
    }

    try:
        await write_lost_person(person)
    except Exception as exc:
        logger.warning("[WA-Router] PG write_lost_person failed: %s", exc)

    DB["lost_persons"].append(person)
    msg = {"type": "LOST_REGISTERED", "data": person}
    try:
        await cache_set(
            Keys.LOST_ALL, {"lost_persons": DB["lost_persons"]}, ttl=30
        )
        await cache_delete(
            Keys.LOST_STATUS.format(status="missing"), Keys.ADMIN_STATS
        )
        await manager.broadcast(msg)
    except Exception as exc:
        logger.warning("[WA-Router] lost broadcast failed: %s", exc)
        try:
            await manager._local_broadcast(msg)
        except Exception:
            pass

    text = (
        f"✅ Missing person registered.\n"
        f"Name: *{name}*"
        + (f" (age {age})" if age else "")
        + f"\nReport ID: {person['id'][:8]}\n"
        "Volunteers across all ghats have been notified. We will message you "
        "on this number as soon as there is an update.\n\n"
        "If the person is not found in 30 min, please ALSO visit the nearest "
        "Enquiry Counter and call *100* to file a police report.\n" + SIGNATURE
    )
    return RouterReply(
        text=text,
        intent="lost",
        side_effects=[f"lost_registered:{person['id']}"],
    )


# ═══════════════════════════════════════════════════════════════════════════
# Top-level dispatch
# ═══════════════════════════════════════════════════════════════════════════
SOS_KEYWORDS = {"SOS", "EMERGENCY", "HELP", "URGENT", "URGENTLY"}
GHATS_KEYWORDS = {"GHATS", "GHAT", "STATUS", "CROWD"}
NEAREST_KEYWORDS = {"NEAREST", "NEAR", "FIND"}
HELPLINE_KEYWORDS = {"HELPLINE", "HELPLINES", "CONTACT", "CONTACTS", "NUMBERS"}
LOST_KEYWORDS = {"LOST", "MISSING", "FIND_PERSON"}
MENU_KEYWORDS = {"MENU", "HI", "HELLO", "HEY", "START", "NAMASTE", "NAMASKAR"}


async def route(m: IncomingMessage) -> RouterReply:
    """Dispatch a normalized incoming WhatsApp message. Always returns a reply."""
    body = (m.body or "").strip()
    first = _first_token(body)
    lang = _detect_lang(body)

    # Location-only message with no text → assume NEAREST
    if not body and _has_location(m):
        return _handle_nearest(m)

    # Empty / pure greeting → onboarding menu
    if not body or first in MENU_KEYWORDS or body == "?":
        return RouterReply(text=_menu_text(lang), intent="menu")

    # SOS — always highest priority. Any of the SOS keywords + location wins.
    if first in SOS_KEYWORDS or any(
        kw in body.upper() for kw in ("SOS", "EMERGENCY")
    ):
        return await _handle_sos(m)

    # HELPLINES
    if first in HELPLINE_KEYWORDS:
        return _handle_helpline()

    # GHATS
    if first in GHATS_KEYWORDS:
        # `GHAT Pushkar` → arg = 'Pushkar'
        arg = body.split(None, 1)[1] if " " in body else ""
        return _handle_ghats(arg)

    # NEAREST
    if first in NEAREST_KEYWORDS:
        return _handle_nearest(m)

    # LOST
    if first in LOST_KEYWORDS:
        details = body.split(None, 1)[1] if " " in body else ""
        return await _handle_lost(m, details)

    # Free-form question → forward to TourGo Pushkara AI chatbot for a reply.
    return await _handle_chat_fallback(body, lang)


async def _handle_chat_fallback(body: str, lang: str) -> RouterReply:
    """Forward a free-form question to the existing chat router."""
    try:
        # Keep this lazy — chat.py imports main.DB so we want to avoid the
        # cycle at module load.
        from chat import (
            _is_off_topic,
            _refusal_for_lang,
            _get_cached_system_prompt,
            GROQ_API_KEY,
            GROQ_API_URL,
            GROQ_MODEL,
            GROQ_MODEL_FALLBACK,
        )
        from main import DB
    except Exception as exc:
        logger.warning("[WA-Router] chat fallback unavailable: %s", exc)
        return RouterReply(
            text=_menu_text(lang),
            intent="fallback_menu",
        )

    if _is_off_topic(body):
        return RouterReply(text=_refusal_for_lang(body), intent="off_topic")

    if not GROQ_API_KEY:
        # Chatbot not configured — fall back to menu but don't error.
        return RouterReply(text=_menu_text(lang), intent="fallback_menu_noai")

    try:
        sys_prompt = _get_cached_system_prompt(DB)
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": body[:400]},
        ]
        # FIX (perf): reuse the singleton groq client. See chat.py for context.
        from app.core.http_client import groq_client
        client = await groq_client()
        resp = await client.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": messages,
                "max_tokens": 300,
                "temperature": 0.4,
            },
        )
        if resp.status_code != 200:
            # Try fallback model once on rate limit / error
            if resp.status_code == 429:
                resp = await client.post(
                    GROQ_API_URL,
                    headers={
                        "Authorization": f"Bearer {GROQ_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": GROQ_MODEL_FALLBACK,
                        "messages": messages,
                        "max_tokens": 300,
                        "temperature": 0.4,
                    },
                )
            if resp.status_code != 200:
                logger.warning("[WA-Router] groq fallback %s", resp.status_code)
                return RouterReply(
                    text=_menu_text(lang) + "\n(AI is busy — try again shortly.)",
                    intent="fallback_menu_aierr",
                )
        data = resp.json()
        reply = data["choices"][0]["message"]["content"].strip()
        # AI already appends its own signature — we don't re-sign.
        return RouterReply(text=reply, intent="ai_chat")
    except Exception as exc:
        logger.warning("[WA-Router] groq fallback crashed: %s", exc)
        return RouterReply(
            text=_menu_text(lang),
            intent="fallback_menu_crash",
        )
