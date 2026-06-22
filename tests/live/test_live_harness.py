"""LIVE test — real httpx client over real sockets against a real HTTP server.

No stubs. A stdlib HTTP server implements the robot-harness contract (the same
routes the Rust harness exposes, with the firmware-correct gimbal mapping), and
the REAL HarnessClient / HarnessVerbs drive it end to end. Run:

    .venv/bin/python tests/live/test_live_harness.py
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import mini_plus_agent_kit.rover as rover_mod
from mini_plus_agent_kit.harness_client import HarnessClient
from mini_plus_agent_kit.rover import HarnessVerbs

# A real (synthetic) JPEG body: SOI ... EOI. Real bytes over the wire.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + b"\x00" * 32 + b"\xff\xd9"

STATE = {"tokens": [], "drives": [], "lights": [], "cameras": [], "stops": 0, "yaw_reads": 0}


class Harness(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _body(self):
        n = int(self.headers.get("content-length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/telemetry":
            STATE["yaw_reads"] += 1
            last = STATE["drives"][-1] if STATE["drives"] else {"left": 0, "right": 0}
            return self._json({
                "ts_ms": 1724189733208,
                "battery_v": 12.4,
                "yaw": (STATE["yaw_reads"] * 50) % 360,   # advances so closed-loop turn ends
                "left_cmd": last["left"], "right_cmd": last["right"],
                "estop": False,
                "lidar": {"front_m": 1.5, "blocked": False},
                "imu": {"accel": {"x": 0.0, "y": 0.0, "z": 9.8}, "gyro": {"x": 0, "y": 0, "z": 0}},
            })
        if self.path == "/camera/snapshot":
            return self._bytes(JPEG, "image/jpeg")
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = self._body()
        if self.path == "/pilot/authorize":
            STATE["tokens"].append(body.get("token"))
            return self._json({"ok": True, "expires_in_secs": body.get("ttl_secs", 300),
                               "speed_mode": body.get("speed_mode", "medium"), "max_speed": 0.35})
        if self.path == "/drive":
            STATE["drives"].append({"left": body.get("left"), "right": body.get("right"),
                                    "token": body.get("token")})
            return self._json({"ok": True, "speed_mode": "medium", "max_speed": 0.35})
        if self.path == "/stop":
            STATE["stops"] += 1
            return self._json({"ok": True})
        if self.path == "/light":
            base = body.get("brightness", 1.0 if body.get("on") else 0.0)
            io4 = body.get("io4", base); io5 = body.get("io5", base)
            STATE["lights"].append({"io4": io4, "io5": io5, "token": body.get("token")})
            return self._json({"ok": True, "io4": io4, "io5": io5})
        if self.path == "/camera/move":
            # Mirror the Rust handler's firmware-correct mapping.
            pan = max(-1.0, min(1.0, body.get("pan", 0.0)))
            tilt = max(-1.0, min(1.0, body.get("tilt", 0.0)))
            pan_deg = body.get("pan_deg", pan * 180.0)
            tilt_deg = body.get("tilt_deg", (tilt * 90.0 if tilt >= 0 else tilt * 30.0))
            STATE["cameras"].append({"pan_deg": pan_deg, "tilt_deg": tilt_deg})
            return self._json({"ok": True, "pan_deg": pan_deg, "tilt_deg": tilt_deg})
        if self.path == "/capture":
            import hashlib
            return self._json({"ok": True, "content_type": "image/jpeg",
                               "byte_length": len(JPEG),
                               "sha256": "0x" + hashlib.sha256(JPEG).hexdigest(),
                               "captured_at_ms": 1724189733208})
        return self._json({"error": "not found"}, 404)


def main():
    rover_mod.TICK_SECONDS = 0.0  # real code path, no sleeps
    server = ThreadingHTTPServer(("127.0.0.1", 0), Harness)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"harness emulator on {base}")

    # REAL client over REAL HTTP.
    client = HarnessClient(base, speed_mode="medium")            # real POST /pilot/authorize
    assert STATE["tokens"] and STATE["tokens"][0] == client.token
    verbs = HarnessVerbs(client)

    # status_report → real GET /telemetry, lidar surfaced
    rep = verbs.status_report()
    assert "battery=12.4" in rep["reply"] and "lidar_front=1.50m" in rep["reply"], rep
    print("status_report:", rep["reply"])

    # move 2 ft → real POST /drive bursts; twist(0.4,0)→diff(0.4,0.4) on the wire
    res = verbs.move(distance_ft=2.0)
    assert res["ok"] and STATE["drives"], res
    fwd = [d for d in STATE["drives"] if d["left"] and d["left"] > 0]
    assert fwd and abs(fwd[0]["left"] - 0.4) < 1e-9 and abs(fwd[0]["right"] - 0.4) < 1e-9, fwd[0]
    assert fwd[0]["token"] == client.token
    print(f"move: {len(fwd)} forward drive(s) over the wire, left=right={fwd[0]['left']}")

    # photo → real GET /camera/snapshot → real JPEG bytes round-trip (b64→decode)
    jpg = verbs.photo()
    assert isinstance(jpg, bytes) and jpg[:3] == b"\xff\xd8\xff" and jpg == JPEG
    print(f"photo: {len(jpg)} real JPEG bytes over the wire (SOI {jpg[:3].hex()})")

    # turn 90° → real closed-loop using telemetry yaw
    tr = verbs.turn(90)
    assert tr["ok"], tr
    print("turn:", tr)

    # set_lamp → real POST /light (T:132 path); on→full PWM
    verbs.set_lamp(True)
    assert STATE["lights"] and STATE["lights"][-1]["io4"] == 1.0, STATE["lights"]
    print("set_lamp: server got", STATE["lights"][-1])

    # camera_move → real POST /camera/move with firmware-correct tilt mapping
    verbs.camera_move(pan=0.5, tilt=1.0)
    cam = STATE["cameras"][-1]
    assert cam["pan_deg"] == 90.0 and cam["tilt_deg"] == 90.0, cam   # tilt +1 → 90 (up)
    verbs.camera_move(pan=0.0, tilt=-1.0)
    assert STATE["cameras"][-1]["tilt_deg"] == -30.0, STATE["cameras"][-1]  # tilt -1 → -30 (down)
    print("camera_move: pan0.5→90°, tilt+1→90°, tilt-1→-30° (firmware ranges) ✓")

    client.close()
    server.shutdown()
    print("\nLIVE HARNESS ROUND-TRIP PASSED (real httpx, real sockets, real JSON)")


if __name__ == "__main__":
    main()
