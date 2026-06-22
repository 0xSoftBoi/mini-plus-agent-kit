"""Autonomous Claude-driven run on a FrodoBots Earth Rover Mini+ (openClaw branch).

The agent drives through openClaw verbs (status_report, turn, look, track_color,
autonav, speak) and can record Verifiable Robotic Work to the BitRobot subnet API.

Prereqs:
  1. Earth Rovers SDK (feature/openClaw) running locally:  hypercorn main:app (:8000)
  2. ANTHROPIC_API_KEY set.
  3. For VRW: BITROBOT_SUBNET_ID, BITROBOT_API_KEY, BITROBOT_OWNER set.

    python examples/autonomous_mission.py
"""

import os

from mini_plus_agent_kit import EarthRoverClient, MiniPlusAgent, BitRobotSink


def main() -> None:
    with EarthRoverClient(os.environ.get("ROVER_URL", "http://localhost:8000")) as rover:
        rover.start_mission()
        work = BitRobotSink() if os.environ.get("BITROBOT_API_KEY") else None
        agent = MiniPlusAgent(rover, work=work, resource_name="frodobot_001", on_event=print)
        result = agent.run(
            "Find and follow the yellow card, then describe where it leads. "
            "Capture work when you reach it. Finish when done."
        )
        print(f"\nsuccess={result.success} after {result.turns} turns: {result.reason}")


if __name__ == "__main__":
    main()
