"""The rover verb registry — one declarative source of truth.

Each openClaw verb (status-report, look, move, turn, track-color, autonav …) is a
single :class:`Verb` entry holding *both* its JSON schema and its handler. From
this one registry we derive:

* ``make_tools(capabilities, has_work)`` — the Anthropic tool schemas (filtered to
  what the current robot supports),
* ``dispatch(verbs, name, args, …)`` — the handler for one call,
* ``TOOLS`` — the back-compat schema list,

and the MCP server (``mcp_server.py``), the Claude agent (``agent.py``), and the
Telegram chat (``telegram.py``) all consume those — so adding a verb is one
registry entry plus one backend method, with no switch to edit and no verb name
duplicated anywhere.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Callable

from .client import EarthRoverError
from .rover import RoverVerbs
from .work import submit_work


# --------------------------------------------------------------------------- #
# Outcome + content helpers
# --------------------------------------------------------------------------- #
class ToolOutcome:
    def __init__(self, blocks, finished=False, success=False, reason="", is_error=False):
        self.blocks = blocks
        self.finished = finished
        self.success = success
        self.reason = reason
        self.is_error = is_error


def _text(s: str) -> dict[str, Any]:
    return {"type": "text", "text": s}


def _image_block(b64: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}


def _observe_blocks(verbs: RoverVerbs) -> list[dict[str, Any]]:
    """Fresh scene + telemetry, returned after a movement verb."""
    blocks: list[dict[str, Any]] = []
    try:
        scene = verbs.look()
        if scene.caption:
            blocks.append(_text(f"View: {scene.caption}"))
        if scene.image_b64:
            blocks.append(_image_block(scene.image_b64))
    except EarthRoverError:
        pass
    try:
        blocks.append(_text(f"Telemetry: {verbs.telemetry().summary()}"))
    except EarthRoverError:
        pass
    return blocks or [_text("(no fresh observation available)")]


# --------------------------------------------------------------------------- #
# Registry types
# --------------------------------------------------------------------------- #
@dataclass
class Ctx:
    """Per-call context handlers need beyond (verbs, args)."""

    work: Any = None
    resource_name: str | None = None


@dataclass(frozen=True)
class Verb:
    name: str
    cap: str  # capability gate; "_always" = always on, "_work" = needs a WorkSink
    description: str
    schema: dict
    run: Callable[[RoverVerbs, dict, Ctx], ToolOutcome]


# --------------------------------------------------------------------------- #
# Handlers (the bodies that used to live in the dispatch switch)
# --------------------------------------------------------------------------- #
def _h_status_report(verbs, args, ctx):
    rep = verbs.status_report()
    return ToolOutcome([_text(rep.get("reply") or str(rep))])


def _h_look(verbs, args, ctx):
    scene = verbs.look()
    blocks = [_text(scene.caption or "(scene captured)")]
    if scene.image_b64:
        blocks.append(_image_block(scene.image_b64))
    return ToolOutcome(blocks)


def _h_photo(verbs, args, ctx):
    jpg = verbs.photo()
    return ToolOutcome([_image_block(base64.b64encode(jpg).decode("ascii"))])


def _h_move(verbs, args, ctx):
    res = verbs.move(distance_ft=float(args["distance_ft"]),
                     backward=bool(args.get("backward", False)))
    return ToolOutcome([_text(str(res))] + _observe_blocks(verbs),
                       is_error=not res.get("ok", True))


def _h_turn(verbs, args, ctx):
    res = verbs.turn(float(args["degrees"]))
    return ToolOutcome([_text(str(res))] + _observe_blocks(verbs))


def _h_obstacle_check(verbs, args, ctx):
    res = verbs.obstacle_check()
    return ToolOutcome([_text(res.get("reply") or str(res))])


def _h_track_color(verbs, args, ctx):
    res = verbs.track_color(str(args["color"]))
    return ToolOutcome([_text(str(res))] + _observe_blocks(verbs))


def _h_autonav(verbs, args, ctx):
    res = verbs.autonav(str(args["action"]))
    return ToolOutcome([_text(str(res))])


def _h_navigate(verbs, args, ctx):
    return ToolOutcome([_text(verbs.navigate().get("reply") or "navigating")])


def _h_checkpoint_reached(verbs, args, ctx):
    return ToolOutcome([_text(str(verbs.checkpoint_reached()))])


def _h_speak(verbs, args, ctx):
    verbs.speak(str(args["text"]))
    return ToolOutcome([_text(f"Spoke: {args['text']!r}")])


def _h_set_lamp(verbs, args, ctx):
    verbs.set_lamp(bool(args["on"]))
    return ToolOutcome([_text(f"Lamp {'on' if args['on'] else 'off'}.")])


def _h_camera_move(verbs, args, ctx):
    res = verbs.camera_move(pan=float(args.get("pan", 0.0)), tilt=float(args.get("tilt", 0.0)))
    return ToolOutcome([_text(str(res))] + _observe_blocks(verbs))


def _h_capture_work(verbs, args, ctx):
    if ctx.work is None:
        return ToolOutcome([_text("No work sink configured.")], is_error=True)
    rec = submit_work(
        ctx.work, verbs.photo(),
        label=str(args.get("label", "agent work")),
        vrw_points=int(args.get("vrw_points", 100)),
        resource_name=ctx.resource_name,
    )
    a = rec.artifact
    return ToolOutcome([_text(
        f"Work submitted ({rec.vrw_points} VRW): {rec.label}\n"
        f"sha256={a.sha256} cid={a.ipfs_cid}\n{a.walrus_url}"
    )])


def _h_finish(verbs, args, ctx):
    return ToolOutcome([_text("Run ended.")], finished=True,
                       success=bool(args.get("success", False)),
                       reason=str(args.get("reason", "")))


# --------------------------------------------------------------------------- #
# The registry — schema + handler co-located, one entry per verb
# --------------------------------------------------------------------------- #
_NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}

VERBS: list[Verb] = [
    Verb("status_report", "status_report",
         "Read the robot's real sensors (battery, heading, GPS/lidar) and get a "
         "chat-ready status line. Call this before any greeting or status reply; "
         "never make up telemetry.",
         _NO_ARGS, _h_status_report),
    Verb("look", "look",
         "Capture the current scene and return a caption plus the camera frame. "
         "Use before deciding where to move and whenever asked what you see.",
         _NO_ARGS, _h_look),
    Verb("photo", "photo",
         "Take a single still photo from the forward camera and return it.",
         _NO_ARGS, _h_photo),
    Verb("move", "move",
         "Drive in a straight line. distance_ft ≈ feet to travel (one unit ≈ 1 ft); "
         "set backward=true to reverse. Forward moves abort if the path is blocked. "
         "Returns a fresh frame + telemetry.",
         {"type": "object",
          "properties": {"distance_ft": {"type": "number", "minimum": 0, "maximum": 20},
                         "backward": {"type": "boolean"}},
          "required": ["distance_ft"], "additionalProperties": False},
         _h_move),
    Verb("turn", "turn",
         "Rotate in place by a number of degrees (positive = right, negative = "
         "left) using heading feedback. Use this for ALL turns. Returns a fresh "
         "frame + telemetry.",
         {"type": "object",
          "properties": {"degrees": {"type": "number", "minimum": -360, "maximum": 360}},
          "required": ["degrees"], "additionalProperties": False},
         _h_turn),
    Verb("obstacle_check", "obstacle_check",
         "Check whether the path ahead is blocked (lidar/vision). Call before moving forward.",
         _NO_ARGS, _h_obstacle_check),
    Verb("track_color", "track_color",
         "Find and follow a colored target (e.g. 'yellow', 'red'). The flagship VLA demo.",
         {"type": "object", "properties": {"color": {"type": "string"}},
          "required": ["color"], "additionalProperties": False},
         _h_track_color),
    Verb("autonav", "autonav",
         "Hand off to the built-in safe-navigation loop for open-ended movement. "
         "action = start | stop | status. Use instead of stepping move-by-move.",
         {"type": "object",
          "properties": {"action": {"type": "string", "enum": ["start", "stop", "status"]}},
          "required": ["action"], "additionalProperties": False},
         _h_autonav),
    Verb("navigate", "navigate",
         "GPS guidance to the next mission checkpoint: returns distance (m), "
         "compass bearing, and the signed turn needed (+right/-left). Call before "
         "each move on a checkpoint mission, then turn toward the bearing and move; "
         "when it says within tolerance, call checkpoint_reached.",
         _NO_ARGS, _h_navigate),
    Verb("checkpoint_reached", "navigate",
         "Claim arrival at the next checkpoint (succeeds only within the ~15 m GPS "
         "tolerance). Call when navigate reports within tolerance.",
         _NO_ARGS, _h_checkpoint_reached),
    Verb("speak", "speak",
         "Speak text aloud through the rover's speaker (warnings, greetings).",
         {"type": "object", "properties": {"text": {"type": "string"}},
          "required": ["text"], "additionalProperties": False},
         _h_speak),
    Verb("set_lamp", "lamp",
         "Turn the headlamp/LED on or off (useful in low light).",
         {"type": "object", "properties": {"on": {"type": "boolean"}},
          "required": ["on"], "additionalProperties": False},
         _h_set_lamp),
    Verb("camera_move", "camera",
         "Aim the pan/tilt camera gimbal. pan/tilt in [-1,1] (pan: -1 left … "
         "+1 right; tilt: -1 down … +1 up). Use to look around without driving.",
         {"type": "object",
          "properties": {"pan": {"type": "number", "minimum": -1, "maximum": 1},
                         "tilt": {"type": "number", "minimum": -1, "maximum": 1}},
          "additionalProperties": False},
         _h_camera_move),
    Verb("capture_work", "_work",
         "Record Verifiable Robotic Work: capture the current frame, store it, and "
         "submit it to the on-chain ledger(s) as a completed task. Use when you "
         "finish an objective or observe something notable. Give a factual label; "
         "vrw_points reflects the work's value (default 100).",
         {"type": "object",
          "properties": {"label": {"type": "string"},
                         "vrw_points": {"type": "integer", "minimum": 0, "maximum": 10000}},
          "required": ["label"], "additionalProperties": False},
         _h_capture_work),
    Verb("finish", "_always",
         "End the run: objective complete, stuck and need a human, or told to stop. "
         "Provide success (bool) and a short reason.",
         {"type": "object",
          "properties": {"success": {"type": "boolean"}, "reason": {"type": "string"}},
          "required": ["success", "reason"], "additionalProperties": False},
         _h_finish),
]

_BY_NAME: dict[str, Verb] = {v.name: v for v in VERBS}

# Back-compat: the original schema-list shape (with the "_cap" tag).
TOOLS: list[dict[str, Any]] = [
    {"_cap": v.cap, "name": v.name, "description": v.description, "input_schema": v.schema}
    for v in VERBS
]


# --------------------------------------------------------------------------- #
# Derived: tool schemas + dispatch
# --------------------------------------------------------------------------- #
def make_tools(capabilities, has_work: bool = False) -> list[dict[str, Any]]:
    """Filter the registry to the robot's capabilities (+ finish, + work if wired)."""
    caps = set(capabilities) | {"_always"}
    if has_work:
        caps.add("_work")
    return [
        {"name": v.name, "description": v.description, "input_schema": v.schema}
        for v in VERBS if v.cap in caps
    ]


def dispatch(verbs: RoverVerbs, name: str, tool_input: dict[str, Any], work=None,
             resource_name: str | None = None) -> ToolOutcome:
    """Execute one verb tool and return content blocks for the tool_result."""
    verb = _BY_NAME.get(name)
    if verb is None:
        return ToolOutcome([_text(f"Unknown tool: {name}")], is_error=True)
    ctx = Ctx(work=work, resource_name=resource_name)
    try:
        return verb.run(verbs, tool_input, ctx)
    except EarthRoverError as e:
        _safe_stop(verbs)
        return ToolOutcome([_text(f"Error {e.status_code}: {e.detail}")], is_error=True)
    except Exception as e:  # defensive: never let one bad tool kill the loop
        _safe_stop(verbs)
        return ToolOutcome([_text(f"Tool failed: {e}")], is_error=True)


def _safe_stop(verbs: RoverVerbs) -> None:
    try:
        verbs.stop()
    except Exception:
        pass
