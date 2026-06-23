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

    def __init__(self, kp: float = 0.12, ki: float = 0.01,
                 kp_course: float = 0.4, course_gate_mps: float = 0.4,
                 mag_suppress: float = 0.8, course_conf_decay: float = 0.97):
        self.kp = kp
        self.ki = ki
        self.kp_course = kp_course            # correction gain toward GPS course-over-ground
        self.course_gate_mps = course_gate_mps  # below this speed GPS course is unusable noise
        self.mag_suppress = mag_suppress     # how much a confident course down-weights the mag
        self.course_conf_decay = course_conf_decay
        self.heading: float | None = None
        self.bias: float = 0.0               # estimated gyro bias (deg/s)
        self._course_conf = 0.0              # recent GPS-course confidence (decays between fixes)

    def update(self, dt: float, gyro_z_dps: float = 0.0,
               absolute_deg: float | None = None, course_deg: float | None = None,
               speed_mps: float = 0.0) -> float:
        """Predict on gyro, correct toward the magnetometer and (speed-gated) GPS course.

        The magnetometer/orientation (``absolute_deg``) is absolute but suffers
        hard/soft-iron and local magnetic disturbance. GPS *course-over-ground*
        (``course_deg``) is magnetically immune and drift-free but only meaningful
        when actually moving — it is pure noise below ~1 km/h. So the course
        correction is gated by ``speed_mps`` and ramped in with speed, letting it
        override a biased magnetometer once the rover is driving.
        """
        if self.heading is None:
            self.heading = (absolute_deg if absolute_deg is not None
                            else (course_deg if course_deg is not None else 0.0))
            return self.heading
        # predict with bias-compensated gyro
        self.heading = _wrap360(self.heading + (gyro_z_dps - self.bias) * dt)
        self._course_conf *= self.course_conf_decay
        if absolute_deg is not None:
            # down-weight the magnetometer while GPS course is confident (it may be
            # magnetically disturbed — course is the trustworthy reference when moving)
            gain = self.kp * (1.0 - self.mag_suppress * self._course_conf)
            err = heading_error_deg(self.heading, absolute_deg)   # signed shortest
            self.heading = _wrap360(self.heading + gain * err)
            self.bias -= self.ki * (1.0 - self.mag_suppress * self._course_conf) * err * dt
        if course_deg is not None and speed_mps >= self.course_gate_mps:
            # trust ramps from 0 at the gate to 1 at ~3× the gate speed
            w = min(1.0, (speed_mps - self.course_gate_mps) / (2.0 * self.course_gate_mps))
            self._course_conf = max(self._course_conf, w)
            cerr = heading_error_deg(self.heading, course_deg)
            self.heading = _wrap360(self.heading + self.kp_course * w * cerr)
            self.bias -= self.ki * w * cerr * dt
        return self.heading


class PoseFilter:
    """2-D position Kalman filter with a full 2×2 covariance + GPS latency handling.

    Proper Bayesian fusion, not a fixed-gain complementary pull. The estimate carries
    a symmetric covariance ``P = [[pxx,pxy],[pxy,pyy]]`` that grows on each ``predict``
    by **anisotropic** odometry process noise — more uncertainty *along* the heading
    (wheel slip / scale error accumulate along travel) than across it — and shrinks on
    each GPS update by the optimal Kalman gain ``K = P·Sⁱ`` (``S = P + R·I``). GPS
    fixes are **Mahalanobis-gated** on the full innovation covariance
    ``d² = νᵀS⁻¹ν > gate`` (χ²(2 DOF); 9.21 ≈ 99%): multipath/jumps are rejected, with
    ``P`` inflated on repeated rejects to re-acquire (anti-divergence). A delayed fix
    (network/telemetry latency) is fused against the pose it actually describes —
    ``age_steps`` back in a short displacement buffer — not the current pose.

    ``R`` = GPS variance (σ_gps²); ``q_along``/``q_cross`` = process variance per metre.
    Local-ENU about ``(base_lat, base_lon)``: x = East, y = North.
    """

    def __init__(self, base_lat: float, base_lon: float, *, sigma_gps_m: float = 4.0,
                 q_along: float = 0.08, q_cross: float = 0.02, gate: float = 9.21,
                 p0: float = 25.0, latency_buffer: int = 25):
        self.base_lat = base_lat
        self.base_lon = base_lon
        self.R = sigma_gps_m ** 2
        self.q_along, self.q_cross = q_along, q_cross
        self.gate = gate
        self.x = 0.0
        self.y = 0.0
        self.pxx, self.pxy, self.pyy = p0, 0.0, p0      # covariance matrix
        self.n_rejected = 0
        self.last_rejected = False
        self._disp: list[tuple[float, float]] = []      # recent (dx,dy) for latency rewind
        self._buf = max(0, latency_buffer)
        self._m_lon = _M_PER_DEG_LAT * math.cos(math.radians(base_lat))

    def predict(self, ds_m: float, heading_deg: float) -> None:
        """Advance ``ds_m`` along ``heading_deg`` (0=N=+y, 90=E=+x); grow P anisotropically."""
        r = math.radians(heading_deg)
        ux, uy = math.sin(r), math.cos(r)               # along-track unit (East,North)
        vx, vy = math.cos(r), -math.sin(r)              # cross-track unit
        dx, dy = ds_m * ux, ds_m * uy
        self.x += dx
        self.y += dy
        if self._buf:
            self._disp.append((dx, dy))
            if len(self._disp) > self._buf:
                self._disp.pop(0)
        a, c = self.q_along * abs(ds_m), self.q_cross * abs(ds_m)   # along / cross variance
        self.pxx += a * ux * ux + c * vx * vx + 1e-6
        self.pxy += a * ux * uy + c * vx * vy
        self.pyy += a * uy * uy + c * vy * vy + 1e-6

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        """Project an absolute lat/lon into this filter's local-ENU frame (metres)."""
        return ((lon - self.base_lon) * self._m_lon, (lat - self.base_lat) * _M_PER_DEG_LAT)

    def correct_gps(self, lat: float, lon: float, age_steps: int = 0) -> bool:
        """Fuse a GPS fix (optionally ``age_steps`` old) via the Kalman gain; gate outliers.

        Returns ``True`` if accepted, ``False`` if Mahalanobis-gated out.
        """
        gx, gy = self.to_xy(lat, lon)
        # the fix describes where the rover was age_steps ago; compare it there
        bx, by = self.x, self.y
        # only rewind when the buffer actually spans the fix's age; otherwise we'd
        # silently rewind too few steps and fuse at the wrong pose — use current pose.
        if age_steps > 0 and self._disp and age_steps <= len(self._disp):
            for dx, dy in self._disp[-age_steps:]:
                bx -= dx
                by -= dy
        ix, iy = gx - bx, gy - by
        sxx, sxy, syy = self.pxx + self.R, self.pxy, self.pyy + self.R
        det = sxx * syy - sxy * sxy or 1e-9
        # Mahalanobis distance²  νᵀ S⁻¹ ν  (full 2×2 innovation covariance)
        d2 = (syy * ix * ix - 2.0 * sxy * ix * iy + sxx * iy * iy) / det
        if d2 > self.gate:
            self.n_rejected += 1
            self.last_rejected = True
            self.pxx += 0.5 * self.R                     # anti-divergence: open the gate
            self.pyy += 0.5 * self.R
            return False
        # Kalman gain K = P · S⁻¹  (S⁻¹ = [[syy,-sxy],[-sxy,sxx]]/det)
        ia, ib, ic = syy / det, -sxy / det, sxx / det    # S⁻¹ entries (symmetric)
        kxx = self.pxx * ia + self.pxy * ib
        kxy = self.pxx * ib + self.pxy * ic
        kyx = self.pxy * ia + self.pyy * ib
        kyy = self.pxy * ib + self.pyy * ic
        self.x += kxx * ix + kxy * iy
        self.y += kyx * ix + kyy * iy
        # P ← (I − K) P
        nxx = (1 - kxx) * self.pxx - kxy * self.pxy
        nxy = (1 - kxx) * self.pxy - kxy * self.pyy
        nyx = -kyx * self.pxx + (1 - kyy) * self.pxy
        nyy = -kyx * self.pxy + (1 - kyy) * self.pyy
        self.pxx, self.pxy, self.pyy = nxx, 0.5 * (nxy + nyx), nyy
        self.last_rejected = False
        return True

    def position_variance(self) -> float:
        """Scalar uncertainty summary: mean of the covariance diagonal (trace/2)."""
        return 0.5 * (self.pxx + self.pyy)

    def xy(self) -> tuple[float, float]:
        return self.x, self.y

    def latlon(self) -> tuple[float, float]:
        return self.base_lat + self.y / _M_PER_DEG_LAT, self.base_lon + self.x / self._m_lon


def mag_heading_deg(mx: float, my: float, declination_deg: float = 0.0) -> float:
    """Tilt-free magnetometer heading from x/y components (+ optional declination)."""
    return _wrap360(math.degrees(math.atan2(mx, my)) + declination_deg)


class MagnetometerCalibrator:
    """Hard- and soft-iron magnetometer calibration (min/max method).

    A raw magnetometer does not trace a sphere when rotated but an off-centre
    ellipsoid: **hard-iron** (ferrous parts / DC currents) shifts the centre,
    **soft-iron** (nearby metal distorting the field) scales/skews the axes — both
    corrupt heading. Collect samples while rotating the robot through yaw, then:

      * hard-iron offset  ``b_i = (max_i + min_i)/2``     (re-centre to the origin)
      * soft-iron scale   ``s_i = r̄ / r_i``, ``r_i=(max_i−min_i)/2``  (re-round to a sphere)
      * calibrated        ``m_i' = s_i (m_i − b_i)``

    This stdlib version corrects hard-iron fully and soft-iron on the diagonal;
    full off-diagonal soft-iron needs an ellipsoid least-squares fit (numpy).
    """

    def __init__(self):
        self.samples: list[tuple[float, float, float]] = []
        self.offset = (0.0, 0.0, 0.0)
        self.scale = (1.0, 1.0, 1.0)
        self._center = None       # ellipsoid-fit centre (numpy array)
        self._T = None            # ellipsoid-fit shaping matrix (3×3, numpy)

    def add(self, mx: float, my: float, mz: float) -> None:
        self.samples.append((mx, my, mz))

    def fit(self) -> None:
        if not self.samples:
            return
        lo = [min(s[i] for s in self.samples) for i in range(3)]
        hi = [max(s[i] for s in self.samples) for i in range(3)]
        off = [(hi[i] + lo[i]) / 2.0 for i in range(3)]
        rad = [(hi[i] - lo[i]) / 2.0 for i in range(3)]
        nz = [r for r in rad if r > 1e-9]
        mean_r = sum(nz) / len(nz) if nz else 1.0
        scale = [(mean_r / r) if r > 1e-9 else 1.0 for r in rad]
        self.offset, self.scale = tuple(off), tuple(scale)

    def apply(self, mx: float, my: float, mz: float) -> tuple[float, float, float]:
        b, s = self.offset, self.scale
        return (s[0] * (mx - b[0]), s[1] * (my - b[1]), s[2] * (mz - b[2]))

    def heading_deg(self, mx: float, my: float, mz: float = 0.0,
                    declination_deg: float = 0.0) -> float:
        """Calibrated tilt-free heading from a raw magnetometer reading."""
        cx, cy, _ = self.apply(mx, my, mz)
        return mag_heading_deg(cx, cy, declination_deg)

    # --- full ellipsoid fit (numpy) — handles off-diagonal (rotated) soft-iron ---
    def fit_ellipsoid(self) -> None:
        """Least-squares fit a general ellipsoid; recover centre + shaping matrix.

        Solves ``Xᵀ A X + 2nᵀX = 1`` for the samples, giving centre ``c = −A⁻¹n`` and a
        shaping matrix ``T`` (matrix square root of ``A/k``) so that ``T(m − c)`` maps
        the ellipsoid to the unit sphere — correcting hard-iron *and* full (rotated)
        soft-iron, which the diagonal min/max method cannot. Requires numpy.
        """
        import numpy as np

        P = np.asarray(self.samples, float)
        x, y, z = P[:, 0], P[:, 1], P[:, 2]
        D = np.column_stack([x * x, y * y, z * z, 2 * y * z, 2 * x * z, 2 * x * y,
                             2 * x, 2 * y, 2 * z])
        v, *_ = np.linalg.lstsq(D, np.ones(len(x)), rcond=None)
        a, b, c, f, g, h, p, q, r = v
        A = np.array([[a, h, g], [h, b, f], [g, f, c]])
        n = np.array([p, q, r])
        center = np.linalg.solve(A, -n)
        k = 1.0 - n.dot(center)                       # quadratic form value at the centre
        w, V = np.linalg.eigh(A / k)                  # A/k = V diag(w) Vᵀ  (SPD)
        self._T = V @ np.diag(np.sqrt(np.abs(w))) @ V.T
        self._center = center

    def apply_ellipsoid(self, mx: float, my: float, mz: float):
        import numpy as np
        return self._T @ (np.array([mx, my, mz], float) - self._center)

    def heading_ellipsoid_deg(self, mx: float, my: float, mz: float,
                              declination_deg: float = 0.0) -> float:
        c = self.apply_ellipsoid(mx, my, mz)
        return mag_heading_deg(float(c[0]), float(c[1]), declination_deg)
