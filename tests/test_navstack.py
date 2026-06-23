"""Navigation stack — filters, controllers, safety (pure math, stubbed deps)."""

import _bootstrap  # noqa: F401

import math

from mini_plus_agent_kit.estimator import HeadingFilter, PoseFilter, mag_heading_deg
from mini_plus_agent_kit.control import (
    HeadingPID, PursuitController, DistanceController, SafetyEnvelope, SafetyLimits,
    NavController, DWAPlanner,
)
from mini_plus_agent_kit.geo import heading_error_deg


def test_heading_filter_smooths_noise():
    f = HeadingFilter(kp=0.1)
    h = None
    for i in range(80):                       # gyro reads ~0 (no rotation); mag noisy ±10 about 100
        mag = 100.0 + (10.0 if i % 2 else -10.0)
        h = f.update(0.1, gyro_z_dps=0.0, absolute_deg=mag)
    assert abs(heading_error_deg(h, 100.0)) < 4.0, h   # tracks mean, rejects noise


def test_heading_filter_integrates_gyro_without_absolute():
    f = HeadingFilter(kp=0.1)
    f.update(0.1, gyro_z_dps=0.0, absolute_deg=100.0)   # seed at 100
    for _ in range(10):                                  # +10 dps for 1 s, no absolute
        h = f.update(0.1, gyro_z_dps=10.0, absolute_deg=None)
    assert abs(heading_error_deg(h, 110.0)) < 1.0, h     # integrated ~+10°


def test_heading_pid_sign_and_settle():
    pid = HeadingPID()
    # target to the RIGHT (compass +90) → angular must be negative (CCW=left is +)
    assert pid.step(0.1, current_deg=0.0, target_deg=90.0) < 0
    pid.reset()
    assert pid.step(0.1, current_deg=0.0, target_deg=-90.0) > 0   # target left → +angular
    assert pid.settled(0.0, 3.0) and not pid.settled(0.0, 30.0)


def test_pursuit_steers_and_arrives():
    pp = PursuitController(tol_m=15.0)
    ahead = pp.step(0, 0, 0.0, 0.0, 100.0)               # goal due north, facing north
    assert not ahead.arrived and ahead.linear > 0 and abs(ahead.angular) < 0.05
    right = pp.step(0, 0, 0.0, 100.0, 0.0)               # goal due east (to the right)
    assert right.angular < 0                              # right turn = negative angular
    near = pp.step(0, 0, 0.0, 0.0, 10.0)                 # within tolerance
    assert near.arrived and near.linear == 0


def test_distance_controller_uses_odometry():
    d = DistanceController(v=0.4, tol_m=0.2)
    d.begin(100.0)                                        # odometer starts at 100 m
    v, done = d.step(101.0, target_m=2.0); assert v > 0 and not done   # 1 m travelled
    v, done = d.step(102.1, target_m=2.0); assert done and v == 0      # 2.1 m ≥ target


def test_safety_envelope():
    s = SafetyEnvelope(SafetyLimits(battery_floor=10, tilt_limit_deg=25, ttc_min_s=1.5, ttc_slow_s=3.0))
    assert not s.check(0.5, battery=8).ok                          # low battery
    assert not s.check(0.5, pitch=30).ok                           # tilted (ramp/pickup)
    assert not s.check(0.5, lidar_front_m=0.5).ok                  # TTC 1.0s ≤ 1.5 → stop
    slow = s.check(0.5, lidar_front_m=1.25); assert slow.ok and slow.scale < 1.0   # TTC 2.5s → slow
    assert s.check(0.5, battery=90, pitch=2, lidar_front_m=10).scale == 1.0         # clear


def test_posefilter_kalman_gain_and_outlier_gate():
    base_lat, base_lon = 37.87, -122.25
    pf = PoseFilter(base_lat, base_lon, sigma_gps_m=4.0, p0=25.0)
    p_start = pf.P
    # a good fix at the origin is accepted and shrinks the covariance (optimal gain)
    assert pf.correct_gps(base_lat, base_lon) is True
    assert pf.P < p_start and not pf.last_rejected
    # consistent fixes while driving north keep the estimate locked on truth
    for _ in range(10):
        pf.predict(1.0, 0.0)                       # +1 m north (grows P a touch)
        lat, lon = pf.latlon()
        assert pf.correct_gps(lat, lon) is True    # consistent → accepted
    y_before = pf.xy()[1]
    # a 25 m multipath spike is Mahalanobis-gated → rejected, estimate not dragged
    assert pf.correct_gps(base_lat + 25.0 / 111_320.0, base_lon) is False
    assert pf.last_rejected and pf.n_rejected == 1
    assert abs(pf.xy()[1] - y_before) < 1.0        # barely moved despite the 25 m spike


def test_navcontroller_converges_and_gates():
    base_lat, base_lon = 37.87, -122.25
    M = 111_320.0
    mlon = M * math.cos(math.radians(base_lat))
    glat, glon = base_lat + 60.0 / M, base_lon + 25.0 / mlon   # goal 25E,60N (~65 m)
    nav = NavController(base_lat, base_lon, tol_m=15.0, v_scale_mps=1.5)
    # clean closed loop: drive perfect kinematics off the controller's own commands
    x = y = th = 0.0
    arrived = False
    for _ in range(500):
        lat, lon = base_lat + y / M, base_lon + x / mlon
        s = nav.step(0.2, heading_deg=th, goal_lat=glat, goal_lon=glon, lat=lat, lon=lon)
        if s.arrived:
            arrived = True
            break
        th = (th - s.angular * 60.0 * 0.2) % 360.0            # +angular = CCW = left
        ds = 1.5 * s.linear * 0.2
        x += ds * math.sin(math.radians(th))
        y += ds * math.cos(math.radians(th))
    assert arrived and math.hypot(25.0 - x, 60.0 - y) <= 15.0
    # safety gate: low battery zeroes the twist
    g = nav.step(0.2, heading_deg=0.0, goal_lat=glat, goal_lon=glon, battery=5.0)
    assert g.linear == 0.0 and g.angular == 0.0 and not g.safe


def test_dwa_seeks_goal_when_clear_and_avoids_when_blocked():
    dwa = DWAPlanner(v_scale_mps=1.2, robot_radius=1.0, tol_m=2.0)
    # clear path to a goal due north → drive ahead, ~no turn
    clear = dwa.step(0.0, 0.0, 0.0, 0.0, 20.0, v_cmd0=0.5, obstacles=None)
    assert clear.linear > 0 and abs(clear.angular) < 0.2 and not clear.arrived
    # obstacle squarely ahead on the straight line → must steer off-axis to avoid it
    blocked = dwa.step(0.0, 0.0, 0.0, 0.0, 20.0, v_cmd0=0.5, obstacles=[(0.0, 3.0)])
    assert abs(blocked.angular) > abs(clear.angular)


def test_dwa_rounds_a_static_obstacle_closed_loop():
    dwa = DWAPlanner(v_scale_mps=2.0, robot_radius=1.2, tol_m=2.0)
    goal = (0.0, 24.0)
    x = y = th = 0.0
    v0 = w0 = 0.0
    obstacle = (0.0, 12.0)                       # blocking the straight line
    min_clear = float("inf")
    arrived = False
    for _ in range(600):
        min_clear = min(min_clear, math.hypot(x - obstacle[0], y - obstacle[1]))
        c = dwa.step(x, y, th, goal[0], goal[1], v_cmd0=v0, w_cmd0=w0, obstacles=[obstacle])
        if c.arrived:
            arrived = True
            break
        v0, w0 = c.linear, c.angular
        th = (th - c.angular * 60.0 * 0.2) % 360.0
        ds = 2.0 * c.linear * 0.2
        x += ds * math.sin(math.radians(th))
        y += ds * math.cos(math.radians(th))
    assert arrived and min_clear >= 1.2          # reached goal without breaching clearance


def test_mag_heading():
    assert abs(heading_error_deg(mag_heading_deg(0.0, 1.0), 0.0)) < 1e-6    # +y → North
    assert abs(heading_error_deg(mag_heading_deg(1.0, 0.0), 90.0)) < 1e-6   # +x → East


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
