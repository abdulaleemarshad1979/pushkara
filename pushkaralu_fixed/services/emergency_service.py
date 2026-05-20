# ═══════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — Emergency Service Finder
# Pure business-logic layer; no web framework imports
# ═══════════════════════════════════════════════════════════════════

from typing import Optional
from state.emergency_services import (
    EMERGENCY_SERVICES,
    POLICE, HOSPITALS, FIRE_STATIONS, ALL_LOCATED,
    AMBULANCE_NUMBER, FIRE_NUMBER, POLICE_NUMBER,
)
from utils.location_utils import nearest_in_list


def find_nearest(user_lat: float, user_lon: float, service_type: str) -> Optional[dict]:
    """
    Return the nearest service of `service_type` to the user.
    service_type: 'police' | 'hospital' | 'fire' | 'any'
    Helplines (no coordinates) are excluded — they are returned separately.
    """
    pool = {
        "police":   POLICE,
        "hospital": HOSPITALS,
        "fire":     FIRE_STATIONS,
        "any":      ALL_LOCATED,
    }.get(service_type, ALL_LOCATED)

    return nearest_in_list(user_lat, user_lon, pool)


def find_nearest_police(user_lat: float, user_lon: float) -> Optional[dict]:
    return find_nearest(user_lat, user_lon, "police")


def find_nearest_hospital(user_lat: float, user_lon: float) -> Optional[dict]:
    return find_nearest(user_lat, user_lon, "hospital")


def find_nearest_fire_station(user_lat: float, user_lon: float) -> Optional[dict]:
    return find_nearest(user_lat, user_lon, "fire")


def get_emergency_numbers() -> dict:
    """Return the essential phone-only helplines as a compact dict."""
    return {
        "ambulance": AMBULANCE_NUMBER,
        "fire":      FIRE_NUMBER,
        "police":    POLICE_NUMBER,
    }


def get_all_services_by_category() -> dict:
    """Return all services grouped by type — used by admin dashboard."""
    result: dict = {}
    for s in EMERGENCY_SERVICES:
        t = s["type"]
        result.setdefault(t, []).append(s)
    return result
