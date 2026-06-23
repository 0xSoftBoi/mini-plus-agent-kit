"""Closed-loop motion controllers + safety envelope (replacing open-loop bursts).

Pure control law math (stdlib only), unit-tested and validated in a noisy
kinematic simulation (``tests/live/test_live_navstack.py``). Designed to run
inside a fixed-rate loop over the fused pose from ``estimator.py``.

* ``HeadingPID``        — in-place turn-to-heading with settle/clamp (vs blind timer)
* ``PursuitController`` — smooth seek-to-waypoint (linear, angular); slows on
  approach; steers proportional to bearing error (no bang-bang oscillation)
* ``DistanceController``— closed-loop forward distance using wheel odometry
* ``SafetyEnvelope``    — battery floor, tilt cutoff, lidar time-to-collision
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .estimator import HeadingFilter, PoseFilter
from .geo import heading_error_deg, gps_course_and_speed


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


# --------------------------------------------------------------------------- #
# Heading PID (turn-to-heading)
# --------------------------------------------------------------------------- #
class HeadingPID:
    """PID on signed heading error → angular command in [-out_clip, out_clip]."""

    def __init__(self, kp: float = 0.012, ki: float = 0.0, kd: float = 0.004,
                 out_clip: float = 0.6, settle_deg: float = 5.0):
        self.kp, self.ki, self.kd = kp, ki, kd
        self.out_clip, self.settle_deg = out_clip, settle_deg
        self._i = 0.0
        self._prev: float | None = None

    def reset(self) -> None:
        self._i = 0.0
        self._prev = None

    def step(self, dt: float, current_deg: float, target_deg: float) -> float:
        e = heading_error_deg(current_deg, target_deg)
        self._i += e * dt
        d = 0.0 if self._prev is None or dt <= 0 else (e - self._prev) / dt
        self._prev = e
        # Output is a twist angular command (ROS sign: +angular = CCW = left).
        # heading_error is compass (+ = right/CW), so negate to null it.
        out = -(self.kp * e + self.ki * self._i + self.kd * d)
        return _clamp(out, -self.out_clip, self.out_clip)

    def settled(self, current_deg: float, target_deg: float) -> bool:
        return abs(heading_error_deg(current_deg, target_deg)) <= self.settle_deg


# --------------------------------------------------------------------------- #
# Pursuit waypoint controller
# --------------------------------------------------------------------------- #
@dataclass
class Cmd:
    linear: float
    angular: float
    distance_m: float
    bearing_deg: float
    heading_error_deg: float
    arrived: bool


class PursuitController:
    """Seek-to-waypoint: proportional steering + approach slow-down.

    Curvature-style steering ``angular = clamp(k_ang · sin(α))`` (α = signed
    heading error to the goal) keeps the turn smooth and bounded; forward speed
    ``linear = v_max · max(0,cos α) · min(1, d / slow_radius)`` slows when the goal
    is off-axis or near. This is a single-target pure-pursuit reduction — no
    turn-then-go bang-bang, no oscillation about the bearing.
    """

    def __init__(self, v_max: float = 0.6, k_ang: float = 1.6,
                 slow_radius_m: float = 8.0, tol_m: float = 15.0, min_creep: float = 0.12):
        self.v_max, self.k_ang = v_max, k_ang
        self.slow_radius_m, self.tol_m, self.min_creep = slow_radius_m, tol_m, min_creep

    def step(self, x: float, y: float, heading_deg: float, gx: float, gy: float) -> Cmd:
        dx, dy = gx - x, gy - y
        dist = math.hypot(dx, dy)
        bearing = math.degrees(math.atan2(dx, dy)) % 360.0      # 0=N, 90=E
        err = heading_error_deg(heading_deg, bearing)            # signed deg
        if dist <= self.tol_m:
            return Cmd(0.0, 0.0, dist, bearing, err, True)
        a = math.radians(err)
        # +angular = CCW = left; err is compass (+ = right) → negate to steer toward goal.
        angular = _clamp(-self.k_ang * math.sin(a), -1.0, 1.0)
        linear = self.v_max * max(0.0, math.cos(a)) * min(1.0, dist / self.slow_radius_m)
        if abs(err) < 90.0:
            linear = max(linear, self.min_creep)                # always make headway when roughly facing it
        return Cmd(linear, angular, dist, bearing, err, False)


# --------------------------------------------------------------------------- #
# Regulated pure pursuit (path tracking)
# --------------------------------------------------------------------------- #
class RegulatedPurePursuit:
    """Track a polyline path with pure-pursuit steering + Nav2-style speed regulation.

    Unlike `PursuitController` (steers at a *single* waypoint), this follows a planned
    path that routes around obstacles. Each step: find the closest point on the path,
    advance a velocity-scaled **lookahead distance** ``L = clamp(t_la·v, L_min, L_max)``
    to a lookahead point, and steer by the pure-pursuit curvature
    ``κ = 2·sin(α)/L`` (α = signed bearing error to that point). Linear speed is then
    **regulated** down on (a) sharp curvature (turn radius ``r = 1/|κ| < r_min``),
    (b) approach to the final goal, and (c) — when a cost lookup is supplied — proximity
    to obstacles. Plain pure pursuit oscillates/fails above ~1.5 m/s; these regulators
    are what make it track accurately. Ref: Nav2 ``regulated_pure_pursuit``.
    """

    def __init__(self, v_max: float = 0.6, lookahead_time: float = 1.5,
                 min_lookahead: float = 2.0, max_lookahead: float = 12.0,
                 regulation_min_radius: float = 4.0, approach_dist: float = 6.0,
                 tol_m: float = 15.0, min_speed: float = 0.1,
                 curvature_to_cmd: float = 2.0):
        self.v_max = v_max
        self.lookahead_time = lookahead_time
        self.min_lookahead, self.max_lookahead = min_lookahead, max_lookahead
        self.regulation_min_radius = regulation_min_radius
        self.approach_dist = approach_dist
        self.tol_m, self.min_speed = tol_m, min_speed
        self.curvature_to_cmd = curvature_to_cmd

    def _lookahead_point(self, x, y, path, L):
        """Closest point on the path, then march ``L`` metres forward along it."""
        # closest projection over all segments
        best_d2, best_i, best_pt = float("inf"), 0, path[0]
        for i in range(len(path) - 1):
            ax, ay = path[i]
            bx, by = path[i + 1]
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy or 1e-9
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / seg2))
            px, py = ax + t * dx, ay + t * dy
            d2 = (x - px) ** 2 + (y - py) ** 2
            if d2 < best_d2:
                best_d2, best_i, best_pt = d2, i, (px, py)
        # walk forward L metres from the projection
        remaining = L
        cur = best_pt
        for i in range(best_i, len(path) - 1):
            nxt = path[i + 1]
            seg = math.hypot(nxt[0] - cur[0], nxt[1] - cur[1])
            if seg >= remaining:
                f = remaining / seg if seg else 1.0
                return (cur[0] + (nxt[0] - cur[0]) * f, cur[1] + (nxt[1] - cur[1]) * f)
            remaining -= seg
            cur = nxt
        return path[-1]

    def step(self, x: float, y: float, heading_deg: float,
             path: list, v_now: float | None = None) -> Cmd:
        if not path:
            return Cmd(0.0, 0.0, 0.0, heading_deg, 0.0, True)
        gx, gy = path[-1]
        dist_goal = math.hypot(gx - x, gy - y)
        v = self.v_max if v_now is None else max(self.min_speed, v_now)
        L = _clamp(self.lookahead_time * v, self.min_lookahead, self.max_lookahead)
        lx, ly = self._lookahead_point(x, y, path, L)
        bearing = math.degrees(math.atan2(lx - x, ly - y)) % 360.0
        err = heading_error_deg(heading_deg, bearing)
        if dist_goal <= self.tol_m:
            return Cmd(0.0, 0.0, dist_goal, bearing, err, True)
        a = math.radians(err)
        ld = max(math.hypot(lx - x, ly - y), 1e-3)
        kappa = 2.0 * math.sin(a) / ld                       # pure-pursuit curvature
        angular = _clamp(-self.curvature_to_cmd * kappa, -1.0, 1.0)   # ROS sign (neg = right)
        # --- speed regulation ---
        v_cmd = self.v_max
        radius = (1.0 / abs(kappa)) if abs(kappa) > 1e-6 else math.inf
        if radius < self.regulation_min_radius:              # (a) curvature
            v_cmd *= max(0.15, radius / self.regulation_min_radius)
        v_cmd = min(v_cmd, self.v_max * min(1.0, dist_goal / self.approach_dist))  # (b) approach
        linear = max(self.min_speed if abs(err) < 90.0 else 0.0, v_cmd)
        return Cmd(linear, angular, dist_goal, bearing, err, False)


# --------------------------------------------------------------------------- #
# Dynamic Window Approach (local obstacle avoidance)
# --------------------------------------------------------------------------- #
def _linspace(lo: float, hi: float, n: int) -> list:
    if n <= 1 or hi <= lo:
        return [lo]
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


class DWAPlanner:
    """Dynamic Window Approach — steer *around* obstacles, not just brake.

    The reactive `SafetyEnvelope` can only stop; a path planner (`planner.py`) only
    knows the obstacles on its costmap. The DWA local planner closes the gap for
    *newly-sensed or moving* obstacles: it samples the reachable ``(v, ω)`` velocity
    window (bounded by acceleration limits about the current command), rolls each
    candidate forward over a short horizon, **discards trajectories that collide**,
    and scores the survivors by a weighted sum of **goal progress**, **obstacle
    clearance**, and **speed** — then commits the best command (Fox, Burgard &
    Thrun, 1997). The result actively rounds a pedestrian/cone while still driving to
    the goal. Velocities/yaw are the kit's normalized commands; rollouts are physical
    via ``v_scale_mps`` (m/s at v=1) and ``yaw_rate_dps`` (deg/s at ω=1).
    """

    def __init__(self, v_scale_mps: float = 1.2, yaw_rate_dps: float = 60.0,
                 robot_radius: float = 0.6, horizon_s: float = 2.0, sim_dt: float = 0.2,
                 v_samples: int = 7, w_samples: int = 19, accel_v: float = 3.0,
                 accel_w: float = 6.0, tol_m: float = 2.0, clear_cap_m: float = 3.0,
                 w_goal: float = 0.6, w_clear: float = 0.3, w_speed: float = 0.1):
        self.v_scale_mps, self.yaw_rate_dps = v_scale_mps, yaw_rate_dps
        self.robot_radius = robot_radius
        self.horizon_s, self.sim_dt = horizon_s, sim_dt
        self.v_samples, self.w_samples = v_samples, w_samples
        self.accel_v, self.accel_w = accel_v, accel_w
        self.tol_m, self.clear_cap_m = tol_m, clear_cap_m
        self.w_goal, self.w_clear, self.w_speed = w_goal, w_clear, w_speed

    def _clearance(self, x, y, obstacles):
        if not obstacles:
            return self.clear_cap_m
        d = min(math.hypot(x - ox, y - oy) for ox, oy in obstacles)
        return min(d, self.clear_cap_m)

    def _rollout(self, x, y, th, v_cmd, w_cmd, obstacles):
        """Constant-(v,ω) rollout; returns (endpoint, min_clearance, collided)."""
        v_mps = v_cmd * self.v_scale_mps
        steps = max(1, int(self.horizon_s / self.sim_dt))
        min_clear = self.clear_cap_m
        for _ in range(steps):
            th = (th - w_cmd * self.yaw_rate_dps * self.sim_dt) % 360.0
            r = math.radians(th)
            x += v_mps * self.sim_dt * math.sin(r)
            y += v_mps * self.sim_dt * math.cos(r)
            c = self._clearance(x, y, obstacles)
            if c <= self.robot_radius:
                return (x, y, th), c, True
            min_clear = min(min_clear, c)
        return (x, y, th), min_clear, False

    def step(self, x: float, y: float, heading_deg: float, gx: float, gy: float, *,
             v_cmd0: float = 0.0, w_cmd0: float = 0.0, obstacles: list | None = None) -> Cmd:
        dist = math.hypot(gx - x, gy - y)
        bearing = math.degrees(math.atan2(gx - x, gy - y)) % 360.0
        err = heading_error_deg(heading_deg, bearing)
        if dist <= self.tol_m:
            return Cmd(0.0, 0.0, dist, bearing, err, True)
        # dynamic window: velocities reachable from the current command this period
        v_lo = max(0.0, v_cmd0 - self.accel_v * self.sim_dt)
        v_hi = min(1.0, v_cmd0 + self.accel_v * self.sim_dt)
        w_lo = max(-1.0, w_cmd0 - self.accel_w * self.sim_dt)
        w_hi = min(1.0, w_cmd0 + self.accel_w * self.sim_dt)
        cands = []
        for v in _linspace(v_lo, v_hi, self.v_samples):
            for w in _linspace(w_lo, w_hi, self.w_samples):
                (ex, ey, _), clear, hit = self._rollout(x, y, heading_deg, v, w, obstacles)
                if hit:
                    continue
                goal_d = math.hypot(gx - ex, gy - ey)
                cands.append([v, w, goal_d, clear])
        if not cands:
            return Cmd(0.0, 0.0, dist, bearing, err, False)   # boxed in → stop (failsafe)
        # normalize each objective across candidates, then weighted-sum (Fox et al.)
        gd = [c[2] for c in cands]
        cl = [c[3] for c in cands]
        gmin, gmax = min(gd), max(gd)
        cmin, cmax = min(cl), max(cl)
        best, best_score = cands[0], -1e18
        for c in cands:
            s_goal = 1.0 - (c[2] - gmin) / (gmax - gmin) if gmax > gmin else 1.0   # closer = better
            s_clear = (c[3] - cmin) / (cmax - cmin) if cmax > cmin else 1.0
            s_speed = c[0]
            score = self.w_goal * s_goal + self.w_clear * s_clear + self.w_speed * s_speed
            if score > best_score:
                best_score, best = score, c
        return Cmd(best[0], best[1], dist, bearing, err, False)


# --------------------------------------------------------------------------- #
# Odometry distance controller
# --------------------------------------------------------------------------- #
class DistanceController:
    """Closed-loop forward distance from accumulated wheel odometry (not a timer)."""

    def __init__(self, v: float = 0.4, tol_m: float = 0.2):
        self.v, self.tol_m = v, tol_m
        self._start: float | None = None

    def begin(self, odom_m: float) -> None:
        self._start = odom_m

    def step(self, odom_m: float, target_m: float) -> tuple[float, bool]:
        if self._start is None:
            self._start = odom_m
        travelled = odom_m - self._start
        remaining = target_m - travelled
        if remaining <= self.tol_m:
            return 0.0, True
        return (self.v if remaining > 1.0 else self.v * 0.5), False


# --------------------------------------------------------------------------- #
# Safety envelope
# --------------------------------------------------------------------------- #
@dataclass
class SafetyLimits:
    battery_floor: float = 10.0     # %  (or volts, caller's unit)
    tilt_limit_deg: float = 25.0    # |roll| or |pitch| → ramp/pickup/stuck
    ttc_min_s: float = 1.5          # lidar time-to-collision hard stop
    ttc_slow_s: float = 3.0         # begin scaling speed below this


@dataclass
class SafetyVerdict:
    ok: bool
    scale: float          # multiply commanded linear by this (0..1)
    reason: str


class SafetyEnvelope:
    """Gate/scale forward speed from battery, tilt, and lidar time-to-collision."""

    def __init__(self, limits: SafetyLimits | None = None):
        self.limits = limits or SafetyLimits()

    def check(self, linear_cmd: float, *, battery: float | None = None,
              roll: float | None = None, pitch: float | None = None,
              lidar_front_m: float | None = None, estop: bool = False) -> SafetyVerdict:
        L = self.limits
        if estop:
            return SafetyVerdict(False, 0.0, "estop engaged")
        if battery is not None and battery <= L.battery_floor:
            return SafetyVerdict(False, 0.0, f"battery {battery} ≤ floor {L.battery_floor}")
        tilt = max(abs(roll or 0.0), abs(pitch or 0.0))
        if tilt >= L.tilt_limit_deg:
            return SafetyVerdict(False, 0.0, f"tilt {tilt:.0f}° ≥ {L.tilt_limit_deg}° (ramp/pickup/stuck)")
        # lidar time-to-collision (only when moving forward)
        if lidar_front_m is not None and linear_cmd > 1e-3:
            ttc = lidar_front_m / max(1e-3, linear_cmd)   # crude TTC in "seconds" at unit speed
            if ttc <= L.ttc_min_s:
                return SafetyVerdict(False, 0.0, f"TTC {ttc:.1f}s ≤ {L.ttc_min_s}s")
            if ttc < L.ttc_slow_s:
                return SafetyVerdict(True, _clamp(ttc / L.ttc_slow_s, 0.0, 1.0), f"slow: TTC {ttc:.1f}s")
        return SafetyVerdict(True, 1.0, "clear")


# --------------------------------------------------------------------------- #
# NavController — the composed closed-loop stack (estimator + pursuit + safety)
# --------------------------------------------------------------------------- #
@dataclass
class NavStep:
    linear: float            # safety-scaled twist command
    angular: float
    distance_m: float        # estimated range to goal
    heading_error_deg: float
    arrived: bool
    safe: bool
    safety: str              # safety reason (e.g. "clear", "slow: TTC 2.1s")
    est_lat: float
    est_lon: float
    gps_rejected: bool = False   # this step's GPS fix was Mahalanobis-gated (outlier)


class NavController:
    """One-call fused waypoint controller: heading + pose fusion → pursuit → safety.

    Wraps :class:`HeadingFilter` (orientation/IMU), :class:`PoseFilter` (odometry +
    GPS), :class:`PursuitController`, and :class:`SafetyEnvelope` behind a single
    ``step(...)`` that takes raw telemetry and returns a safety-gated twist. The
    controller advances its own dead-reckoning between GPS fixes from the *previous*
    command (×``v_scale_mps``) when no wheel odometry is available, so it still
    produces a smoothed pose at loop rate on platforms (e.g. Earth Rover) that only
    expose GPS + heading. Validated in ``tests/live/test_live_navstack.py``.
    """

    def __init__(self, base_lat: float, base_lon: float, *, v_max: float = 0.6,
                 k_ang: float = 1.6, slow_radius_m: float = 8.0, tol_m: float = 15.0,
                 v_scale_mps: float = 0.6, limits: SafetyLimits | None = None,
                 use_rpp: bool = False, use_dwa: bool = False, yaw_rate_dps: float = 60.0):
        self.hf = HeadingFilter()
        self.pf = PoseFilter(base_lat, base_lon)
        self.pp = PursuitController(v_max=v_max, k_ang=k_ang,
                                    slow_radius_m=slow_radius_m, tol_m=tol_m)
        self.rpp = RegulatedPurePursuit(v_max=v_max, tol_m=tol_m) if use_rpp else None
        self.dwa = (DWAPlanner(v_scale_mps=v_scale_mps, yaw_rate_dps=yaw_rate_dps,
                               tol_m=tol_m) if use_dwa else None)
        self.path: list | None = None       # ENU world points from the global planner
        self.safety = SafetyEnvelope(limits)
        self.v_scale_mps = v_scale_mps
        self._last_v = 0.0
        self._last_w = 0.0
        self._prev_acc: tuple[float, float] | None = None   # last KF-accepted GPS fix
        self._t_acc = 0.0                                    # time since that accepted fix
        self._pending_course: float | None = None           # course for next heading update
        self._pending_speed = 0.0

    def set_path(self, path_xy: list) -> None:
        """Track this planned ENU path (from ``planner.plan_path``) with regulated pure pursuit."""
        if self.rpp is None:
            self.rpp = RegulatedPurePursuit(v_max=self.pp.v_max, tol_m=self.pp.tol_m)
        self.path = path_xy

    def step(self, dt: float, *, heading_deg: float, goal_lat: float, goal_lon: float,
             lat: float | None = None, lon: float | None = None,
             yaw_rate_dps: float | None = None, ds_m: float | None = None,
             battery: float | None = None, roll: float | None = None,
             pitch: float | None = None, lidar_front_m: float | None = None,
             estop: bool = False, obstacles: list | None = None,
             gps_age_steps: int = 0, gps_course_deg: float | None = None,
             gps_speed_mps: float | None = None) -> NavStep:
        # heading: predict on gyro, correct toward mag + (speed-gated) GPS course.
        # The course was derived last step from KF-accepted fixes only (see below).
        hhat = self.hf.update(dt, gyro_z_dps=yaw_rate_dps or 0.0, absolute_deg=heading_deg,
                              course_deg=self._pending_course, speed_mps=self._pending_speed)
        self._pending_course, self._pending_speed = None, 0.0
        # dead-reckon: measured wheel odometry if given, else commanded-velocity proxy
        if ds_m is None:
            ds_m = self.v_scale_mps * self._last_v * dt
        self.pf.predict(ds_m, hhat)
        gps_rejected = False
        self._t_acc += dt
        if lat is not None and lon is not None:
            accepted = self.pf.correct_gps(lat, lon, age_steps=gps_age_steps)
            gps_rejected = not accepted
            if accepted:
                if gps_course_deg is not None:
                    # GPS Doppler velocity: a low-noise course/speed straight from the
                    # receiver (preferred over position differencing when available).
                    self._pending_course = gps_course_deg
                    self._pending_speed = gps_speed_mps or 0.0
                elif self._prev_acc is not None:
                    # else course-over-ground from consecutive *accepted* fixes
                    # (multipath already gated out), and only when the displacement
                    # clears the GPS noise — else differenced course is noise.
                    c, sp = gps_course_and_speed(self._prev_acc[0], self._prev_acc[1],
                                                 lat, lon, self._t_acc)
                    if c is not None and sp * self._t_acc > 6.0 * math.sqrt(self.pf.R):
                        self._pending_course, self._pending_speed = c, sp
                self._prev_acc = (lat, lon)
                self._t_acc = 0.0
        x, y = self.pf.xy()
        gx, gy = self.pf.to_xy(goal_lat, goal_lon)
        if self.path and self.rpp is not None:               # track the planned path
            cmd = self.rpp.step(x, y, hhat, self.path)
        elif self.dwa is not None and obstacles:             # reactive local avoidance
            cmd = self.dwa.step(x, y, hhat, gx, gy, v_cmd0=self._last_v,
                                w_cmd0=self._last_w, obstacles=obstacles)
        else:                                                # seek the single waypoint
            cmd = self.pp.step(x, y, hhat, gx, gy)
        verdict = self.safety.check(cmd.linear, battery=battery, roll=roll, pitch=pitch,
                                    lidar_front_m=lidar_front_m, estop=estop)
        linear = cmd.linear * verdict.scale if verdict.ok else 0.0
        angular = cmd.angular if verdict.ok else 0.0
        self._last_v = linear
        self._last_w = angular
        est_lat, est_lon = self.pf.latlon()
        return NavStep(linear, angular, cmd.distance_m, cmd.heading_error_deg,
                       cmd.arrived, verdict.ok, verdict.reason, est_lat, est_lon,
                       gps_rejected)
