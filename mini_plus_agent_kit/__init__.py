"""Mini+ Agent Kit — drive a BitRobot-compatible robot with Claude.

Three layers, each conforming to a real spec:

* **Robot** — LeRobot contract (``WaveshareUGV`` ↔ upstream ``EarthRoverMiniPlus``).
* **Verbs** — the openClaw safe-verb surface (``RoverVerbs``: status_report, turn,
  look, track_color, autonav, …) the Claude agent drives through.
* **Work** — Verifiable Robotic Work to chain via one ``WorkSink``
  (BitRobot subnet events + your onchain-rover settle).

See https://bitrobot.ai/miniplusagentkit, the openClaw branch of the Earth Rovers
SDK, docs.bitrobot.ai, and the LeRobot EarthRover Mini+ integration.
"""

from .client import EarthRoverClient, EarthRoverError, Telemetry
from .harness_client import HarnessClient, twist_to_diff
from .rover import (
    RoverVerbs, EarthRoverVerbs, HarnessVerbs, make_verbs, Scene,
)
from .work import (
    WorkSink, BitRobotSink, OnchainRoverSink, SolanaRoverSink, RaceProofSink, MultiSink,
    Artifact, WorkRecord, submit_work, store_artifact, walrus_put,
    ipfs_cid, cid_v1_raw, solana_explorer_tx,
)
from .agent import MiniPlusAgent, RunResult, load_system_prompt, DEFAULT_MODEL
from .observability import Run, get_logger
from .safety import SafetySupervisor, MissionLimits, Watchdog
from .tools import TOOLS, make_tools, dispatch
from .telegram import RoverChat, TelegramBridge, ChatReply
from .mcp_server import build_mcp_server, run_stdio, serve

__version__ = "0.2.0"

__all__ = [
    # transports
    "EarthRoverClient", "HarnessClient", "Telemetry", "EarthRoverError", "twist_to_diff",
    # verbs
    "RoverVerbs", "EarthRoverVerbs", "HarnessVerbs", "make_verbs", "Scene",
    # work / onchain
    "WorkSink", "BitRobotSink", "OnchainRoverSink", "SolanaRoverSink", "RaceProofSink", "MultiSink",
    "Artifact", "WorkRecord", "submit_work", "store_artifact", "walrus_put",
    "ipfs_cid", "cid_v1_raw", "solana_explorer_tx",
    # agent
    "MiniPlusAgent", "RunResult", "load_system_prompt", "DEFAULT_MODEL",
    # observability + safety
    "Run", "get_logger", "SafetySupervisor", "MissionLimits", "Watchdog",
    # chat surface
    "RoverChat", "TelegramBridge", "ChatReply",
    # MCP server (drive from any MCP client)
    "build_mcp_server", "run_stdio", "serve",
    # tools
    "TOOLS", "make_tools", "dispatch",
]


def WaveshareUGV(*args, **kwargs):  # lazy: avoid importing lerobot at package load
    """Construct the LeRobot Waveshare backend (requires ``lerobot``)."""
    from .lerobot_backend import WaveshareUGV as _W
    return _W(*args, **kwargs)


def WaveshareUGVConfig(*args, **kwargs):
    from .lerobot_backend import WaveshareUGVConfig as _C
    return _C(*args, **kwargs)
