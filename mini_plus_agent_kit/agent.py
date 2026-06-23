"""Claude agent that pilots a robot through the openClaw verb surface.

Design follows the BitRobot Mini+ openClaw kit: the agent's behavior comes from
markdown **instruction files** (AGENTS/SOUL/IDENTITY/TOOLS), and it drives the
robot through safe high-level verbs (:mod:`mini_plus_agent_kit.tools`) over a
backend-agnostic :class:`~mini_plus_agent_kit.rover.RoverVerbs`. Completed work is
recorded as Verifiable Robotic Work via an optional
:class:`~mini_plus_agent_kit.work.WorkSink`.

Model: Opus 4.8, adaptive thinking.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import anthropic

from .observability import Run, get_logger
from .rover import RoverVerbs, make_verbs
from .safety import MissionLimits, SafetySupervisor, Watchdog
from .tools import dispatch, make_tools, _observe_blocks

DEFAULT_MODEL = "claude-opus-4-8"
_INSTRUCTIONS_DIR = Path(__file__).parent / "instructions"
_INSTRUCTION_ORDER = ["IDENTITY.md", "SOUL.md", "AGENTS.md", "TOOLS.md"]


def load_system_prompt(extra: str | None = None) -> str:
    """Compose the system prompt from the instruction files (openClaw model)."""
    parts = []
    for name in _INSTRUCTION_ORDER:
        p = _INSTRUCTIONS_DIR / name
        if p.exists():
            parts.append(p.read_text().strip())
    if extra:
        parts.append(extra.strip())
    return "\n\n---\n\n".join(parts)


@dataclass
class RunResult:
    finished: bool
    success: bool
    reason: str
    turns: int
    messages: list[dict[str, Any]] = field(default_factory=list)
    run_id: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)


class MiniPlusAgent:
    """Drives a :class:`RoverVerbs` (or any transport client) with Claude."""

    def __init__(
        self,
        rover,
        client: anthropic.Anthropic | None = None,
        model: str = DEFAULT_MODEL,
        max_turns: int = 60,
        effort: str = "high",
        work=None,
        resource_name: str | None = None,
        system_extra: str | None = None,
        on_event: Callable[[str], None] | None = None,
        limits: MissionLimits | None = None,
        watchdog_timeout_s: float = 120.0,
        manifest_path: str | None = None,
    ):
        self.verbs: RoverVerbs = make_verbs(rover)
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.max_turns = max_turns
        self.effort = effort
        self.work = work
        self.resource_name = resource_name
        self.limits = limits or MissionLimits()
        self.watchdog_timeout_s = watchdog_timeout_s
        self.manifest_path = manifest_path or os.environ.get("MPAK_MANIFEST_PATH")
        self._run: Run | None = None
        self.on_event = on_event or (lambda msg: None)
        self.system = load_system_prompt(system_extra)
        self.tools = make_tools(self.verbs.capabilities, has_work=work is not None)
        # Anthropic prompt caching: send the (frozen) system prompt as a content
        # block with a trailing cache breakpoint, and mark the last tool def so the
        # tools+system prefix is cached across the loop's turns.
        self._system_blocks = [{"type": "text", "text": self.system,
                                "cache_control": {"type": "ephemeral"}}]
        self._cached_tools = [dict(t) for t in self.tools]
        if self._cached_tools:
            self._cached_tools[-1]["cache_control"] = {"type": "ephemeral"}

    def _log(self, msg: str) -> None:
        self.on_event(msg)

    def _emergency_stop(self) -> None:
        """Idempotent hard stop — prefer a latching estop, fall back to stop().

        Safe to call from the watchdog thread or the supervisor path.
        """
        for method in ("estop", "stop"):
            fn = getattr(self.verbs, method, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    continue
                if self._run is not None:
                    self._run.event("emergency_stop", level="error", via=method)
                get_logger().error(f"emergency_stop via {method}()")
                return

    def run(self, objective: str) -> RunResult:
        """Pursue ``objective`` autonomously until the agent calls ``finish``.

        Wrapped in mission-level safety: a runtime-budget :class:`SafetySupervisor`
        checked each turn and a deadman :class:`Watchdog` that emergency-stops the
        robot if a turn hangs (e.g. a blocked LLM call). Every step is recorded to a
        structured :class:`Run` manifest, attached to the result.
        """
        run = Run(objective)
        self._run = run
        supervisor = SafetySupervisor(self.limits)
        watchdog = (Watchdog(self.watchdog_timeout_s, on_timeout=self._emergency_stop).start()
                    if self.watchdog_timeout_s and self.watchdog_timeout_s > 0 else None)

        initial = [{"type": "text", "text": f"Objective: {objective}\n\nCurrent situation:"}]
        initial += _observe_blocks(self.verbs)
        messages: list[dict[str, Any]] = [{"role": "user", "content": initial}]
        result = RunResult(False, False, "max_turns reached", 0, run_id=run.run_id)

        try:
            for turn in range(1, self.max_turns + 1):
                result.turns = turn
                if watchdog:
                    watchdog.pet()
                sv = supervisor.check()                       # mission runtime budget
                if not sv.ok:
                    run.event("safety_trip", level="warning", scope="mission", reason=sv.reason)
                    self._emergency_stop()
                    result.reason = f"safety: {sv.reason}"
                    break

                with run.timer("llm_turn"):
                    response = self.client.messages.create(
                        model=self.model,
                        max_tokens=16384,
                        system=self._system_blocks,
                        thinking={"type": "adaptive"},
                        output_config={"effort": self.effort},
                        tools=self._cached_tools,
                        messages=messages,
                    )
                if watchdog and watchdog.fired:              # loop stalled while we were blocked
                    run.event("watchdog_fired", level="error", turn=turn)
                    result.reason = "watchdog: loop stalled, robot stopped"
                    break

                run.event("turn", turn=turn, stop_reason=response.stop_reason)
                for block in response.content:
                    if block.type == "text" and block.text.strip():
                        self._log(f"[claude] {block.text.strip()}")
                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason != "tool_use":
                    if response.stop_reason == "refusal":
                        result.reason = "model refused"
                        run.event("refusal", level="warning")
                        break
                    if response.stop_reason == "max_tokens":
                        messages.append({"role": "user", "content": [{"type": "text", "text":
                            "Your previous response was cut off. Continue."}]})
                        continue
                    messages.append({"role": "user", "content": [{"type": "text", "text":
                        "Use a tool to act on the robot, or call finish if you are done."}]})
                    continue

                tool_results, stop_after = [], False
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    self._log(f"[verb] {block.name}({block.input})")
                    run.event("verb", name=block.name, input=dict(block.input))
                    run.counter("verbs")
                    with run.timer(f"verb.{block.name}"):
                        outcome = dispatch(self.verbs, block.name, dict(block.input),
                                           work=self.work, resource_name=self.resource_name)
                    run.event("verb_result", name=block.name,
                              ok=not outcome.is_error, finished=outcome.finished,
                              level="warning" if outcome.is_error else "info")
                    if outcome.is_error:
                        run.counter("verb_errors")
                    tr: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id,
                                          "content": outcome.blocks}
                    if outcome.is_error:
                        tr["is_error"] = True
                    tool_results.append(tr)
                    if outcome.finished:
                        result.finished, result.success, result.reason = True, outcome.success, outcome.reason
                        stop_after = True
                messages.append({"role": "user", "content": tool_results})
                if stop_after:
                    break
        finally:
            if watchdog:
                watchdog.stop()
            try:
                self.verbs.stop()
            except Exception:
                pass
            run.event("run_end", success=result.success, finished=result.finished,
                      turns=result.turns, reason=result.reason)
            result.manifest = run.manifest()
            if self.manifest_path:
                try:
                    run.save(self.manifest_path)
                except Exception:
                    pass
            self._run = None

        result.messages = messages
        return result
