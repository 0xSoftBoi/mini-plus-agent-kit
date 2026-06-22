"""LIVE test — real GPS waypoint navigation against a 2D kinematic rover sim.

A real HTTP server simulates an Earth Rover: it holds (x,y,heading), converts to
lat/lon for /data, turns on /turn, and advances on forward /control. The REAL
EarthRoverVerbs.goto_checkpoint (real geo + real HTTP) must steer to the checkpoint
within the 15 m Urban-track tolerance and claim it. Run:

    .venv/bin/python tests/live/test_live_navigate.py
"""

import json
import math
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import mini_plus_agent_kit.rover as rover_mod
from mini_plus_agent_kit.client import EarthRoverClient
from mini_plus_agent_kit.geo import haversine_m
from mini_plus_agent_kit.rover import EarthRoverVerbs

BASE_LAT, BASE_LON = 37.8700, -122.2500
M_PER_DEG = 111_320.0
LON_M = M_PER_DEG * math.cos(math.radians(BASE_LAT))

# Rover sim state (meters in a local ENU frame) + checkpoint 60 m N, 25 m E.
SIM = {"x": 0.0, "y": 0.0, "heading": 0.0, "scanned": 0, "controls": 0, "turns": 0}
CP = {"x": 25.0, "y": 60.0}


def to_latlon(x, y):
    return BASE_LAT + y / M_PER_DEG, BASE_LON + x / LON_M


def cp_latlon():
    return to_latlon(CP["x"], CP["y"])


def dist_to_cp():
    return math.hypot(CP["x"] - SIM["x"], CP["y"] - SIM["y"])


class Sim(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, o, code=200):
        d = json.dumps(o).encode()
        self.send_response(code); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(d))); self.end_headers(); self.wfile.write(d)

    def do_GET(self):
        if self.path == "/data":
            lat, lon = to_latlon(SIM["x"], SIM["y"])
            return self._json({"latitude": lat, "longitude": lon, "orientation": SIM["heading"],
                               "battery": 100, "speed": 0})
        if self.path == "/checkpoints-list":
            clat, clon = cp_latlon()
            return self._json({"checkpoints_list": [{"id": 1, "sequence": 1,
                                                     "latitude": str(clat), "longitude": str(clon)}],
                               "latest_scanned_checkpoint": SIM["scanned"]})
        return self._json({"error": "nf"}, 404)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if self.path == "/turn":
            SIM["heading"] = (SIM["heading"] + float(body.get("degrees", 0))) % 360.0
            SIM["turns"] += 1
            return self._json({"requested": body.get("degrees"), "actual": body.get("degrees")})
        if self.path == "/control":
            lin = (body.get("command") or {}).get("linear", 0)
            if lin and lin > 0:  # advance 2 m along heading (0=N=+y, 90=E=+x)
                SIM["x"] += 2.0 * math.sin(math.radians(SIM["heading"]))
                SIM["y"] += 2.0 * math.cos(math.radians(SIM["heading"]))
                SIM["controls"] += 1
            return self._json({"message": "Command sent successfully"})
        if self.path == "/stop":
            return self._json({"message": "ok"})
        if self.path == "/checkpoint-reached":
            if dist_to_cp() <= 15.0:
                SIM["scanned"] = 1
                return self._json({"message": "Checkpoint reached successfully", "next_checkpoint_sequence": 2})
            return self._json({"detail": {"error": "not within range", "proximate_distance_to_checkpoint": dist_to_cp()}}, 400)
        return self._json({"message": "ok"})


def main():
    rover_mod.TICK_SECONDS = 0.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Sim)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"earth-rover sim on {base}  (checkpoint {dist_to_cp():.0f} m away)")

    verbs = EarthRoverVerbs(EarthRoverClient(base))

    # navigate(): real geo guidance from real GPS over HTTP
    nav = verbs.navigate()
    print("navigate:", nav["reply"])
    assert 60 < nav["distance_m"] < 75 and 18 < nav["bearing_deg"] < 28, nav   # toward NE
    assert not nav["within_tolerance"]

    # goto_checkpoint(): the real controller must converge and claim the checkpoint
    res = verbs.goto_checkpoint(max_steps=200)
    final = dist_to_cp()
    print(f"goto_checkpoint: {res}  | final dist {final:.1f} m | "
          f"{SIM['turns']} turns, {SIM['controls']} forward moves")
    assert res["ok"] and res.get("reached") == 1, res
    assert final <= 15.0 and SIM["scanned"] == 1

    server.shutdown()
    print("\nLIVE NAVIGATE PASSED (real geo + real HTTP → rover reaches GPS checkpoint)")


if __name__ == "__main__":
    main()
