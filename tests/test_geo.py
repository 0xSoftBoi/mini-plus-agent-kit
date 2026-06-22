"""Geo math for GPS-waypoint navigation (Earth Rover Challenge Urban track)."""

import _bootstrap  # noqa: F401

from mini_plus_agent_kit.geo import haversine_m, initial_bearing_deg, heading_error_deg


def test_haversine_known():
    assert abs(haversine_m(0, 0, 0, 1) - 111195) < 200      # 1° lon @ equator ≈ 111.2 km
    assert haversine_m(10, 20, 10, 20) == 0


def test_bearing_cardinals():
    assert abs(initial_bearing_deg(0, 0, 1, 0)) < 1e-6        # due north
    assert abs(initial_bearing_deg(0, 0, 0, 1) - 90) < 1e-6   # due east
    assert abs(initial_bearing_deg(0, 0, -1, 0) - 180) < 1e-6  # due south


def test_heading_error_sign_and_wrap():
    assert heading_error_deg(0, 90) == 90       # turn right
    assert heading_error_deg(0, 270) == -90     # turn left
    assert heading_error_deg(350, 10) == 20     # wrap → right
    assert heading_error_deg(10, 350) == -20    # wrap → left


def test_berkeley_to_stanford_real_route():
    d = haversine_m(37.8719, -122.2585, 37.4275, -122.1697)
    b = initial_bearing_deg(37.8719, -122.2585, 37.4275, -122.1697)
    assert 48_000 < d < 51_000          # ~50 km straight-line (the Marathon route)
    assert 165 < b < 180                # south, slightly east (Stanford lon is east)


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
