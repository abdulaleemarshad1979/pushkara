"""
Godavari Pushkaralu 2027 — Location Utilities
Pure functions. No I/O, no network. O(N) nearest search.
"""
import math
from typing import Optional


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in kilometres between two GPS coordinates."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def nearest_in_list(
    user_lat: float,
    user_lon: float,
    items: list,
    lat_key: str = "lat",
    lon_key: str = "lon",
) -> Optional[dict]:
    """Return the item in `items` closest to (user_lat, user_lon). None if list empty."""
    located = [
        i for i in items
        if i.get(lat_key) is not None and i.get(lon_key) is not None
    ]
    if not located:
        return None
    return min(
        located,
        key=lambda i: haversine(user_lat, user_lon, i[lat_key], i[lon_key]),
    )
