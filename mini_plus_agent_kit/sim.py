"""Unified rover simulator: physics + configurable sensor models for the REAL stack.

Drives the actual `NavController` (heading/pose fusion, planner, controllers, safety)
with synthetic but realistically-corrupted telemetry, so every navigation claim is a
closed-loop result of the *shipping code* rather than a bespoke per-test loop. Sensor
error is fully parameterized (`SensorModel`) to enable **domain-randomized Monte-Carlo
validation**: prove the controller is robust across a wide range of GPS/IMU/odometry
error so the real robot's (unknown) parameters fall inside a range already passed.

What this *can* prove: algorithm correctness and closed-loop robustness across a
modelled error envelope. What it *cannot* prove: that the envelope matches reality
(real GPS-multipath statistics, gyro bias stability, magnetic environment, telemetry
latency, wheel slip) or the platform's true parameter values — those need hardware.

Pure stdlib. Frame: local-ENU metres; heading = compass degrees (0=N, 90=E, CW+).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .geo import heading_error_deg

YAW_RATE_DPS = 60.0      # true yaw rate (deg/s) at |angular cmd| = 1
SPEED_MPS = 1.2          # true ground speed (m/s) at |linear cmd| = 1
_M_PER_DEG_LAT = 111_320.0


@dataclass
class SensorModel:
    """Every knob of sensor corruption — the axes a Monte-Carlo run randomizes over."""
    gps_sigma_m: float = 3.0
    gps_period_steps: int = 5         # a fix every N steps (1 Hz at dt=0.2)
    gps_latency_steps: int = 0        # fix describes the pose this many steps ago
    multipath_prob: float = 0.0       # per-fix probability of an urban-canyon spike
    multipath_m: float = 25.0
    doppler: bool = False             # receiver provides velocity course/speed directly
    doppler_course_noise_deg: float = 5.0
    gyro_bias_dps: float = 0.0
    gyro_noise_dps: float = 0.0
    mag_bias_deg: float = 0.0         # net heading offset (hard-iron / declination)
    mag_noise_deg: float = 0.0
    odom_scale: float = 1.0           # wheel-odometry scale error
    odom_noise_m: float = 0.0
    battery_pct: float = 90.0


@dataclass
class RoverSim:
    """Differential-drive truth model + a `SensorModel`-corrupted observation stream."""
    model: SensorModel = field(default_factory=SensorModel)
    base_lat: float = 37.87
    base_lon: float = -122.25
    dt: float = 0.2
    seed: int = 0
    x: float = 0.0
    y: float = 0.0
    th: float = 0.0                   # true compass heading

    def __post_init__(self):
        self._rng = random.Random(self.seed)
        self._t = 0
        self._mlon = _M_PER_DEG_LAT * math.cos(math.radians(self.base_lat))
        self._hist: list[tuple[float, float]] = [(self.x, self.y)]
        self._cmd = (0.0, 0.0)

    def apply(self, linear: float, angular: float) -> None:
        """Advance the true pose one ``dt`` under a (linear, angular) twist command."""
        self._cmd = (linear, angular)
        self.th = (self.th - angular * YAW_RATE_DPS * self.dt) % 360.0
        ds = SPEED_MPS * linear * self.dt
        self.x += ds * math.sin(math.radians(self.th))
        self.y += ds * math.cos(math.radians(self.th))
        self._hist.append((self.x, self.y))
        self._t += 1

    def _to_ll(self, x, y):
        return self.base_lat + y / _M_PER_DEG_LAT, self.base_lon + x / self._mlon

    def goal_enu(self, goal_lat, goal_lon):
        return ((goal_lon - self.base_lon) * self._mlon,
                (goal_lat - self.base_lat) * _M_PER_DEG_LAT)

    def _lidar(self, obstacles):
        if not obstacles:
            return None
        best = None
        for ox, oy in obstacles:
            d = math.hypot(ox - self.x, oy - self.y)
            brg = math.degrees(math.atan2(ox - self.x, oy - self.y)) % 360.0
            if abs(heading_error_deg(self.th, brg)) < 35.0 and (best is None or d < best):
                best = d
        return best

    def observe(self, obstacles=None) -> dict:
        """The corrupted telemetry NavController.step consumes this tick."""
        m, rng = self.model, self._rng
        true_yaw = -self._cmd[1] * YAW_RATE_DPS
        obs = {
            "heading_deg": (self.th + m.mag_bias_deg + rng.gauss(0, m.mag_noise_deg)) % 360.0,
            "yaw_rate_dps": true_yaw + m.gyro_bias_dps + rng.gauss(0, m.gyro_noise_dps),
            "ds_m": SPEED_MPS * self._cmd[0] * self.dt * m.odom_scale + rng.gauss(0, m.odom_noise_m),
            "battery": m.battery_pct,
            "lidar_front_m": self._lidar(obstacles),
            "obstacles": obstacles,
            "lat": None, "lon": None, "gps_age_steps": 0,
            "gps_course_deg": None, "gps_speed_mps": None,
        }
        if self._t % m.gps_period_steps == 0:
            idx = max(0, len(self._hist) - 1 - m.gps_latency_steps)   # latency
            tx, ty = self._hist[idx]
            gx = tx + rng.gauss(0, m.gps_sigma_m)
            gy = ty + rng.gauss(0, m.gps_sigma_m)
            if rng.random() < m.multipath_prob:                      # multipath spike
                a = rng.uniform(0, 2 * math.pi)
                gx += m.multipath_m * math.cos(a)
                gy += m.multipath_m * math.sin(a)
            obs["lat"], obs["lon"] = self._to_ll(gx, gy)
            obs["gps_age_steps"] = m.gps_latency_steps
            if m.doppler:                                            # Doppler velocity
                spd = SPEED_MPS * abs(self._cmd[0])
                if spd > 0.05:
                    obs["gps_course_deg"] = (self.th + rng.gauss(0, m.doppler_course_noise_deg)) % 360.0
                    obs["gps_speed_mps"] = spd
        return obs


def run_scenario(nav, sim: RoverSim, goal_lat: float, goal_lon: float, *,
                 obstacles=None, max_steps: int = 800, tol_m: float = 15.0) -> dict:
    """Drive ``nav`` (a NavController) over ``sim`` to the goal; return truth metrics.

    ``success`` requires the controller to *believe* it arrived AND to *truly* be within
    ``tol_m`` of the goal — a fix-faked arrival outside tolerance is a failure.
    """
    gx, gy = sim.goal_enu(goal_lat, goal_lon)
    head_err_sq = 0.0
    steps = 0
    min_clear = float("inf")
    arrived = False
    for _ in range(max_steps):
        obs = sim.observe(obstacles)
        s = nav.step(sim.dt, heading_deg=obs["heading_deg"], goal_lat=goal_lat,
                     goal_lon=goal_lon, lat=obs["lat"], lon=obs["lon"],
                     yaw_rate_dps=obs["yaw_rate_dps"], ds_m=obs["ds_m"],
                     battery=obs["battery"], lidar_front_m=obs["lidar_front_m"],
                     obstacles=obs["obstacles"], gps_age_steps=obs["gps_age_steps"],
                     gps_course_deg=obs["gps_course_deg"], gps_speed_mps=obs["gps_speed_mps"])
        if nav.hf.heading is not None:
            head_err_sq += heading_error_deg(nav.hf.heading, sim.th) ** 2
        if obstacles:
            min_clear = min(min_clear, min(math.hypot(sim.x - ox, sim.y - oy)
                                           for ox, oy in obstacles))
        steps += 1
        if s.arrived:
            arrived = True
            break
        sim.apply(s.linear, s.angular)
    true_dist = math.hypot(gx - sim.x, gy - sim.y)
    return {
        "success": arrived and true_dist <= tol_m,
        "arrived": arrived, "true_dist": true_dist, "steps": steps,
        "heading_rmse": math.sqrt(head_err_sq / steps) if steps else None,
        "min_clear": (None if min_clear == float("inf") else min_clear),
        "gps_rejected": nav.pf.n_rejected,
    }


def random_model(rng: random.Random) -> SensorModel:
    """A randomly-drawn sensor-error profile for domain-randomized Monte-Carlo runs."""
    return SensorModel(
        gps_sigma_m=rng.uniform(2.0, 6.0),
        gps_latency_steps=rng.randint(0, 4),
        multipath_prob=rng.uniform(0.0, 0.12),
        gyro_bias_dps=rng.uniform(-4.0, 4.0),
        gyro_noise_dps=rng.uniform(0.0, 3.0),
        mag_bias_deg=rng.uniform(-15.0, 15.0),
        mag_noise_deg=rng.uniform(3.0, 10.0),
        odom_scale=rng.uniform(0.97, 1.04),     # realistic calibrated wheel-odometry error
        odom_noise_m=rng.uniform(0.0, 0.05),
    )
