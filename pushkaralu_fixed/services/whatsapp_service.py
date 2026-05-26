# ═══════════════════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — WhatsApp / Mana Mitra Adapter
#
# Provider-agnostic outbound layer. The rest of the codebase calls
#     await whatsapp.send_text(to, body)
# without caring whether the message goes through:
#     • Mana Mitra (AP Govt's official WhatsApp Governance gateway)
#     • Meta WhatsApp Cloud API (Business API)
#     • Twilio Sandbox (great for HackRx demo / dev)
#     • Mock          (default — logs only, no network)
#
# Provider is selected at runtime via WHATSAPP_PROVIDER env. Unconfigured or
# unknown providers fall back to "mock" so the rest of the app never crashes
# just because WhatsApp credentials aren't set.
#
# All sends are FAIL-SOFT: a failed WhatsApp message must NEVER bubble up and
# break a SOS / lost-person / hazard-alert flow. The caller logs the warning
# and continues.
# ═══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger("pushkaralu.whatsapp")

# ── Fire-and-forget concurrency caps ────────────────────────────────────────
# Bound how many outbound WhatsApp sends can be in flight at once so a spike
# (e.g. a mass SOS event) cannot starve the event loop or exhaust the
# provider's API quota. Tasks beyond the cap queue rather than fan out.
WHATSAPP_FF_CONCURRENCY = int(os.getenv("WHATSAPP_FF_CONCURRENCY", "16"))
_FF_SEMAPHORE: "asyncio.Semaphore" = asyncio.Semaphore(WHATSAPP_FF_CONCURRENCY)
# Strong refs to in-flight tasks — without this they can be GC'd mid-flight
# (CPython warns since 3.11).
_FF_TASKS: "set[asyncio.Task]" = set()

# ── Env config ──────────────────────────────────────────────────────────────
WHATSAPP_PROVIDER = os.getenv("WHATSAPP_PROVIDER", "mock").strip().lower()
WHATSAPP_ENABLED = os.getenv("WHATSAPP_ENABLED", "true").strip().lower() == "true"
WHATSAPP_DEFAULT_COUNTRY_CODE = os.getenv("WHATSAPP_DEFAULT_COUNTRY_CODE", "91")  # India
WHATSAPP_PUBLIC_NUMBER = os.getenv("WHATSAPP_PUBLIC_NUMBER", "")  # Shown on dashboards/UI

# Twilio
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")  # e.g. whatsapp:+14155238886
TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"

# Mana Mitra (AP Govt) — schema is illustrative; flip env when real spec lands
MANA_MITRA_API_URL = os.getenv("MANA_MITRA_API_URL", "")
MANA_MITRA_API_KEY = os.getenv("MANA_MITRA_API_KEY", "")
MANA_MITRA_SENDER_ID = os.getenv("MANA_MITRA_SENDER_ID", "PUSHKARALU")

# Meta WhatsApp Cloud API
META_WA_TOKEN = os.getenv("META_WA_TOKEN", "")
META_WA_PHONE_NUMBER_ID = os.getenv("META_WA_PHONE_NUMBER_ID", "")
META_WA_VERIFY_TOKEN = os.getenv("META_WA_VERIFY_TOKEN", "pushkaralu-verify")
META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v20.0")


# ── Phone helpers ──────────────────────────────────────────────────────────
_DIGITS_RE = re.compile(r"\d+")


def normalize_phone(raw: str) -> str:
    """
    Normalize a phone number to E.164-ish form WITHOUT the leading '+'.
    Examples:
        '+91 98765 43210'        → '919876543210'
        '9876543210'             → '919876543210'   (default country code)
        'whatsapp:+919876543210' → '919876543210'
    Returns '' if nothing usable.
    """
    if not raw:
        return ""
    digits = "".join(_DIGITS_RE.findall(raw))
    if not digits:
        return ""
    # If looks like a 10-digit Indian mobile, prefix country code.
    if len(digits) == 10:
        digits = WHATSAPP_DEFAULT_COUNTRY_CODE + digits
    # Strip leading 00 (international prefix in some carriers)
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def to_whatsapp_address(phone_e164: str) -> str:
    """Twilio uses the 'whatsapp:+<E164>' prefix."""
    p = normalize_phone(phone_e164)
    return f"whatsapp:+{p}" if p else ""


# ── Provider interface ─────────────────────────────────────────────────────
@dataclass
class SendResult:
    ok: bool
    provider: str
    message_id: Optional[str] = None
    error: Optional[str] = None


class _BaseProvider:
    name = "base"

    async def send_text(self, to_phone: str, body: str) -> SendResult:
        raise NotImplementedError

    async def send_location(
        self, to_phone: str, lat: float, lon: float, name: str = "", address: str = ""
    ) -> SendResult:
        # Default fallback: serialize as text + Google Maps link.
        link = f"https://www.google.com/maps/?q={lat},{lon}"
        body = f"{name or 'Location'}\n{address}\n{link}".strip()
        return await self.send_text(to_phone, body)


class MockProvider(_BaseProvider):
    """No-op provider — always succeeds, logs to stdout. Default for dev."""

    name = "mock"

    async def send_text(self, to_phone: str, body: str) -> SendResult:
        norm = normalize_phone(to_phone)
        if not norm:
            return SendResult(ok=False, provider=self.name, error="invalid phone")
        preview = body.replace("\n", " ⏎ ")[:200]
        logger.info("[WhatsApp:mock] → +%s  %s", norm, preview)
        return SendResult(ok=True, provider=self.name, message_id=f"mock-{norm[-6:]}")


class TwilioProvider(_BaseProvider):
    """Twilio Sandbox / WhatsApp Business via Twilio."""

    name = "twilio"

    def __init__(self) -> None:
        if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
            raise RuntimeError(
                "Twilio provider requires TWILIO_ACCOUNT_SID, "
                "TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER"
            )

    async def send_text(self, to_phone: str, body: str) -> SendResult:
        addr = to_whatsapp_address(to_phone)
        if not addr:
            return SendResult(ok=False, provider=self.name, error="invalid phone")
        url = f"{TWILIO_API_BASE}/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
        try:
            from app.core.http_client import whatsapp_client
            client = await whatsapp_client()
            resp = await client.post(
                url,
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                data={"From": TWILIO_FROM_NUMBER, "To": addr, "Body": body[:1500]},
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                return SendResult(
                    ok=True, provider=self.name, message_id=data.get("sid")
                )
            return SendResult(
                ok=False,
                provider=self.name,
                error=f"twilio {resp.status_code}: {resp.text[:200]}",
            )
        except Exception as exc:
            return SendResult(ok=False, provider=self.name, error=str(exc))


class ManaMitraProvider(_BaseProvider):
    """
    AP Government's Mana Mitra WhatsApp Governance gateway.

    The public spec for the institutional gateway is not openly published, so
    this implementation uses a generic JSON-over-HTTPS contract that mirrors
    most government messaging APIs (sender_id, to, message, type). When the
    real gateway specification lands via the RTGS sandbox MoU, only this
    class needs to change.
    """

    name = "mana_mitra"

    def __init__(self) -> None:
        if not MANA_MITRA_API_URL:
            raise RuntimeError(
                "Mana Mitra provider requires MANA_MITRA_API_URL"
            )

    async def send_text(self, to_phone: str, body: str) -> SendResult:
        norm = normalize_phone(to_phone)
        if not norm:
            return SendResult(ok=False, provider=self.name, error="invalid phone")
        headers = {"Content-Type": "application/json"}
        if MANA_MITRA_API_KEY:
            headers["Authorization"] = f"Bearer {MANA_MITRA_API_KEY}"
        payload = {
            "sender_id": MANA_MITRA_SENDER_ID,
            "to": norm,
            "type": "text",
            "message": body[:1500],
            "channel": "whatsapp",
        }
        try:
            from app.core.http_client import whatsapp_client
            client = await whatsapp_client()
            resp = await client.post(MANA_MITRA_API_URL, json=payload, headers=headers)
            if 200 <= resp.status_code < 300:
                try:
                    data = resp.json()
                    msg_id = (
                        data.get("message_id")
                        or data.get("id")
                        or data.get("reference_id")
                    )
                except Exception:
                    msg_id = None
                return SendResult(ok=True, provider=self.name, message_id=msg_id)
            return SendResult(
                ok=False,
                provider=self.name,
                error=f"mana_mitra {resp.status_code}: {resp.text[:200]}",
            )
        except Exception as exc:
            return SendResult(ok=False, provider=self.name, error=str(exc))


class MetaCloudProvider(_BaseProvider):
    """Meta (Facebook) WhatsApp Cloud API — graph.facebook.com."""

    name = "meta"

    def __init__(self) -> None:
        if not (META_WA_TOKEN and META_WA_PHONE_NUMBER_ID):
            raise RuntimeError(
                "Meta provider requires META_WA_TOKEN and META_WA_PHONE_NUMBER_ID"
            )

    async def send_text(self, to_phone: str, body: str) -> SendResult:
        norm = normalize_phone(to_phone)
        if not norm:
            return SendResult(ok=False, provider=self.name, error="invalid phone")
        url = (
            f"https://graph.facebook.com/{META_GRAPH_VERSION}"
            f"/{META_WA_PHONE_NUMBER_ID}/messages"
        )
        headers = {
            "Authorization": f"Bearer {META_WA_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": norm,
            "type": "text",
            "text": {"body": body[:1500]},
        }
        try:
            from app.core.http_client import whatsapp_client
            client = await whatsapp_client()
            resp = await client.post(url, json=payload, headers=headers)
            if 200 <= resp.status_code < 300:
                try:
                    data = resp.json()
                    msgs = data.get("messages") or []
                    msg_id = msgs[0].get("id") if msgs else None
                except Exception:
                    msg_id = None
                return SendResult(ok=True, provider=self.name, message_id=msg_id)
            return SendResult(
                ok=False,
                provider=self.name,
                error=f"meta {resp.status_code}: {resp.text[:200]}",
            )
        except Exception as exc:
            return SendResult(ok=False, provider=self.name, error=str(exc))


# ── Singleton selector ─────────────────────────────────────────────────────
_provider: Optional[_BaseProvider] = None


def _build_provider() -> _BaseProvider:
    """Pick a provider from env. Falls back to MockProvider on any failure."""
    if not WHATSAPP_ENABLED:
        logger.info("[WhatsApp] disabled via WHATSAPP_ENABLED=false — using mock")
        return MockProvider()
    name = WHATSAPP_PROVIDER
    try:
        if name == "twilio":
            return TwilioProvider()
        if name == "mana_mitra":
            return ManaMitraProvider()
        if name == "meta":
            return MetaCloudProvider()
        if name == "mock":
            return MockProvider()
        logger.warning("[WhatsApp] unknown provider=%r — using mock", name)
        return MockProvider()
    except Exception as exc:
        logger.warning(
            "[WhatsApp] provider=%s init failed (%s) — falling back to mock",
            name,
            exc,
        )
        return MockProvider()


def get_provider() -> _BaseProvider:
    global _provider
    if _provider is None:
        _provider = _build_provider()
        logger.info("[WhatsApp] active provider=%s", _provider.name)
    return _provider


def status() -> dict:
    """Lightweight introspection — used by /whatsapp/status."""
    p = get_provider()
    return {
        "enabled": WHATSAPP_ENABLED,
        "provider": p.name,
        "configured_provider": WHATSAPP_PROVIDER,
        "default_country_code": WHATSAPP_DEFAULT_COUNTRY_CODE,
        "public_number": WHATSAPP_PUBLIC_NUMBER,
        "twilio_configured": bool(TWILIO_ACCOUNT_SID and TWILIO_FROM_NUMBER),
        "mana_mitra_configured": bool(MANA_MITRA_API_URL),
        "meta_configured": bool(META_WA_TOKEN and META_WA_PHONE_NUMBER_ID),
    }


# ── Public API used by the rest of the codebase ────────────────────────────
async def send_text(to_phone: str, body: str) -> SendResult:
    """
    Fail-soft text send. NEVER raises — returns SendResult with ok=False
    instead so callers can log and continue.
    """
    if not to_phone:
        return SendResult(ok=False, provider="none", error="empty phone")
    try:
        return await get_provider().send_text(to_phone, body)
    except Exception as exc:
        logger.warning("[WhatsApp] send_text crashed: %s", exc)
        return SendResult(ok=False, provider="error", error=str(exc))


async def send_location(
    to_phone: str, lat: float, lon: float, name: str = "", address: str = ""
) -> SendResult:
    if not to_phone:
        return SendResult(ok=False, provider="none", error="empty phone")
    try:
        return await get_provider().send_location(to_phone, lat, lon, name, address)
    except Exception as exc:
        logger.warning("[WhatsApp] send_location crashed: %s", exc)
        return SendResult(ok=False, provider="error", error=str(exc))


def fire_and_forget_send(to_phone: str, body: str) -> None:
    """
    Schedule a WhatsApp send without awaiting it.

    Use this from request handlers that must NOT block their HTTP response
    on a third-party API. The result is logged at debug level.

    Concurrency is bounded so a burst (e.g. mass SOS) cannot spawn thousands
    of orphan coroutines all blocked on Twilio/Meta HTTPS — once the cap is
    reached, additional sends wait their turn rather than piling up.
    """
    if not to_phone or not body:
        return

    async def _runner() -> None:
        async with _FF_SEMAPHORE:
            result = await send_text(to_phone, body)
        if not result.ok:
            logger.warning(
                "[WhatsApp] fire_and_forget failed: provider=%s err=%s",
                result.provider,
                result.error,
            )
        else:
            logger.debug(
                "[WhatsApp] fire_and_forget ok: provider=%s mid=%s",
                result.provider,
                result.message_id,
            )

    try:
        task = asyncio.create_task(_runner())
        _FF_TASKS.add(task)
        task.add_done_callback(_FF_TASKS.discard)
    except RuntimeError:
        # No running loop (e.g. from sync test code) — silently drop.
        logger.debug("[WhatsApp] fire_and_forget called outside event loop")
