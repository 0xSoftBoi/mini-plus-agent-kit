"""Observability: run manifest, structured events, counters, timers (stdlib)."""

import _bootstrap  # noqa: F401

import json

from mini_plus_agent_kit.observability import Run


class _Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def tick(self, dt): self.t += dt


def test_run_records_event_timeline_and_counters():
    clk = _Clock()
    run = Run("deliver the package", clock=clk)
    clk.tick(1.0)
    run.event("verb", name="move", input={"distance_ft": 3})
    run.counter("verbs")
    clk.tick(0.5)
    run.event("verb_result", name="move", ok=True)
    run.counter("verbs")
    m = run.manifest()
    assert m["objective"] == "deliver the package" and m["run_id"].startswith("run-")
    assert m["counters"]["verbs"] == 2
    types = [e["type"] for e in m["events"]]
    assert types == ["run_start", "verb", "verb_result"]
    assert m["events"][1]["name"] == "move" and m["events"][1]["input"]["distance_ft"] == 3
    assert m["duration_s"] == 1.5


def test_run_timer_accumulates_and_emits_timing():
    clk = _Clock()
    run = Run("x", clock=clk)
    with run.timer("verb.look"):
        clk.tick(0.2)
    m = run.manifest()
    assert m["counters"]["verb.look.count"] == 1
    assert m["counters"]["verb.look.ms_total"] == 200
    assert any(e["type"] == "timing" and e["name"] == "verb.look" for e in m["events"])


def test_run_save_is_valid_json(tmp_path=None):
    import tempfile, os
    run = Run("audit me")
    run.event("anchor", tx="0xabc", ok=True)
    d = tempfile.mkdtemp()
    p = run.save(os.path.join(d, "manifest.json"))
    loaded = json.load(open(p))
    assert loaded["objective"] == "audit me"
    assert any(e["type"] == "anchor" and e["tx"] == "0xabc" for e in loaded["events"])


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
