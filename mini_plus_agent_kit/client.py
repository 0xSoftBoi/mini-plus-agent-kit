"""Thin, complete Python client for the Earth Rover Mini+ SDK (v5.1).

The Earth Rovers SDK runs locally (``hypercorn main:app`` → http://localhost:8000)
and exposes REST endpoints for control, telemetry, vision, speech, missions,
checkpoints, and interventions. This module wraps every one of them so the rest
of the kit (and your own code) never has to hand-build HTTP requests.

Reference: https://github.com/frodobots-org/earth-rovers-sdk  (linked from
https://bitrobot.ai/miniplusagentkit)
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

ViewType = Literal["front", "rear", "map"]


class EarthRoverError(RuntimeError):
    """Raised when the SDK returns a non-2xx response."""

    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Earth Rover SDK error {status_code}: {detail}")


@dataclass
class Telemetry:
    """Normalized telemetry across backends. Raw payload kept on ``.raw``.

    Both the FrodoBots Earth Rovers ``GET /data`` and the Jetson harness
    ``GET /telemetry`` are mapped into this shape so the agent reasons over one
    schema. Backend-specific fields (GPS for Earth Rovers; lidar/estop for the
    Waveshare harness) are optional and only appear in ``summary()`` when present.
    """

    battery: float | None = None
    signal_level: float | None = None
    orientation: float | None = None  # heading / yaw, degrees
    lamp: int | None = None
    speed: float | None = None
    gps_signal: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    vibration: float | None = None
    timestamp: float | None = None
    # Harness / lidar-equipped robots:
    lidar_front_m: float | None = None
    lidar_blocked: bool | None = None
    estop: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Telemetry":
        """Map a FrodoBots Earth Rovers ``GET /data`` payload."""
        get = d.get
        return cls(
            battery=get("battery"),
            signal_level=get("signal_level"),
            orientation=get("orientation"),
            lamp=get("lamp"),
            speed=get("speed"),
            gps_signal=get("gps_signal"),
            latitude=get("latitude"),
            longitude=get("longitude"),
            vibration=get("vibration"),
            timestamp=get("timestamp"),
            raw=d,
        )

    @classmethod
    def from_harness(cls, d: dict[str, Any]) -> "Telemetry":
        """Map a Jetson ``robot-harness`` ``GET /telemetry`` frame."""
        lidar = d.get("lidar") or {}
        left = d.get("left_cmd")
        right = d.get("right_cmd")
        speed = None
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            speed = (left + right) / 2.0
        ts = d.get("ts_ms")
        return cls(
            battery=d.get("battery_v"),
            orientation=d.get("yaw"),
            speed=speed,
            timestamp=(ts / 1000.0) if isinstance(ts, (int, float)) else None,
            lidar_front_m=lidar.get("front_m"),
            lidar_blocked=lidar.get("blocked"),
            estop=d.get("estop"),
            raw=d,
        )

    def summary(self) -> str:
        """One-line human/agent-readable status string."""
        bits = []
        if self.battery is not None:
            bits.append(f"battery={self.battery}")
        if self.speed is not None:
            bits.append(f"speed={self.speed:.2f}")
        if self.orientation is not None:
            bits.append(f"heading={self.orientation}°")
        if self.latitude is not None and self.longitude is not None:
            bits.append(f"gps=({self.latitude:.6f},{self.longitude:.6f})")
        if self.gps_signal is not None:
            bits.append(f"gps_signal={self.gps_signal}")
        if self.signal_level is not None:
            bits.append(f"signal={self.signal_level}")
        if self.lidar_front_m is not None:
            bits.append(f"lidar_front={self.lidar_front_m:.2f}m")
        if self.lidar_blocked is not None:
            bits.append(f"path_blocked={'YES' if self.lidar_blocked else 'no'}")
        if self.estop:
            bits.append("ESTOP_ENGAGED")
        if self.lamp is not None:
            bits.append(f"lamp={'on' if self.lamp else 'off'}")
        if self.vibration is not None:
            bits.append(f"vibration={self.vibration}")
        return ", ".join(bits) if bits else "(no telemetry)"


class EarthRoverClient:
    """Synchronous client for the Earth Rover Mini+ SDK.

    Example
    -------
    >>> rover = EarthRoverClient("http://localhost:8000")
    >>> rover.start_mission()
    >>> rover.control(linear=1, angular=0)      # full speed ahead
    >>> frames = rover.screenshot_v2()          # fast front+rear capture
    >>> rover.stop()
    """

    #: Which agent tools make sense for this backend (see tools.make_tools).
    capabilities = frozenset(
        {"drive", "vision", "telemetry", "speak", "missions", "lamp"}
    )

    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)

    # -- low level -----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> Any:
        resp = self._http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise EarthRoverError(resp.status_code, detail)
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.content

    # -- control -------------------------------------------------------------

    def control(self, linear: float = 0.0, angular: float = 0.0, lamp: int = 0) -> dict:
        """Drive the rover.

        ``linear`` and ``angular`` are clamped to [-1, 1]: linear>0 forward,
        linear<0 reverse; angular>0 rotate one way, angular<0 the other.
        ``lamp`` is 0 (off) or 1 (on). POST /control.
        """
        linear = max(-1.0, min(1.0, linear))
        angular = max(-1.0, min(1.0, angular))
        body = {"command": {"linear": linear, "angular": angular, "lamp": int(bool(lamp))}}
        return self._request("POST", "/control", json=body)

    def stop(self) -> dict:
        """Convenience: zero all motion (keeps lamp state untouched at 0)."""
        return self.control(0.0, 0.0)

    def set_lamp(self, on: bool) -> dict:
        """Toggle the headlamp without changing motion."""
        return self.control(0.0, 0.0, lamp=1 if on else 0)

    # -- telemetry -----------------------------------------------------------

    def data(self) -> Telemetry:
        """GET /data → decoded :class:`Telemetry` (battery, GPS, IMU, etc.)."""
        return Telemetry.from_dict(self._request("GET", "/data"))

    # -- vision --------------------------------------------------------------

    def screenshot(self, view_types: list[ViewType] | None = None) -> dict:
        """GET /screenshot. Optional ``view_types`` e.g. ``["front","map"]``.

        Returns ``{front_frame, rear_frame, map_frame, timestamp}`` (base64).
        """
        params = {"view_types": ",".join(view_types)} if view_types else None
        return self._request("GET", "/screenshot", params=params)

    def screenshot_v2(self) -> dict:
        """GET /v2/screenshot — ~15x faster front+rear capture (no map)."""
        return self._request("GET", "/v2/screenshot")

    def front(self) -> dict:
        """GET /v2/front → ``{front_frame, timestamp}`` (base64)."""
        return self._request("GET", "/v2/front")

    def rear(self) -> dict:
        """GET /v2/rear → ``{rear_frame, timestamp}`` (base64)."""
        return self._request("GET", "/v2/rear")

    @staticmethod
    def decode_frame(b64: str) -> bytes:
        """Decode a base64 frame string to raw image bytes."""
        return base64.b64decode(b64)

    # -- speech --------------------------------------------------------------

    def speak(self, text: str) -> dict:
        """POST /speak — text-to-speech out of the rover's speaker."""
        return self._request("POST", "/speak", json={"text": text})

    # -- missions ------------------------------------------------------------

    def start_mission(self) -> dict:
        """POST /start-mission. Raises EarthRoverError if the bot is unavailable."""
        return self._request("POST", "/start-mission")

    def end_mission(self) -> dict:
        """POST /end-mission. ⚠️ Causes progress loss — emergencies only."""
        return self._request("POST", "/end-mission")

    def missions_history(self) -> dict:
        """GET /missions-history → past + active mission rides."""
        return self._request("GET", "/missions-history")

    # -- checkpoints ---------------------------------------------------------

    def checkpoints(self) -> dict:
        """GET /checkpoints-list → ordered checkpoints + latest scanned."""
        return self._request("GET", "/checkpoints-list")

    def checkpoint_reached(self) -> dict:
        """POST /checkpoint-reached.

        On success returns ``next_checkpoint_sequence``. On failure the SDK
        returns a 4xx with the proximate distance, surfaced as EarthRoverError.
        """
        return self._request("POST", "/checkpoint-reached")

    # -- openClaw branch verbs (v5.2 feature/openClaw) -----------------------
    # The openClaw branch exposes higher-level, safety-checked endpoints that the
    # agent should prefer over raw /control. See examples/openclaw/AGENTS.md.

    def status_report(self) -> dict:
        """POST /status-report — real sensor snapshot + a chat-ready ``reply``.

        AGENTS.md rule: call this before greeting/status replies; never fabricate.
        """
        return self._request("POST", "/status-report")

    def turn(self, degrees: float, timeout: float | None = None) -> dict:
        """POST /turn — blocking, heading-feedback rotation (+deg = right).

        Body ``{degrees, ...}``; the server's ``_perform_turn`` also accepts
        ``speed``/``min_speed``/``tolerance``/``timeout``. Returns
        ``{requested, actual, steps}``. AGENTS.md rule: ALL turns go through
        /turn, never /control angular.
        """
        body: dict[str, Any] = {"degrees": degrees}
        if timeout is not None:
            body["timeout"] = timeout
        return self._request("POST", "/turn", json=body)

    def prompt(self, text: str = "what do you see?") -> dict:
        """POST /prompt — vision caption + frame.

        The endpoint only accepts the trigger phrases ("what do you see?" …) and
        returns ``{type, caption, front_frame, timestamp}`` (base64 in
        ``front_frame``).
        """
        return self._request("POST", "/prompt", json={"text": text})

    def describe_scene(self, text: str = "what do you see?") -> str:
        """POST /describe-scene — returns ``"<caption>\\nMEDIA:scene.png"`` (text)."""
        out = self._request("POST", "/describe-scene", json={"text": text})
        return out.decode() if isinstance(out, (bytes, bytearray)) else str(out)

    def photo(self) -> str:
        """GET /photo — saves front.png server-side, returns ``"MEDIA:front.png"``.

        Note: this returns a media *reference* string, not image bytes. For raw
        base64 frames use :meth:`front` / :meth:`screenshot_v2`.
        """
        out = self._request("GET", "/photo")
        return out.decode() if isinstance(out, (bytes, bytearray)) else str(out)

    def clip(self, camera: str = "front", duration: float = 10.0, fps: int = 10) -> str:
        """GET /v2/clip — records an mp4 server-side, returns ``"MEDIA:<file>"``."""
        out = self._request("GET", "/v2/clip", params={"camera": camera, "duration": duration, "fps": fps})
        return out.decode() if isinstance(out, (bytes, bytearray)) else str(out)

    def gif(self, camera: str = "front", duration: float = 3.0, fps: int = 5) -> str:
        """GET /v2/gif — records a GIF server-side, returns ``"MEDIA:<file>"``."""
        out = self._request("GET", "/v2/gif", params={"camera": camera, "duration": duration, "fps": fps})
        return out.decode() if isinstance(out, (bytes, bytearray)) else str(out)

    def track_color(self, color: str = "red", **opts) -> dict:
        """POST /track-color — start the VLA "follow the <color> card" loop.

        Background loop: returns a snapshot immediately (``status: started``).
        Optional body: ``duration_seconds, speed, kp_angular, stop_fill,
        search_angular``. Use :meth:`track_color_stop` / :meth:`track_color_status`.
        """
        return self._request("POST", "/track-color", json={"color": color, **opts})

    def track_color_stop(self) -> dict:
        return self._request("POST", "/track-color/stop")

    def track_color_status(self) -> dict:
        return self._request("GET", "/track-color/status")

    def obstacle_alert(self, description: str, action: str | None = None) -> dict:
        """POST /obstacle-alert — announce a blockage (speaks it + notifies chat).

        This is an *announce* action, not a sensor query: body
        ``{description, action?}``. The Mini+ has no lidar; use vision (``prompt``)
        to detect obstacles, then announce here before maneuvering.
        """
        return self._request("POST", "/obstacle-alert", json={"description": description, "action": action})

    def autonav(self, action: str = "status") -> dict:
        """``/autonav/{start|stop}`` (POST) or ``/autonav/status`` (GET)."""
        if action == "status":
            return self._request("GET", "/autonav/status")
        if action in {"start", "stop"}:
            return self._request("POST", f"/autonav/{action}")
        raise ValueError("autonav action must be start|stop|status")

    def personality(self, mode: str) -> dict:
        """POST /personality — friendly | sarcastic | formal."""
        return self._request("POST", "/personality", json={"mode": mode})

    # -- interventions -------------------------------------------------------

    def intervention_start(self) -> dict:
        """POST /interventions/start → ``{intervention_id}``."""
        return self._request("POST", "/interventions/start")

    def intervention_end(self) -> dict:
        """POST /interventions/end."""
        return self._request("POST", "/interventions/end")

    def interventions_history(self) -> dict:
        """GET /interventions/history."""
        return self._request("GET", "/interventions/history")

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "EarthRoverClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
