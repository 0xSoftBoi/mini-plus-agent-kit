"""Light + camera gimbal verbs wired to the harness /light and /camera/move routes."""

import _bootstrap  # noqa: F401

import mini_plus_agent_kit as M
from mini_plus_agent_kit.client import EarthRoverClient
from mini_plus_agent_kit.harness_client import HarnessClient
from mini_plus_agent_kit.rover import EarthRoverVerbs, HarnessVerbs


def _recording_harness():
    calls = []
    c = HarnessClient("http://x", authorize=False)
    c._request = lambda m, p, **k: (calls.append((m, p, k.get("json"))) or {"ok": True})
    return c, calls


def test_harness_set_lamp_posts_light():
    c, calls = _recording_harness()
    HarnessVerbs(c).set_lamp(True)
    assert calls == [("POST", "/light", {"on": True, "token": c.token})]


def test_harness_camera_move_posts_gimbal():
    c, calls = _recording_harness()
    HarnessVerbs(c).camera_move(pan=0.5, tilt=-0.25)
    m, p, body = calls[0]
    assert (m, p) == ("POST", "/camera/move")
    assert body["pan"] == 0.5 and body["tilt"] == -0.25 and body["token"] == c.token


def test_earthrover_set_lamp_uses_control():
    calls = []
    c = EarthRoverClient("http://x")
    c._request = lambda m, p, **k: (calls.append((m, p, k.get("json"))) or {"message": "ok"})
    EarthRoverVerbs(c).set_lamp(True)
    # EarthRover lamp is the /control lamp field.
    assert calls[0][0] == "POST" and calls[0][1] == "/control"
    assert calls[0][2]["command"]["lamp"] == 1


def test_capabilities_and_tools_expose_new_verbs():
    ha = {t["name"] for t in M.make_tools(HarnessVerbs.capabilities, has_work=False)}
    er = {t["name"] for t in M.make_tools(EarthRoverVerbs.capabilities, has_work=False)}
    assert {"set_lamp", "camera_move"} <= ha          # Waveshare: LED + gimbal
    assert "set_lamp" in er and "camera_move" not in er  # Earth Rover: lamp, no gimbal


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
