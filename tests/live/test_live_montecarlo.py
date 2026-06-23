"""LIVE — domain-randomized Monte-Carlo validation of the REAL navigation stack.

Instead of one hand-picked noise scenario, draw a fresh random SensorModel for each
of N trials (GPS sigma, latency, multipath, gyro bias/noise, mag bias/noise, odometry
scale/noise) and drive the actual NavController to a checkpoint over the unified
RoverSim. Report the success rate and statistics. This is the strongest sim-only
evidence: the controller works not at one operating point but across a wide error
envelope, so the real robot's (unknown) parameters should land inside a range already
validated.

    .venv/bin/python tests/live/test_live_montecarlo.py
"""

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mini_plus_agent_kit.control import NavController
from mini_plus_agent_kit.sim import RoverSim, run_scenario, random_model, SPEED_MPS

BASE_LAT, BASE_LON = 37.87, -122.25
M = 111_320.0
MLON = M * math.cos(math.radians(BASE_LAT))
GOAL_ENU = (25.0, 60.0)                 # ~65 m at bearing ~23 deg
GOAL = (BASE_LAT + GOAL_ENU[1] / M, BASE_LON + GOAL_ENU[0] / MLON)
TOL = 15.0              # Earth Rover Challenge scoring tolerance
ARRIVE_MARGIN = 9.0    # controller targets well inside it (noisy state → leave margin)
N = 150


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def main():
    rng = random.Random(2026)
    successes = 0
    dists, heads, rejects = [], [], 0
    false_arrivals = 0
    for i in range(N):
        model = random_model(rng)
        sim = RoverSim(model=model, base_lat=BASE_LAT, base_lon=BASE_LON, seed=1000 + i)
        nav = NavController(BASE_LAT, BASE_LON, tol_m=ARRIVE_MARGIN, v_scale_mps=SPEED_MPS)
        r = run_scenario(nav, sim, GOAL[0], GOAL[1], max_steps=900, tol_m=TOL)
        successes += 1 if r["success"] else 0
        if r["arrived"] and not r["success"]:
            false_arrivals += 1
        dists.append(r["true_dist"])
        heads.append(r["heading_rmse"])
        rejects += r["gps_rejected"]

    rate = successes / N
    print(f"domain-randomized Monte-Carlo: {N} trials, randomized GPS/IMU/odometry error")
    print(f"  true-arrival success rate : {rate * 100:.1f}%  ({successes}/{N})")
    print(f"  mean final true distance  : {mean(dists):.1f} m  (tolerance {TOL:.0f} m)")
    print(f"  mean heading RMSE         : {mean(heads):.1f} deg")
    print(f"  GPS multipath outliers gated across all trials : {rejects}")
    print(f"  fix-faked (false) arrivals: {false_arrivals}")

    assert rate >= 0.90, f"success rate {rate:.2%} below 90%"
    assert mean(dists) < TOL, mean(dists)
    print(f"\n  -> the shipping NavController reached the checkpoint in {rate*100:.0f}% of "
          f"randomized worlds — robust across the error envelope, not one tuned point")
    print("\nLIVE MONTE-CARLO PASSED (real nav stack robust under domain randomization)")


if __name__ == "__main__":
    main()
