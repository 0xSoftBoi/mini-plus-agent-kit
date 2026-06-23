"""Safety supervisor (mission budgets) + deadman watchdog (stdlib)."""

import _bootstrap  # noqa: F401

import time

from mini_plus_agent_kit.safety import MissionLimits, SafetySupervisor, Watchdog


class _Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def tick(self, dt): self.t += dt


def test_supervisor_runtime_distance_battery_geofence():
    clk = _Clock()
    sup = SafetySupervisor(MissionLimits(max_runtime_s=10, max_distance_m=100,
                                         battery_floor=15), clock=clk)
    assert sup.check(battery=80, distance_m=10).ok
    # distance budget
    bad = sup.check(battery=80, distance_m=120)
    assert not bad.ok and "distance" in bad.reason
    # latches: subsequent checks stay tripped even if inputs are fine
    assert not sup.check(battery=80, distance_m=0).ok
    sup.reset()
    # battery floor
    assert not sup.check(battery=10).ok
    sup.reset()
    # runtime budget
    clk.tick(11)
    assert not sup.check(battery=90).ok


def test_supervisor_geofence_breach():
    sup = SafetySupervisor(MissionLimits(geofence_center=(37.0, -122.0), geofence_radius_m=50))
    assert sup.check(lat=37.0, lon=-122.0).ok                    # at the center
    far = sup.check(lat=37.01, lon=-122.0)                       # ~1.1 km north
    assert not far.ok and "geofence" in far.reason


def test_watchdog_fires_when_not_pet():
    fired = []
    wd = Watchdog(timeout_s=0.15, on_timeout=lambda: fired.append(True), poll_s=0.02)
    wd.start()
    try:
        time.sleep(0.35)                                        # never pet → must fire
        assert wd.fired and fired == [True]
    finally:
        wd.stop()


def test_watchdog_does_not_fire_while_pet():
    fired = []
    wd = Watchdog(timeout_s=0.2, on_timeout=lambda: fired.append(True), poll_s=0.02)
    wd.start()
    try:
        for _ in range(6):
            time.sleep(0.05)
            wd.pet()
        assert not wd.fired and fired == []
    finally:
        wd.stop()


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
