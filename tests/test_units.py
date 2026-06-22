"""Pure-logic units: kinematics, telemetry mapping, content addressing, tools."""

import _bootstrap  # noqa: F401  (path + dep stubs)
import hashlib

import mini_plus_agent_kit as M
from mini_plus_agent_kit.client import Telemetry, EarthRoverClient
from mini_plus_agent_kit.harness_client import HarnessClient, twist_to_diff
from mini_plus_agent_kit.rover import EarthRoverVerbs, HarnessVerbs, make_verbs


def test_twist_to_diff_roundtrips_with_diffToTwist():
    assert twist_to_diff(1, 0) == (1, 1)
    assert twist_to_diff(0, 1) == (-1, 1)
    assert twist_to_diff(0.5, 0.5) == (0.0, 1.0)
    for lin, ang in [(0.6, 0.0), (0.2, -0.4), (-0.5, 0.0)]:
        l, r = twist_to_diff(lin, ang)
        assert abs((l + r) / 2 - lin) < 1e-9 and abs((r - l) / 2 - ang) < 1e-9


def test_telemetry_from_harness_surfaces_lidar_and_estop():
    t = Telemetry.from_harness({"ts_ms": 1724189733208, "battery_v": 12.4, "yaw": 128.0,
                                "left_cmd": 0.3, "right_cmd": 0.5, "estop": False,
                                "lidar": {"front_m": 0.42, "blocked": True}})
    assert t.battery == 12.4 and t.orientation == 128.0 and abs(t.speed - 0.4) < 1e-9
    assert t.lidar_front_m == 0.42 and t.lidar_blocked is True
    s = t.summary()
    assert "lidar_front=0.42m" in s and "path_blocked=YES" in s


def test_telemetry_from_dict_frodobots():
    t = Telemetry.from_dict({"battery": 100, "orientation": 128, "latitude": 22.7,
                             "longitude": 114.0, "lamp": 0})
    s = t.summary()
    assert "battery=100" in s and "gps=" in s


def test_ipfs_cid_v1_raw_is_correct():
    c = M.cid_v1_raw(b"hello")
    assert c.startswith("bafkrei")           # raw + sha256 multibase prefix
    assert M.ipfs_cid(b"hello") == c          # ≤1MiB == single raw block
    import base64
    raw = base64.b32decode((c[1:] + "=" * ((8 - (len(c) - 1) % 8) % 8)).upper())
    assert raw[:4] == bytes([0x01, 0x55, 0x12, 0x20])
    assert raw[4:] == hashlib.sha256(b"hello").digest()


def test_make_tools_filters_by_capability():
    er = {t["name"] for t in M.make_tools(EarthRoverVerbs.capabilities, has_work=True)}
    ha = {t["name"] for t in M.make_tools(HarnessVerbs.capabilities, has_work=False)}
    assert {"status_report", "look", "move", "turn", "track_color", "autonav",
            "speak", "capture_work", "finish"} <= er
    assert "obstacle_check" not in er          # Earth Rover has no lidar
    assert {"status_report", "look", "photo", "move", "turn", "obstacle_check",
            "autonav", "track_color", "finish"} <= ha   # client-side visual servo
    assert not ({"speak", "capture_work"} & ha)          # no TTS; no work sink here
    for t in M.make_tools(HarnessVerbs.capabilities, has_work=True):
        assert "_cap" not in t                 # internal tag must not leak to the API


def test_make_verbs_picks_backend():
    assert isinstance(make_verbs(EarthRoverClient("http://x")), EarthRoverVerbs)
    assert isinstance(make_verbs(HarnessClient("http://x", authorize=False)), HarnessVerbs)


def test_system_prompt_assembled_from_instruction_files():
    sp = M.load_system_prompt("EXTRA-XYZ")
    assert "Operating rules" in sp and "status_report" in sp and "EXTRA-XYZ" in sp


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
