# ═══════════════════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — WhatsApp / Mana Mitra Webhook Router
#
# Endpoints:
#   GET  /whatsapp/webhook    — Meta Cloud API verification handshake
#   POST /whatsapp/webhook    — provider-agnostic incoming message handler
#                                (auto-detects Twilio form / Meta JSON / generic)
#   GET  /whatsapp/status     — show active provider + masked config
#   POST /whatsapp/send       — admin-protected outbound send (X-Admin-Key)
#   POST /whatsapp/simulate   — local-only debug endpoint to drive the router
#                                with a synthetic IncomingMessage
#
# All routes are deliberately tolerant: bad payloads return 200 with a logged
# warning so the upstream provider does not retry-storm us.
# ═══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import logging
import os
from typing import Any, Optional
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, HTTPException, Header, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from services import whatsapp_service as wa
from services.whatsapp_router import IncomingMessage, RouterReply, route

logger = logging.getLogger("pushkaralu.whatsapp.api")
router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")


# ─── helpers ────────────────────────────────────────────────────────────────
def _twiml_reply(body: str) -> Response:
    """Build a TwiML response that Twilio renders as an inline WhatsApp reply."""
    safe = xml_escape(body or "")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Response><Message>{safe}</Message></Response>'
    )
    return Response(content=xml, media_type="application/xml")


def _parse_twilio_form(form: dict) -> Optional[IncomingMessage]:
    """Twilio inbound webhook: x-www-form-urlencoded."""
    sender = form.get("From") or form.get("WaId") or ""
    if not sender:
        return None
    body = (form.get("Body") or "").strip()
    lat = form.get("Latitude")
    lon = form.get("Longitude")
    media = form.get("MediaUrl0")
    profile_name = form.get("ProfileName") or ""

    try:
        lat_f = float(lat) if lat not in (None, "") else None
        lon_f = float(lon) if lon not in (None, "") else None
    except (TypeError, ValueError):
        lat_f = lon_f = None

    return IncomingMessage(
        from_phone=wa.normalize_phone(sender),
        body=body,
        latitude=lat_f,
        longitude=lon_f,
        media_url=media or None,
        contact_name=profile_name or None,
        raw=form,
    )


def _parse_meta_payload(payload: dict) -> Optional[IncomingMessage]:
    """
    Meta WhatsApp Cloud API webhook (Graph). Shape:
    {
      "entry": [{
        "changes": [{
          "value": {
            "messages": [ {"from":"919...", "type":"text|location|image",
                           "text": {"body":"..."}, "location": {...}} ],
            "contacts": [ {"profile":{"name":"..."}} ]
          }
        }]
      }]
    }
    """
    try:
        entry = (payload.get("entry") or [{}])[0]
        change = (entry.get("changes") or [{}])[0]
        value = change.get("value") or {}
        messages = value.get("messages") or []
        if not messages:
            return None
        msg = messages[0]
        contacts = value.get("contacts") or [{}]
        profile_name = (contacts[0].get("profile") or {}).get("name", "")

        from_phone = msg.get("from", "")
        mtype = msg.get("type", "")
        body = ""
        lat = lon = None
        media_url = None

        if mtype == "text":
            body = (msg.get("text") or {}).get("body", "")
        elif mtype == "location":
            loc = msg.get("location") or {}
            try:
                lat = float(loc.get("latitude"))
                lon = float(loc.get("longitude"))
            except (TypeError, ValueError):
                lat = lon = None
            body = (loc.get("name") or "").strip()
        elif mtype in ("image", "document"):
            body = (msg.get(mtype) or {}).get("caption", "") or ""
            media_url = (msg.get(mtype) or {}).get("id")  # Meta returns id; full URL needs separate fetch
        elif mtype == "interactive":
            inter = msg.get("interactive") or {}
            br = inter.get("button_reply") or inter.get("list_reply") or {}
            body = br.get("title") or br.get("id") or ""

        return IncomingMessage(
            from_phone=wa.normalize_phone(from_phone),
            body=body,
            latitude=lat,
            longitude=lon,
            media_url=media_url,
            contact_name=profile_name or None,
            raw=payload,
        )
    except Exception as exc:
        logger.warning("[WA webhook] meta parse failed: %s", exc)
        return None


def _parse_generic_payload(payload: dict) -> Optional[IncomingMessage]:
    """
    Generic JSON shape — used for the Mana Mitra gateway and our own
    /whatsapp/simulate endpoint:
        { "from": "919...", "body": "...", "latitude": .., "longitude": ..,
          "media_url": "...", "contact_name": "..." }
    """
    sender = (
        payload.get("from")
        or payload.get("from_phone")
        or payload.get("phone")
        or payload.get("msisdn")
        or ""
    )
    if not sender:
        return None
    body = (
        payload.get("body")
        or payload.get("message")
        or payload.get("text")
        or ""
    )
    lat = payload.get("latitude") or payload.get("lat")
    lon = payload.get("longitude") or payload.get("lon")
    try:
        lat_f = float(lat) if lat not in (None, "") else None
        lon_f = float(lon) if lon not in (None, "") else None
    except (TypeError, ValueError):
        lat_f = lon_f = None

    return IncomingMessage(
        from_phone=wa.normalize_phone(sender),
        body=str(body).strip(),
        latitude=lat_f,
        longitude=lon_f,
        media_url=payload.get("media_url"),
        contact_name=payload.get("contact_name") or payload.get("name"),
        raw=payload,
    )


# ─── routes ─────────────────────────────────────────────────────────────────
@router.get("/status")
async def whatsapp_status():
    """Public read-only — returns active provider + which gateways are configured."""
    return wa.status()


@router.get("/webhook")
async def webhook_verify(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
):
    """
    Meta WhatsApp Cloud API verification handshake.
    Configured at developers.facebook.com → WhatsApp → Configuration.
    Returns the challenge string back to Meta when the verify_token matches.
    """
    expected = os.getenv("META_WA_VERIFY_TOKEN", "pushkaralu-verify")
    if hub_mode == "subscribe" and hub_verify_token == expected and hub_challenge:
        return PlainTextResponse(content=hub_challenge, status_code=200)
    logger.warning(
        "[WA webhook] meta verify rejected mode=%s tokenMatches=%s",
        hub_mode,
        hub_verify_token == expected,
    )
    raise HTTPException(status_code=403, detail="verify token mismatch")


@router.post("/webhook")
async def webhook_inbound(request: Request):
    """
    Provider-agnostic inbound webhook.

    Auto-detects:
        • Twilio   → application/x-www-form-urlencoded (responds with TwiML)
        • Meta     → JSON with `object: whatsapp_business_account`
        • Mana Mitra / generic → any JSON with `from` and `body`

    Reply path:
        • Twilio  → TwiML inline so the reply hits the user without a second call
        • Others  → call provider.send_text() and return JSON ack
    """
    content_type = (request.headers.get("content-type") or "").lower()
    msg: Optional[IncomingMessage] = None
    is_twilio = False
    raw_payload: Any = None

    try:
        if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            form = dict(await request.form())
            raw_payload = form
            msg = _parse_twilio_form(form)
            is_twilio = bool(form.get("AccountSid") or form.get("MessageSid")) or bool(
                form.get("From", "").startswith("whatsapp:")
            )
        else:
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            raw_payload = payload
            if isinstance(payload, dict):
                if payload.get("object") == "whatsapp_business_account" or "entry" in payload:
                    msg = _parse_meta_payload(payload)
                else:
                    msg = _parse_generic_payload(payload)
    except Exception as exc:
        logger.error("[WA webhook] payload parse error: %s", exc)
        return JSONResponse(content={"ok": False, "error": "parse_error"}, status_code=200)

    if not msg or not msg.from_phone:
        # Non-message events (delivery receipts, status callbacks) come in here —
        # acknowledge with 200 so the upstream provider does not retry.
        logger.info(
            "[WA webhook] non-message event content_type=%s payload_keys=%s",
            content_type,
            list(raw_payload.keys()) if isinstance(raw_payload, dict) else type(raw_payload).__name__,
        )
        if is_twilio:
            # Empty TwiML is the documented way to ack without replying.
            return _twiml_reply("")
        return JSONResponse(content={"ok": True, "ignored": True}, status_code=200)

    # Run the intent router.
    try:
        reply: RouterReply = await route(msg)
    except Exception as exc:
        logger.exception("[WA webhook] router crashed: %s", exc)
        reply = RouterReply(
            text="Something went wrong. For real emergencies call 112 immediately.",
            intent="error",
            handled=False,
        )

    logger.info(
        "[WA webhook] in from=%s intent=%s body=%r loc=%s effects=%s",
        msg.from_phone,
        reply.intent,
        msg.body[:60],
        bool(msg.latitude is not None),
        reply.side_effects,
    )

    if is_twilio:
        # Inline reply — no second API call needed.
        return _twiml_reply(reply.text)

    # For Meta / Mana Mitra / generic: send via outbound provider, return ack JSON.
    send_result = await wa.send_text(msg.from_phone, reply.text)
    return JSONResponse(
        content={
            "ok": True,
            "intent": reply.intent,
            "side_effects": reply.side_effects,
            "outbound": {
                "ok": send_result.ok,
                "provider": send_result.provider,
                "message_id": send_result.message_id,
                "error": send_result.error,
            },
        },
        status_code=200,
    )


class SendRequest(BaseModel):
    to: str
    body: str


@router.post("/send")
async def whatsapp_send(
    payload: SendRequest,
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
):
    """Admin-only outbound send. Useful for ops broadcasts or testing."""
    if not ADMIN_API_KEY or x_admin_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin key")
    if not payload.to or not payload.body:
        raise HTTPException(status_code=400, detail="`to` and `body` are required")
    result = await wa.send_text(payload.to, payload.body)
    return {
        "ok": result.ok,
        "provider": result.provider,
        "message_id": result.message_id,
        "error": result.error,
    }


@router.post("/simulate")
async def whatsapp_simulate(
    request: Request,
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
):
    """
    Local debug endpoint — accepts the same generic JSON shape as the inbound
    webhook and runs it through the router WITHOUT calling any provider.
    Locked behind X-Admin-Key.
    """
    if not ADMIN_API_KEY or x_admin_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin key")
    payload = await request.json()
    msg = _parse_generic_payload(payload)
    if not msg:
        raise HTTPException(status_code=400, detail="payload missing `from`/`body`")
    reply = await route(msg)
    return {
        "ok": True,
        "intent": reply.intent,
        "handled": reply.handled,
        "side_effects": reply.side_effects,
        "reply": reply.text,
    }
