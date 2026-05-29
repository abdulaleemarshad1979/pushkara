# ═══════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — Emergency Services Registry
# Central dataset for all emergency services in Rajahmundry / East Godavari
#
# Runtime-mutable store. The list below is the SEED; the admin portal can
# add / edit / delete entries at runtime through the CRUD helpers at the
# bottom of this module (add_service / update_service / delete_service).
#
# NOTE: mutations are in-memory and process-local (consistent with how this
# registry has always been loaded — there is no dedicated Postgres table).
# Edits are reset on process restart and are not synced across instances.
# ═══════════════════════════════════════════════════════════════════

from typing import List, Optional

EMERGENCY_SERVICES: List[dict] = [

    # ── NATIONAL HELPLINES (no lat/lon — phone-only) ──────────────
    {"id": 1,  "name": "National Emergency",              "type": "police",         "category": "helpline",   "phone": "112"},
    {"id": 2,  "name": "Police Emergency",                "type": "police",         "category": "helpline",   "phone": "100"},
    {"id": 3,  "name": "Ambulance",                       "type": "medical",        "category": "helpline",   "phone": "108"},
    {"id": 4,  "name": "Fire Emergency",                  "type": "fire",           "category": "helpline",   "phone": "101"},
    {"id": 5,  "name": "NDRF Control Room",               "type": "administration", "category": "helpline",   "phone": "1916"},
    {"id": 6,  "name": "Pushkaralu Festival Helpline",    "type": "administration", "category": "helpline",   "phone": "18004250066"},
    {"id": 7,  "name": "Citizen Call Centre",             "type": "administration", "category": "helpline",   "phone": "1100"},

    # ── POLICE STATIONS ───────────────────────────────────────────
    {"id": 10, "name": "I Town Police Station",           "type": "police", "category": "station",
     "lat": 16.9887, "lon": 81.7814, "phone": "08832471033",
     "address": "RJY I Town, East Godavari"},

    {"id": 11, "name": "II Town Police Station",          "type": "police", "category": "station",
     "lat": 16.9969, "lon": 81.7793, "phone": "08832421133",
     "address": "RJY II Town, East Godavari"},

    {"id": 12, "name": "III Town Police Station",         "type": "police", "category": "station",
     "lat": 16.9824, "lon": 81.7896, "phone": "08832471273",
     "address": "RJY III Town, East Godavari"},

    {"id": 13, "name": "IV Town Control Room",            "type": "police", "category": "control",
     "lat": 16.9984, "lon": 81.7832, "phone": "08832555257",
     "address": "RJY IV Town, East Godavari"},

    {"id": 14, "name": "Kovvuru Rural Police Station",    "type": "police", "category": "station",
     "lat": 17.0163, "lon": 81.7315, "phone": "08813231633",
     "address": "Kovvuru, East Godavari"},

    # ── HOSPITALS ─────────────────────────────────────────────────
    {"id": 20, "name": "Government General Hospital Rajahmundry", "type": "hospital", "category": "medical",
     "lat": 16.9895, "lon": 81.7806, "phone": "08832422202",
     "address": "Main Road, Rajahmundry"},

    {"id": 21, "name": "ESI Hospital Rajahmundry",        "type": "hospital", "category": "medical",
     "lat": 16.9979, "lon": 81.7764, "phone": "08832479079",
     "address": "ESI Campus, Rajahmundry"},

    {"id": 22, "name": "CGHS Wellness Centre",            "type": "hospital", "category": "medical",
     "lat": 17.0004, "lon": 81.7902, "phone": "08832475090",
     "address": "CGHS Campus, Rajahmundry"},

    # ── FIRE STATION ──────────────────────────────────────────────
    {"id": 30, "name": "Rajahmundry Fire Station",        "type": "fire",    "category": "emergency",
     "lat": 16.9946, "lon": 81.7825, "phone": "101",
     "address": "Fire Station Road, Rajahmundry"},

    # ── DISTRICT ADMINISTRATION ───────────────────────────────────
    {"id": 40, "name": "District Collector Office East Godavari", "type": "administration", "category": "government",
     "lat": 17.0011, "lon": 81.7894, "phone": "08842361200",
     "address": "Collectorate, Kakinada"},

    {"id": 41, "name": "District Disaster Control Room",  "type": "administration", "category": "control",
     "lat": 17.0011, "lon": 81.7894, "phone": "18004253077",
     "address": "Collectorate, Kakinada"},
]

# ─────────────────────────────────────────────────────────────────────
# Dynamic accessors
# These are computed on every call (not snapshotted at import) so that
# runtime add/edit/delete operations are immediately reflected everywhere
# — find_nearest_*, the grouped view, and the public /emergency_services API.
# ─────────────────────────────────────────────────────────────────────

def list_services() -> List[dict]:
    """Return the live list of all emergency services."""
    return EMERGENCY_SERVICES


def helplines() -> List[dict]:
    return [s for s in EMERGENCY_SERVICES if s.get("category") == "helpline"]


def located_services(service_type: Optional[str] = None) -> List[dict]:
    """All services that have coordinates, optionally filtered by type."""
    out = [s for s in EMERGENCY_SERVICES if "lat" in s and "lon" in s]
    if service_type:
        out = [s for s in out if s.get("type") == service_type]
    return out


# ─────────────────────────────────────────────────────────────────────
# CRUD helpers (used by the admin portal via main.py endpoints)
# ─────────────────────────────────────────────────────────────────────

# Allowed values — kept permissive but documented so the UI and API agree.
SERVICE_TYPES      = ("police", "hospital", "medical", "fire", "administration")
SERVICE_CATEGORIES = ("helpline", "station", "control", "medical",
                       "emergency", "government", "other")


def _next_id() -> int:
    """Next integer id (max existing + 1, min 1)."""
    existing = [int(s["id"]) for s in EMERGENCY_SERVICES
                if isinstance(s.get("id"), (int, str)) and str(s.get("id")).isdigit()]
    return (max(existing) + 1) if existing else 1


def get_service(service_id) -> Optional[dict]:
    try:
        sid = int(service_id)
    except (TypeError, ValueError):
        return None
    return next((s for s in EMERGENCY_SERVICES if int(s["id"]) == sid), None)


def _clean(fields: dict) -> dict:
    """Keep only the known, non-empty service fields with correct types."""
    out: dict = {}
    for key in ("name", "type", "category", "phone", "address"):
        val = fields.get(key)
        if val is not None and str(val).strip() != "":
            out[key] = str(val).strip()
    for key in ("lat", "lon"):
        val = fields.get(key)
        if val is not None and str(val).strip() != "":
            try:
                out[key] = float(val)
            except (TypeError, ValueError):
                pass
    return out


def add_service(fields: dict) -> dict:
    """Create a new service entry and return it."""
    data = _clean(fields)
    data["id"] = _next_id()
    data.setdefault("type", "administration")
    data.setdefault("category", "other")
    EMERGENCY_SERVICES.append(data)
    return data


def update_service(service_id, fields: dict) -> Optional[dict]:
    """Update an existing service in place; returns the updated dict or None."""
    svc = get_service(service_id)
    if svc is None:
        return None
    updates = _clean(fields)
    svc.update(updates)
    # Allow explicit clearing of coordinates (e.g. converting to a phone-only
    # helpline) when both are sent blank.
    if str(fields.get("lat", "")).strip() == "" and "lat" in svc and "lat" in fields:
        svc.pop("lat", None)
        svc.pop("lon", None)
    return svc


def delete_service(service_id) -> bool:
    """Remove a service by id. Returns True if something was removed."""
    svc = get_service(service_id)
    if svc is None:
        return False
    EMERGENCY_SERVICES.remove(svc)
    return True


AMBULANCE_NUMBER = "108"
FIRE_NUMBER      = "101"
POLICE_NUMBER    = "100"
