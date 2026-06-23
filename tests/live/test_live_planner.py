"""LIVE sim — global planning + regulated pure pursuit vs straight-line seeking.

A building sits between the rover and the checkpoint. Two controllers drive the same
kinematics to the goal:

  A (naive): NavController seeking the GPS waypoint in a straight line (no planner).
  B (planned): A* over an inflated costmap -> path around the building, tracked by
               NavController in regulated-pure-pursuit mode.

We count how many ticks each spends *inside the obstacle* and whether each reaches
the goal. The naive seeker's path drives through the building; the planned path
routes around it with zero incursions.

    .venv/bin/python tests/live/test_live_planner.py
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mini_plus_agent_kit.control import NavController
from mini_plus_agent_kit.planner import Costmap, plan_path

DT = 0.2
YAW_RATE = 60.0
V_SCALE = 2.0
BASE_LAT, BASE_LON = 37.87, -122.25
M = 111_320.0
MLON = M * math.cos(math.radians(BASE_LAT))
GOAL = (0.0, 60.0)        # ENU metres — due north
TOL = 2.0


def _enu_to_ll(x, y):
    return BASE_LAT + y / M, BASE_LON + x / MLON


def build_costmap():
    cm = Costmap(width_m=60.0, height_m=80.0, resolution=1.0, origin_xy=(-30.0, -10.0))
    cm.add_rect(-12.0, 25.0, 12.0, 35.0)      # a building straddling the straight line x=0
    cm.inflate(4.0)                            # clearance >= robot margin + pursuit corner-cut
    return cm


def simulate(nav, costmap, max_steps=800):
    glat, glon = _enu_to_ll(*GOAL)
    x = y = 0.0
    th = 0.0
    collisions = 0
    arrived = False
    for _ in range(max_steps):
        lat, lon = _enu_to_ll(x, y)
        s = nav.step(DT, heading_deg=th, goal_lat=glat, goal_lon=glon, lat=lat, lon=lon)
        if costmap.collides(x, y):
            collisions += 1
        if s.arrived:
            arrived = True
            break
        th = (th - s.angular * YAW_RATE * DT) % 360.0
        ds = V_SCALE * s.linear * DT
        x += ds * math.sin(math.radians(th))
        y += ds * math.cos(math.radians(th))
    return {"arrived": arrived, "collisions": collisions,
            "final": (round(x, 1), round(y, 1)),
            "final_dist": round(math.hypot(GOAL[0] - x, GOAL[1] - y), 1)}


def main():
    cm = build_costmap()
    path = plan_path(cm, (0.0, 0.0), GOAL)
    assert path, "planner failed to find a route"
    length = sum(math.hypot(path[i + 1][0] - path[i][0], path[i + 1][1] - path[i][1])
                 for i in range(len(path) - 1))
    print(f"obstacle: building x[-12,12] y[25,35]; straight-line goal would cross it")
    print(f"A* path: {len(path)} waypoints, {length:.0f} m "
          f"(vs {math.hypot(*GOAL):.0f} m straight) -> {[(round(p[0]), round(p[1])) for p in path]}")

    naive = NavController(BASE_LAT, BASE_LON, v_max=0.6, tol_m=TOL, v_scale_mps=V_SCALE)
    a = simulate(naive, cm)

    planned = NavController(BASE_LAT, BASE_LON, v_max=0.6, tol_m=TOL,
                            v_scale_mps=V_SCALE, use_rpp=True)
    planned.set_path(path)
    b = simulate(planned, cm)

    print(f"  naive (straight-line seek)   arrived={a['arrived']}  "
          f"obstacle_ticks={a['collisions']:3d}  final={a['final']}")
    print(f"  planned (A* + reg. pursuit)  arrived={b['arrived']}  "
          f"obstacle_ticks={b['collisions']:3d}  final={b['final']}")

    # The planned route reaches the goal without ever entering the building.
    assert b["arrived"] and b["collisions"] == 0, b
    # The naive straight-line seeker drives through the obstacle.
    assert a["collisions"] > 0, a

    print(f"\n  -> naive seeker drove through the building for {a['collisions']} ticks; "
          f"planned route had 0 incursions and reached the checkpoint")
    print("\nLIVE PLANNER PASSED (global A* + regulated pure pursuit routes around obstacles)")


if __name__ == "__main__":
    main()
