"""LIVE test — the LeRobot backend's telemetry/IMU mapping over real sockets.

No stubs. A stdlib HTTP server speaks the REAL robot-harness telemetry contract
(``robot-harness/src/main.rs``): the IMU is serialized as JSON *arrays*
``accel:[x,y,z]`` / ``gyro:[x,y,z]`` (not the ``{x,y,z}`` dict the old backend
wrongly parsed), plus odometry, lidar and estop. The REAL ``HarnessClient`` and
the REAL ``lerobot_backend`` parsing helpers map it end to end and we assert the
IMU columns are non-zero and the GPS/lidar/estop columns are present.

The heavy ``lerobot``/``cv2`` deps are not needed here — only the telemetry path
is exercised — so this runs under the project ``.venv``:

    .venv/bin/python tests/live/test_live_lerobot.py
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mini_plus_agent_kit.harness_client import HarnessClient
from mini_plus_agent_kit.lerobot_backend import (
    WaveshareUGV,
    WaveshareUGVConfig,
    _imu_vec3,
    _require_vision_deps,
)

# The REAL harness telemetry frame shape: imu vectors are 3-element ARRAYS.
TELEMETRY = {
    "ts_ms": 1724189733208,
    "robot": "waveshare_ugv",
    "battery_v": 12.4,
    "left_cmd": 0.4, "right_cmd": 0.4,
    "odometry_left": 1.10, "odometry_right": 1.30,
    "yaw": 128.0,
    "estop": True,
    "lidar": {"status": "available", "front_m": 0.42, "blocked": True, "points": 36},
    "imu": {"status": "available",
            "accel": [0.12, -0.34, 9.81],
            "gyro": [0.01, 0.02, -0.03],
            "mag": [21.0, -5.0, 41.0],
            "yaw": 128.0},
}


class Harness(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/telemetry":
            return self._json(TELEMETRY)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        if n:
            self.rfile.read(n)
        if self.path == "/pilot/authorize":
            return self._json({"ok": True, "expires_in_secs": 300,
                               "speed_mode": "medium", "max_speed": 0.35})
        if self.path in ("/stop", "/drive"):
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)


def _telemetry_obs(t):
    """Reproduce the telemetry half of WaveshareUGV.get_observation (no camera).

    Uses the REAL backend helpers + Telemetry so a regression in the array/IMU
    parse or in the observation schema is caught here.
    """
    ax, ay, az = _imu_vec3(t.raw, "accel")
    gx, gy, gz = _imu_vec3(t.raw, "gyro")
    return dict(
        speed=float(t.speed or 0.0),
        battery=float(t.battery or 0.0),
        orientation=float(t.orientation or 0.0),
        latitude=float(t.latitude or 0.0),
        longitude=float(t.longitude or 0.0),
        accel_x=ax, accel_y=ay, accel_z=az,
        gyro_x=gx, gyro_y=gy, gyro_z=gz,
        lidar_front_m=float(t.lidar_front_m or 0.0),
        lidar_blocked=float(bool(t.lidar_blocked)),
        estop=float(bool(t.estop)),
    )


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Harness)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"harness emulator on {base}")

    # REAL client over REAL HTTP → REAL Telemetry.from_harness.
    client = HarnessClient(base, speed_mode="medium")
    t = client.data()

    # The bug: IMU arrays were read as dicts and silently became all-zero.
    ax, ay, az = _imu_vec3(t.raw, "accel")
    gx, gy, gz = _imu_vec3(t.raw, "gyro")
    assert (ax, ay, az) == (0.12, -0.34, 9.81), (ax, ay, az)
    assert (gx, gy, gz) == (0.01, 0.02, -0.03), (gx, gy, gz)
    assert any(v != 0.0 for v in (ax, ay, az)), "accel columns must be non-zero"
    assert any(v != 0.0 for v in (gx, gy, gz)), "gyro columns must be non-zero"
    print(f"imu accel={ax,ay,az} gyro={gx,gy,gz} (real arrays, non-zero) ✓")

    obs = _telemetry_obs(t)
    # GPS / lidar / estop columns must be present in the observation.
    for k in ("latitude", "longitude", "lidar_blocked", "estop"):
        assert k in obs, f"missing observation column {k!r}"
    assert obs["lidar_blocked"] == 1.0 and obs["estop"] == 1.0, obs
    assert abs(obs["lidar_front_m"] - 0.42) < 1e-9, obs
    # speed derives from odometry, not the command average.
    assert abs(obs["speed"] - 1.20) < 1e-9 and t.speed_is_estimated is False, (obs, t)
    print("obs has lat/lon/lidar_blocked/estop; speed from odometry ✓")

    # The observation schema must declare every populated telemetry column.
    # observation_features is a plain property over a cached camera schema; build
    # a throwaway instance via __new__ (avoids the lerobot _require guard in
    # __init__) and seed the one attribute the property reads.
    inst = WaveshareUGV.__new__(WaveshareUGV)
    inst.config = WaveshareUGVConfig(harness_url=base)
    ft = WaveshareUGV.observation_features.fget(inst)
    for k in obs:
        assert k in ft, f"observation_features omits {k!r}"
    assert "front" in ft, "observation_features must keep the camera column"
    print("observation_features covers every populated column ✓")

    # connect() should fail fast with a clear error when cv2 is absent.
    try:
        _require_vision_deps()
        print("vision deps present (cv2 + numpy)")
    except ImportError as e:
        assert "opencv" in str(e) or "numpy" in str(e), e
        print(f"vision-dep guard raises clearly: {e} ✓")

    client.close()
    server.shutdown()
    print("\nLIVE LEROBOT TELEMETRY ROUND-TRIP PASSED (real httpx, real sockets, IMU arrays)")


if __name__ == "__main__":
    main()
