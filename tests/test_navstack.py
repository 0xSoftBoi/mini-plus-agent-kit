"""Navigation stack — filters, controllers, safety (pure math, stubbed deps)."""

import _bootstrap  # noqa: F401

import math

from mini_plus_agent_kit.estimator import HeadingFilter, PoseFilter, mag_heading_deg
from mini_plus_agent_kit.control import (
    HeadingPID, PursuitController, DistanceController, SafetyEnvelope, SafetyLimits,
    NavController,
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


def test_mag_heading():
    assert abs(heading_error_deg(mag_heading_deg(0.0, 1.0), 0.0)) < 1e-6    # +y → North
    assert abs(heading_error_deg(mag_heading_deg(1.0, 0.0), 90.0)) < 1e-6   # +x → East


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
