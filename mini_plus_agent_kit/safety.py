"""Mission-level safety supervisor + deadman watchdog.

``control.SafetyEnvelope`` gates a *single* control command (battery/tilt/lidar TTC).
This is the **outer** safety layer for a whole mission:

* :class:`SafetySupervisor` — enforces mission budgets the per-step envelope can't
  see: total runtime, total distance travelled, a geofence, and a battery floor.
  Once any budget is breached the supervisor **latches** tripped until reset.
* :class:`Watchdog` — a deadman timer on its own thread. The control/agent loop must
  ``pet()`` it each iteration; if it goes un-pet for ``timeout_s`` (e.g. a blocked
  LLM call or a wedged transport leaves a moving robot unattended) the watchdog
  fires ``on_timeout`` (an emergency stop) exactly once. This is the guarantee a
  synchronous agent loop otherwise lacks.

Pure stdlib (``threading``/``time``) + :func:`geo.haversine_m`; unit-testable with an
injected clock and a fake stop callback.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .geo import haversine_m


@dataclass
class MissionLimits:
    max_runtime_s: float = 600.0          # whole-mission timeout
    max_distance_m: float = 1000.0        # cumulative travel budget
    battery_floor: float = 10.0           # % (or volts — caller's unit)
    geofence_center: tuple[float, float] | None = None   # (lat, lon)
    geofence_radius_m: float = 0.0        # 0 = geofence disabled


@dataclass
class SupervisorVerdict:
    ok: bool
    reason: str


class SafetySupervisor:
    """Latching mission-budget supervisor — call :meth:`check` each control step."""

    def __init__(self, limits: MissionLimits | None = None, *, clock=time.monotonic):
        self.limits = limits or MissionLimits()
        self._clock = clock
        self.started = clock()
        self.tripped = False
        self.trip_reason = ""

    def _trip(self, reason: str) -> SupervisorVerdict:
        self.tripped = True
        self.trip_reason = reason
        return SupervisorVerdict(False, reason)

    def elapsed_s(self) -> float:
        return self._clock() - self.started

    def check(self, *, battery: float | None = None, lat: float | None = None,
              lon: float | None = None, distance_m: float | None = None) -> SupervisorVerdict:
        """Return ok/why for the current state; trips (and latches) on any breach."""
        if self.tripped:
            return SupervisorVerdict(False, self.trip_reason)
        L = self.limits
        if self.elapsed_s() > L.max_runtime_s:
            return self._trip(f"runtime {self.elapsed_s():.0f}s > {L.max_runtime_s:.0f}s")
        if distance_m is not None and distance_m > L.max_distance_m:
            return self._trip(f"distance {distance_m:.0f}m > {L.max_distance_m:.0f}m")
        if battery is not None and battery <= L.battery_floor:
            return self._trip(f"battery {battery} <= floor {L.battery_floor}")
        if (L.geofence_center and L.geofence_radius_m > 0
                and lat is not None and lon is not None):
            d = haversine_m(L.geofence_center[0], L.geofence_center[1], lat, lon)
            if d > L.geofence_radius_m:
                return self._trip(f"geofence breach {d:.0f}m > {L.geofence_radius_m:.0f}m")
        return SupervisorVerdict(True, "ok")

    def reset(self) -> None:
        self.tripped = False
        self.trip_reason = ""
        self.started = self._clock()


class Watchdog:
    """Deadman timer: fire ``on_timeout`` once if not :meth:`pet` within ``timeout_s``."""

    def __init__(self, timeout_s: float, on_timeout, *, poll_s: float | None = None,
                 clock=time.monotonic):
        self.timeout_s = timeout_s
        self.on_timeout = on_timeout
        self._clock = clock
        self._poll = poll_s if poll_s is not None else max(0.02, min(0.5, timeout_s / 4.0))
        self._last = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.fired = False

    def pet(self) -> None:
        self._last = self._clock()

    def start(self) -> "Watchdog":
        self._last = self._clock()
        self._thread = threading.Thread(target=self._loop, name="mpak-watchdog", daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self._poll):
            if self._clock() - self._last > self.timeout_s:
                self.fired = True
                try:
                    self.on_timeout()
                finally:
                    return

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)

    def __enter__(self) -> "Watchdog":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
