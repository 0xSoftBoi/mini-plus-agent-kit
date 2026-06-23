# Mini+ Agent Kit — Prioritized Improvement Roadmap

## 1. Executive summary

The Mini+ Agent Kit is an unusually complete and well-tested *codebase* — a stdlib-only navigation stack (`estimator.py`/`control.py`/`planner.py`/`sim.py`), a clean multi-backend transport split (`EarthRoverClient` vs `HarnessClient`), three front-ends (CLI/MCP/Telegram), a LeRobot recorder, and a four-sink onchain VRW layer — backed by a 66-test hermetic suite. But the system has two structural pathologies that dominate everything else. **First, its most sophisticated work is architecturally stranded:** the fused/planned closed-loop controllers, the DWA planner, and GPS-course fusion exist in `rover.py`/`control.py` but appear *nowhere* in `tools.py`, `cli.py`, or `mcp_server.py` (grep returns zero hits) — so the LLM agent is forced to drive bang-bang via open-loop `move`/`turn`, the exact controller the team's own docstrings say "false-arrives under GPS noise." **Second, a system that moves a physical robot and writes money/reputation on-chain has zero observability** (no `logging` call anywhere in the package) and one safety-critical numerical bug in the only safety check that *is* live (`SafetyEnvelope` TTC divides by a normalized 0..1 command, not m/s). The biggest leverage is therefore not adding features — it's **wiring up and instrumenting what already exists**: expose the proven controller as a verb, fix the TTC unit bug, raise the agent token ceiling, add CI, and add a no-hardware sim quickstart. Each is low effort and high impact.

## 2. Top 10 highest-leverage improvements (impact × effort)

| # | Title | Dim | Why it matters | Concrete first step | Effort | Impact |
|---|-------|-----|----------------|---------------------|--------|--------|
| 1 | **Fix SafetyEnvelope TTC to use real m/s** | Navigation | The only safety-critical numerical defect in the stack. `control.py:343` computes `ttc = lidar_front_m / linear_cmd` where `linear_cmd` is normalized 0..1, so the configured 1.5 s stop margin is not physical seconds and silently changes per platform (`v_scale_mps` 0.6 vs sim 1.2). | Pass `v_scale_mps=self.v_scale_mps` from `control.py:454` into `safety.check`; default arg to 1.0 so scalar callers are unchanged. Add the `lidar=1.2, cmd=1.0, v=0.6 → 2.0s` unit test. | low | high |
| 2 | **Expose the fused closed-loop controller as a `drive_to_checkpoint` verb** | Challenge | `goto_checkpoint_fused` (`rover.py:217-259`) is validated to arrive where bang-bang false-arrives, but `VERBS`/`make_tools` (`tools.py:185-275`) only offer `navigate`/`move`/`turn`. The repo's strongest autonomy code is dead from the agent's view; the challenge is scored on time × difficulty. | Add a `navigate`-capability verb delegating to `goto_checkpoint_fused` with a small `max_steps` slice so control returns to the agent periodically; default `RoverVerbs.drive_to_checkpoint()` raises 501. | low | high |
| 3 | **Raise agent `max_tokens` + handle `stop_reason=='max_tokens'`** | Agent/AI | `agent.py:91` caps at 4096 with adaptive thinking billed against it; a high-effort thinking turn can exhaust the budget and exit with `stop_reason='max_tokens'`, which falls into the generic else (`agent.py:103-108`) and wastes a turn — repeatedly, across 60 turns. | Bump to ~16k (or stream + 32-64k); add an explicit `elif stop_reason=='max_tokens'` branch that re-issues the turn instead of appending "use a tool." | low | high |
| 4 | **Add chat_id allowlist + rate limit to TelegramBridge** | Security | `poll_once` (`telegram.py:182-201`) dispatches every inbound message via `_chat_for` with zero authz; cli wires it to a real `HarnessClient` + `_build_work`. Any stranger who finds the bot can drive the physical robot and trigger onchain writes — the single most exploitable gap. | Add `allowed_chats` (env `TELEGRAM_ALLOWED_CHATS`), check in `poll_once` before `_chat_for`, fail closed when unset. Add `--allow-chat` CLI flag. | low | high |
| 5 | **Add GitHub Actions CI running the hermetic suite** | Testing/CI | No `.github/` exists (confirmed); both sibling repos ship `solana.yml`. `run_all.py` already `sys.exit(1)`s on failure and is dependency-free (`_bootstrap.py` stubs httpx/anthropic), so nothing today stops a commit breaking `test_navstack.py`. | Add `.github/workflows/ci.yml`: job 1 runs `python3 tests/run_all.py` on a python matrix; job 2 (push-to-main, non-blocking) runs `tests/live/run_live.sh`. | low | high |
| 6 | **Fix the IMU dict/array mismatch zeroing 6 of 10 observation channels** | ML/Data | `lerobot_backend.py:133-134` reads `imu.accel` as a dict, but the harness serializes `accel: [x,y,z]` arrays (`main.rs:327`); the `isinstance(...,dict)` guard falls back to `{}` and silently writes six constant-zero IMU columns into every episode — garbage for any IMU-conditioned policy. | Add the list-path branch: `accel = raw["imu"].get("accel") or [0,0,0]; accel_x=float(accel[0])`. Add a hermetic test feeding the real `main.rs` JSON shape. | low | high |
| 7 | **Add Anthropic prompt caching to system prompt + tools** | Performance | `agent.py:89` and `telegram.py:76` re-send the full 4-file system prompt + 15-verb tool schema uncached on every one of 60 turns. `cache_control` on the static prefix cuts per-turn input cost ~90% with no behavior change. | Pass `system` as content blocks with a trailing `cache_control:{type:'ephemeral'}` block; add `cache_control` to the last tool def. (Verify SDK floor — see #9.) | low | high |
| 8 | **Add a `mpak sim` / no-hardware quickstart** | DX | The prompt asks for a hardware-free quickstart; today every example and `mpak` subcommand needs a live backend + API key, yet `sim.run_scenario` (`sim.py:130`) is stdlib-only and drives the real `NavController`. New contributors can't watch the headline feature do anything. | Add a `mpak sim` subcommand calling `run_scenario(NavController(), RoverSim(seed=42), goal)`, printing arrival distance / heading RMSE; document above the existing quickstart. | medium | high |
| 9 | **Verify/align the Anthropic SDK call + raise the version floor** | DX | `pyproject.toml:13` pins `anthropic>=0.69` but `output_config.effort` + adaptive thinking landed later; a fresh `pip install -e .` can resolve an SDK that `TypeError`s on the first agent turn. The call block is also duplicated in `agent.py:89` and `telegram.py:80`. | Pin the floor to the actual release supporting these kwargs; factor a single `_create_kwargs(...)` helper imported by both call sites. | low | high |
| 10 | **Surface sink failures as `is_error` instead of swallowing `{'ok':False}`** | Onchain | `_h_capture_work` (`tools.py:158-171`) reports "Work submitted" success text regardless of whether `task_validate` actually anchored; a failed onchain write looks identical to a success to Claude, undermining the verifiable-work claim. | Add a `WorkRecord.ok` property folding per-stage results; in `_h_capture_work` set `is_error=True` when validate/end legs report `ok==False`. | low | medium |

## 3. Themed sections

### Navigation (`estimator.py`, `control.py`, `geo.py`)
- **Fix SafetyEnvelope TTC to use real m/s** — divide by `linear_cmd * v_scale_mps`, not normalized cmd (`control.py:343,454`). *[#1]*
- **Guard `PoseFilter.correct_gps` latency rewind against a short displacement buffer** — `estimator.py:153` slices `self._disp[-age_steps:]` which silently returns fewer entries before the buffer fills; clamp or skip and increment an underflow counter. RoverSim draws `gps_latency_steps` up to 4, hitting this early in every Monte-Carlo run.
- **Centralize lat/lon→ENU projection in `geo.py`** — the same `_M_PER_DEG_LAT * cos(base_lat)` math is reimplemented three times (`estimator.py:122,143,193`; `sim.py:65,80,83`); a third caller with a different `base_lat` would silently produce a wrong-frame goal. Add `geo.enu`/`geo.enu_inv` with a round-trip test.

### Agent / AI core (`agent.py`, `tools.py`)
- **Raise `max_tokens` + handle `stop_reason=='max_tokens'`** (`agent.py:91,103-108`). *[#3]*
- **Prune history / drop stale base64 image blocks** — `_observe_blocks` (`tools.py:49-64`) embeds a full JPEG per movement verb; `agent.py:101,126` are append-only. Slide a window keeping the last 1-2 frames; add `cache_control` on the byte-stable system prompt.
- **Quarantine untrusted perception text** — fence captions/status/obstacle replies (`tools.py:54,90-92,120-122`) in `<observation source=...>` envelopes; add an operator-authority rule to AGENTS.md.
- **Enforce the obstacle pre-check in `EarthRoverVerbs.move()`** — `rover.py:124-132` has no forward guard (vs `HarnessVerbs.move` lidar check at `rover.py:342`); gate the move on telemetry and scope `obstacle_check` wording to lidar platforms.
- **Make the loop resilient to a hard API failure** — wrap `client.messages.create` (`agent.py:89`) in try/except that breaks with `result.reason`, so `verbs.stop()` + history preservation at `agent.py:130-134` still run.

### Transports (`client.py`, `harness_client.py`)
- **Gate closed-loop driving on telemetry freshness** — port `TELEMETRY_STALE_MS=1600` from `robot-link.ts`; add `Telemetry.age_s()` and `HarnessClient.data(max_age_s=1.6)`, stop + abort on stale. The Python mirror dropped this safety property entirely.
- **Add bounded retry + transient/fatal classification to `_request`** — both clients are single-shot (`client.py:148`, `harness_client.py:84`); retry only GET/auth and 5xx/429, never non-idempotent `/drive`.
- **Surface `_ensure_session` re-auth failure** — `harness_client.py:114` is a bare `pass`; set a `_session_ok` flag + log, and keep `estop()` token-less.
- **Derive `Telemetry.from_harness` speed from odometry** — `client.py:82` uses `(left_cmd+right_cmd)/2`; prefer `odometry_left/right` (`harness-bridge.ts:351`) with a `speed_is_estimated` flag.
- **Detect cv2/numpy at `connect()`, not mid-recording** — `lerobot_backend.py:108` never checks cv2; the lazy import at `:163` crashes on the first frame after the robot is already moving.

### Onchain work (`work.py`)
- **Surface sink failures as `is_error`** (`tools.py:158-171`). *[#10]*
- **Add bounded retry/backoff to `walrus_put` + sidecar POSTs** — `work.py:115` is single-shot; `walrus_put` is idempotent (alreadyCertified, `work.py:122`) so safe to retry today; gate `task_validate` retries on idempotency.
- **Isolate `MultiSink` fan-out failures** — `work.py:471-481` list comprehensions let `BitRobotSink._event` (`work.py:201`, raises) abort the Solana leg; wrap each sink call in try/except.
- **Resolve the `SIDECAR_URL` collision** — both sidecars default to port 4021 and identical routes; require `ARC_SIDECAR_URL`/`SOLANA_SIDECAR_URL` and add a chain-tag assertion on first use.

### ML / Data (`lerobot_backend.py`)
- **Fix the IMU dict/array mismatch** (`lerobot_backend.py:133`). *[#6]*
- **Record a single observation timestamp + enforce camera/telemetry alignment** — `get_observation` (`:124-145`) makes two sequential httpx calls with no shared timestamp.
- **Add `lidar_blocked` + `estop` to the observation space** — both are parsed (`client.py:91-93`) but omitted from `observation_features` (`:88-95`).
- **Record executed actions, not requested** — `send_action` (`:147`) returns locally-computed `left/right` not in the declared schema and pre-cap; drop them or propagate the harness-applied differential.
- **Replace command-average `speed` with real velocity** — use `odometry_left/right` from `Telemetry`, or rename to `commanded_speed`.
- **Add GPS to the observation space** — confirmed: `observation_features` (`:91-95`) omits `latitude`/`longitude` though `Telemetry` carries them, so a recorded dataset cannot train a *navigation* policy. (Gap not in original list; fold into the same schema revision.)
- **Provide a FrodoBots-2K → LeRobot converter** — no converter/loader/policy exists in the package; share one feature-schema constant between the recorder and converter so live and offline episodes co-train.
- **Add a recording-quality preflight** — `_decode_jpeg_b64` (`:161-169`) silently returns `np.zeros`; the bare `assert` at `:125` vanishes under `-O`.

### Security
- **Camera captions/telemetry injected unsanitized** — `tools.py:55` embeds the VLM/SDK caption verbatim; wrap in a trust-labeled envelope + AGENTS.md rule (same fix as agent-core injection item).
- **No e-stop verb** — `HarnessClient.estop()` (`harness_client.py:135`, in capabilities at `:62`) is unreachable; `HarnessVerbs.capabilities` omits it and `_safe_stop` only sends a non-latching zero twist. Add an `estop` Verb and prefer it in the error path.
- **TelegramBridge has no authorization** (`telegram.py:182`). *[#4]*
- **Onchain write authority has no spend/rate ceiling** — `_h_capture_work` lets the model pick `vrw_points` up to 10000 (`tools.py:265`) with no per-run cap or human confirmation; clamp server-side and default sinks to `anchor=False`.
- **Bot token embedded in URL path leaks via exception logging** — `telegram.py:118` builds `bot{token}`; httpx errors embed the URL into `str(e)` logged at `:199,211`. Redact before logging.
- **Soft input validation** — `_h_move` does `float(args["distance_ft"])` (`tools.py:109`) with no `isfinite`/clamp before `ticks = round(abs(distance_ft)/FT_PER_TICK)` (`rover.py:126`) — `1e9` → billions of drive bursts. Enforce hard clamps in handlers.
- **Sidecar onchain POSTs have no auth header** — `work.py:285-311` plain POSTs to `SIDECAR_URL`; add a shared-secret header (sidecar-side change required to enforce).
- **Broad exception swallowing hides safety failures** — `_ensure_session` `pass`, `close`/`stop` swallows, `_h_capture_work` no-error path; surface them.

### Testing / CI
- **Add a GitHub Actions CI workflow** (no `.github/` confirmed). *[#5]*
- **Adopt pytest + coverage with a floor** — `pyproject.toml` has no test/dev extras; replace the unenforced "66 tests" prose (README:357, ARCHITECTURE:685) with a `test_meta.py` asserting `collected >= 66`.
- **Add hypothesis property tests for `geo.py`/`estimator.py`** — pure-math, ideal for fuzzing the heading-wrap and the `correct_gps` buffer-not-full boundary that example tests can't reach.
- **Add a multi-seed Monte-Carlo guard** — `test_sim.py` aggregates to a single `rate>=0.90` with no per-seed failure detail; print offending seeds and add a determinism guard.
- **Make `tests/live/` discoverable by pytest** — no `conftest.py`; files define `main()` not `test_*`, so `pytest tests/` silently skips the whole integration layer.

### Performance
- **Wall-clock-compensate the control loop** — `rover.py:244-257,293-304` use `sleep(dt)` + RTT so real period ≠ `dt=0.25` fed to `nav.step`, biasing dead-reckoning. Use `time.monotonic()` deltas.
- **Pipeline Walrus upload + onchain writes off the turn** — `submit_work` (`work.py:497`) blocks the motionless robot for >1 min; thread the CID compute alongside the PUT and fan `MultiSink` posts concurrently.
- **Add prompt caching** (`agent.py:89`, `telegram.py:76`). *[#7]*
- **Stop re-attaching full base64 frames** — quadratic re-upload of stale images every turn; elide image data from all but the last K tool_results in place.
- **Cut DWAPlanner per-step cost** — 1330 Python `min()` clearance evals/tick (`control.py:226`); vectorize + branch-and-bound. *Note: only active when `use_dwa=True`, which no current loop sets — fix #2 plumbing first.*
- **Coalesce redundant telemetry fetches** — `HarnessVerbs.move`/`track_color` issue multiple `data()` + snapshot calls per tick (`rover.py:336-352,437-461`); single-flight + concurrent fetch.

### Challenge competitiveness
- **Expose `drive_to_checkpoint` verb** (`rover.py:217`). *[#2]*
- **Return structured navigation numbers from `navigate`** — `_h_navigate` (`tools.py:135`) throws away `distance_m`/`bearing_deg`/`heading_error_deg`/`within_tolerance` and returns only prose; emit the JSON too.
- **Add stuck-detection + recovery to the open-loop move path** — `EarthRoverVerbs.move` always returns `ok:True`; sample GPS before/after and return `no_progress` (conservative epsilon).
- **Fix the Earth Rover obstacle path** — drop the unavailable `obstacle_check` from AGENTS.md for that platform; wire `EarthRoverClient.obstacle_alert()` (`client.py:321`) as a verb.
- **Add checkpoint re-acquisition** — `_h_checkpoint_reached` (`tools.py:139`) returns the SDK reply verbatim with no accept/reject parse; re-seek on a rejected claim (also fix `RegulatedPurePursuit.step` returning `arrived=True` on empty path, `control.py:161`).
- **Make the navigate target selectable** — `navigate` is hard-wired to the next sequence (`rover.py:172`); add `list_checkpoints` for time-vs-difficulty routing (verify SDK ordering rules first).
- **Bound agent message history** (shared with the agent-core/perf pruning item).

### DX / Docs
- **Add `mpak sim` no-hardware quickstart** (`sim.py:130`). *[#8]*
- **Verify/align the Anthropic SDK call + raise floor** (`pyproject.toml:13`). *[#9]*
- **Make every example runnable + fix the `cmd_mission` NameError** — confirmed: `result` is assigned at `cli.py:137` inside `try` but read at `:146` after the `finally`; any `agent.run()` exception throws `NameError` masking the real error. Init `result=None`; add `ANTHROPIC_API_KEY`/backend preflight; wire `--solana`/`--race` into `_build_work` (`cli.py:85-96`).
- **Ship a `py.typed` marker** — the package is fully annotated but PEP 561 hides all hints from downstream type-checkers; touch `mini_plus_agent_kit/py.typed` + add to package-data.
- **Expose MCP resources/prompts** — `mcp_server.py:64-80` registers only tools; add `rover://telemetry`, `rover://camera/front` resources and a `pilot` prompt.
- **Stop asserting unenforceable test/benchmark numbers** in README/ARCHITECTURE; pin seeds for the benchmark figures.

### Cross-cutting / systemic (from the completeness critique — currently unrepresented as discrete items)
- **Observability layer (confirmed: zero `logging` in the package).** A physical, value-bearing system has no structured event log, run-manifest (objective → verb trace → artifacts → tx hashes), or counters (loop Hz, GPS reject rate, API latency). Nothing is auditable after a misbehaving mission or a disputed onchain write. *High systemic leverage.*
- **The nav stack is fed degraded inputs even where reachable.** `goto_checkpoint_fused` callers pass only `t.orientation` (`rover.py:248,297`); the course/odometry/age-step inputs `NavController.step` accepts are supplied *only* inside `sim.py`. The sim validates a richer controller than the robot path can feed.
- **No safety-supervisor / dead-man watchdog.** Safety is scattered (lidar check in one `move` only, `SafetyEnvelope` inside the unreachable `NavController`, soft `_safe_stop`). A hung synchronous `messages.create` leaves a moving robot with no supervisor; no geofence, max-runtime, or max-distance budget.
- **Single-robot concurrency is undefined.** `TelegramBridge` spins a `RoverChat` per chat_id, all driving one client with no lock/lease; MCP + CLI mission + Telegram can interleave `control()` on the same motors.
- **Onchain durability/idempotency/reconciliation.** `_pending` is in-memory; a crash between `task_end` and `task_validate` orphans the proof with no resume, no dedup key, and no "artifact → tx → confirmed" ledger. Cross-chain partial-failure (Arc anchors, Solana fails) has no defined consistency story.
- **No agent-as-policy eval harness, no sim2real fidelity, no ops packaging** (no Dockerfile/compose/health endpoint; siblings ship these). The "57% AI ceiling / challenge-ready policy" claim has no measured success-rate or time-to-checkpoint benchmark, and `RoverSim` models Gaussian sensor noise but not the latency/dropout/slip/speed-cap that actually break a cloud robot.

## 4. Quick wins (high impact / low effort)

1. **TTC unit fix** — one-line call-site change + one test (`control.py:454`). *[#1]*
2. **`max_tokens` bump + `stop_reason` branch** — ~2 lines (`agent.py:91,103`). *[#3]*
3. **Telegram chat_id allowlist** — constructor arg + one check before `_chat_for`. *[#4]*
4. **CI workflow** — one `ci.yml`; `run_all.py` is already a valid gate. *[#5]*
5. **IMU array-path branch** — list-path parse in `get_observation` (`lerobot_backend.py:133`). *[#6]*
6. **Prompt caching** — `cache_control` on system + last tool (`agent.py:89`). *[#7]*
7. **`cmd_mission` NameError** — `result=None` before the try (`cli.py:137`). 
8. **Sink failure → `is_error`** — `WorkRecord.ok` fold + check (`tools.py:158`). *[#10]*
9. **`drive_to_checkpoint` verb** — wraps existing `goto_checkpoint_fused`. *[#2]*
10. **`py.typed` marker** — two-file change.
11. **Structured `navigate` output** — emit the JSON dict already computed (`tools.py:135`).
12. **SDK version floor + dedup the create() call** — `pyproject.toml:13`. *[#9]*

## 5. Needs hardware to validate (honest sim-to-real boundary)

The hermetic suite and `RoverSim` cannot exercise these — `RoverSim` is a 2-D kinematic point model with Gaussian sensor noise and **does not model** comms latency/jitter, dropped frames, wheel slip, actuator deadband, harness speed-mode capping, lidar dropout, or GPS outage. Validate the following on a real robot (or against recorded telemetry replay):

- **Telemetry-freshness gating** (`TELEMETRY_STALE_MS`) — the stale-frame failure mode (USB/WS driver hang) only occurs on hardware; sim always produces fresh frames.
- **Wall-clock control-loop compensation** — the RTT-inflated `dt` mismatch the fix targets is exactly the path the fixed-dt sim cannot reproduce; add a jitter-injecting sim test as a proxy, but final validation is on-robot.
- **`drive_to_checkpoint` blocking behavior** — `max_steps` slicing and the EarthRover `lidar_front_m=None` branch behave differently on real hardware; the 100 s worst-case block needs field confirmation.
- **Odometry-based speed** (`from_harness`) — odometry units/scale vs the `[-1,1]` command space must be checked against a live harness frame before trusting the swap.
- **Open-loop move calibration** (`FT_PER_TICK`, `DEFAULT_LINEAR`, stuck-detection epsilon) — distance-per-tick and the GPS-noise epsilon are physical constants only measurable on the robot.
- **Executed-vs-requested action recording** — confirming the harness speed-mode cap diverges from the commanded differential requires reading back actual applied motion.
- **e-stop latching + recovery** — `estop`/`estop_reset` round-trip and whether token-less safety stops are accepted is harness-dependent.
- **Onchain sink end-to-end** (`walrus_put`, sidecar POSTs, chain-tag assertions) — needs the live sidecars (port 4021 collision, auth headers) and Walrus/Arc/Solana testnets; the sibling sidecars were not present in this checkout to verify endpoint behavior.
- **Prompt-injection resistance** — the data-fencing mitigation reduces but does not eliminate the risk; only a live VLM caption + model run confirms whether Opus 4.8 obeys the envelope semantics.

Relevant files (absolute): `/home/mongolraider/onchain/mini-plus-agent-kit/mini_plus_agent_kit/{control.py,estimator.py,geo.py,rover.py,tools.py,agent.py,telegram.py,client.py,harness_client.py,work.py,lerobot_backend.py,mcp_server.py,cli.py,sim.py}`; tests at `/home/mongolraider/onchain/mini-plus-agent-kit/tests/`; CI to be created at `/home/mongolraider/onchain/mini-plus-agent-kit/.github/workflows/ci.yml`.
