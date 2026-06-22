"""Drive the Waveshare UGV with Claude, recording Verifiable Robotic Work.

Same agent + verbs as the Earth Rover — only the transport and the work sinks
change. The agent drives through openClaw verbs (status_report, turn, look,
obstacle_check, autonav) over the robot-harness, and `capture_work` submits the
result to BOTH ledgers behind one WorkSink: BitRobot's subnet VRW API and your
onchain-rover settle flow.

Env: HARNESS_URL, SIDECAR_URL, ANTHROPIC_API_KEY,
     BITROBOT_SUBNET_ID, BITROBOT_API_KEY, BITROBOT_OWNER (Solana wallet).

    python examples/waveshare_mission.py
"""

import os

from mini_plus_agent_kit import (
    HarnessClient, MiniPlusAgent, BitRobotSink, OnchainRoverSink, MultiSink,
)


def main() -> None:
    rover = HarnessClient(os.environ.get("HARNESS_URL", "http://localhost:8000"),
                          speed_mode="medium")
    # Both ledgers, one artifact: BitRobot VRW points + an on-chain Arc reputation
    # write (settle.giveFeedback) via your sidecar.
    work = MultiSink(BitRobotSink(), OnchainRoverSink(robot="guard", skill="deliver"))
    try:
        agent = MiniPlusAgent(rover, work=work, resource_name="ugv_001", on_event=print)
        result = agent.run(
            "Explore the room and find the package on the floor. Check obstacles "
            "before moving. When you reach the package, capture_work it as proof, "
            "then finish."
        )
        print(f"\nsuccess={result.success} after {result.turns} turns: {result.reason}")
    finally:
        rover.close()


if __name__ == "__main__":
    main()
