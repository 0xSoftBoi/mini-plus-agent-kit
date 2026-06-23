"""LIVE sim — fused navigation stack vs the bang-bang baseline, under real noise.

A 2-D differential-drive rover is simulated with realistic sensor corruption:
GPS at 1 Hz with sigma=4 m, a gyro with constant bias + noise (drift), and a noisy
magnetometer. Two controllers drive the SAME truth + SAME noise sequence:

  A (baseline = the kit's old approach): bang-bang on RAW GPS + RAW heading.
  B (deep stack): HeadingFilter (gyro+mag) + PoseFilter (odometry+GPS) → PursuitController.

We report cross-track RMS, final true distance, heading-estimate RMSE, and whether
each controller's *claimed* arrival was actually true (GPS noise can fake it).

    .venv/bin/python tests/live/test_live_navstack.py
"""

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mini_plus_agent_kit.control import NavController
from mini_plus_agent_kit.geo import heading_error_deg

DT = 0.2
YAW_RATE = 60.0      # deg/s at |angular|=1
SPEED = 1.2          # m/s at linear=1
BASE_LAT, BASE_LON = 37.87, -122.25
M = 111_320.0
MLON = M * math.cos(math.radians(BASE_LAT))
GOAL = (25.0, 60.0)  # ENU metres; ~65 m at bearing ~23 deg
TOL = 15.0
GPS_SIGMA = 4.0
GYRO_BIAS = 3.0          # deg/s constant drift
GYRO_NOISE = 2.0
MAG_NOISE = 8.0
MULTIPATH_EVERY = 10     # every 10th GPS fix is an urban-canyon multipath spike
MULTIPATH_M = 25.0       # spike magnitude (well outside the 3-sigma gate)


def _cross_track(x, y):
    gx, gy = GOAL
    gn = math.hypot(gx, gy)
    return abs(x * gy - y * gx) / gn   # |p x ĝ| = perpendicular distance to the O→G line


def simulate(controller, seed):
    rng = random.Random(seed)
    x = y = th = 0.0           # true pose (th compass deg, 0=N, CW+)
    odom = 0.0
    raw_gps = (0.0, 0.0)
    xt_sq = 0.0
    head_err_sq = 0.0
    n = 0
    n_outliers = 0
    claimed = None
    for t in range(1000):
        # --- sensors ---
        gyro = (-getattr(controller, "_last_w", 0.0) * YAW_RATE) + GYRO_BIAS + rng.gauss(0, GYRO_NOISE)
        mag = (th + rng.gauss(0, MAG_NOISE)) % 360.0
        if t % 5 == 0:         # GPS at 1 Hz
            gx = x + rng.gauss(0, GPS_SIGMA)
            gy = y + rng.gauss(0, GPS_SIGMA)
            fix_idx = t // 5
            if fix_idx > 0 and fix_idx % MULTIPATH_EVERY == 0:   # inject multipath spike
                ang = rng.uniform(0, 2 * math.pi)
                gx += MULTIPATH_M * math.cos(ang)
                gy += MULTIPATH_M * math.sin(ang)
                n_outliers += 1
            raw_gps = (gx, gy)
        gps_fix = raw_gps if t % 5 == 0 else None
        odom += SPEED * getattr(controller, "_last_v", 0.0) * DT * 1.02 + rng.gauss(0, 0.02)

        v, w, claim, hhat = controller.command(DT, gyro, mag, raw_gps, gps_fix, odom)
        controller._last_v, controller._last_w = v, w

        if hhat is not None:
            head_err_sq += heading_error_deg(hhat, th) ** 2
        xt_sq += _cross_track(x, y) ** 2
        n += 1
        if claim and claimed is None:
            claimed = (t, math.hypot(GOAL[0] - x, GOAL[1] - y))
            break

        # --- true differential-drive kinematics (+w = CCW = left = th decreases) ---
        th = (th - w * YAW_RATE * DT) % 360.0
        ds = SPEED * v * DT
        x += ds * math.sin(math.radians(th))
        y += ds * math.cos(math.radians(th))

    return {
        "final_true_dist": math.hypot(GOAL[0] - x, GOAL[1] - y),
        "crosstrack_rms": math.sqrt(xt_sq / n),
        "heading_rmse": math.sqrt(head_err_sq / n) if head_err_sq else None,
        "claim_tick": claimed[0] if claimed else None,
        "claim_true_dist": claimed[1] if claimed else None,
        "ticks": n,
        "n_outliers": n_outliers,
    }


class Baseline:
    """A: bang-bang on RAW GPS + RAW magnetometer heading (the old kit approach)."""
    def command(self, dt, gyro, mag, raw_gps, gps_fix, odom):
        rx, ry = raw_gps
        dx, dy = GOAL[0] - rx, GOAL[1] - ry
        dist = math.hypot(dx, dy)
        bearing = math.degrees(math.atan2(dx, dy)) % 360.0
        e = heading_error_deg(mag, bearing)              # raw, noisy
        arrived = dist <= TOL
        if arrived:
            return 0.0, 0.0, True, mag
        if abs(e) > 18.0:
            return 0.0, (-0.5 if e > 0 else 0.5), False, mag   # turn (no forward)
        return 0.5, 0.0, False, mag                            # go straight


class Fused:
    """B: the composed production NavController (heading+pose fusion → pursuit → safety)."""
    def __init__(self):
        self.nc = NavController(BASE_LAT, BASE_LON, v_max=0.6, k_ang=1.6,
                                slow_radius_m=8.0, tol_m=TOL)
        self._odom = None
        self._goal_ll = (BASE_LAT + GOAL[1] / M, BASE_LON + GOAL[0] / MLON)

    def command(self, dt, gyro, mag, raw_gps, gps_fix, odom):
        if self._odom is None:
            self._odom = odom
        ds = odom - self._odom
        self._odom = odom
        lat = lon = None
        if gps_fix is not None:
            lat = BASE_LAT + gps_fix[1] / M
            lon = BASE_LON + gps_fix[0] / MLON
        s = self.nc.step(dt, heading_deg=mag, goal_lat=self._goal_ll[0],
                         goal_lon=self._goal_ll[1], lat=lat, lon=lon,
                         yaw_rate_dps=gyro, ds_m=ds)
        return s.linear, s.angular, s.arrived, self.nc.hf.heading


def main():
    seed = 7
    a_ctrl, b_ctrl = Baseline(), Fused()
    a = simulate(a_ctrl, seed)
    b = simulate(b_ctrl, seed)
    rejected = b_ctrl.nc.pf.n_rejected

    def row(name, r):
        ct = f"{r['crosstrack_rms']:.1f}"
        hr = f"{r['heading_rmse']:.1f}" if r['heading_rmse'] else "  -"
        cl = (f"tick {r['claim_tick']} @ true {r['claim_true_dist']:.1f} m"
              if r['claim_tick'] is not None else "never")
        print(f"  {name:9}  final_true={r['final_true_dist']:5.1f} m  "
              f"crosstrack_rms={ct:>5} m  heading_rmse={hr:>4}°  arrival: {cl}")

    print(f"raw magnetometer heading noise sigma = {MAG_NOISE:.0f}°, GPS sigma = {GPS_SIGMA:.0f} m, "
          f"gyro bias = {GYRO_BIAS:.0f}°/s, {a['n_outliers']} GPS multipath spikes (+{MULTIPATH_M:.0f} m)")
    row("baseline", a)
    row("fused", b)

    # 1) The fused heading beats the raw magnetometer it's built from.
    assert b["heading_rmse"] < MAG_NOISE, (b["heading_rmse"], MAG_NOISE)
    # 2) The fused stack TRULY reaches the checkpoint (claim is real within tolerance).
    assert b["claim_true_dist"] is not None and b["claim_true_dist"] <= TOL, b
    # 3) The baseline acts on raw GPS → it FALSELY declares arrival outside tolerance.
    assert a["claim_true_dist"] is not None and a["claim_true_dist"] > TOL, a
    # 4) The fused stack ends meaningfully closer to the goal.
    assert b["final_true_dist"] < a["final_true_dist"], (b["final_true_dist"], a["final_true_dist"])
    # 5) The Kalman pose filter Mahalanobis-gates the GPS multipath spikes.
    assert rejected >= max(1, int(0.8 * a["n_outliers"])), (rejected, a["n_outliers"])

    print(f"\n  -> baseline FALSE arrival: declared done {a['claim_true_dist']:.1f} m from the "
          f"checkpoint (missed at {TOL:.0f} m tolerance)")
    print(f"  -> fused TRUE arrival within tolerance ({b['claim_true_dist']:.1f} m); "
          f"heading {MAG_NOISE / b['heading_rmse']:.1f}x better than the raw magnetometer")
    print(f"  -> Kalman pose filter rejected {rejected}/{a['n_outliers']} GPS multipath outliers "
          f"(Mahalanobis gate) — estimate never dragged off-line")
    print("\nLIVE NAVSTACK PASSED (fused KF estimator + pursuit reaches the checkpoint; "
          "bang-bang on raw GPS does not)")


if __name__ == "__main__":
    main()
