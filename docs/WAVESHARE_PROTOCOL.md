# Waveshare UGV protocol — verified notes (cross-checked against 微雪 sources)

Research notes confirming the Waveshare UGV command protocol the kit relies on,
checked against Waveshare's own (Shenzhen, 微雪) firmware and host source — not
guesses. Relevant because the **Waveshare UGV is the real robot** behind the
`HarnessClient` backend.

## ⚠️ Two different command schemas — don't mix them

| Layer | Schema | Where | Used by |
|---|---|---|---|
| **ESP32 base firmware** (lower computer / 下位机) | `{"T":1,"L":..,"R":..}`, `{"T":13,"X":..,"Z":..}` | `waveshareteam/ugv_base_general` | your `robot/rover.py`, the Rust `robot-harness`, this kit |
| **ROS2 / NL host layer** (上位机 abstraction) | `{"T":1,"type":"drive_on_heading"\|"spin"\|"stop","data":N}` | `ugv_rpi` ROS2 tutorial | the Waveshare web/NL demo only |

The `drive_on_heading`/`spin` form seen in some Chinese tutorials is a **host-side
ROS2 abstraction**, *not* the wire protocol. The kit (and your harness) speak the
base firmware schema below.

## ESP32 base firmware command set (authoritative)

From `ugv_base_general/General_Driver/json_cmd.h` (UART/USB **115200 baud**):

| `T` | Constant | JSON | Meaning |
|---|---|---|---|
| 1 | `CMD_SPEED_CTRL` | `{"T":1,"L":<-1..1>,"R":<-1..1>}` | **Differential wheel speeds** — the drive path used by `HarnessClient` (twist→L/R via `twist_to_diff`) and `rover.py:drive()` |
| 13 | `CMD_ROS_CTRL` | `{"T":13,"X":<linear>,"Z":<angular>}` | ROS-style twist; firmware mixes X/Z → wheels (`rover.py:drive_xz()`) |
| 0 | — | `{"T":0}` | Emergency stop |
| 126 | `CMD_GET_IMU_DATA` | `{"T":126}` | IMU/attitude → roll/pitch/yaw frame (`rover.py:attitude()`) |
| 130 / 131 | `CMD_BASE_FEEDBACK` / `_FLOW` | `{"T":131,"cmd":1}` | One-shot / continuous telemetry feedback (`rover.py:enable_feedback()`) |
| 136 | `CMD_HEART_BEAT_SET` | `{"T":136,"cmd":<ms>}` | Motion failsafe timeout — motors stop if no command within window (`rover.py:set_heartbeat()`) |
| 138 | `CMD_SET_SPD_RATE` | `{"T":138,...}` | Global speed-rate scaling |
| 132 | `CMD_LED_CTRL` | `{"T":132,"IO4":<0-255>,"IO5":<0-255>}` | 12 V LED PWM (the "lamp"); `ledcWrite(constrain(v,0,255))` |
| 3 / -3 | `CMD_OLED_CTRL` | `{"T":3,"lineNum":n,"Text":"…"}` | OLED text / reset |
| 127–129 | `CMD_*_IMU_OFFSET` | — | IMU calibration |
| 900 | config | `{"T":900,"main":<1\|2\|3>,"module":<0\|1\|2>}` | Robot type (1=RaspRover, 2=UGV Rover, 3=UGV Beast); module (0=none,1=RoArm-M2,2=Camera PT) |
| 133 / 134 / 135 | `CMD_GIMBAL_CTRL_SIMPLE` / `_MOVE` / `_STOP` | `{"T":133,"X":<pan>,"Y":<tilt>,"SPD":..,"ACC":..}` | Camera pan-tilt (PT kits). **pan ∈ [-180,180], tilt ∈ [-30,90]** (`gimbalCtrlSimple` `constrainFloat`); 141 = `CMD_GIMBAL_USER_CTRL` |

Feedback frames stream back as `{"T":1001,...}` (IMU/odometry/voltage; `v` is
centivolts) and `{"T":1002,...}` (roll/pitch/yaw + quaternion) — parsed by
`rover.py` and by the Rust harness.

## What this means for the kit

- **Drive path is correct.** `twist_to_diff(linear,angular) → /drive {left,right} →
  ESP32 T:1` matches the authoritative `{"T":1,"L","R"}`. (T:13 X/Z is an
  alternative twist passthrough if you ever drive the harness via `drive_xz`.)
- **`set_lamp` → `POST /light` → ESP32 `T:132`** — wired. The harness `light`
  handler maps 0..1 brightness to the two IO4/IO5 PWM channels (0..255); the kit's
  `HarnessVerbs.set_lamp` / the `set_lamp` agent tool drive it.
- **Pan-tilt camera → `POST /camera/move` → ESP32 `T:133`** — wired, with the
  firmware-verified ranges: pan maps `[-1,1] → [-180,180]`; tilt maps so 0 = level,
  `+1 → 90` (up), `-1 → -30` (down), matching `gimbalCtrlSimple`'s `tilt ∈ [-30,90]`
  constraint (the firmware also `constrainFloat`s, so absolute `*_deg` is safe).

## Sources

- `waveshareteam/ugv_base_general` — ESP32 firmware (`General_Driver/json_cmd.h`)
- `waveshareteam/ugv_rpi` — Raspberry Pi host (`base_ctrl.py`)
- Waveshare Wiki (微雪): `waveshare.net/wiki/UGV_Rover`, `…/UGV_Beast`, `…/WAVE_ROVER`
- FrodoBots Earth Rover Mini+ (LeRobot integration) for the cloud-SDK robot
