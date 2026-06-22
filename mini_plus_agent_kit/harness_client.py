"""Client for the Waveshare UGV via the onchain-rover ``robot-harness`` contract.

This is the mirror of ``onchain-rover-solana/sidecar/src/earth-rover.ts``: that
adapter exposes *your* robot contract and translates to the FrodoBots SDK; this
exposes the *same Python method surface* as :class:`EarthRoverClient` and
translates to *your harness* — so the Claude agent in ``agent.py`` / ``tools.py``
drives the Waveshare with zero changes, twist→differential handled here.

Harness contract (robot-harness/src/main.rs, default :8000), or the sidecar
adapter that proxies it:
    POST /pilot/authorize {token, ttl_secs?, speed_mode?}  -> session token
    POST /drive {token, left, right}                       -> differential drive
    POST /stop            POST /estop  /estop/reset
    GET  /telemetry                                        -> TelemetryFrame
    GET  /camera/snapshot                                  -> JPEG bytes
    POST /capture                                          -> {sha256, byte_length, ...}

Drive through the sidecar by pointing ``base_url`` at the sidecar's robot route
(e.g. an ``/earthrover``-style adapter) so pilot tokens, speed caps, estop and the
onchain hooks all stay in force; or point it straight at the harness for bench
testing.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from typing import Any

import httpx

from .client import EarthRoverError, Telemetry

SpeedMode = str  # "low" | "medium" | "high"


def twist_to_diff(linear: float, angular: float) -> tuple[float, float]:
    """Twist (linear, angular ∈ [-1,1]) → differential wheel speeds (left, right).

    Inverse of ``diffToTwist`` in earth-rover.ts: there left/right → twist via
    ``linear=(l+r)/2, angular=(r-l)/2``; solving gives ``l=linear-angular,
    r=linear+angular``. Clamped to [-1,1] (the harness also caps by speed mode).
    """
    left = max(-1.0, min(1.0, linear - angular))
    right = max(-1.0, min(1.0, linear + angular))
    return left, right


class HarnessClient:
    """Drives a Waveshare UGV through the robot-harness / sidecar contract.

    Exposes the same surface the agent uses: ``control``, ``stop``, ``data``,
    ``screenshot_v2``, ``front``, ``rear``, ``set_lamp``, ``speak``, ``capture``.
    Mission/checkpoint methods raise — the Waveshare has no mission concept; the
    agent's toolset is filtered by :attr:`capabilities` so it never offers them.
    """

    #: Waveshare via harness: drive + mono camera + lidar telemetry + proof.
    #: No TTS, no missions/checkpoints, no headlamp.
    capabilities = frozenset({"drive", "vision", "telemetry", "capture", "estop"})

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        speed_mode: SpeedMode = "medium",
        ttl_secs: float = 300.0,
        timeout: float = 15.0,
        authorize: bool = True,
    ):
        self.base_url = (base_url or os.environ.get("HARNESS_URL", "http://localhost:8000")).rstrip("/")
        self.token = token or f"mpak-{uuid.uuid4().hex[:12]}"
        self.speed_mode = speed_mode
        self.ttl_secs = ttl_secs
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)
        self._authorized_at = 0.0
        if authorize:
            self.pilot_authorize()

    # -- low level -----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> Any:
        resp = self._http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise EarthRoverError(resp.status_code, detail)
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("application/json"):
            return resp.json()
        return resp.content

    # -- session -------------------------------------------------------------

    def pilot_authorize(self) -> dict:
        """POST /pilot/authorize — mint/refresh the drive session token."""
        res = self._request(
            "POST",
            "/pilot/authorize",
            json={"token": self.token, "ttl_secs": self.ttl_secs, "speed_mode": self.speed_mode},
        )
        self._authorized_at = time.time()
        return res if isinstance(res, dict) else {"ok": True}

    def _ensure_session(self) -> None:
        # Re-authorize a little before TTL so long runs never lose the token.
        if time.time() - self._authorized_at > self.ttl_secs * 0.8:
            try:
                self.pilot_authorize()
            except EarthRoverError:
                pass

    # -- control (twist API, translated to differential) ---------------------

    def control(self, linear: float = 0.0, angular: float = 0.0, lamp: int = 0) -> dict:
        """Drive using twist semantics (same signature as EarthRoverClient).

        ``lamp`` is accepted for interface parity but ignored (no headlamp).
        """
        self._ensure_session()
        left, right = twist_to_diff(linear, angular)
        return self._request("POST", "/drive", json={"token": self.token, "left": left, "right": right})

    def stop(self) -> dict:
        """POST /stop (falls back to a zero drive command)."""
        try:
            return self._request("POST", "/stop", json={"token": self.token})
        except EarthRoverError:
            return self._request("POST", "/drive", json={"token": self.token, "left": 0, "right": 0})

    def estop(self) -> dict:
        """POST /estop — latching hard stop (safety)."""
        return self._request("POST", "/estop", json={})

    def estop_reset(self) -> dict:
        return self._request("POST", "/estop/reset", json={})

    def set_lamp(self, on: bool) -> dict:
        """POST /light — toggle the 12V LED (ESP32 T:132) full on/off."""
        return self._request("POST", "/light", json={"on": bool(on), "token": self.token})

    def set_light(self, brightness: float | None = None,
                  io4: float | None = None, io5: float | None = None) -> dict:
        """POST /light — set LED brightness (0..1) or per-channel PWM (io4/io5)."""
        body: dict[str, Any] = {"token": self.token}
        if brightness is not None:
            body["brightness"] = brightness
        if io4 is not None:
            body["io4"] = io4
        if io5 is not None:
            body["io5"] = io5
        return self._request("POST", "/light", json=body)

    def camera_move(self, pan: float | None = None, tilt: float | None = None,
                    pan_deg: float | None = None, tilt_deg: float | None = None,
                    speed: float | None = None) -> dict:
        """POST /camera/move — aim the pan/tilt gimbal (ESP32 T:133).

        ``pan``/``tilt`` are normalized [-1,1]; ``pan_deg``/``tilt_deg`` override
        with absolute degrees.
        """
        body: dict[str, Any] = {"token": self.token}
        for k, v in (("pan", pan), ("tilt", tilt), ("pan_deg", pan_deg),
                     ("tilt_deg", tilt_deg), ("speed", speed)):
            if v is not None:
                body[k] = v
        return self._request("POST", "/camera/move", json=body)

    # -- telemetry -----------------------------------------------------------

    def data(self) -> Telemetry:
        """GET /telemetry → normalized :class:`Telemetry` (incl. lidar/estop)."""
        return Telemetry.from_harness(self._request("GET", "/telemetry"))

    # -- vision (single forward camera) --------------------------------------

    def screenshot_v2(self) -> dict:
        """GET /camera/snapshot (JPEG) → ``{front_frame, rear_frame, timestamp}``.

        Returns the same dict shape as the Earth Rovers SDK so the agent's vision
        path is unchanged; ``rear_frame`` is ``None`` (the UGV has one camera).
        """
        jpg = self._request("GET", "/camera/snapshot")
        b64 = base64.b64encode(jpg).decode("ascii") if isinstance(jpg, (bytes, bytearray)) else None
        return {"front_frame": b64, "rear_frame": None, "timestamp": time.time()}

    def front(self) -> dict:
        shot = self.screenshot_v2()
        return {"front_frame": shot["front_frame"], "timestamp": shot["timestamp"]}

    def rear(self) -> dict:
        return {"rear_frame": None, "timestamp": time.time()}

    def screenshot(self, view_types: list[str] | None = None) -> dict:
        return self.screenshot_v2()

    # -- proof / onchain data commitment -------------------------------------

    def capture(self) -> dict:
        """POST /capture → ``{sha256, byte_length, captured_at_ms, ...}``.

        The harness hashes the current camera frame; this sha256 is exactly what
        ``settle.giveFeedback`` / ``settleRaceOnChain`` anchor on Arc alongside the
        Walrus blobId (see :mod:`mini_plus_agent_kit.proof`).
        """
        return self._request("POST", "/capture", json={})

    def snapshot_bytes(self) -> bytes:
        """Raw JPEG bytes from GET /camera/snapshot (for Walrus upload)."""
        jpg = self._request("GET", "/camera/snapshot")
        if not isinstance(jpg, (bytes, bytearray)):
            raise EarthRoverError(502, "camera/snapshot did not return image bytes")
        return bytes(jpg)

    # -- unsupported on this backend (filtered out of the toolset) ------------

    def speak(self, text: str) -> dict:
        raise EarthRoverError(501, "this robot has no text-to-speech")

    def start_mission(self) -> dict:
        raise EarthRoverError(501, "missions are an Earth Rovers / BitRobot feature")

    def checkpoints(self) -> dict:
        raise EarthRoverError(501, "checkpoints are an Earth Rovers / BitRobot feature")

    def checkpoint_reached(self) -> dict:
        raise EarthRoverError(501, "checkpoints are an Earth Rovers / BitRobot feature")

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        try:
            self.stop()
        except Exception:
            pass
        self._http.close()

    def __enter__(self) -> "HarnessClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
