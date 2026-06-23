"""LeRobot robot backend for the Waveshare UGV (via the robot-harness).

Conforms to the LeRobot ``Robot`` interface so the Waveshare records datasets and
runs policies exactly like the upstream ``EarthRoverMiniPlus`` — same action
schema (``linear_velocity``, ``angular_velocity``) and an observation schema that
mirrors the Mini+ where the hardware overlaps (camera + telemetry), plus lidar.

    pip install lerobot opencv-python   # required for this module

    lerobot-record --robot.type=waveshare_ugv --teleop.type=keyboard_rover \\
        --dataset.repo_id=you/ugv-nav --dataset.single_task="Navigate"

The import is guarded: the rest of the kit works without LeRobot installed; only
this module needs it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from .harness_client import HarnessClient, twist_to_diff

try:  # LeRobot is an optional, heavy dependency.
    from lerobot.robots import Robot, RobotConfig

    _LEROBOT = True
except Exception:  # pragma: no cover - import guard
    _LEROBOT = False
    Robot = object  # type: ignore

    class RobotConfig:  # type: ignore
        @staticmethod
        def register_subclass(_name):
            def deco(cls):
                return cls
            return deco


def _require_lerobot() -> None:
    if not _LEROBOT:
        raise ImportError(
            "WaveshareUGV needs LeRobot: pip install lerobot opencv-python"
        )


def _require_vision_deps() -> None:
    """Fail fast if the camera-decode stack (numpy + cv2) is missing.

    The image path needs both; detecting this at ``connect()`` gives a single
    clear error instead of crashing lazily on the first ``get_observation``.
    """
    missing = []
    try:
        import numpy  # noqa: F401
    except Exception:
        missing.append("numpy")
    try:
        import cv2  # noqa: F401
    except Exception:
        missing.append("opencv-python")
    if missing:
        raise ImportError(
            "WaveshareUGV camera decode needs "
            + " and ".join(missing)
            + ": pip install " + " ".join(missing)
        )


@RobotConfig.register_subclass("waveshare_ugv")
@dataclass
class WaveshareUGVConfig(RobotConfig):
    """Config for the Waveshare UGV LeRobot backend."""

    harness_url: str = "http://localhost:8000"
    speed_mode: str = "medium"
    camera_width: int = 320
    camera_height: int = 240


class WaveshareUGV(Robot):  # type: ignore[misc]
    """A LeRobot robot driving the Waveshare UGV through the robot-harness.

    Action: ``{linear_velocity, angular_velocity}`` (∈ [-1,1], same as the Mini+;
    converted to differential wheel commands). Observation: forward camera frame
    plus the harness telemetry, named to match the Mini+ feature set where shared.
    """

    config_class = WaveshareUGVConfig
    name = "waveshare_ugv"

    def __init__(self, config: WaveshareUGVConfig):
        _require_lerobot()
        super().__init__(config)
        self.config = config
        self._client: HarnessClient | None = None

    # -- feature schemas -----------------------------------------------------

    @property
    def action_features(self) -> dict[str, type]:
        return {"linear_velocity": float, "angular_velocity": float}

    @cached_property
    def _camera_ft(self) -> dict[str, tuple]:
        return {"front": (self.config.camera_height, self.config.camera_width, 3)}

    @property
    def observation_features(self) -> dict[str, Any]:
        # Mirror the Mini+ telemetry features that the UGV also has, + lidar + GPS.
        scalar = [
            "speed", "battery", "orientation",
            "latitude", "longitude",
            "accel_x", "accel_y", "accel_z",
            "gyro_x", "gyro_y", "gyro_z",
            "lidar_front_m", "lidar_blocked", "estop",
            "timestamp",
        ]
        return {**self._camera_ft, **{k: float for k in scalar}}

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def is_calibrated(self) -> bool:
        return True  # cloud/serial robot — no calibration

    # -- lifecycle -----------------------------------------------------------

    def connect(self, calibrate: bool = True) -> None:
        _require_vision_deps()  # fail fast, not on the first frame
        self._client = HarnessClient(self.config.harness_url, speed_mode=self.config.speed_mode)

    def calibrate(self) -> None:  # no-op
        pass

    def configure(self) -> None:  # no-op
        pass

    def disconnect(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- observe / act -------------------------------------------------------

    def get_observation(self) -> dict[str, Any]:
        assert self._client is not None, "call connect() first"
        obs: dict[str, Any] = {}
        # One shared timestamp for the camera+telemetry pair (the obs is sampled
        # once; downstream alignment shouldn't see two different clocks).
        obs_ts = time.time()
        # Camera frame → HWC uint8 (LeRobot image convention).
        b64 = self._client.screenshot_v2().get("front_frame")
        obs["front"] = _decode_jpeg_b64(b64, self.config.camera_height, self.config.camera_width)
        # Telemetry.
        t = self._client.data()
        ax, ay, az = _imu_vec3(t.raw, "accel")
        gx, gy, gz = _imu_vec3(t.raw, "gyro")
        obs.update(
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
            timestamp=obs_ts,
        )
        return obs

    def send_action(self, action: dict[str, Any]) -> dict[str, Any]:
        assert self._client is not None, "call connect() first"
        linear = float(action.get("linear_velocity", 0.0))
        angular = float(action.get("angular_velocity", 0.0))
        left, right = twist_to_diff(linear, angular)
        self._client.control(linear=linear, angular=angular)
        return {"linear_velocity": linear, "angular_velocity": angular,
                "left": left, "right": right, "ts": time.time()}


def _imu_vec3(raw: dict[str, Any], key: str) -> tuple[float, float, float]:
    """Read ``raw["imu"][key]`` as an (x, y, z) tuple, falling back to zeros.

    The robot-harness serializes ``imu.accel``/``gyro``/``mag`` as JSON arrays
    ``[x, y, z]`` (see robot-harness/src/main.rs ``ImuStatus``). A dict shape
    ``{"x":..,"y":..,"z":..}`` is also accepted for forward/backward tolerance.
    """
    imu = raw.get("imu")
    if not isinstance(imu, dict):
        return (0.0, 0.0, 0.0)
    v = imu.get(key)
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        try:
            return (float(v[0]), float(v[1]), float(v[2]))
        except (TypeError, ValueError):
            return (0.0, 0.0, 0.0)
    if isinstance(v, dict):
        try:
            return (float(v.get("x", 0.0)), float(v.get("y", 0.0)), float(v.get("z", 0.0)))
        except (TypeError, ValueError):
            return (0.0, 0.0, 0.0)
    return (0.0, 0.0, 0.0)


def _decode_jpeg_b64(b64: str | None, h: int, w: int):
    """Decode a base64 JPEG to an HWC uint8 numpy array (zeros if unavailable)."""
    import numpy as np  # lerobot already pulls numpy

    if not b64:
        return np.zeros((h, w, 3), dtype=np.uint8)
    import base64
    import cv2

    buf = np.frombuffer(base64.b64decode(b64), dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        return np.zeros((h, w, 3), dtype=np.uint8)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    if img.shape[:2] != (h, w):
        img = cv2.resize(img, (w, h))
    return img
