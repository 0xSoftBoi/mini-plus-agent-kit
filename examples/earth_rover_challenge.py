"""Earth Rover Challenge (Urban track) — Claude as a GPS-waypoint navigation policy.

The challenge: an off-board policy that takes the rover's camera + GPS and drives
to mission checkpoints (15 m tolerance), scored by difficulty × completion time.
This kit runs off-board against the Earth Rovers SDK, so it's a drop-in policy.

Two baselines:
  1. Deterministic GPS controller (`goto_checkpoint`) — turn-to-bearing + creep.
  2. LLM agent — Claude uses `navigate` (GPS guidance) + `look` (vision) + `move`
     /`turn`/`checkpoint_reached`, so it can also avoid obstacles the GPS can't see.

    ROVER_URL=http://localhost:8000 python examples/earth_rover_challenge.py
"""

import os

from mini_plus_agent_kit import EarthRoverClient, EarthRoverVerbs, MiniPlusAgent


def deterministic(verbs: EarthRoverVerbs):
    """Pure GPS controller — no LLM. Visits every checkpoint in sequence."""
    while True:
        res = verbs.goto_checkpoint(max_steps=300)
        print("checkpoint:", res)
        if res.get("done") or not res.get("ok"):
            break


def llm_agent(rover):
    """Claude-driven: vision + GPS guidance. The novel VLM baseline."""
    MiniPlusAgent(rover, on_event=print).run(
        "Complete the checkpoint mission. For each checkpoint: call navigate for "
        "the bearing and distance, turn toward the bearing, move forward, and "
        "re-check; look to avoid obstacles; call checkpoint_reached when navigate "
        "says you're within tolerance. Finish when all checkpoints are scanned."
    )


def main():
    with EarthRoverClient(os.environ.get("ROVER_URL", "http://localhost:8000")) as rover:
        rover.start_mission()
        verbs = EarthRoverVerbs(rover)
        if os.environ.get("MPAK_LLM"):
            llm_agent(rover)          # needs ANTHROPIC_API_KEY
        else:
            deterministic(verbs)


if __name__ == "__main__":
    main()
