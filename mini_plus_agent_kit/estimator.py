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
    """2-D position Kalman filter: odometry-driven prediction + gated GPS updates.

    Proper Bayesian fusion, not a fixed-gain complementary pull. The estimate
    carries a covariance ``P`` (isotropic scalar — GPS noise and the symmetric
    process model are both ~isotropic) that **grows** with odometry process noise
    on each ``predict`` and **shrinks** on each GPS update by the *optimal* Kalman
    gain ``K = P/(P+R)`` — so early/uncertain fixes are trusted more and the gain
    self-tunes instead of being hand-set. GPS fixes are **Mahalanobis-gated**: a fix
    whose normalized innovation ``d² = ‖z−x‖²/(P+R)`` exceeds ``gate`` (urban-canyon
    multipath, a momentary jump) is rejected rather than dragging the estimate
    off-line; the gate is slowly opened on repeated rejects so a genuine relocation
    is eventually accepted (anti-divergence).

    ``R`` = GPS measurement variance (σ_gps²); ``q_per_m`` = process variance added
    per metre of odometry; ``gate`` ≈ χ²(2 DOF) threshold (9.21 ≈ 99%). Local-ENU
    frame in metres about ``(base_lat, base_lon)``: x = East, y = North.
    """

    def __init__(self, base_lat: float, base_lon: float, *, sigma_gps_m: float = 4.0,
                 q_per_m: float = 0.05, gate: float = 9.21, p0: float = 25.0):
        self.base_lat = base_lat
        self.base_lon = base_lon
        self.R = sigma_gps_m ** 2
        self.q_per_m = q_per_m
        self.gate = gate
        self.x = 0.0
        self.y = 0.0
        self.P = p0
        self.n_rejected = 0
        self.last_rejected = False
        self._m_lon = _M_PER_DEG_LAT * math.cos(math.radians(base_lat))

    def predict(self, ds_m: float, heading_deg: float) -> None:
        """Advance ``ds_m`` forward along ``heading_deg`` (0=N=+y, 90=E=+x); grow P."""
        r = math.radians(heading_deg)
        self.x += ds_m * math.sin(r)
        self.y += ds_m * math.cos(r)
        self.P += self.q_per_m * abs(ds_m) + 1e-6   # process-noise covariance growth

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        """Project an absolute lat/lon into this filter's local-ENU frame (metres)."""
        return ((lon - self.base_lon) * self._m_lon, (lat - self.base_lat) * _M_PER_DEG_LAT)

    def correct_gps(self, lat: float, lon: float) -> bool:
        """Fuse a GPS fix via the optimal Kalman gain; reject Mahalanobis outliers.

        Returns ``True`` if the fix was accepted, ``False`` if gated out.
        """
        gx, gy = self.to_xy(lat, lon)
        ix, iy = gx - self.x, gy - self.y
        s = self.P + self.R                          # innovation covariance
        d2 = (ix * ix + iy * iy) / s                 # normalized innovation² (2 DOF)
        if d2 > self.gate:                           # multipath / jump → reject
            self.n_rejected += 1
            self.last_rejected = True
            self.P += 0.5 * self.R                   # open the gate a little (anti-divergence)
            return False
        k = self.P / s                               # optimal Kalman gain
        self.x += k * ix
        self.y += k * iy
        self.P = (1.0 - k) * self.P
        self.last_rejected = False
        return True

    def xy(self) -> tuple[float, float]:
        return self.x, self.y

    def latlon(self) -> tuple[float, float]:
        return self.base_lat + self.y / _M_PER_DEG_LAT, self.base_lon + self.x / self._m_lon


def mag_heading_deg(mx: float, my: float, declination_deg: float = 0.0) -> float:
    """Tilt-free magnetometer heading from x/y components (+ optional declination)."""
    return _wrap360(math.degrees(math.atan2(mx, my)) + declination_deg)
