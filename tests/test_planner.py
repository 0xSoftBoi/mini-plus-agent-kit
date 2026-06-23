"""Global planner (costmap + A*) and regulated pure pursuit (pure math, stubbed deps)."""

import _bootstrap  # noqa: F401

import math

from mini_plus_agent_kit.planner import Costmap, plan_path, LETHAL
from mini_plus_agent_kit.control import RegulatedPurePursuit


def test_costmap_indexing_and_obstacles():
    cm = Costmap(20.0, 20.0, resolution=1.0, origin_xy=(-10.0, -10.0))
    col, row = cm.world_to_cell(0.0, 0.0)
    assert cm.in_bounds(col, row) and (col, row) == (10, 10)
    cm.add_rect(-2.0, -2.0, 2.0, 2.0)
    assert cm.collides(0.0, 0.0) and not cm.collides(8.0, 8.0)
    assert cm.cost_at(100.0, 0.0) == LETHAL          # out of bounds reads lethal


def test_inflation_rings_obstacle_with_decaying_cost():
    cm = Costmap(20.0, 20.0, resolution=1.0, origin_xy=(-10.0, -10.0))
    cm.add_rect(-1.0, -1.0, 1.0, 1.0)
    cm.inflate(3.0)
    near = cm.cost_at(2.5, 0.0)                       # just outside the lethal rect
    far = cm.cost_at(6.0, 0.0)                        # beyond the inflation radius
    assert 0 < near < LETHAL and far == 0 and near > cm.cost_at(3.5, 0.0)


def test_astar_routes_around_obstacle():
    cm = Costmap(40.0, 60.0, resolution=1.0, origin_xy=(-20.0, -5.0))
    cm.add_rect(-10.0, 20.0, 10.0, 30.0)
    cm.inflate(3.0)
    path = plan_path(cm, (0.0, 0.0), (0.0, 50.0))
    assert path and path[0] == cm.cell_to_world(*cm.world_to_cell(0.0, 0.0))
    # every waypoint and the chords between them avoid the lethal obstacle
    for i in range(len(path) - 1):
        for t in range(11):
            x = path[i][0] + (path[i + 1][0] - path[i][0]) * t / 10
            y = path[i][1] + (path[i + 1][1] - path[i][1]) * t / 10
            assert not cm.collides(x, y), (x, y)
    assert max(abs(p[0]) for p in path) > 8.0         # actually detoured sideways


def test_astar_returns_empty_when_walled_off():
    cm = Costmap(20.0, 40.0, resolution=1.0, origin_xy=(-10.0, 0.0))
    cm.add_rect(-10.0, 18.0, 10.0, 22.0)              # full-width wall between start and goal
    assert plan_path(cm, (0.0, 2.0), (0.0, 38.0)) == []


def test_rpp_steers_and_follows_path_to_arrival():
    pp = RegulatedPurePursuit(v_max=0.6, tol_m=2.0)
    # straight path north, facing north → drive ahead, ~no turn
    fwd = pp.step(0.0, 0.0, 0.0, [(0.0, 0.0), (0.0, 20.0)])
    assert fwd.linear > 0 and abs(fwd.angular) < 0.1 and not fwd.arrived
    # path turns east → steer right (negative angular)
    right = pp.step(0.0, 0.0, 0.0, [(0.0, 0.0), (20.0, 0.0)])
    assert right.angular < 0
    # closed loop along an L-shaped path reaches the final waypoint
    path = [(0.0, 0.0), (0.0, 30.0), (20.0, 30.0)]
    x = y = th = 0.0
    arrived = False
    for _ in range(600):
        s = pp.step(x, y, th, path)
        if s.arrived:
            arrived = True
            break
        th = (th - s.angular * 60.0 * 0.2) % 360.0
        ds = 1.5 * s.linear * 0.2
        x += ds * math.sin(math.radians(th))
        y += ds * math.cos(math.radians(th))
    assert arrived and math.hypot(20.0 - x, 30.0 - y) <= 2.0


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
