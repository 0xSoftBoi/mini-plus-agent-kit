"""LIVE sim — Dynamic Window Approach vs plain pursuit, with a MOVING obstacle.

A pedestrian walks across the corridor between the rover and the checkpoint. Two
controllers drive the same kinematics to the goal:

  A (pursuit): PursuitController seeking the goal — no obstacle awareness.
  B (DWA): DWAPlanner — samples the reachable velocity window, rolls each candidate
           out, discards colliding trajectories, scores goal/clearance/speed.

We track the closest the rover ever comes to the pedestrian. The pursuit controller
walks straight into it; DWA rounds it while still reaching the goal.

    .venv/bin/python tests/live/test_live_dwa.py
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mini_plus_agent_kit.control import DWAPlanner, PursuitController

DT = 0.2
YAW_RATE = 60.0
V_SCALE = 2.4
GOAL = (0.0, 40.0)
TOL = 2.0
SAFE = 1.2                       # robot+pedestrian radius — closer than this = collision
PED_Y = 18.0
PED_X0 = 7.5                     # pedestrian starts here, walks west to intercept the path
PED_VX = -0.6                    # m/s (reaches x=0 at y=18 just as the rover gets there)


def ped_pos(t):
    return (PED_X0 + PED_VX * DT * t, PED_Y)


def simulate(controller, is_dwa):
    x = y = th = 0.0
    v0 = w0 = 0.0
    min_clear = float("inf")
    arrived = False
    for t in range(600):
        px, py = ped_pos(t)
        min_clear = min(min_clear, math.hypot(x - px, y - py))
        if is_dwa:
            cmd = controller.step(x, y, th, GOAL[0], GOAL[1],
                                  v_cmd0=v0, w_cmd0=w0, obstacles=[(px, py)])
        else:
            cmd = controller.step(x, y, th, GOAL[0], GOAL[1])
        if cmd.arrived:
            arrived = True
            break
        v0, w0 = cmd.linear, cmd.angular
        th = (th - cmd.angular * YAW_RATE * DT) % 360.0
        ds = V_SCALE * cmd.linear * DT
        x += ds * math.sin(math.radians(th))
        y += ds * math.cos(math.radians(th))
    return {"arrived": arrived, "min_clear": round(min_clear, 2),
            "final": (round(x, 1), round(y, 1))}


def main():
    print(f"pedestrian crosses at y={PED_Y:.0f}, intercepting the straight path; "
          f"collision if rover comes within {SAFE:.1f} m")

    a = simulate(PursuitController(v_max=0.6, tol_m=TOL), is_dwa=False)
    b = simulate(DWAPlanner(v_scale_mps=V_SCALE, yaw_rate_dps=YAW_RATE,
                            robot_radius=SAFE, tol_m=TOL), is_dwa=True)

    print(f"  pursuit (no avoidance)  arrived={a['arrived']}  "
          f"closest_approach={a['min_clear']:5} m  final={a['final']}")
    print(f"  DWA (local avoidance)   arrived={b['arrived']}  "
          f"closest_approach={b['min_clear']:5} m  final={b['final']}")

    # DWA keeps clear of the moving pedestrian AND reaches the goal.
    assert b["arrived"] and b["min_clear"] >= SAFE, b
    # plain pursuit walks into the pedestrian.
    assert a["min_clear"] < SAFE, a

    print(f"\n  -> pursuit closed to {a['min_clear']} m (collision); "
          f"DWA held {b['min_clear']} m clearance and still reached the checkpoint")
    print("\nLIVE DWA PASSED (dynamic-window local planner steers around a moving obstacle)")


if __name__ == "__main__":
    main()
