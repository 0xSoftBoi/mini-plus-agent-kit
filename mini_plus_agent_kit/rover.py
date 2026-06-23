"""RoverVerbs — the openClaw high-level control surface, backend-agnostic.

The Mini+ Agent Kit's openClaw branch teaches a key lesson: an agent should drive
through *safe, high-level verbs* (``status_report``, ``turn`` with heading
feedback, ``look``, ``track_color``, ``autonav``) — never raw ``/control`` spam.
This module makes that surface a single abstraction with two implementations:

* :class:`EarthRoverVerbs` — delegates to the openClaw branch's server endpoints
  (``/status-report``, ``/turn``, ``/track-color``, ``/autonav/*`` …), which
  already implement the safety checks.
* :class:`HarnessVerbs` — implements the same verbs *client-side* over the
  Waveshare ``robot-harness`` (closed-loop turn via yaw, lidar obstacle checks,
  calibrated drive), so the identical agent runs on either robot.

The Claude agent (``agent.py``) and tools (``tools.py``) only ever see
:class:`RoverVerbs`.
"""

from __future__ import annotations

import base64
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from .client import EarthRoverClient, EarthRoverError, Telemetry
from .geo import haversine_m, initial_bearing_deg, heading_error_deg
from .harness_client import HarnessClient

CHECKPOINT_TOLERANCE_M = 15.0  # Earth Rover Challenge Urban-track GPS tolerance

# AGENTS.md calibration: "1 ft ≈ 1.5 ticks", default linear speed 0.3–0.5.
FT_PER_TICK = 1.0 / 1.5
DEFAULT_LINEAR = 0.4
TICK_SECONDS = 0.6  # one "tick" ≈ a short drive burst then stop


@dataclass
class Scene:
    """Result of a ``look``: a caption (may be empty) + a JPEG frame (base64)."""

    caption: str
    image_b64: str | None


class RoverVerbs(ABC):
    """The openClaw verb surface the agent drives through."""

    #: openClaw verbs this backend supports (filters the agent's toolset).
    capabilities: frozenset = frozenset()
    name: str = "rover"

    @abstractmethod
    def status_report(self) -> dict: ...
    @abstractmethod
    def telemetry(self) -> Telemetry: ...
    @abstractmethod
    def move(self, distance_ft: float = 1.0, backward: bool = False) -> dict: ...
    @abstractmethod
    def turn(self, degrees: float) -> dict: ...
    @abstractmethod
    def look(self) -> Scene: ...
    @abstractmethod
    def photo(self) -> bytes: ...
    @abstractmethod
    def stop(self) -> dict: ...

    # Optional verbs — default to "unsupported"; backends override.
    def speak(self, text: str) -> dict:
        raise EarthRoverError(501, f"{self.name}: speak is not supported")

    def clip(self, seconds: float = 5.0) -> bytes:
        raise EarthRoverError(501, f"{self.name}: clip is not supported")

    def track_color(self, color: str) -> dict:
        raise EarthRoverError(501, f"{self.name}: track_color is not supported")

    def obstacle_check(self) -> dict:
        raise EarthRoverError(501, f"{self.name}: obstacle_check is not supported")

    def autonav(self, action: str = "status") -> dict:
        raise EarthRoverError(501, f"{self.name}: autonav is not supported")

    def set_lamp(self, on: bool) -> dict:
        raise EarthRoverError(501, f"{self.name}: lamp is not supported")

    def camera_move(self, pan: float = 0.0, tilt: float = 0.0) -> dict:
        raise EarthRoverError(501, f"{self.name}: camera gimbal is not supported")

    def navigate(self) -> dict:
        raise EarthRoverError(501, f"{self.name}: GPS waypoint navigation is not supported")

    def checkpoint_reached(self) -> dict:
        raise EarthRoverError(501, f"{self.name}: checkpoints are not supported")

    def close(self) -> None:  # pragma: no cover - thin
        pass


# --------------------------------------------------------------------------- #
# FrodoBots Earth Rover (openClaw branch) — delegate to the safe server verbs.
# --------------------------------------------------------------------------- #
class EarthRoverVerbs(RoverVerbs):
    name = "earthrover"
    # No lidar → no sensor obstacle_check; obstacle awareness is via vision (look).
    # Has a headlamp (control lamp field); no pan/tilt gimbal over HTTP. GPS +
    # checkpoints → real waypoint navigation (Earth Rover Challenge Urban track).
    capabilities = frozenset(
        {"status_report", "move", "turn", "look", "photo", "speak",
         "track_color", "autonav", "lamp", "navigate"}
    )

    def __init__(self, client: EarthRoverClient):
        self.client = client

    def status_report(self) -> dict:
        return self.client.status_report()

    def telemetry(self) -> Telemetry:
        return self.client.data()

    def move(self, distance_ft: float = 1.0, backward: bool = False) -> dict:
        # Movement symmetry (AGENTS.md): ~1 ft per control+stop; repeat for more.
        ticks = max(1, round(abs(distance_ft) / FT_PER_TICK))
        lin = -DEFAULT_LINEAR if backward else DEFAULT_LINEAR
        for _ in range(ticks):
            self.client.control(linear=lin, angular=0)
            time.sleep(TICK_SECONDS)
            self.client.stop()
        return {"ok": True, "ticks": ticks, "distance_ft": distance_ft}

    def turn(self, degrees: float) -> dict:
        return self.client.turn(degrees)  # blocking, heading-feedback

    def look(self) -> Scene:
        # /prompt only accepts the trigger phrase; returns {caption, front_frame}.
        res = self.client.prompt("what do you see?")
        return Scene(caption=res.get("caption", ""), image_b64=res.get("front_frame"))

    def photo(self) -> bytes:
        # /photo returns a "MEDIA:" reference; fetch real base64 from /v2/front.
        b64 = self.client.front().get("front_frame")
        return base64.b64decode(b64) if b64 else b""

    def speak(self, text: str) -> dict:
        return self.client.speak(text)

    def track_color(self, color: str) -> dict:
        return self.client.track_color(color)

    def autonav(self, action: str = "status") -> dict:
        return self.client.autonav(action)

    def set_lamp(self, on: bool) -> dict:
        return self.client.set_lamp(on)  # /control lamp field

    def checkpoint_reached(self) -> dict:
        return self.client.checkpoint_reached()

    def navigate(self) -> dict:
        """GPS guidance to the next checkpoint (Earth Rover Challenge Urban track).

        Reads the rover's GPS + heading and the next un-scanned checkpoint, and
        returns distance, bearing, and the signed heading error to steer by — the
        information an LLM agent can't eyeball from the camera alone.
        """
        t = self.client.data()
        if t.latitude is None or t.longitude is None:
            raise EarthRoverError(409, "no GPS fix")
        cps = self.client.checkpoints()
        scanned = cps.get("latest_scanned_checkpoint") or 0
        nxt = next((c for c in sorted(cps.get("checkpoints_list", []),
                                      key=lambda c: c.get("sequence", 0))
                    if c.get("sequence", 0) > scanned), None)
        if nxt is None:
            return {"done": True, "reply": "all checkpoints scanned — mission complete"}
        clat, clon = float(nxt["latitude"]), float(nxt["longitude"])
        dist = haversine_m(t.latitude, t.longitude, clat, clon)
        brg = initial_bearing_deg(t.latitude, t.longitude, clat, clon)
        herr = heading_error_deg(t.orientation or 0.0, brg)
        within = dist <= CHECKPOINT_TOLERANCE_M
        return {
            "next_checkpoint_sequence": nxt.get("sequence"),
            "distance_m": round(dist, 1), "bearing_deg": round(brg, 1),
            "heading_error_deg": round(herr, 1), "within_tolerance": within,
            "reply": (f"checkpoint #{nxt.get('sequence')}: {dist:.0f} m away, bearing "
                      f"{brg:.0f}°, turn {herr:+.0f}°"
                      + (" — within tolerance, claim it" if within else "")),
        }

    def goto_checkpoint(self, max_steps: int = 80, turn_thresh: float = 18.0,
                        step_ft: float = 3.0) -> dict:
        """Deterministic GPS waypoint controller — the autonomous baseline.

        Loops: get guidance → if within tolerance claim the checkpoint → else turn
        toward the bearing (heading-feedback) or creep forward. The LLM-agent path
        instead calls ``navigate`` + ``move``/``turn``/``checkpoint_reached`` itself.
        """
        last: dict = {}
        for step in range(1, max_steps + 1):
            last = self.navigate()
            if last.get("done"):
                return {"ok": True, "done": True, "steps": step}
            if last["within_tolerance"]:
                res = self.client.checkpoint_reached()
                return {"ok": True, "reached": last["next_checkpoint_sequence"],
                        "steps": step, "result": res}
            herr = last["heading_error_deg"]
            if abs(herr) > turn_thresh:
                self.turn(herr)
            else:
                self.move(step_ft)
        return {"ok": False, "reason": "max_steps", "last": last}

    def goto_checkpoint_fused(self, max_steps: int = 400, dt: float = 0.25,
                              v_scale_mps: float = 0.6) -> dict:
        """Closed-loop *fused* waypoint controller — the production autonomy path.

        Where ``goto_checkpoint`` is bang-bang on raw GPS+heading, this runs the
        navigation stack: heading fusion (orientation/IMU), pose fusion (commanded-
        velocity odometry corrected by GPS), pursuit steering, and a safety envelope
        (battery / tilt / lidar time-to-collision), emitting a smoothed twist each
        loop via ``/control``. Proven to reach the checkpoint where the bang-bang
        baseline false-arrives under GPS noise (``tests/live/test_live_navstack.py``).
        """
        from .control import NavController

        t = self.client.data()
        if t.latitude is None or t.longitude is None:
            raise EarthRoverError(409, "no GPS fix")
        cps = self.client.checkpoints()
        scanned = cps.get("latest_scanned_checkpoint") or 0
        nxt = next((c for c in sorted(cps.get("checkpoints_list", []),
                                      key=lambda c: c.get("sequence", 0))
                    if c.get("sequence", 0) > scanned), None)
        if nxt is None:
            return {"done": True, "reply": "all checkpoints scanned — mission complete"}
        glat, glon = float(nxt["latitude"]), float(nxt["longitude"])
        nav = NavController(t.latitude, t.longitude,
                            tol_m=CHECKPOINT_TOLERANCE_M, v_scale_mps=v_scale_mps)
        last = {}
        for step in range(1, max_steps + 1):
            t = self.client.data()
            s = nav.step(dt, heading_deg=t.orientation or 0.0, goal_lat=glat, goal_lon=glon,
                         lat=t.latitude, lon=t.longitude, lidar_front_m=t.lidar_front_m,
                         battery=t.battery, estop=bool(t.estop))
            last = {"distance_m": round(s.distance_m, 1), "safety": s.safety,
                    "heading_error_deg": round(s.heading_error_deg, 1)}
            if s.arrived:
                self.client.stop()
                res = self.client.checkpoint_reached()
                return {"ok": True, "reached": nxt.get("sequence"), "steps": step,
                        "controller": "fused", "result": res}
            self.client.control(s.linear, s.angular)
            time.sleep(dt)
        self.client.stop()
        return {"ok": False, "reason": "max_steps", "controller": "fused", "last": last}

    def goto_checkpoint_planned(self, costmap, max_steps: int = 600, dt: float = 0.25,
                                v_scale_mps: float = 0.6) -> dict:
        """Plan a path *around obstacles* (A* over ``costmap``), then track it.

        The Nav2-style global+local split: ``costmap`` is a ``planner.Costmap`` in the
        rover's local-ENU frame (origin = the rover's pose at call time), populated by
        the caller from whatever obstacle sense the platform exposes (camera-derived
        occupancy, a known site map; the Earth Rover SDK only gives 1-D front lidar).
        A straight-line waypoint seeker drives into anything between it and the goal;
        this routes around it with regulated pure pursuit. Falls back to the
        closed-loop straight-line controller if no route is found.
        """
        from .control import NavController
        from .planner import plan_path

        t = self.client.data()
        if t.latitude is None or t.longitude is None:
            raise EarthRoverError(409, "no GPS fix")
        cps = self.client.checkpoints()
        scanned = cps.get("latest_scanned_checkpoint") or 0
        nxt = next((c for c in sorted(cps.get("checkpoints_list", []),
                                      key=lambda c: c.get("sequence", 0))
                    if c.get("sequence", 0) > scanned), None)
        if nxt is None:
            return {"done": True, "reply": "all checkpoints scanned — mission complete"}
        glat, glon = float(nxt["latitude"]), float(nxt["longitude"])
        nav = NavController(t.latitude, t.longitude, tol_m=CHECKPOINT_TOLERANCE_M,
                            v_scale_mps=v_scale_mps, use_rpp=True)
        path = plan_path(costmap, (0.0, 0.0), nav.pf.to_xy(glat, glon))
        if not path:                                    # unreachable on the costmap
            return self.goto_checkpoint_fused(max_steps=max_steps, dt=dt, v_scale_mps=v_scale_mps)
        nav.set_path(path)
        for step in range(1, max_steps + 1):
            t = self.client.data()
            s = nav.step(dt, heading_deg=t.orientation or 0.0, goal_lat=glat, goal_lon=glon,
                         lat=t.latitude, lon=t.longitude, lidar_front_m=t.lidar_front_m,
                         battery=t.battery, estop=bool(t.estop))
            if s.arrived:
                self.client.stop()
                res = self.client.checkpoint_reached()
                return {"ok": True, "reached": nxt.get("sequence"), "steps": step,
                        "controller": "planned", "waypoints": len(path), "result": res}
            self.client.control(s.linear, s.angular)
            time.sleep(dt)
        self.client.stop()
        return {"ok": False, "reason": "max_steps", "controller": "planned", "waypoints": len(path)}

    def stop(self) -> dict:
        return self.client.stop()

    def close(self) -> None:
        self.client.close()


# --------------------------------------------------------------------------- #
# Waveshare UGV via robot-harness — implement the same verbs client-side.
# --------------------------------------------------------------------------- #
class HarnessVerbs(RoverVerbs):
    name = "waveshare"
    capabilities = frozenset(
        {"status_report", "move", "turn", "look", "photo", "obstacle_check",
         "autonav", "lamp", "camera", "track_color"}
    )

    def __init__(self, client: HarnessClient, turn_tolerance_deg: float = 8.0):
        self.client = client
        self.turn_tolerance_deg = turn_tolerance_deg

    def status_report(self) -> dict:
        t = self.client.data()
        return {"reply": t.summary(), "telemetry": t.raw}

    def telemetry(self) -> Telemetry:
        return self.client.data()

    def move(self, distance_ft: float = 1.0, backward: bool = False) -> dict:
        ticks = max(1, round(abs(distance_ft) / FT_PER_TICK))
        lin = -DEFAULT_LINEAR if backward else DEFAULT_LINEAR
        moved = 0
        for _ in range(ticks):
            # Lidar safety: never drive forward into a blocked path.
            if not backward:
                t = self.client.data()
                if t.lidar_blocked:
                    self.client.stop()
                    return {"ok": False, "reason": "path blocked by lidar",
                            "ticks": moved, "lidar_front_m": t.lidar_front_m}
            self.client.control(linear=lin, angular=0)
            time.sleep(TICK_SECONDS)
            self.client.stop()
            moved += 1
        return {"ok": True, "ticks": moved, "distance_ft": distance_ft}

    def turn(self, degrees: float, max_time: float = 12.0) -> dict:
        """Closed-loop turn using the harness yaw telemetry (+deg = right)."""
        start = self.client.data().orientation
        # +degrees = turn right (compass CW). ROS twist: +angular = CCW = left, so
        # a right turn needs NEGATIVE angular. (If a unit's IMU yaw is CCW-positive,
        # set ROVER_YAW_SIGN=-1 to flip — calibrate with a measured 90° turn.)
        cmd = -0.5 if degrees >= 0 else 0.5
        if start is None:
            # No heading feedback — fall back to a timed open-loop spin.
            self.client.control(linear=0, angular=cmd)
            time.sleep(min(abs(degrees) / 90.0, max_time))
            self.client.stop()
            return {"ok": True, "mode": "open_loop", "degrees": degrees}

        target = abs(degrees)
        sign = 1.0 if degrees >= 0 else -1.0   # report signed magnitude turned
        turned = 0.0
        prev = start
        t0 = time.time()
        self.client.control(linear=0, angular=cmd)
        while time.time() - t0 < max_time and turned < target - self.turn_tolerance_deg:
            time.sleep(0.1)
            cur = self.client.data().orientation
            if cur is None:
                continue
            d = abs((cur - prev + 180) % 360 - 180)  # shortest angular delta
            turned += d
            prev = cur
        self.client.stop()
        return {"ok": True, "mode": "closed_loop", "degrees_target": degrees,
                "degrees_turned": round(turned * sign, 1)}

    def look(self) -> Scene:
        b64 = self.client.screenshot_v2().get("front_frame")
        caption = _gemini_caption(b64) if b64 else ""
        return Scene(caption=caption, image_b64=b64)

    def photo(self) -> bytes:
        return self.client.snapshot_bytes()

    def obstacle_check(self) -> dict:
        t = self.client.data()
        return {"blocked": bool(t.lidar_blocked), "front_m": t.lidar_front_m,
                "reply": ("path is blocked" if t.lidar_blocked
                          else f"clear ahead ({t.lidar_front_m} m)" if t.lidar_front_m
                          else "no lidar reading")}

    def autonav(self, action: str = "status", steps: int = 8) -> dict:
        """Minimal lidar-guided safe-forward loop (no policy server on harness)."""
        if action == "status":
            return {"running": False, "note": "harness autonav is per-call"}
        if action == "stop":
            return self.client.stop()
        # action == "start": drive forward, turning away when lidar blocks.
        for _ in range(steps):
            t = self.client.data()
            if t.lidar_blocked:
                self.turn(45)
            else:
                self.move(1.0)
        self.client.stop()
        return {"ok": True, "steps": steps}

    def set_lamp(self, on: bool) -> dict:
        return self.client.set_lamp(on)  # POST /light → ESP32 T:132

    def camera_move(self, pan: float = 0.0, tilt: float = 0.0) -> dict:
        return self.client.camera_move(pan=pan, tilt=tilt)  # POST /camera/move → T:133

    def track_color(self, color: str, max_steps: int = 20, speed: float = 0.35,
                    kp: float = 1.2, stop_fill: float = 0.12,
                    search_angular: float = 0.4, tick_s: float = 0.2) -> dict:
        """Client-side visual-servo: find and follow a colored target.

        The Waveshare has no server-side track-color (that's the Earth Rover
        openClaw endpoint), so we close the loop here: snapshot → HSV blob detect
        → proportional heading correction toward the blob → creep forward until it
        fills the frame (``stop_fill``). Lidar-safe. Mirrors the firmware's
        ``track_color`` (HSV ranges + ``kp_angular`` + ``stop_fill``).
        """
        last = {"found": False, "x_frac": 0.5, "area_frac": 0.0}
        arrived = False
        steps = 0
        for steps in range(1, max_steps + 1):
            try:
                if self.client.data().lidar_blocked:
                    self.client.stop()
                    return {"ok": False, "reason": "path blocked", "color": color,
                            "steps": steps, **last}
            except EarthRoverError:
                pass
            found, x_frac, area = _detect_color(self.client.snapshot_bytes(), color)
            last = {"found": found, "x_frac": round(x_frac, 3), "area_frac": round(area, 4)}
            if not found:
                self.client.control(linear=0.0, angular=search_angular)  # rotate to search
                if tick_s:
                    time.sleep(tick_s)
                continue
            if area >= stop_fill:
                self.client.stop()
                arrived = True
                break
            err = x_frac - 0.5                       # >0 → blob right of center
            angular = max(-1.0, min(1.0, -kp * err))  # angular>0 = left, so right→negative
            linear = speed * (1.0 - min(0.8, abs(err) * 1.5))  # straighten before speeding up
            self.client.control(linear=linear, angular=angular)
            if tick_s:
                time.sleep(tick_s)
        self.client.stop()
        return {"ok": True, "color": color, "arrived": arrived, "steps": steps, **last}

    def stop(self) -> dict:
        return self.client.stop()

    def close(self) -> None:
        self.client.close()


# HSV blob detection for track_color (Pillow HSV space: H/S/V each 0-255).
# Ranges mirror the Waveshare/openClaw firmware's track-color buckets; tune per
# lighting. Red wraps the hue circle, so it carries two H windows.
_COLOR_HSV: dict[str, tuple[list[tuple[int, int]], int, int]] = {
    "red": ([(0, 8), (247, 255)], 90, 60),
    "orange": ([(9, 20)], 90, 70),
    "yellow": ([(21, 45)], 60, 80),
    "green": ([(46, 95)], 45, 50),
    "cyan": ([(96, 130)], 45, 50),
    "blue": ([(131, 175)], 45, 40),
    "purple": ([(176, 215)], 40, 40),
    "magenta": ([(216, 246)], 50, 50),
    "pink": ([(216, 255)], 25, 130),
}
_COLOR_ALIAS = {"violet": "purple", "fuchsia": "magenta", "lime": "green", "aqua": "cyan"}


def _detect_color(jpeg: bytes, color: str) -> tuple[bool, float, float]:
    """Locate a colored blob in a JPEG → (found, x_frac, area_frac).

    ``x_frac`` is the blob-centroid x as a fraction of width (0=left, 1=right);
    ``area_frac`` is the blob's fraction of the frame. Requires Pillow + numpy
    (``pip install "mini-plus-agent-kit[track]"``).
    """
    try:
        from io import BytesIO

        import numpy as np
        from PIL import Image
    except Exception as e:  # pragma: no cover - optional dep
        raise EarthRoverError(501, f"track_color needs Pillow+numpy: {e}")

    key = _COLOR_ALIAS.get(color.lower().strip(), color.lower().strip())
    spec = _COLOR_HSV.get(key)
    if spec is None:
        raise EarthRoverError(400, f"unknown color {color!r}; try: {', '.join(_COLOR_HSV)}")
    ranges, smin, vmin = spec

    arr = np.asarray(Image.open(BytesIO(jpeg)).convert("HSV"))
    h, s, v = arr[..., 0], arr[..., 1], arr[..., 2]
    hmask = np.zeros(h.shape, dtype=bool)
    for lo, hi in ranges:
        hmask |= (h >= lo) & (h <= hi)
    mask = hmask & (s >= smin) & (v >= vmin)
    area = float(mask.mean())
    if area < 0.003:
        return (False, 0.5, area)
    xs = np.nonzero(mask)[1]
    return (True, float(xs.mean()) / mask.shape[1], area)


def _gemini_caption(image_b64: str) -> str:
    """Optional scene caption via Gemini if GEMINI_API_KEY is set (else empty)."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not image_b64:
        return ""
    try:  # pragma: no cover - network/optional dep
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=base64.b64decode(image_b64), mime_type="image/jpeg"),
                "Briefly describe what this robot's forward camera sees.",
            ],
        )
        return (resp.text or "").strip()
    except Exception:
        return ""


def make_verbs(target: Any) -> RoverVerbs:
    """Wrap a transport client (or pass-through a RoverVerbs) into RoverVerbs."""
    if isinstance(target, RoverVerbs):
        return target
    if isinstance(target, EarthRoverClient):
        return EarthRoverVerbs(target)
    if isinstance(target, HarnessClient):
        return HarnessVerbs(target)
    raise TypeError(f"don't know how to make RoverVerbs from {type(target)!r}")
