# Mini+ Agent Kit — System Architecture

> A reference architecture for driving a BitRobot-compatible ground robot (a
> FrodoBots **EarthRover Mini+** or a **Waveshare UGV**) with a large language
> model, and for converting its runs into on-chain **Verifiable Robotic Work**.
> All figures are Mermaid (render natively on GitHub) and were syntax-validated.

---

## Abstract

This document specifies the architecture of the Mini+ Agent Kit. The kit is
organised so that **one declarative description of the robot's verbs** drives
three interchangeable front-ends — an autonomous Claude agent, a Telegram chat
surface, and a Model Context Protocol (MCP) server — over **two interchangeable
robot back-ends**, and emits a single content-addressed work artifact to **one or
more on-chain ledgers**. The design deliberately conforms to three existing
specifications rather than inventing glue: the **LeRobot** robot interface, the
**openClaw** verb surface, and the **BitRobot** subnet (Verifiable Robotic Work)
API. We give the component model, the control-plane protocol, the kinematics and
GPS-navigation control laws (the EarthRover Challenge Urban track), the visual
servoing law (`track_color`), and the data-commitment pipeline, each with a
validated figure.

---

## 1. Design principles

1. **Single source of truth.** Every verb is declared once (name, capability,
   JSON schema, handler). The Anthropic tool schemas, the MCP tool list, and the
   dispatch table are all *derived* from that registry (§4).
2. **Conform, don't reinvent.** Robot ↔ LeRobot; agent ↔ openClaw verbs;
   on-chain work ↔ BitRobot VRW. Standard interfaces give ecosystem
   interoperability (datasets, MCP clients, subnet rewards) for free.
3. **Capability gating.** Each back-end advertises a capability set; a front-end
   is only ever offered the verbs its current robot can perform (§6).
4. **Fan-out at the edges, one core.** Many front-ends and many ledgers, but a
   single verb core and a single content-addressed artifact.

---

## 2. System context

The kit sits between a controller (a human, the Claude agent, an MCP client, or a
Telegram user) and the external services it integrates.

```mermaid
flowchart LR
  OP["Operator / Claude agent /<br/>MCP client / Telegram user"] --> KIT["Mini+ Agent Kit"]
  KIT --> ANTH["Anthropic Claude API"]
  KIT --> ROV["EarthRover Mini+ /<br/>Waveshare UGV"]
  KIT --> WAL["Walrus storage"]
  KIT --> BIT["BitRobot subnet API<br/>VRW &rarr; Bolts"]
  KIT --> SC["onchain-rover sidecar<br/>&rarr; Arc / Solana"]
```
*Figure 1 — System context.*

---

## 3. Layered architecture

Front-ends depend only on the verb core (`make_tools` / `dispatch`); the core
depends on the `RoverVerbs` abstraction; each backend resolves to a transport and
a physical robot. `capture_work` branches into the work layer.

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
*Figure 2 — Layered architecture.*

---

## 4. The verb registry (single source of truth)

Each verb is a `Verb(name, cap, schema, run)` record. `make_tools(capabilities,
has_work)` filters the registry to the back-end's capabilities and emits Anthropic
tool schemas; `dispatch(name, args)` looks up the handler. The MCP server reuses
exactly these two functions, so the agent, the chat surface, and any MCP client
share **one** definition — adding a verb is a single registry entry.

```mermaid
flowchart LR
  REG["VERBS registry<br/>Verb(name, cap, schema, run)"]
  REG --> MT["make_tools(caps, has_work)"]
  REG --> DP["dispatch(name, args)"]
  MT --> SCH["filtered tool schemas"]
  SCH --> AG["MiniPlusAgent"]
  SCH --> CH["RoverChat / Telegram"]
  SCH --> MS["MCP list_tools"]
  AG --> DP
  CH --> DP
  MS --> DP
```
*Figure 3 — One registry derives every front-end's tools and the dispatcher.*

---

## 5. Control plane: the agent loop

The autonomous agent runs a manual tool-use loop on the Anthropic Messages API.
Vision flows back through `tool_result` blocks so the model always reasons over the
robot's current frame. The loop terminates on a `finish` verb or `end_turn`, and
the robot is stopped on exit.

```mermaid
sequenceDiagram
  participant A as MiniPlusAgent
  participant M as Claude (Messages API)
  participant D as dispatch
  participant V as RoverVerbs
  participant R as Robot (SDK / harness)
  A->>R: initial observe (look + telemetry)
  A->>M: system prompt + tools + messages
  loop until finish or end_turn
    M-->>A: tool_use(name, input)
    A->>D: dispatch(verbs, name, input)
    D->>V: verb handler
    V->>R: HTTP (control / data / snapshot / turn ...)
    R-->>V: result + camera frame
    V-->>D: ToolOutcome(blocks)
    D-->>A: text + image content
    A->>M: tool_result
  end
  A->>R: stop()
```
*Figure 4 — Agent control loop (the Telegram and MCP front-ends reuse `dispatch`).*

---

## 6. Robot abstraction and kinematics

Both back-ends present the same verb surface; only the wire protocol differs. The
EarthRover SDK accepts a unicycle **twist** `(linear, angular)`; the Waveshare
ESP32 accepts **differential** wheel speeds. The kit converts:

$$\text{left} = \text{linear} - \text{angular}, \qquad \text{right} = \text{linear} + \text{angular}$$

which is the exact inverse of the harness adapter's `diffToTwist`
($\text{linear}=\tfrac{l+r}{2},\ \text{angular}=\tfrac{r-l}{2}$).

```mermaid
flowchart LR
  TW["twist<br/>(linear, angular)"] --> FN["twist_to_diff<br/>left = lin &minus; ang<br/>right = lin + ang"]
  FN --> DF["differential<br/>(left, right)"]
  DF --> ESP["ESP32 T:1 {L,R}<br/>Waveshare"]
  TW -. EarthRover .-> CT["SDK /control<br/>{linear, angular, lamp}"]
```
*Figure 5 — Twist↔differential; the EarthRover takes twist directly.*

### 6.1 Capability matrix

A front-end is offered a verb only if the active back-end advertises its
capability. `capture_work` additionally requires a configured `WorkSink`.

| Verb | EarthRover Mini+ | Waveshare UGV | Notes |
|---|:---:|:---:|---|
| `status_report` | ✓ | ✓ | real sensors, never fabricated |
| `look` | ✓ | ✓ | caption (Gemini optional) + frame |
| `photo` | ✓ | ✓ | JPEG bytes |
| `move` | ✓ | ✓ | distance-calibrated; aborts if lidar-blocked |
| `turn` | ✓ (server heading-feedback) | ✓ (client closed-loop yaw) | |
| `obstacle_check` | — | ✓ | lidar (UGV only) |
| `track_color` | ✓ (server VLA) | ✓ (client HSV servo, §8) | |
| `autonav` | ✓ | ✓ | built-in / lidar safe-forward |
| `navigate` | ✓ (GPS) | — | Urban-track waypoints (§7) |
| `checkpoint_reached` | ✓ | — | GPS missions |
| `speak` | ✓ | — | UGV has no TTS |
| `set_lamp` | ✓ (control lamp) | ✓ (ESP32 T:132) | |
| `camera_move` | — | ✓ (ESP32 T:133 gimbal) | |
| `capture_work` | ✓\* | ✓\* | \*requires a WorkSink |
| `finish` | ✓ | ✓ | always available |

---

## 7. GPS waypoint navigation (EarthRover Challenge — Urban track)

The Urban track is GPS-goal navigation with a **15 m** tolerance. Given the
rover's position $(\varphi_1,\lambda_1)$, heading $\psi$, and the next checkpoint
$(\varphi_2,\lambda_2)$, the kit computes (`geo.py`):

**Great-circle distance** ($R$ = mean Earth radius):
$$a=\sin^2\!\tfrac{\Delta\varphi}{2}+\cos\varphi_1\cos\varphi_2\sin^2\!\tfrac{\Delta\lambda}{2},\qquad d=2R\,\arcsin\sqrt{a}$$

**Initial bearing:**
$$\theta=\operatorname{atan2}\!\big(\sin\Delta\lambda\,\cos\varphi_2,\ \cos\varphi_1\sin\varphi_2-\sin\varphi_1\cos\varphi_2\cos\Delta\lambda\big)$$

**Signed heading error** (turn convention: $+$ = right), in $(-180°, 180°]$:
$$e=\big((\theta-\psi+540°)\bmod 360°\big)-180°$$

The `goto_checkpoint` controller turns to null $e$, then creeps forward, and
claims the checkpoint once $d \le 15\text{ m}$:

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
*Figure 6 — GPS waypoint controller. The LLM-agent variant instead calls
`navigate` for guidance and `look` to avoid obstacles GPS cannot see, then issues
`turn`/`move`/`checkpoint_reached` itself.*

The geometry is verified against the Berkeley→Stanford Marathon route
($d \approx 50$ km, $\theta \approx 171°$) and the controller is shown converging
to a checkpoint in a kinematic simulation over real HTTP (§13).

---

## 8. Visual servoing: `track_color`

The flagship "find and follow the coloured card" demo. On the Waveshare it is a
client-side loop (no server VLA): decode the JPEG, threshold in HSV, take the
blob centroid $x_f\in[0,1]$ and area fraction $A$, then steer proportionally.

With error $e = x_f - \tfrac{1}{2}$, gain $k_p$, base speed $v$:
$$\omega=\operatorname{clamp}(-k_p\,e,\,-1,\,1),\qquad u=v\big(1-\min(0.8,\,1.5|e|)\big)$$
Arrival when $A \ge A_\text{stop}$ (default $0.12$).

```mermaid
flowchart TD
  A["track_color(color)"] --> B["GET /camera/snapshot &rarr; JPEG"]
  B --> C["_detect_color: HSV mask &rarr;<br/>centroid x_frac, area_frac"]
  C --> F{blob found?}
  F -- no --> SR["control(0, search_angular)<br/>rotate to search"] --> B
  F -- yes --> AR{area &ge; stop_fill?}
  AR -- yes --> ST["stop() &mdash; arrived"]
  AR -- no --> E["err = x_frac &minus; 0.5"]
  E --> CMD["control(linear, &minus;kp&middot;err)<br/>steer to blob + creep forward"]
  CMD --> B
```
*Figure 7 — `track_color` visual-servo loop (validated on generated frames, §13).*

---

## 9. Verifiable Robotic Work (on-chain data)

A run produces an **artifact**: the camera frame is stored once on Walrus and
content-addressed (sha256 + IPFS CIDv1). The same artifact fans out to one or more
ledgers behind `MultiSink`.

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
*Figure 8 — One content-addressed artifact, multiple ledgers.*

The canonical BitRobot path is a four-event lifecycle culminating in network-wide
Bolts:

```mermaid
stateDiagram-v2
  [*] --> Registered: register_resource (Entity NFT on Solana)
  Registered --> Running: task_start
  Running --> Ended: task_end (raw_data_uri + IPFS CID)
  Ended --> Validated: task_validate (vrw_points)
  Validated --> SubnetPoints: instant, uncapped
  SubnetPoints --> Bolts: periodic conversion (pro-rata)
  Bolts --> [*]
```
*Figure 9 — BitRobot Verifiable Robotic Work lifecycle.*

| Sink | Endpoint(s) | Anchor |
|---|---|---|
| `BitRobotSink` | `POST /subnets/{id}/events` | Subnet Points → Bolts; resource = Entity NFT (Solana) |
| `OnchainRoverSink` | `POST /proof`, `POST /give-feedback` | `settle.giveFeedback` → `ReputationRegistry` (Arc) |
| `RaceProofSink` | `POST /race/settle` | `settle.settleRaceOnChain` → `RaceMarket` (Arc) |

`raw_data_uri` is the public Walrus URL; `raw_data_cid` is computed in-process
(`cid_v1_raw` for ≤ 1 MiB, the `ipfs` CLI for larger). sha256 is sent as bare hex
to `giveFeedback`/`settleRaceOnChain` (they re-add the `0x`).

---

## 10. Waveshare command stack

The kit talks HTTP to the Rust `robot-harness`, which owns the serial link and
emits the authoritative ESP32 JSON commands (verified against
`waveshareteam/ugv_base_general`; see `WAVESHARE_PROTOCOL.md`).

```mermaid
sequenceDiagram
  participant K as Kit (HarnessClient)
  participant H as robot-harness (Rust)
  participant E as ESP32 (lower computer)
  K->>H: POST /drive {token, left, right}
  H->>E: {"T":1,"L":left,"R":right} @ 115200
  K->>H: POST /light {on}
  H->>E: {"T":132,"IO4":pwm,"IO5":pwm}
  K->>H: POST /camera/move {pan, tilt}
  H->>E: {"T":133,"X":pan_deg,"Y":tilt_deg,...}
  E-->>H: feedback {"T":1001} / {"T":1002}
  K->>H: GET /telemetry
  H-->>K: TelemetryFrame (battery, yaw, lidar)
```
*Figure 10 — Host → harness → ESP32 command stack (pan ∈ [−180,180], tilt ∈ [−30,90]).*

---

## 11. Module dependency graph

```mermaid
graph TD
  geo["geo.py"] --> rover["rover.py"]
  client["client.py"] --> rover
  harness["harness_client.py"] --> rover
  client --> work["work.py"]
  rover --> tools["tools.py"]
  work --> tools
  tools --> agent["agent.py"]
  tools --> telegram["telegram.py"]
  tools --> mcp["mcp_server.py"]
  rover --> agent
  agent --> telegram
  harness --> lerobot["lerobot_backend.py"]
  agent --> cli["cli.py"]
  mcp --> cli
  telegram --> cli
```
*Figure 11 — Internal module dependencies (acyclic; `tools.py` is the hub).*

---

## 12. End-to-end scenario

A complete Urban-track checkpoint with on-chain settlement:

```mermaid
sequenceDiagram
  participant A as Claude agent
  participant V as EarthRoverVerbs
  participant S as Earth Rovers SDK
  participant W as Walrus
  participant B as BitRobot subnet
  A->>V: navigate()
  V->>S: GET /data + /checkpoints-list
  S-->>V: gps, heading, next checkpoint
  V-->>A: distance, bearing, turn
  A->>V: turn(bearing) then move()
  V->>S: POST /turn then /control
  A->>V: checkpoint_reached()
  V->>S: POST /checkpoint-reached
  A->>V: capture_work(label)
  V->>S: GET /v2/front frame
  V->>W: PUT blob
  W-->>V: blobId + IPFS CID
  V->>B: task_start, task_end, task_validate
  B-->>V: VRW points
```
*Figure 12 — Reach a GPS checkpoint, then post Verifiable Robotic Work.*

---

## 13. Verification

Two suites. The **hermetic** suite stubs `httpx`/`anthropic` for fast,
dependency-free, deterministic coverage. The **live** suite uses real libraries
and real I/O (a local HTTP server emulates the harness; Walrus is a public
testnet) — no robot or keys required.

```mermaid
flowchart TB
  subgraph H["Hermetic suite &mdash; 40 tests (stubbed httpx / anthropic)"]
    HU["units · geo · registry · verbs · work · tools<br/>agent-loop · mcp · telegram · actuators"]
  end
  subgraph L["Live suite &mdash; 5 tests (real I/O, no stubs)"]
    L1["harness: real httpx &harr; HTTP emulator"]
    L2["mcp: real protocol &harr; dispatch"]
    L3["track_color: real HSV on generated JPEG"]
    L4["navigate: real geo &harr; kinematic sim"]
    L5["walrus: real testnet store + retrieve"]
  end
```
*Figure 13 — Test topology.*

| Live test | What is exercised for real |
|---|---|
| harness | real `httpx` round-trip; twist→`0.4/0.4` on the wire; telemetry/lidar parse; JPEG bytes; closed-loop turn; `/light`; `/camera/move` mapping |
| mcp | real MCP `initialize → list_tools → call_tool` → `dispatch` → HTTP; `ImageContent` |
| track_color | real HSV detection + servo on real generated JPEGs (right-blob → turn-right; arrival stop) |
| navigate | real geo + the `goto_checkpoint` controller converging to a checkpoint (65 m → 9 m) in a 2-D kinematic sim |
| walrus | real testnet store + byte-identical retrieve + IPFS CIDv1 |

> **Scope of validation.** The plumbing, protocols, content-addressing, geometry,
> and the perception/control loops are exercised against real I/O or a simulator.
> On-hardware control-gain tuning, the Rust harness compile (`libudev-dev`), and
> keyed services (live Anthropic / FrodoBots SDK / BitRobot subnet / on-chain
> `giveFeedback`) remain validated against their documented contracts, not a live
> deployment.

---

## 14. Mapping to the Earth Rover Challenge

The kit is a drop-in **off-board policy** for the EarthRover Challenge: it speaks
the Remote Access SDK, accepts the live camera + GPS, and outputs directional
commands. The Urban track maps to `navigate` + `move`/`turn` + `checkpoint_reached`
(§7); the same harness supports a deterministic controller *and* a vision-aware
LLM-agent baseline. Difficulty × completion-time scoring is a property of the
mission; the kit provides the policy.

## References

- LeRobot — EarthRover Mini+ integration (HuggingFace).
- Earth Rovers SDK — `frodobots-org/earth-rovers-sdk` (openClaw branch).
- BitRobot subnet API — `docs.bitrobot.ai`.
- Earth Rover Challenge — `earth-rover-challenge.github.io` (IROS 2026).
- Waveshare ESP32 firmware — `waveshareteam/ugv_base_general` (see `WAVESHARE_PROTOCOL.md`).
