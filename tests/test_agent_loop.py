"""End-to-end agent-loop test: scripted Claude model + fake robot + recording sink.

Runs MiniPlusAgent.run() with no real Anthropic/robot/network — proving the loop
wiring: instruction-file prompt, capability-filtered tools, verb dispatch,
tool_result construction, capture_work → WorkSink, and finish handling.
"""

import _bootstrap  # noqa: F401  (path + dep stubs)

from mini_plus_agent_kit.rover import RoverVerbs, Scene
from mini_plus_agent_kit.client import Telemetry
from mini_plus_agent_kit.work import WorkSink
from mini_plus_agent_kit.agent import MiniPlusAgent
import mini_plus_agent_kit.work as W


# --- scripted Anthropic stand-in -------------------------------------------
class _Block:
    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type, self.text, self.name, self.input, self.id = type, text, name, input, id


class _Resp:
    def __init__(self, content, stop_reason):
        self.content, self.stop_reason = content, stop_reason


class _FakeAnthropic:
    """Returns the next scripted response on each messages.create()."""

    def __init__(self, script):
        self._script, self._i = script, 0
        self.messages = self
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        r = self._script[self._i]; self._i += 1; return r


class FakeVerbs(RoverVerbs):
    name = "fake"
    capabilities = frozenset({"status_report", "move", "turn", "look", "photo", "obstacle_check"})

    def __init__(self):
        self.calls = []

    def status_report(self): self.calls.append("status_report"); return {"reply": "battery 80%"}
    def telemetry(self): return Telemetry(battery=80.0, lidar_front_m=1.2, lidar_blocked=False)
    def look(self): self.calls.append("look"); return Scene(caption="a box ahead", image_b64="QUJD")
    def photo(self): self.calls.append("photo"); return b"JPEGBYTES"
    def move(self, distance_ft=1.0, backward=False):
        self.calls.append(("move", distance_ft, backward)); return {"ok": True, "ticks": 2}
    def turn(self, degrees): self.calls.append(("turn", degrees)); return {"ok": True}
    def obstacle_check(self): self.calls.append("obstacle_check"); return {"blocked": False, "reply": "clear"}
    def stop(self): self.calls.append("stop"); return {"ok": True}


class RecordingSink(WorkSink):
    def __init__(self): self.events = []
    def register_resource(self, *a, **k): return {}
    def task_start(self, event_id, **k): self.events.append("start"); return {"task_run_id": "RUN1"}
    def task_end(self, run, art, **k): self.events.append(("end", art.walrus_blob_id, art.ipfs_cid)); return {}
    def task_validate(self, run, pts): self.events.append(("validate", pts)); return {"tx": "0xok"}


def _tu(name, inp, id):
    return _Block("tool_use", name=name, input=inp, id=id)


def main():
    W.walrus_put = lambda data, **k: "BLOBX"
    script = [
        _Resp([_Block("text", text="I see a box; checking the path."), _tu("obstacle_check", {}, "t1")], "tool_use"),
        _Resp([_tu("move", {"distance_ft": 2}, "t2")], "tool_use"),
        _Resp([_Block("text", text="Recording proof."),
               _tu("capture_work", {"label": "found the box", "vrw_points": 90}, "t3")], "tool_use"),
        _Resp([_tu("finish", {"success": True, "reason": "box reached"}, "t4")], "tool_use"),
    ]
    verbs, sink = FakeVerbs(), RecordingSink()
    fake = _FakeAnthropic(script)
    agent = MiniPlusAgent(verbs, client=fake, work=sink,
                          resource_name="ugv_001", on_event=lambda *_: None)

    tnames = {t["name"] for t in agent.tools}
    assert {"obstacle_check", "move", "capture_work", "finish"} <= tnames
    assert "speak" not in tnames and "track_color" not in tnames

    result = agent.run("Reach the box and prove it.")

    assert result.finished and result.success and result.reason == "box reached"
    assert result.turns == 4
    assert "obstacle_check" in verbs.calls and ("move", 2.0, False) in verbs.calls
    assert "photo" in verbs.calls and verbs.calls[-1] == "stop"
    assert sink.events[0] == "start"
    assert sink.events[1][0] == "end" and sink.events[1][1] == "BLOBX" and sink.events[1][2].startswith("bafkrei")
    assert sink.events[2] == ("validate", 90)

    # Request shape: raised max_tokens + Anthropic prompt caching on system + tools.
    kw = fake.calls[0]
    assert kw["max_tokens"] == 16384
    sys_blocks = kw["system"]
    assert isinstance(sys_blocks, list)
    assert sys_blocks[-1]["cache_control"] == {"type": "ephemeral"}
    assert sys_blocks[-1]["type"] == "text" and sys_blocks[-1]["text"]
    assert kw["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    # Caching markers must not leak onto non-final tool defs.
    assert all("cache_control" not in t for t in kw["tools"][:-1])


def test_max_tokens_continues_turn():
    # A max_tokens stop reason should continue the turn, not nudge "use a tool".
    script = [
        _Resp([_Block("text", text="thinking very hard and getting cut off")], "max_tokens"),
        _Resp([_tu("finish", {"success": True, "reason": "done"}, "t1")], "tool_use"),
    ]
    verbs = FakeVerbs()
    fake = _FakeAnthropic(script)
    agent = MiniPlusAgent(verbs, client=fake, on_event=lambda *_: None)
    result = agent.run("Reach the box.")
    assert result.finished and result.success and result.reason == "done"
    assert result.turns == 2
    # The follow-up after a max_tokens stop is a "continue" nudge, not a tool nudge.
    # (messages is mutated in place across turns: index 2 is the post-truncation user
    # turn — initial user [0], truncated assistant [1], then the continue nudge [2].)
    follow_up = result.messages[2]["content"][0]["text"]
    assert "Continue" in follow_up and "tool" not in follow_up.lower()


if __name__ == "__main__":
    main()
    test_max_tokens_continues_turn()
    print("agent-loop e2e: PASS")
