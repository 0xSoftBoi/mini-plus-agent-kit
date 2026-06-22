"""Verifiable Robotic Work: artifacts, BitRobot VRW, onchain-rover, race settle."""

import _bootstrap  # noqa: F401

import mini_plus_agent_kit as M
import mini_plus_agent_kit.work as W
from _bootstrap import Resp


def _patch_walrus():
    W.walrus_put = lambda data, **k: "BLOBZ"


def _record_module_post():
    posts = []

    def fake_post(url, json=None, timeout=None):
        posts.append((url, json))
        if url.endswith("/give-feedback"):
            return Resp({"tx": "0xfb", "status": "success"})
        if url.endswith("/race/settle"):
            return Resp({"tx": "0xrace", "status": "success"})
        return Resp({"ok": True})

    W.httpx.post = fake_post
    return posts


def test_submit_work_lifecycle_and_artifact():
    _patch_walrus()
    calls = []

    class FakeSink(W.WorkSink):
        def register_resource(s, *a, **k): return {}
        def task_start(s, e, **k): calls.append("start"); return {"task_run_id": "RUN1"}
        def task_end(s, r, a, **k): calls.append(("end", r, a.ipfs_cid)); return {}
        def task_validate(s, r, p): calls.append(("validate", r, p)); return {}

    rec = M.submit_work(FakeSink(), b"frame", label="found", vrw_points=120)
    assert calls == ["start", ("end", "RUN1", rec.artifact.ipfs_cid), ("validate", "RUN1", 120)]
    assert rec.run_ref == "RUN1" and rec.artifact.walrus_blob_id == "BLOBZ"
    assert rec.artifact.ipfs_cid.startswith("bafkrei") and rec.artifact.sha256.startswith("0x")


def test_onchain_rover_anchors_via_give_feedback():
    _patch_walrus()
    posts = _record_module_post()
    sink = M.OnchainRoverSink(sidecar_url="http://x", robot="guard", skill="deliver")
    rec = M.submit_work(sink, b"frame", label="delivery", vrw_points=140)
    proof = next(j for u, j in posts if u.endswith("/proof"))
    gf = next(j for u, j in posts if u.endswith("/give-feedback"))
    assert proof["sha256"].startswith("0x") and proof["blobId"] == "BLOBZ"
    assert gf["robot"] == "guard" and gf["skill"] == "deliver"
    assert gf["score"] == 100                         # 140 VRW clamps to 0..100
    assert not gf["sha256"].startswith("0x")          # giveFeedback re-adds 0x
    assert gf["sha256"] == rec.artifact.sha256[2:]
    assert rec.results["validate"]["tx"] == "0xfb"
    assert not sink._pending                          # artifact popped after validate


def test_onchain_rover_anchor_disabled_and_score_override():
    _patch_walrus()
    posts = _record_module_post()
    r = M.submit_work(M.OnchainRoverSink(sidecar_url="http://x", anchor=False),
                      b"x", label="t", vrw_points=10)
    assert any(u.endswith("/proof") for u, _ in posts)
    assert not any(u.endswith("/give-feedback") for u, _ in posts)
    assert r.results["validate"] == {"ok": True, "note": "anchor disabled"}

    posts2 = _record_module_post()
    M.submit_work(M.OnchainRoverSink(sidecar_url="http://x", score=88), b"x", label="t", vrw_points=5)
    assert next(j for u, j in posts2 if u.endswith("/give-feedback"))["score"] == 88


def test_race_proof_sink_settles_market():
    _patch_walrus()
    posts = _record_module_post()
    r = M.submit_work(M.RaceProofSink(sidecar_url="http://x", winner_idx=0, race_id=7),
                      b"finish", label="guard wins", vrw_points=0)
    s = next(j for u, j in posts if u.endswith("/race/settle"))
    assert s["winnerIdx"] == 0 and s["raceId"] == 7 and s["blobId"] == "BLOBZ"
    assert not s["sha256"].startswith("0x") and s["sha256"] == r.artifact.sha256[2:]
    assert r.results["validate"]["tx"] == "0xrace"

    posts2 = _record_module_post()
    M.submit_work(M.RaceProofSink(sidecar_url="http://x", winner_idx=1), b"f", label="w", vrw_points=0)
    assert "raceId" not in next(j for u, j in posts2 if u.endswith("/race/settle"))


def test_bitrobot_register_and_attribution():
    _patch_walrus()
    events = []

    br = M.BitRobotSink(subnet_id="sn1", api_key="brb_x", owner="SOLWALLET",
                        resource_subtype="waveshare_ugv")

    def client_post(path, json=None):
        events.append((path, json))
        et = (json or {}).get("event_type")
        if et == "register_resource":
            return Resp({"resource_id": "01RES", "ent_address": "SoLpda"})
        if et == "task_start":
            return Resp({"task_run_id": "RUN1"})
        return Resp({"ok": True})

    br._http.post = client_post

    reg = br.register("ugv_001", symbol="UGV", description="Waveshare UGV", image="http://img")
    assert reg["ent_address"] == "SoLpda"
    rev = events[-1][1]
    assert rev["event_type"] == "register_resource" and rev["resource_name"] == "ugv_001"
    assert rev["resource_subtype"] == "waveshare_ugv" and rev["owner"] == "SOLWALLET"

    events.clear()
    M.submit_work(br, b"frame", label="found", vrw_points=140)
    ts = next(j for _, j in events if j and j.get("event_type") == "task_start")
    te = next(j for _, j in events if j and j.get("event_type") == "task_end")
    tv = next(j for _, j in events if j and j.get("event_type") == "task_validate")
    assert ts["resource_name"] == "ugv_001" and ts["resource_subtype"] == "waveshare_ugv"
    assert ts["ent_owner"] == "SOLWALLET"
    assert te["raw_data_uri"].endswith("/v1/blobs/BLOBZ") and te["raw_data_cid"].startswith("bafkrei")
    assert tv["vrw_points"] == 140


def test_multisink_fans_out():
    _patch_walrus()
    _record_module_post()

    class FakeSink(W.WorkSink):
        def register_resource(s, *a, **k): return {}
        def task_start(s, e, **k): return {"task_run_id": "RUN1"}
        def task_end(s, r, a, **k): return {}
        def task_validate(s, r, p): return {}

    ms = W.MultiSink(FakeSink(), M.OnchainRoverSink(sidecar_url="http://x"))
    assert len(ms.task_start("e1")) == 2


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
