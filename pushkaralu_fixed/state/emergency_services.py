# ═══════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — Emergency Services Registry
# Central dataset for all emergency services in Rajahmundry / East Godavari
# READ-ONLY — never mutate at runtime; treated like a seed file
# ═══════════════════════════════════════════════════════════════════

EMERGENCY_SERVICES = [

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

# Convenience lookups
HELPLINES     = [s for s in EMERGENCY_SERVICES if s["category"] == "helpline"]
POLICE        = [s for s in EMERGENCY_SERVICES if s["type"] == "police"   and "lat" in s]
HOSPITALS     = [s for s in EMERGENCY_SERVICES if s["type"] == "hospital" and "lat" in s]
FIRE_STATIONS = [s for s in EMERGENCY_SERVICES if s["type"] == "fire"     and "lat" in s]
ALL_LOCATED   = [s for s in EMERGENCY_SERVICES if "lat" in s]

AMBULANCE_NUMBER = "108"
FIRE_NUMBER      = "101"
POLICE_NUMBER    = "100"
