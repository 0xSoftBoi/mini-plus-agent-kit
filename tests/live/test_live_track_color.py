"""LIVE test — the real track_color visual-servo on real generated frames.

Generates real JPEGs (a colored blob in a known position) with Pillow, serves them
over real HTTP, and runs the REAL HarnessVerbs.track_color loop — proving the HSV
detection and proportional steering actually work end to end. Run:

    .venv/bin/python tests/live/test_live_track_color.py
"""

import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PIL import Image
import mini_plus_agent_kit.rover as rover_mod
from mini_plus_agent_kit.harness_client import HarnessClient
from mini_plus_agent_kit.rover import HarnessVerbs, _detect_color

W, H = 96, 64


def frame(blob_color=(255, 255, 0), cx_frac=0.78, size=18):
    """Real JPEG: gray background with a colored square centered at cx_frac."""
    img = Image.new("RGB", (W, H), (60, 60, 60))
    px = img.load()
    cx = int(cx_frac * W)
    for y in range(H // 2 - size // 2, H // 2 + size // 2):
        for x in range(cx - size // 2, cx + size // 2):
            if 0 <= x < W and 0 <= y < H:
                px[x, y] = blob_color
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# Server state: which frame to serve + recorded drives.
SERVE = {"jpeg": frame()}
DRIVES = []


class Cam(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, o):
        d = json.dumps(o).encode()
        self.send_response(200); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(d))); self.end_headers(); self.wfile.write(d)

    def do_GET(self):
        if self.path == "/camera/snapshot":
            d = SERVE["jpeg"]
            self.send_response(200); self.send_header("content-type", "image/jpeg")
            self.send_header("content-length", str(len(d))); self.end_headers(); self.wfile.write(d)
        elif self.path == "/telemetry":
            self._json({"battery_v": 12.4, "yaw": 0, "left_cmd": 0, "right_cmd": 0,
                        "estop": False, "lidar": {"front_m": 2.0, "blocked": False}})
        else:
            self._json({"error": "nf"})

    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if self.path == "/pilot/authorize":
            self._json({"ok": True, "expires_in_secs": 300, "speed_mode": "medium", "max_speed": 0.35})
        elif self.path == "/drive":
            DRIVES.append((body.get("left"), body.get("right")))
            self._json({"ok": True})
        elif self.path == "/stop":
            self._json({"ok": True})
        else:
            self._json({"ok": True})


def main():
    # 1) Pure detector on a real generated frame: yellow blob on the right.
    found, x_frac, area = _detect_color(frame(cx_frac=0.78), "yellow")
    assert found and x_frac > 0.6, (found, x_frac, area)
    foundL, xL, _ = _detect_color(frame(cx_frac=0.22), "yellow")
    assert foundL and xL < 0.4, (foundL, xL)
    assert not _detect_color(frame(blob_color=(60, 60, 60)), "yellow")[0]  # no yellow → not found
    print(f"_detect_color: yellow right→x={x_frac:.2f}, left→x={xL:.2f}, absent→not found ✓")

    rover_mod.TICK_SECONDS = 0.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Cam)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    verbs = HarnessVerbs(HarnessClient(base, speed_mode="medium"))

    # 2) Blob on the RIGHT → loop should steer right (left wheel faster than right) + forward.
    SERVE["jpeg"] = frame(cx_frac=0.80, size=16)
    DRIVES.clear()
    res = verbs.track_color("yellow", max_steps=1, tick_s=0.0)
    assert res["found"] and res["x_frac"] > 0.6, res
    left, right = DRIVES[0]
    assert left > right, f"blob right → turn right (left>right): {DRIVES[0]}"
    assert (left + right) / 2 > 0, f"should also creep forward: {DRIVES[0]}"
    print(f"track_color right blob: drive left={left:.2f} > right={right:.2f} (turning right) + forward ✓")

    # 3) Blob fills the frame → 'arrived', robot stops (no forward drive recorded).
    SERVE["jpeg"] = frame(cx_frac=0.5, size=60)   # huge blob → area >= stop_fill
    DRIVES.clear()
    res = verbs.track_color("yellow", max_steps=3, tick_s=0.0, stop_fill=0.12)
    assert res["arrived"] and res["area_frac"] >= 0.12, res
    assert DRIVES == [], f"arrived → no drive commands, got {DRIVES}"
    print(f"track_color arrival: area={res['area_frac']:.2f} ≥ stop_fill → stopped, arrived ✓")

    server.shutdown()
    print("\nLIVE TRACK_COLOR PASSED (real HSV detection + visual servo over real HTTP)")


if __name__ == "__main__":
    main()
