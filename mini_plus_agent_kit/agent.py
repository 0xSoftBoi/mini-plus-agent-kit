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

from .rover import RoverVerbs, make_verbs
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
    ):
        self.verbs: RoverVerbs = make_verbs(rover)
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.max_turns = max_turns
        self.effort = effort
        self.work = work
        self.resource_name = resource_name
        self.on_event = on_event or (lambda msg: None)
        self.system = load_system_prompt(system_extra)
        self.tools = make_tools(self.verbs.capabilities, has_work=work is not None)

    def _log(self, msg: str) -> None:
        self.on_event(msg)

    def run(self, objective: str) -> RunResult:
        """Pursue ``objective`` autonomously until the agent calls ``finish``."""
        initial = [{"type": "text", "text": f"Objective: {objective}\n\nCurrent situation:"}]
        initial += _observe_blocks(self.verbs)
        messages: list[dict[str, Any]] = [{"role": "user", "content": initial}]
        result = RunResult(False, False, "max_turns reached", 0)

        for turn in range(1, self.max_turns + 1):
            result.turns = turn
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=self.system,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                tools=self.tools,
                messages=messages,
            )
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    self._log(f"[claude] {block.text.strip()}")
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                if response.stop_reason == "refusal":
                    result.reason = "model refused"
                    break
                messages.append({"role": "user", "content": [{"type": "text", "text":
                    "Use a tool to act on the robot, or call finish if you are done."}]})
                continue

            tool_results, stop_after = [], False
            for block in response.content:
                if block.type != "tool_use":
                    continue
                self._log(f"[verb] {block.name}({block.input})")
                outcome = dispatch(self.verbs, block.name, dict(block.input),
                                   work=self.work, resource_name=self.resource_name)
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

        try:
            self.verbs.stop()
        except Exception:
            pass
        result.messages = messages
        return result
