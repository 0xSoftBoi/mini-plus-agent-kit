"""Geo helpers for GPS-waypoint navigation (Earth Rover Challenge — Urban track).

The challenge's Urban track is GPS-goal navigation with a 15 m tolerance: given the
rover's current GPS + heading and the next checkpoint's GPS, drive toward it. These
are the pure, exact computations behind that — great-circle distance, initial
bearing, and the signed heading error a controller turns to null out.

All angles in degrees; distances in metres. No dependencies.
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8  # mean Earth radius (IUGG)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 → point 2, in [0, 360).

    0 = North, 90 = East, 180 = South, 270 = West.
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def heading_error_deg(current_heading: float, target_bearing: float) -> float:
    """Signed shortest turn (deg) from current heading to target bearing.

    Result in (-180, 180]: positive = turn right (clockwise), negative = left.
    Matches the kit's ``turn`` convention (+degrees = right).
    """
    return (target_bearing - current_heading + 540.0) % 360.0 - 180.0


def gps_course_and_speed(lat0: float, lon0: float, lat1: float, lon1: float,
                         dt_s: float) -> tuple[float | None, float]:
    """Course-over-ground (deg) and ground speed (m/s) between two GPS fixes.

    Course is the bearing of actual motion — a magnetically-immune, drift-free
    heading reference — but it is undefined when stationary, so it is returned as
    ``None`` if the rover barely moved (< ~5 cm) between fixes. The caller speed-gates
    the course before trusting it (GPS course is pure noise at very low speed).
    """
    d = haversine_m(lat0, lon0, lat1, lon1)
    speed = d / dt_s if dt_s > 0 else 0.0
    course = initial_bearing_deg(lat0, lon0, lat1, lon1) if d > 0.05 else None
    return course, speed
