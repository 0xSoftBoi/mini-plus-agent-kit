"""openClaw verb → SDK endpoint wiring (reconciled against feature/openClaw main.py)."""

import _bootstrap  # noqa: F401
import base64

from mini_plus_agent_kit.client import EarthRoverClient
from mini_plus_agent_kit.rover import EarthRoverVerbs
from mini_plus_agent_kit.tools import dispatch


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


def test_navigate_handler_emits_structured_numbers():
    v = EarthRoverVerbs(EarthRoverClient("http://x"))
    v.navigate = lambda: {"reply": "checkpoint #2: 40 m away", "distance_m": 40.0,
                          "bearing_deg": 95.0, "heading_error_deg": -12.0,
                          "within_tolerance": False}
    out = dispatch(v, "navigate", {})
    texts = [b["text"] for b in out.blocks if b.get("type") == "text"]
    assert any("checkpoint #2" in t for t in texts)            # prose retained
    import json
    nums = next(json.loads(t) for t in texts if t.startswith("{"))
    assert nums == {"distance_m": 40.0, "bearing_deg": 95.0,
                    "heading_error_deg": -12.0, "within_tolerance": False}


def test_capture_work_flags_onchain_failure():
    import mini_plus_agent_kit.tools as T

    class _Art:
        sha256, ipfs_cid, walrus_url = "0xabc", "bafkrei1", "https://w/x"

    class _Rec:
        vrw_points, label, artifact = 100, "proof", _Art()
        results = {"start": {"ok": True}, "end": {"ok": True},
                   "validate": {"ok": False, "error": "chain down"}}

    orig = T.submit_work
    T.submit_work = lambda *a, **k: _Rec()
    try:
        v = EarthRoverVerbs(EarthRoverClient("http://x"))
        v.photo = lambda: b"JPG"
        out = dispatch(v, "capture_work", {"label": "proof"}, work=object())
    finally:
        T.submit_work = orig
    assert out.is_error is True
    assert "validate" in out.blocks[0]["text"]


def test_capture_work_success_is_not_an_error():
    import mini_plus_agent_kit.tools as T

    class _Art:
        sha256, ipfs_cid, walrus_url = "0xabc", "bafkrei1", "https://w/x"

    class _Rec:
        vrw_points, label, artifact = 100, "proof", _Art()
        results = {"start": {"ok": True}, "end": {"ok": True}, "validate": {"tx": "0xok"}}

    orig = T.submit_work
    T.submit_work = lambda *a, **k: _Rec()
    try:
        v = EarthRoverVerbs(EarthRoverClient("http://x"))
        v.photo = lambda: b"JPG"
        out = dispatch(v, "capture_work", {"label": "proof"}, work=object())
    finally:
        T.submit_work = orig
    assert out.is_error is False
    assert "WARNING" not in out.blocks[0]["text"]


def test_drive_to_checkpoint_delegates_to_fused_controller():
    v = EarthRoverVerbs(EarthRoverClient("http://x"))
    seen = {}
    v.goto_checkpoint_fused = lambda **kw: (seen.update(kw) or {"ok": True, "reached": 1})
    # max_steps=None keeps goto_checkpoint_fused's own default (not forwarded).
    assert v.drive_to_checkpoint() == {"ok": True, "reached": 1}
    assert seen == {}
    # An explicit cap is forwarded.
    assert v.drive_to_checkpoint(max_steps=12)["ok"] is True
    assert seen == {"max_steps": 12}


def test_dispatch_drive_to_checkpoint_handler():
    v = EarthRoverVerbs(EarthRoverClient("http://x"))
    v.goto_checkpoint_fused = lambda **kw: {"ok": False, "reason": "max_steps"}
    out = dispatch(v, "drive_to_checkpoint", {"max_steps": 3})
    assert out.is_error is True  # ok==False surfaces as a tool error
    assert "max_steps" in out.blocks[0]["text"]


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
