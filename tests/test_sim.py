"""Unified simulator + closed-loop scenarios over the real NavController (stdlib)."""

import _bootstrap  # noqa: F401

import math
import random

from mini_plus_agent_kit.control import NavController
from mini_plus_agent_kit.sim import RoverSim, SensorModel, run_scenario, random_model, SPEED_MPS

BASE_LAT, BASE_LON = 37.87, -122.25
M = 111_320.0
MLON = M * math.cos(math.radians(BASE_LAT))
GOAL = (BASE_LAT + 60.0 / M, BASE_LON + 25.0 / MLON)


def _nav():
    return NavController(BASE_LAT, BASE_LON, tol_m=9.0, v_scale_mps=SPEED_MPS)


def test_scenario_reaches_goal_under_modest_noise():
    sim = RoverSim(model=SensorModel(gps_sigma_m=3.0, gyro_bias_dps=2.0, mag_noise_deg=6.0),
                   base_lat=BASE_LAT, base_lon=BASE_LON, seed=3)
    r = run_scenario(_nav(), sim, GOAL[0], GOAL[1], max_steps=900, tol_m=15.0)
    assert r["success"] and r["true_dist"] <= 15.0


def test_scenario_gates_multipath_and_still_arrives():
    sim = RoverSim(model=SensorModel(gps_sigma_m=3.0, multipath_prob=0.2, multipath_m=25.0),
                   base_lat=BASE_LAT, base_lon=BASE_LON, seed=5)
    r = run_scenario(_nav(), sim, GOAL[0], GOAL[1], max_steps=900, tol_m=15.0)
    assert r["success"] and r["gps_rejected"] > 0   # multipath rejected, goal still reached


def test_small_monte_carlo_high_success_rate():
    rng = random.Random(42)
    ok = 0
    trials = 30
    for i in range(trials):
        sim = RoverSim(model=random_model(rng), base_lat=BASE_LAT, base_lon=BASE_LON, seed=200 + i)
        ok += 1 if run_scenario(_nav(), sim, GOAL[0], GOAL[1], max_steps=900, tol_m=15.0)["success"] else 0
    assert ok >= int(0.9 * trials), ok


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
