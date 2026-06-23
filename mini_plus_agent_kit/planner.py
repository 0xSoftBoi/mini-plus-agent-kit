"""Global path planning: an inflated occupancy costmap + A* (route around obstacles).

A GPS-waypoint seeker drives *straight* at the checkpoint — into any building, curb,
or blocked sidewalk between it and the goal. The standard fix (ROS Nav2) splits
navigation into a **global planner** that searches a path over a costmap and a
**local controller** that tracks it (`control.RegulatedPurePursuit`). This module is
the planner half: a local-ENU occupancy grid with obstacle inflation, an
8-connected A* search that prefers clearance, and line-of-sight path smoothing.
Pure stdlib (`heapq`), fully unit-testable; no I/O.

The costmap is *brought by the caller* — populated from whatever obstacle sense the
platform exposes (camera-derived occupancy, lidar scans, a known site map). The
Earth Rover SDK exposes only a 1-D front lidar range, so on that platform the
costmap is sparse and the reactive `SafetyEnvelope` remains the last line of
defence; this module is the framework for richer obstacle data.
"""

from __future__ import annotations

import heapq
import math

LETHAL = 100

# 8-connected moves: (dcol, drow, step-cost)
_NEIGH = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
          (-1, -1, 1.41421356), (-1, 1, 1.41421356),
          (1, -1, 1.41421356), (1, 1, 1.41421356)]


class Costmap:
    """Inflated occupancy grid in local-ENU metres about ``origin_xy`` (lower-left).

    Cells are ``resolution`` m square; cost 0 = free, ``LETHAL`` (100) = obstacle,
    and ``inflate`` rings obstacles with a linearly-decaying cost so planned paths
    keep clearance from walls. ``(col, row)`` indexes columns (x/East) and rows
    (y/North).
    """

    def __init__(self, width_m: float, height_m: float, resolution: float = 1.0,
                 origin_xy: tuple[float, float] = (0.0, 0.0)):
        self.res = float(resolution)
        self.nx = max(1, int(round(width_m / self.res)))
        self.ny = max(1, int(round(height_m / self.res)))
        self.ox, self.oy = origin_xy
        self.cost = [[0] * self.nx for _ in range(self.ny)]

    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        return (int(math.floor((x - self.ox) / self.res)),
                int(math.floor((y - self.oy) / self.res)))

    def cell_to_world(self, col: int, row: int) -> tuple[float, float]:
        return (self.ox + (col + 0.5) * self.res, self.oy + (row + 0.5) * self.res)

    def in_bounds(self, col: int, row: int) -> bool:
        return 0 <= col < self.nx and 0 <= row < self.ny

    def lethal(self, col: int, row: int) -> bool:
        return (not self.in_bounds(col, row)) or self.cost[row][col] >= LETHAL

    def add_rect(self, x0: float, y0: float, x1: float, y1: float) -> None:
        """Mark a world-rectangle as a lethal obstacle."""
        c0, r0 = self.world_to_cell(min(x0, x1), min(y0, y1))
        c1, r1 = self.world_to_cell(max(x0, x1), max(y0, y1))
        for r in range(max(0, r0), min(self.ny, r1 + 1)):
            for c in range(max(0, c0), min(self.nx, c1 + 1)):
                self.cost[r][c] = LETHAL

    def inflate(self, radius_m: float) -> None:
        """Ring every lethal cell with a linearly-decaying cost out to ``radius_m``."""
        rad = int(math.ceil(radius_m / self.res))
        lethal_cells = [(r, c) for r in range(self.ny) for c in range(self.nx)
                        if self.cost[r][c] >= LETHAL]
        for (r, c) in lethal_cells:
            for dr in range(-rad, rad + 1):
                for dc in range(-rad, rad + 1):
                    rr, cc = r + dr, c + dc
                    if not self.in_bounds(cc, rr) or self.cost[rr][cc] >= LETHAL:
                        continue
                    dist = math.hypot(dr, dc) * self.res
                    if dist <= radius_m:
                        val = int((1.0 - dist / radius_m) * (LETHAL - 1))
                        if val > self.cost[rr][cc]:
                            self.cost[rr][cc] = val

    def cost_at(self, x: float, y: float) -> int:
        col, row = self.world_to_cell(x, y)
        return LETHAL if not self.in_bounds(col, row) else self.cost[row][col]

    def collides(self, x: float, y: float) -> bool:
        return self.lethal(*self.world_to_cell(x, y))


def plan_path(costmap: Costmap, start_xy: tuple[float, float],
              goal_xy: tuple[float, float], smooth: bool = True) -> list[tuple[float, float]]:
    """8-connected A* over ``costmap``; returns a world-point path or ``[]`` if unreachable.

    The step cost is scaled by ``1 + cell_cost/50`` so the search trades a little
    length for clearance (it hugs the inflation gradient away from walls). Diagonal
    moves that would cut a lethal corner are forbidden. With ``smooth`` the staircase
    path is string-pulled to the minimal set of collision-free waypoints.
    """
    start = costmap.world_to_cell(*start_xy)
    goal = costmap.world_to_cell(*goal_xy)
    if costmap.lethal(*start) or costmap.lethal(*goal):
        return []

    def h(cell: tuple[int, int]) -> float:
        return math.hypot(cell[0] - goal[0], cell[1] - goal[1])

    open_heap = [(h(start), 0.0, start)]
    came: dict[tuple[int, int], tuple[int, int]] = {}
    gscore = {start: 0.0}
    closed: set[tuple[int, int]] = set()

    while open_heap:
        _, g, cur = heapq.heappop(open_heap)
        if cur == goal:
            cells = [cur]
            while cur in came:
                cur = came[cur]
                cells.append(cur)
            cells.reverse()
            pts = [costmap.cell_to_world(c, r) for (c, r) in cells]
            return _smooth(costmap, pts) if smooth else pts
        if cur in closed:
            continue
        closed.add(cur)
        cc, cr = cur
        for dc, dr, step in _NEIGH:
            nb = (cc + dc, cr + dr)
            if costmap.lethal(*nb) or nb in closed:
                continue
            if dc and dr and (costmap.lethal(cc + dc, cr) or costmap.lethal(cc, cr + dr)):
                continue                                   # don't cut diagonal corners
            penalty = 1.0 + costmap.cost[nb[1]][nb[0]] / 50.0
            ng = g + step * penalty
            if ng < gscore.get(nb, math.inf):
                gscore[nb] = ng
                came[nb] = cur
                heapq.heappush(open_heap, (ng + h(nb), ng, nb))
    return []


def _line_clear(costmap: Costmap, a: tuple[float, float], b: tuple[float, float],
                cost_thresh: int = LETHAL) -> bool:
    """True if every ½-cell sample on ``a→b`` has cost ``< cost_thresh``.

    With ``cost_thresh < LETHAL`` the chord must also stay out of the *inflation*
    layer — used by smoothing so string-pulling preserves wall clearance instead of
    hugging the lethal edge (which a corner-cutting tracker would then clip).
    """
    dist = math.hypot(b[0] - a[0], b[1] - a[1])
    n = max(1, int(dist / (costmap.res * 0.5)))
    for i in range(n + 1):
        t = i / n
        if costmap.cost_at(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t) >= cost_thresh:
            return False
    return True


def _smooth(costmap: Costmap, pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """String-pulling: keep only waypoints needed to stay clear of the inflation layer."""
    if len(pts) <= 2:
        return pts
    thresh = max(1, LETHAL // 2)        # keep ~half the inflation radius of clearance
    out = [pts[0]]
    i = 0
    while i < len(pts) - 1:
        j = len(pts) - 1
        while j > i + 1 and not _line_clear(costmap, pts[i], pts[j], thresh):
            j -= 1
        out.append(pts[j])
        i = j
    return out
