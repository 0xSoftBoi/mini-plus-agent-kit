"""openClaw verb → SDK endpoint wiring (reconciled against feature/openClaw main.py)."""

import _bootstrap  # noqa: F401
import base64

from mini_plus_agent_kit.client import EarthRoverClient
from mini_plus_agent_kit.rover import EarthRoverVerbs


def _recording_client():
    calls = []
    c = EarthRoverClient("http://x")

    def fake_request(method, path, **kw):
        calls.append((method, path, kw.get("json"), kw.get("params")))
        if path == "/prompt":
            return {"type": "scene_caption", "caption": "a hallway",
                    "front_frame": "AAA", "timestamp": 1}
        if path == "/v2/front":
            return {"front_frame": base64.b64encode(b"JPEGBYTES").decode(), "timestamp": 1}
        if path == "/autonav/status":
            return {"running": False}
        return {"ok": True}

    c._request = fake_request
    return c, calls


def test_look_reads_caption_and_front_frame():
    c, calls = _recording_client()
    scene = EarthRoverVerbs(c).look()
    assert scene.caption == "a hallway" and scene.image_b64 == "AAA"
    assert ("POST", "/prompt", {"text": "what do you see?"}, None) in calls


def test_photo_uses_v2_front_not_photo_endpoint():
    c, calls = _recording_client()
    out = EarthRoverVerbs(c).photo()
    assert out == b"JPEGBYTES"
    assert any(m == "GET" and p == "/v2/front" for m, p, _, _ in calls)
    assert not any(p == "/photo" for _, p, _, _ in calls)


def test_autonav_status_is_get_start_is_post():
    c, calls = _recording_client()
    v = EarthRoverVerbs(c)
    v.autonav("status")
    v.autonav("start")
    assert ("GET", "/autonav/status", None, None) in calls
    assert ("POST", "/autonav/start", None, None) in calls


def test_obstacle_alert_carries_description():
    c = EarthRoverClient("http://x")
    seen = []
    c._request = lambda m, p, **k: (seen.append((m, p, k.get("json"))) or {"narrative": "x"})
    c.obstacle_alert("chair blocking path", "going around left")
    assert seen == [("POST", "/obstacle-alert",
                     {"description": "chair blocking path", "action": "going around left"})]


def test_turn_sends_degrees():
    c = EarthRoverClient("http://x")
    seen = []
    c._request = lambda m, p, **k: (seen.append((p, k.get("json"))) or {"requested": 90})
    c.turn(90)
    assert seen[0][0] == "/turn" and seen[0][1]["degrees"] == 90


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
