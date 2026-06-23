# Mini+ Agent Kit

Drive a **[BitRobot](https://bitrobot.ai)**-compatible ground robot with Claude — an
[Earth Rover Mini+](https://bitrobot.ai/miniplusagentkit) or a Waveshare UGV — and
turn its runs into **Verifiable Robotic Work** on-chain.

It's built on the three real specs in the BitRobot ecosystem, not bespoke glue:

| Layer | Conforms to | This kit |
|---|---|---|
| **Robot** | [LeRobot](https://huggingface.co/docs/lerobot/en/earthrover_mini_plus) robot interface (`action {linear_velocity, angular_velocity}`, observation features) | `WaveshareUGV` (LeRobot backend) ↔ upstream `EarthRoverMiniPlus` |
| **Agent** | [openClaw branch](https://github.com/frodobots-org/earth-rovers-sdk/tree/feature/openClaw) — instruction files + safe high-level **verbs** | `RoverVerbs` + `MiniPlusAgent` (Claude, Opus 4.8) |
| **Work** | [BitRobot subnet API](https://docs.bitrobot.ai) — Verifiable Robotic Work (`task_start/end/validate`) | `WorkSink`: `BitRobotSink` + your `OnchainRoverSink` + `RaceProofSink`, fan-out via `MultiSink` |

## Architecture

> 📐 Full system architecture with 13 validated diagrams, control equations, and
> the capability matrix: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

```
            instruction files (AGENTS/SOUL/IDENTITY/TOOLS) ── compose ──► system prompt
                                                                              │
 Claude (Opus 4.8) ──► openClaw VERBS (tools) ─────────────────────────────► RoverVerbs
   status_report · look · photo · move · turn · obstacle_check · track_color ·   │   │
   autonav · navigate · checkpoint_reached · speak · set_lamp · camera_move ·    │   │
   capture_work · finish                                                         │   │
                                                                ┌───────────────┘   └───────────────┐
                                                       EarthRoverVerbs                       HarnessVerbs
                                                  (delegates to openClaw                (closed-loop turn via yaw,
                                                   server /turn /track-color…)           lidar obstacle_check…)
                                                          │                                     │
                                                  EarthRoverClient                        HarnessClient
                                                  (FrodoBots SDK)                         (robot-harness, Waveshare)

 capture_work ─► store_artifact (Walrus URL + IPFS CID + sha256) ─► WorkSink
                                                                     ├─ BitRobotSink   → /subnets/{id}/events (VRW → Bolts)
                                                                     └─ OnchainRoverSink → sidecar /proof → settle.ts (Arc/Solana)
```

One agent, two robots (swap the client), one artifact, two ledgers.

```mermaid
flowchart TB
  subgraph FE["Front-ends (clients)"]
    AG["MiniPlusAgent<br/>Claude tool-use loop"]
    CH["RoverChat / TelegramBridge"]
    MC["MCP server<br/>any MCP client"]
  end
  subgraph CORE["Verb core — single source of truth"]
    REG["VERBS registry<br/>name · cap · schema · handler"]
    MT["make_tools(caps, has_work)"]
    DP["dispatch(verbs, name, args)"]
    REG --> MT
    REG --> DP
  end
  subgraph VB["RoverVerbs — openClaw verb surface"]
    ER["EarthRoverVerbs"]
    HV["HarnessVerbs"]
  end
  subgraph TR["Transports"]
    EC["EarthRoverClient<br/>FrodoBots SDK"]
    HC["HarnessClient<br/>robot-harness"]
  end
  subgraph WK["Work layer"]
    WS["WorkSink"]
    BR["BitRobotSink"]
    OR["OnchainRoverSink"]
    RP["RaceProofSink"]
    WS --- BR
    WS --- OR
    WS --- RP
  end
  AG --> MT
  CH --> MT
  MC --> MT
  AG --> DP
  CH --> DP
  MC --> DP
  DP --> ER
  DP --> HV
  ER --> EC --> MINI["EarthRover Mini+"]
  HV --> HC --> UGV["Waveshare UGV"]
  DP -. capture_work .-> WS
```

**GPS waypoint navigation** (Earth Rover Challenge — Urban track):

```mermaid
flowchart TD
  S["goto_checkpoint()"] --> N["navigate(): GPS + heading,<br/>next un-scanned checkpoint"]
  N --> D{all checkpoints<br/>scanned?}
  D -- yes --> OK["mission complete"]
  D -- no --> T{distance &le; 15 m?}
  T -- yes --> CR["checkpoint_reached()"]
  CR --> N
  T -- no --> H{abs heading_error<br/>&gt; 18&deg;?}
  H -- yes --> TU["turn(heading_error)"]
  H -- no --> MV["move(forward)"]
  TU --> N
  MV --> N
```

**Verifiable Robotic Work** — one content-addressed artifact, multiple ledgers:

```mermaid
flowchart TB
  CAP["capture_work / submit_work"] --> ART["store_artifact:<br/>Walrus blobId + IPFS CIDv1 + sha256"]
  ART --> MS["MultiSink (one artifact, many ledgers)"]
  MS --> BR["BitRobotSink"]
  MS --> OR["OnchainRoverSink"]
  MS --> RP["RaceProofSink"]
  BR --> E1["POST /subnets/{id}/events<br/>VRW points &rarr; Bolts"]
  OR --> E2["POST /proof + /give-feedback<br/>settle.giveFeedback &rarr; Arc"]
  RP --> E3["POST /race/settle<br/>settle.settleRaceOnChain &rarr; Arc"]
```

More figures (agent loop, kinematics, visual servo, VRW lifecycle, Waveshare
command stack, module graph, end-to-end, tests) in
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Why verbs, not raw control

The openClaw kit's key lesson: an agent should drive through **safe, high-level
verbs**, never raw `/control`. So `turn` uses heading feedback (and is the *only*
way to rotate), `move` is distance-calibrated and aborts on a blocked path,
`status_report` returns real sensors (never fabricated), and `track_color` /
`autonav` hand off to purpose-built loops — exactly the two flagship demos
("send stats via chat", "find and follow the yellow card"). The agent's behavior
comes from editable markdown in [`instructions/`](mini_plus_agent_kit/instructions/).

## Install

```bash
pip install -e .                  # core
pip install -e ".[mcp]"           # + expose the rover as an MCP server
pip install -e ".[track]"         # + Waveshare client-side track_color (Pillow+numpy)
pip install -e ".[lerobot]"       # + Waveshare LeRobot backend (datasets/policies)
pip install -e ".[vision]"        # + Gemini scene captions for the harness
cp .env.example .env              # fill in keys
```

You also need a robot backend running:
- **Earth Rover**: the Earth Rovers SDK (`feature/openClaw`) — `hypercorn main:app` (:8000)
- **Waveshare**: your `robot-harness` (or the sidecar adapter) (:8000)

## Quick start — Claude drives, work goes on-chain

```python
from mini_plus_agent_kit import HarnessClient, MiniPlusAgent, BitRobotSink, OnchainRoverSink, MultiSink

rover = HarnessClient("http://localhost:8000", speed_mode="medium")   # Waveshare
work  = MultiSink(BitRobotSink(), OnchainRoverSink())                 # VRW + your settle
agent = MiniPlusAgent(rover, work=work, resource_name="ugv_001", on_event=print)

result = agent.run(
    "Explore the room, find the package, capture_work it as proof, then finish. "
    "Check obstacles before moving forward."
)
```

Swap `HarnessClient(...)` for `EarthRoverClient(...)` and the same agent drives an
Earth Rover Mini+ (gaining `speak` and a server-side blocking `turn`). The toolset
auto-adapts to each backend's verb capabilities. `track_color` ("follow the yellow
card") works on both — server-side on the Earth Rover, and as a client-side HSV
visual-servo loop on the Waveshare (`[track]` extra: Pillow+numpy).

### Drive from any MCP client (the elegant core)

The bundled agent and Telegram bridge are conveniences — the **standard** way to
drive the rover is as an [MCP](https://modelcontextprotocol.io) server. Point any
MCP client (Claude Desktop, Claude Code, Cursor, the Agent SDK) at it and the
verbs become its tools — no kit-specific agent loop:

```bash
mpak mcp --backend waveshare         # stdio MCP server (verbs as tools)
```

```jsonc
// Claude Desktop / Code config
{ "mcpServers": { "rover": { "command": "mpak", "args": ["mcp", "--backend", "waveshare"] } } }
```

Crucially this isn't a fourth code path: the MCP tool list is `make_tools(...)` and
each call routes through `dispatch(...)` — **the same single source of truth** the
Claude agent and the Telegram chat use. One verb definition, three front-ends
(agent / chat / MCP), every robot backend.

### CLI

```bash
mpak mission "Find and follow the yellow card." --backend earthrover --bitrobot
mpak mission "Patrol and flag obstacles."        --backend waveshare  --onchain --bitrobot
mpak telegram --backend waveshare                # chat-drive the rover (openClaw demo)
mpak register ugv_001 --backend waveshare --owner <SOL>   # BitRobot Entity NFT
mpak mcp --backend waveshare                     # serve as an MCP server (see above)
mpak status            # telemetry + mission state
mpak checkpoints       # list mission checkpoints (Earth Rover)
mpak shot --map -o frames/                       # save camera frames
mpak speak "hello"     # text-to-speech (Earth Rover)
mpak teleop            # manual keyboard driving
```

### Chat surface (Telegram — the openClaw flagship demo)

`RoverChat` is a conversational agent over the same verbs; `TelegramBridge`
long-polls the Bot API and pipes messages through it, replying with text and
inline camera frames.

```python
from mini_plus_agent_kit import HarnessClient, TelegramBridge
bridge = TelegramBridge(HarnessClient("http://localhost:8000"),
                        token="<TELEGRAM_BOT_TOKEN>")
bridge.run_forever()      # "what's your status?" → status_report; "look" → photo inline
```

Or just `mpak telegram --backend waveshare` (reads `TELEGRAM_BOT_TOKEN` +
`ANTHROPIC_API_KEY`). Add `--bitrobot`/`--onchain` to record VRW from chat-driven runs.

## Verifiable Robotic Work

`capture_work` (or `submit_work`) stores the frame once and runs the task
lifecycle on whichever sink(s) you configured:

```python
from mini_plus_agent_kit import submit_work, BitRobotSink
rec = submit_work(BitRobotSink(), open("clip.jpg","rb").read(),
                  label="delivery @ door", vrw_points=120, resource_name="frodobot_001")
print(rec.artifact.ipfs_cid, rec.artifact.walrus_url)
```

- **`BitRobotSink`** → `register_resource` → `task_start` → `task_end {raw_data_uri, raw_data_cid}` → `task_validate {vrw_points}` (Subnet Points → Bolts; Entity NFT on Solana). `raw_data_uri` is the Walrus URL; `raw_data_cid` is computed (`cid_v1_raw` for ≤1 MiB, the `ipfs` CLI for larger).
- **`OnchainRoverSink`** → `task_end` posts `/proof {blobId, sha256, label}` (the tracker); `task_validate` posts `/give-feedback {robot, skill, score, blobId, sha256}`, which your sidecar (`index.ts:597`) anchors on Arc via `settle.giveFeedback` → `ReputationRegistry.giveFeedback`. The robot's on-chain `agentId` is resolved sidecar-side; key custody stays in the sidecar. `OnchainRoverSink(robot="guard", skill="deliver", score=None, anchor=True)` — VRW points map to the 0–100 reputation score unless you pass an explicit `score`; set `anchor=False` to register the proof without the chain write. (The kit sends the bare hex sha256 since `giveFeedback` re-adds `0x`.)
- **`RaceProofSink(winner_idx=..., race_id=None)`** → `task_validate` posts `/race/settle {raceId, winnerIdx, sha256, blobId}` → `settle.settleRaceOnChain` → `RaceMarket.settle` (judge = guard). Use when the agent is the race oracle and *its* captured finish frame should settle the parimutuel market (unlike `/race/finish`, which re-captures the guard's own photo). Needs the small `POST /race/settle` route added to the sidecar (mirrors `/give-feedback`). `race_id=None` lets the sidecar use its current `onChainRaceId`.

### Register a robot as an Entity NFT (earn VRW under its own resource)

```bash
mpak register ugv_001 --backend waveshare --owner <SOLANA_WALLET> --symbol UGV
```

```python
from mini_plus_agent_kit import BitRobotSink
sink = BitRobotSink(resource_subtype="waveshare_ugv", resource_name="ugv_001", owner="<sol>")
sink.register(symbol="UGV", description="Waveshare UGV", image="https://…/ugv.png")
# every subsequent submit_work/capture_work on `sink` attributes VRW to ugv_001
```

`BitRobotSink` carries `resource_name`/`resource_subtype` defaults so all work auto-attributes to the registered Entity NFT (Waveshare → `waveshare_ugv`, Earth Rover → `frodobot`).

## Earth Rover Challenge (Urban track) — Claude as a navigation policy

The [Earth Rover Challenge](https://earth-rover-challenge.github.io/) (IROS 2026)
runs off-board policies that take the rover's camera + GPS and drive to mission
checkpoints within a **15 m** tolerance, scored by difficulty × completion time —
pitting autonomous policies against human teleoperators (current AI ceiling ~57%).
This kit *is* a challenge-ready off-board policy (it speaks the same Remote Access
SDK), with real GPS waypoint navigation:

- `navigate` — great-circle distance, bearing, and signed turn to the next
  checkpoint from live GPS + heading (`geo.py`: haversine + initial bearing +
  heading error; verified against the Berkeley→Stanford route).
- `checkpoint_reached` — claim arrival within tolerance.
- `EarthRoverVerbs.goto_checkpoint()` — a deterministic turn-to-bearing + creep
  controller (the autonomous baseline), or let Claude drive via `navigate` +
  `look` + `move`/`turn` (the VLM-agent baseline — vision handles obstacles GPS
  can't see). See [`examples/earth_rover_challenge.py`](examples/earth_rover_challenge.py).
- `EarthRoverVerbs.goto_checkpoint_fused()` — the **closed-loop navigation stack**
  (`estimator.py` + `control.py`): a PI complementary heading filter with online
  gyro-bias estimation (a 1-axis Mahony filter), a **Kalman pose filter**
  (covariance-weighted GPS fusion + Mahalanobis outlier gating), pure-pursuit
  steering, and a safety envelope (battery / tilt / lidar time-to-collision),
  composed behind `NavController.step(telemetry) → twist`. In a noisy A/B sim
  *with GPS multipath spikes*, the bang-bang baseline *false-arrives 34.6 m* from
  the checkpoint (a miss at 15 m tolerance) while the fused stack *gates every
  outlier*, *truly arrives* (14.9 m), and tracks heading 2.2× better than the raw
  magnetometer. See [§7.1 of the architecture spec](docs/ARCHITECTURE.md#71-closed-loop-fused-navigation-sensor-fusion--pursuit--safety).
- `EarthRoverVerbs.goto_checkpoint_planned(costmap)` — the **global+local split**
  (Nav2-style): A\* over an inflated occupancy `Costmap` (`planner.py`) finds a path
  *around* obstacles, tracked by **regulated pure pursuit** (velocity-scaled
  lookahead + curvature/approach speed regulation). A straight-line seeker drives
  into anything between it and the goal; in the sim it ploughs through a building
  for 46 ticks while the planned route reaches the checkpoint with **0 incursions**.
  See [§7.2](docs/ARCHITECTURE.md#72-global-planning-around-obstacles-a--regulated-pure-pursuit).
- `NavController(use_dwa=True)` — a **Dynamic Window Approach** local planner
  (`control.py`): samples the reachable `(v, ω)` window, rolls each candidate out,
  discards colliding trajectories, and scores goal/clearance/speed to *steer around*
  a moving obstacle (the reactive layer beneath the global plan). In the sim a
  pedestrian crosses the corridor: plain pursuit closes to 0.16 m (collision) while
  DWA holds 2.87 m clearance and still reaches the goal.
  See [§7.3](docs/ARCHITECTURE.md#73-local-obstacle-avoidance-the-dynamic-window-approach).
- **Estimator refinements** ([§7.4](docs/ARCHITECTURE.md#74-estimator-refinements-course-fusion-mag-calibration-full-covariance)):
  speed-gated **GPS-course fusion** (down-weights a magnetically-disturbed compass —
  cuts heading RMSE 24.9° → 10.4° under a 25° hard-iron bias), **magnetometer
  hard/soft-iron calibration** (`MagnetometerCalibrator`, ellipsoid → sphere), and a
  full **2×2 covariance** pose filter with anisotropic process noise + GPS-latency
  rewind, and a full-ellipsoid **soft-iron calibration** (`fit_ellipsoid`, numpy)
  that recovers heading to ~0° on a *rotated* ellipsoid (vs 16.7° for diagonal).
- **Domain-randomized validation** (`sim.py`): a unified `RoverSim` with a fully
  parameterized `SensorModel` drives the **real `NavController`**; a Monte-Carlo over
  **150 randomized worlds** (GPS/IMU/odometry error drawn per trial) reaches the
  checkpoint within tolerance **99.3%** of the time — robustness across an error
  envelope, not one tuned point. See
  [§7.5](docs/ARCHITECTURE.md#75-closing-the-sim-to-real-gap-unified-simulator--domain-randomization).

The live tests drive a 2D kinematic rover sim over real HTTP to a GPS checkpoint
(`tests/live/test_live_navigate.py`), run the fused-vs-baseline A/B under sensor
noise + GPS multipath (`tests/live/test_live_navstack.py`), rescue a biased
magnetometer with GPS-course fusion (`tests/live/test_live_heading.py`), route around
an obstacle with A\* + regulated pursuit (`tests/live/test_live_planner.py`), and
dodge a moving pedestrian with DWA (`tests/live/test_live_dwa.py`).

## Waveshare as a LeRobot robot

```bash
lerobot-record --robot.type=waveshare_ugv --teleop.type=keyboard_rover \
    --dataset.repo_id=you/ugv-nav --dataset.single_task="Navigate"
```

`WaveshareUGV` presents the Mini+ action/observation schema (plus lidar) so
datasets and policies are cross-compatible with the upstream EarthRover Mini+.

## Layout

```
mini_plus_agent_kit/
  client.py          EarthRoverClient   — FrodoBots SDK transport (+ openClaw verbs)
  harness_client.py  HarnessClient      — Waveshare robot-harness transport
  rover.py           RoverVerbs         — openClaw verb surface (2 backends)
  estimator.py       HeadingFilter/PoseFilter/MagnetometerCalibrator — heading+pose fusion, mag cal
  control.py         NavController       — pursuit + RPP + DWA + PID + safety stack
  planner.py         Costmap + A*        — global path planning around obstacles
  sim.py             RoverSim + SensorModel — unified simulator for domain-randomized MC
  work.py            WorkSink           — BitRobot VRW + onchain-rover, artifacts, IPFS CID
  agent.py           MiniPlusAgent      — Claude loop + instruction-file prompt
  tools.py           verb tools + dispatch
  telegram.py        RoverChat + TelegramBridge — chat surface (openClaw demo)
  mcp_server.py      MCP server over the same make_tools()+dispatch() core
  lerobot_backend.py WaveshareUGV       — LeRobot robot (optional)
  instructions/      AGENTS/SOUL/IDENTITY/TOOLS.md
  cli.py             mpak
```

## Tests

A hermetic suite (no robot, no network, no real SDKs — `httpx`/`anthropic` are
stubbed) covers kinematics, telemetry mapping, IPFS CID, capability-filtered
tools, the openClaw verb→endpoint wiring, the navigation stack (filters,
controllers, safety, costmap + A* planner, closed-loop sim scenarios), all three
work sinks, and a full scripted agent-loop run:

```bash
python3 tests/run_all.py     # zero-dependency runner  → 63 passed
pytest tests/                # also works (conftest applies the same stubs)
```

And a **live** suite using real libraries and real I/O — no stubs (a local HTTP
server emulates the harness; Walrus is a public testnet; no robot or keys needed):

```bash
bash tests/live/run_live.sh   # installs real deps (httpx, mcp, Pillow, numpy) into .venv, then runs:
#  • real HarnessClient/HarnessVerbs over real sockets (twist→diff on the wire,
#    telemetry/lidar, JPEG bytes, closed-loop turn, /light, /camera/move mapping)
#  • the real MCP server: real protocol (initialize→list_tools→call_tool)→dispatch→HTTP
#  • real track_color: HSV blob detection + visual-servo steering on generated frames
#  • real GPS navigate: the goto_checkpoint controller reaches a checkpoint in a sim
#  • navstack A/B: fused NavController (Kalman + Mahalanobis gating) truly arrives and rejects
#    GPS multipath where the bang-bang baseline false-arrives 34.6 m out
#  • heading: speed-gated GPS-course fusion rescues a 25°-biased magnetometer (24.9°→10.4°)
#  • planner: A* over a costmap + regulated pure pursuit routes around a building (0 incursions)
#  • dwa: dynamic-window local planner steers around a moving pedestrian (holds clearance, arrives)
#  • montecarlo: real NavController over 150 domain-randomized worlds → 99.3% true-arrival
#  • ellipsoid: full hard+soft-iron mag calibration recovers heading on a rotated ellipsoid
#  • real Walrus testnet store + byte-identical retrieve + IPFS CIDv1
```

## Safety

`turn` uses heading feedback; `move` aborts on a lidar-blocked path; the agent is
prompted to avoid people/traffic/ledges and to `obstacle_check` before advancing;
the loop stops the robot on exit and on any verb error. This is research/hobbyist
tooling — keep a human in the loop.

## Sources

- [bitrobot.ai/miniplusagentkit](https://bitrobot.ai/miniplusagentkit) · [docs.bitrobot.ai](https://docs.bitrobot.ai)
- [Earth Rovers SDK — openClaw branch](https://github.com/frodobots-org/earth-rovers-sdk/tree/feature/openClaw)
- [LeRobot EarthRover Mini+](https://huggingface.co/docs/lerobot/en/earthrover_mini_plus)

MIT.
