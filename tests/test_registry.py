"""Registry consistency — checks the single-source-of-truth couldn't drift.

These catch classes of bug the old (schema + separate switch) shape allowed:
a verb with no handler, a capability string with no tool, or a duplicate name.
"""

import _bootstrap  # noqa: F401

from mini_plus_agent_kit.tools import VERBS, TOOLS, _BY_NAME, make_tools
from mini_plus_agent_kit.rover import EarthRoverVerbs, HarnessVerbs

_BACKENDS = [EarthRoverVerbs, HarnessVerbs]
_BACKEND_CAPS = set().union(*(set(b.capabilities) for b in _BACKENDS))
_SPECIAL = {"_always", "_work"}


def test_names_unique_and_resolvable():
    names = [v.name for v in VERBS]
    assert len(names) == len(set(names)), "duplicate verb name"
    assert set(_BY_NAME) == set(names)
    for v in VERBS:
        assert callable(v.run), f"{v.name} has no handler"


def test_every_verb_cap_is_supported_by_a_backend():
    for v in VERBS:
        if v.cap in _SPECIAL:
            continue
        assert v.cap in _BACKEND_CAPS, f"verb {v.name!r} gated on cap {v.cap!r} no backend offers"


def test_every_backend_capability_maps_to_a_verb():
    verb_caps = {v.cap for v in VERBS}
    for cap in _BACKEND_CAPS:
        assert cap in verb_caps, f"backend capability {cap!r} has no tool in the registry"


def test_tools_backcompat_shape():
    # TOOLS keeps the original {_cap,name,description,input_schema} shape.
    assert all({"_cap", "name", "description", "input_schema"} <= set(t) for t in TOOLS)
    assert [t["name"] for t in TOOLS] == [v.name for v in VERBS]


def test_drive_to_checkpoint_is_earthrover_only():
    # The fused closed-loop controller is exposed as a verb on its own capability,
    # gated to EarthRover (which has GPS + the fused controller) and not Harness.
    v = _BY_NAME["drive_to_checkpoint"]
    assert v.cap == "drive_to_checkpoint"
    assert "drive_to_checkpoint" in EarthRoverVerbs.capabilities
    assert "drive_to_checkpoint" not in HarnessVerbs.capabilities
    er = {t["name"] for t in make_tools(EarthRoverVerbs.capabilities)}
    hv = {t["name"] for t in make_tools(HarnessVerbs.capabilities)}
    assert "drive_to_checkpoint" in er and "drive_to_checkpoint" not in hv


def test_make_tools_strips_internal_and_filters():
    # _always always present; _work only when has_work; _cap never leaks.
    base = {t["name"] for t in make_tools(HarnessVerbs.capabilities, has_work=False)}
    withw = {t["name"] for t in make_tools(HarnessVerbs.capabilities, has_work=True)}
    assert "finish" in base and "capture_work" not in base
    assert "capture_work" in withw
    for t in withw:
        assert "_cap" not in t


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
