"""Command-line interface for the Mini+ Agent Kit.

    mpak status                      # battery, GPS, signal, mission state
    mpak teleop                      # manual keyboard driving
    mpak shot [--map] [-o DIR]       # save current camera frames to disk
    mpak speak "hello"               # text-to-speech out of the rover
    mpak mission "<objective>"       # autonomous Claude-driven run
    mpak checkpoints                 # list mission checkpoints

Set ROVER_URL (default http://localhost:8000) and ANTHROPIC_API_KEY in the env.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from .client import EarthRoverClient, EarthRoverError


def _rover(args) -> EarthRoverClient:
    return EarthRoverClient(args.url)


def cmd_status(args) -> int:
    with _rover(args) as rover:
        telem = rover.data()
        print(telem.summary())
        try:
            hist = rover.missions_history()
            rides = hist.get("mission_rides", [])
            active = [r for r in rides if r.get("status") == "active"]
            if active:
                r = active[0]
                print(f"active mission: {r.get('mission_slug')} "
                      f"(checkpoint {r.get('latest_scanned_checkpoint')})")
            else:
                print("no active mission")
        except EarthRoverError as e:
            print(f"(mission history unavailable: {e.detail})")
    return 0


def cmd_checkpoints(args) -> int:
    with _rover(args) as rover:
        cps = rover.checkpoints()
        print(f"latest scanned: {cps.get('latest_scanned_checkpoint')}")
        for cp in cps.get("checkpoints_list", []):
            print(f"  #{cp.get('sequence')} id={cp.get('id')} "
                  f"({cp.get('latitude')}, {cp.get('longitude')})")
    return 0


def cmd_shot(args) -> int:
    out_dir = args.output
    os.makedirs(out_dir, exist_ok=True)
    with _rover(args) as rover:
        if args.map:
            shot = rover.screenshot(view_types=["front", "rear", "map"])
            frames = {"front": shot.get("front_frame"), "rear": shot.get("rear_frame"),
                      "map": shot.get("map_frame")}
        else:
            shot = rover.screenshot_v2()
            frames = {"front": shot.get("front_frame"), "rear": shot.get("rear_frame")}
        ts = int(shot.get("timestamp", time.time()))
        for label, frame in frames.items():
            if not frame:
                continue
            path = os.path.join(out_dir, f"{label}_{ts}.jpg")
            with open(path, "wb") as f:
                f.write(EarthRoverClient.decode_frame(frame))
            print(f"saved {path}")
    return 0


def cmd_speak(args) -> int:
    with _rover(args) as rover:
        rover.speak(args.text)
        print(f"spoke: {args.text!r}")
    return 0


def _build_work(args):
    """Assemble a WorkSink from --bitrobot / --onchain flags (or None)."""
    from .work import BitRobotSink, OnchainRoverSink, MultiSink

    sinks = []
    if args.bitrobot:
        sinks.append(BitRobotSink())  # reads BITROBOT_SUBNET_ID / BITROBOT_API_KEY
    if args.onchain:
        sinks.append(OnchainRoverSink())  # reads SIDECAR_URL
    if not sinks:
        return None
    return sinks[0] if len(sinks) == 1 else MultiSink(*sinks)


def cmd_register(args) -> int:
    """Register the robot as a BitRobot Entity NFT so it earns VRW under its own resource."""
    from .work import BitRobotSink

    subtype = args.subtype or ("waveshare_ugv" if args.backend == "waveshare" else "frodobot")
    sink = BitRobotSink(resource_subtype=subtype, resource_name=args.name)
    res = sink.register(args.name, owner=args.owner, symbol=args.symbol,
                        description=args.description, image=args.image)
    print(res)  # { resource_id, ent_address }
    return 0


def cmd_mission(args) -> int:
    # Imported lazily so `status`/`teleop`/`shot` work without the anthropic pkg.
    from .agent import MiniPlusAgent

    if args.backend == "waveshare":
        from .harness_client import HarnessClient
        rover = HarnessClient(os.environ.get("HARNESS_URL", args.url), speed_mode="medium")
    else:
        rover = _rover(args)
        if args.start:
            try:
                rover.start_mission()
                print("mission started")
            except EarthRoverError as e:
                print(f"could not start mission: {e.detail}", file=sys.stderr)
                return 1
    try:
        agent = MiniPlusAgent(
            rover,
            model=args.model,
            max_turns=args.max_turns,
            effort=args.effort,
            work=_build_work(args),
            resource_name=args.resource_name,
            on_event=lambda m: print(m, flush=True),
        )
        result = agent.run(args.objective)
        print("\n=== run complete ===")
        print(f"finished={result.finished} success={result.success} "
              f"turns={result.turns}\nreason: {result.reason}")
    finally:
        try:
            rover.close()
        except Exception:
            pass
    return 0 if result.success else 2


def cmd_telegram(args) -> int:
    """Run the Telegram chat bridge (the openClaw flagship demo)."""
    import os as _os
    from .telegram import TelegramBridge

    token = args.token or _os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("set TELEGRAM_BOT_TOKEN (or pass --token)", file=sys.stderr)
        return 1
    if args.backend == "waveshare":
        from .harness_client import HarnessClient
        rover = HarnessClient(_os.environ.get("HARNESS_URL", args.url), speed_mode="medium")
    else:
        rover = _rover(args)
    bridge = TelegramBridge(rover, token, work=_build_work(args),
                            resource_name=args.resource_name,
                            on_event=lambda m: print(m, flush=True))
    try:
        bridge.run_forever()
    finally:
        try:
            rover.close()
        except Exception:
            pass
    return 0


def cmd_mcp(args) -> int:
    """Serve the rover as an MCP server (drive from any MCP client)."""
    import os as _os
    from .mcp_server import serve

    if args.backend == "waveshare":
        from .harness_client import HarnessClient
        rover = HarnessClient(_os.environ.get("HARNESS_URL", args.url), speed_mode="medium")
    else:
        rover = _rover(args)
    try:
        serve(rover, transport="stdio", work=_build_work(args), resource_name=args.resource_name)
    finally:
        try:
            rover.close()
        except Exception:
            pass
    return 0


def cmd_teleop(args) -> int:
    print("Manual teleop. Commands: w/a/s/d move, x stop, l lamp, space stop, q quit.")
    print("Each move is a short burst then auto-stop.")
    step = args.step
    with _rover(args) as rover:
        lamp = 0
        try:
            while True:
                key = input("> ").strip().lower()
                if key in ("q", "quit", "exit"):
                    break
                if key in ("w", "s", "a", "d"):
                    linear = step if key == "w" else -step if key == "s" else 0
                    angular = step if key == "a" else -step if key == "d" else 0
                    rover.control(linear=linear, angular=angular, lamp=lamp)
                    time.sleep(args.burst)
                    rover.stop()
                elif key == "l":
                    lamp = 0 if lamp else 1
                    rover.set_lamp(bool(lamp))
                    print(f"lamp {'on' if lamp else 'off'}")
                elif key in ("x", "", "space"):
                    rover.stop()
                    print("stopped")
                else:
                    print("keys: w/a/s/d x l q")
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            rover.stop()
            print("\nstopped, exiting")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mpak", description="Mini+ Agent Kit CLI")
    p.add_argument("--url", default=os.environ.get("ROVER_URL", "http://localhost:8000"),
                   help="Earth Rovers SDK base URL (env ROVER_URL).")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("status", help="Show telemetry and mission state.")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("checkpoints", help="List mission checkpoints.")
    sp.set_defaults(func=cmd_checkpoints)

    sp = sub.add_parser("shot", help="Save current camera frames.")
    sp.add_argument("--map", action="store_true", help="Also capture the map frame.")
    sp.add_argument("-o", "--output", default="frames", help="Output directory.")
    sp.set_defaults(func=cmd_shot)

    sp = sub.add_parser("speak", help="Text-to-speech out of the rover.")
    sp.add_argument("text")
    sp.set_defaults(func=cmd_speak)

    sp = sub.add_parser("mcp", help="Serve the rover as an MCP server (drive from any MCP client).")
    sp.add_argument("--backend", default="earthrover", choices=["earthrover", "waveshare"])
    sp.add_argument("--bitrobot", action="store_true", help="Expose capture_work → BitRobot VRW.")
    sp.add_argument("--onchain", action="store_true", help="Expose capture_work → your sidecar.")
    sp.add_argument("--resource-name", default=None)
    sp.set_defaults(func=cmd_mcp)

    sp = sub.add_parser("telegram", help="Chat-drive the rover over Telegram (openClaw demo).")
    sp.add_argument("--backend", default="earthrover", choices=["earthrover", "waveshare"])
    sp.add_argument("--token", default=None, help="Bot token (or env TELEGRAM_BOT_TOKEN).")
    sp.add_argument("--bitrobot", action="store_true", help="Submit VRW to the BitRobot subnet.")
    sp.add_argument("--onchain", action="store_true", help="Anchor proofs via your sidecar.")
    sp.add_argument("--resource-name", default=None)
    sp.set_defaults(func=cmd_telegram)

    sp = sub.add_parser("teleop", help="Manual keyboard driving.")
    sp.add_argument("--step", type=float, default=0.6, help="Linear/angular magnitude.")
    sp.add_argument("--burst", type=float, default=0.5, help="Seconds per move burst.")
    sp.set_defaults(func=cmd_teleop)

    sp = sub.add_parser("register", help="Register the robot as a BitRobot Entity NFT (VRW resource).")
    sp.add_argument("name", help="Resource name, e.g. ugv_001.")
    sp.add_argument("--backend", default="waveshare", choices=["earthrover", "waveshare"],
                    help="Sets the default resource_subtype.")
    sp.add_argument("--subtype", default=None, help="Override resource_subtype.")
    sp.add_argument("--owner", default=None, help="Solana owner wallet (or env BITROBOT_OWNER).")
    sp.add_argument("--symbol", default="ROVER")
    sp.add_argument("--description", default="")
    sp.add_argument("--image", default="")
    sp.set_defaults(func=cmd_register)

    sp = sub.add_parser("mission", help="Autonomous Claude-driven run (openClaw verbs).")
    sp.add_argument("objective", help="Free-text objective for the agent.")
    sp.add_argument("--backend", default="earthrover", choices=["earthrover", "waveshare"],
                    help="earthrover = FrodoBots SDK; waveshare = robot-harness.")
    sp.add_argument("--start", action="store_true", help="Earth Rover: call /start-mission first.")
    sp.add_argument("--bitrobot", action="store_true",
                    help="Submit Verifiable Robotic Work to the BitRobot subnet API.")
    sp.add_argument("--onchain", action="store_true",
                    help="Register proofs with your onchain-rover sidecar (settle.ts).")
    sp.add_argument("--resource-name", default=None, help="Robot resource name for VRW.")
    sp.add_argument("--model", default=os.environ.get("MPAK_MODEL", "claude-opus-4-8"))
    sp.add_argument("--max-turns", type=int, default=60)
    sp.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    sp.set_defaults(func=cmd_mission)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except EarthRoverError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
