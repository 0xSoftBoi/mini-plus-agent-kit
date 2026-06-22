"""Loosely-coupled state estimation for the rover (GPS / odometry / IMU fusion).

A real ground-robot navigation stack does not steer off raw 1 Hz GPS and raw yaw.
The hardware streams complementary signals at different rates and qualities:

  * gyro (z-rate)      — fast, smooth, but drifts (bias integrates to error)
  * magnetometer / GPS course — slow, noisy, but absolute (no drift)
  * wheel odometry     — smooth relative motion, but slips / scale error
  * GPS position       — absolute, but ~5 m noise at 1 Hz

This module fuses them so the controller (`control.py`) acts on a smoothed pose
estimated at the control-loop rate (5–10 Hz) instead of the sensor rate. Pure
math (stdlib only) — fully unit-testable; no I/O.
"""

from __future__ import annotations

import math

from .geo import heading_error_deg

_M_PER_DEG_LAT = 111_320.0


def _wrap360(a: float) -> float:
    return a % 360.0


class HeadingFilter:
    """PI complementary heading filter with online gyro-bias estimation.

    A plain complementary filter integrates gyro (smooth, but drifts) and nudges
    toward an absolute source (magnetometer / GPS course) by ``kp``. Under a
    *constant* gyro bias that leaves a steady-state offset of ≈ bias·dt/kp (e.g.
    ~10° for a 3°/s bias). We add an integral term (``ki``) that learns the bias
    online and subtracts it in the prediction step — so the estimate rejects both
    gyro drift *and* magnetometer noise (Mahony-style PI complementary filter).
    """

    def __init__(self, kp: float = 0.12, ki: float = 0.01):
        self.kp = kp
        self.ki = ki
        self.heading: float | None = None
        self.bias: float = 0.0       # estimated gyro bias (deg/s)

    def update(self, dt: float, gyro_z_dps: float = 0.0,
               absolute_deg: float | None = None) -> float:
        if self.heading is None:
            self.heading = absolute_deg if absolute_deg is not None else 0.0
            return self.heading
        # predict with bias-compensated gyro
        self.heading = _wrap360(self.heading + (gyro_z_dps - self.bias) * dt)
        if absolute_deg is not None:
            err = heading_error_deg(self.heading, absolute_deg)   # signed shortest
            self.heading = _wrap360(self.heading + self.kp * err)
            # integral: learn the bias from the persistent correction
            self.bias -= self.ki * err * dt
        return self.heading


class PoseFilter:
    """Dead-reckon local-ENU position from odometry+heading; correct toward GPS.

    Between GPS fixes the pose is propagated by forward odometry along the current
    heading (loop rate); each GPS fix pulls the estimate toward the measurement by
    ``k_gps`` (complementary correction). This yields a high-rate, smoothed pose
    that rejects per-fix GPS noise while staying GPS-anchored (no odometry drift).

    Local ENU frame in metres about ``(base_lat, base_lon)``: x = East, y = North.
    """

    def __init__(self, base_lat: float, base_lon: float, k_gps: float = 0.25):
        self.base_lat = base_lat
        self.base_lon = base_lon
        self.k_gps = k_gps
        self.x = 0.0
        self.y = 0.0
        self._m_lon = _M_PER_DEG_LAT * math.cos(math.radians(base_lat))

    def predict(self, ds_m: float, heading_deg: float) -> None:
        """Advance ``ds_m`` forward along ``heading_deg`` (0=N=+y, 90=E=+x)."""
        r = math.radians(heading_deg)
        self.x += ds_m * math.sin(r)
        self.y += ds_m * math.cos(r)

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        """Project an absolute lat/lon into this filter's local-ENU frame (metres)."""
        return ((lon - self.base_lon) * self._m_lon, (lat - self.base_lat) * _M_PER_DEG_LAT)

    def correct_gps(self, lat: float, lon: float) -> None:
        gx, gy = self.to_xy(lat, lon)
        self.x += self.k_gps * (gx - self.x)
        self.y += self.k_gps * (gy - self.y)

    def xy(self) -> tuple[float, float]:
        return self.x, self.y

    def latlon(self) -> tuple[float, float]:
        return self.base_lat + self.y / _M_PER_DEG_LAT, self.base_lon + self.x / self._m_lon


def mag_heading_deg(mx: float, my: float, declination_deg: float = 0.0) -> float:
    """Tilt-free magnetometer heading from x/y components (+ optional declination)."""
    return _wrap360(math.degrees(math.atan2(mx, my)) + declination_deg)
