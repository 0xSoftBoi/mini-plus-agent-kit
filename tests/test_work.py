"""Verifiable Robotic Work: artifacts, BitRobot VRW, onchain-rover, race settle."""

import _bootstrap  # noqa: F401

import mini_plus_agent_kit as M
import mini_plus_agent_kit.work as W
from _bootstrap import Resp


def _ensure_httpx_errors():
    """Give the hermetic httpx stub a real exception hierarchy so retry tests can
    distinguish transport errors (retried) from HTTP status errors (surfaced)."""
    if not hasattr(W.httpx, "HTTPError"):
        class HTTPError(Exception):
            def __init__(self, *a, **k):
                super().__init__(*(a[:1]))

        class RequestError(HTTPError):
            pass

        class ConnectError(RequestError):
            pass

        class HTTPStatusError(HTTPError):
            pass

        W.httpx.HTTPError = HTTPError
        W.httpx.RequestError = RequestError
        W.httpx.ConnectError = ConnectError
        W.httpx.HTTPStatusError = HTTPStatusError


_ensure_httpx_errors()

# Capture the real walrus_put before any test monkeypatches it to a stub.
_REAL_WALRUS_PUT = W.walrus_put


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


def test_solana_rover_anchors_on_clanker5000():
    _patch_walrus()
    posts = _record_module_post()
    sink = M.SolanaRoverSink(sidecar_url="http://x", robot="guard", skill="deliver", cluster="devnet")
    rec = M.submit_work(sink, b"frame", label="delivery", vrw_points=140)
    proof = next(j for u, j in posts if u.endswith("/proof"))
    gf = next(j for u, j in posts if u.endswith("/give-feedback"))
    assert proof["blobId"] == "BLOBZ" and proof["sha256"].startswith("0x")
    assert gf["robot"] == "guard" and gf["skill"] == "deliver" and gf["score"] == 100
    assert not gf["sha256"].startswith("0x") and gf["sha256"] == rec.artifact.sha256[2:]
    v = rec.results["validate"]
    assert v["tx"] == "0xfb" and v["verified"] is True            # score 100 ≥ 70 threshold
    assert v["explorer"] == "https://explorer.solana.com/tx/0xfb?cluster=devnet"
    assert not sink._pending


def test_solana_rover_verified_threshold_and_anchor_disabled():
    _patch_walrus()
    posts = _record_module_post()
    # score 50 < 70 → not verified
    r = M.submit_work(M.SolanaRoverSink(sidecar_url="http://x", score=50), b"x", label="t", vrw_points=0)
    assert r.results["validate"]["verified"] is False
    # anchor disabled → no give-feedback call
    posts2 = _record_module_post()
    r2 = M.submit_work(M.SolanaRoverSink(sidecar_url="http://x", anchor=False), b"x", label="t", vrw_points=9)
    assert any(u.endswith("/proof") for u, _ in posts2)
    assert not any(u.endswith("/give-feedback") for u, _ in posts2)
    assert r2.results["validate"] == {"ok": True, "note": "anchor disabled"}


def test_multisink_bitrobot_plus_solana_one_run():
    # one artifact fanned to both ledgers (BitRobot subnet + Solana clanker5000)
    _patch_walrus()
    posts = _record_module_post()
    sol = M.SolanaRoverSink(sidecar_url="http://x", robot="guard")

    class FakeBR(W.WorkSink):
        def register_resource(s, *a, **k): return {}
        def task_start(s, e, **k): return {"task_run_id": "RUN1"}
        def task_end(s, r, a, **k): return {"ok": True}
        def task_validate(s, r, p): return {"vrw": p}

    rec = M.submit_work(W.MultiSink(FakeBR(), sol), b"frame", label="x", vrw_points=80)
    assert any(u.endswith("/give-feedback") for u, _ in posts)     # Solana leg fired
    assert isinstance(rec.results["validate"], list) and len(rec.results["validate"]) == 2


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


def test_multisink_isolates_one_sink_failure():
    # A sink that raises must not abort the fan-out: its slot is captured as an
    # error dict and the other sinks still run.
    class BoomSink(W.WorkSink):
        def register_resource(s, *a, **k): return {}
        def task_start(s, e, **k): raise RuntimeError("ledger down")
        def task_end(s, r, a, **k): return {}
        def task_validate(s, r, p): return {}

    class OkSink(W.WorkSink):
        def register_resource(s, *a, **k): return {}
        def task_start(s, e, **k): return {"task_run_id": "RUN1"}
        def task_end(s, r, a, **k): return {}
        def task_validate(s, r, p): return {}

    ms = W.MultiSink(BoomSink(), OkSink())
    res = ms.task_start("e1")
    assert len(res) == 2
    assert res[0] == {"ok": False, "error": "ledger down"}
    assert res[1] == {"task_run_id": "RUN1"}


def test_walrus_put_retries_transport_error_then_succeeds():
    # First call raises a transport error, second returns the blob. Retry must
    # recover; sleep is monkeypatched so the test never waits.
    calls = {"n": 0}

    def flaky_put(url, params=None, content=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise W.httpx.ConnectError("boom")
        return Resp({"newlyCreated": {"blobObject": {"blobId": "BLOBZ"}}})

    orig_put, orig_sleep = W.httpx.put, W.time.sleep
    W.httpx.put = flaky_put
    W.time.sleep = lambda *_a, **_k: None
    try:
        assert _REAL_WALRUS_PUT(b"x", attempts=3) == "BLOBZ"
        assert calls["n"] == 2
    finally:
        W.httpx.put, W.time.sleep = orig_put, orig_sleep


def test_walrus_put_single_shot_when_attempts_one():
    def boom_put(url, params=None, content=None, timeout=None):
        raise W.httpx.ConnectError("boom")

    orig_put = W.httpx.put
    W.httpx.put = boom_put
    try:
        raised = False
        try:
            _REAL_WALRUS_PUT(b"x", attempts=1)
        except Exception:
            raised = True
        assert raised
    finally:
        W.httpx.put = orig_put


def test_give_feedback_does_not_retry_chain_rejection():
    # An HTTP error status (definitive chain rejection) is NOT retried — it is
    # surfaced once as an error result and the POST fires exactly one time.
    _patch_walrus()
    posts = {"n": 0}

    def rejecting_post(url, json=None, timeout=None):
        if url.endswith("/give-feedback"):
            posts["n"] += 1
            raise W.httpx.HTTPStatusError("rejected", request=None, response=None)
        return Resp({"ok": True})

    orig_post, orig_sleep = W.httpx.post, W.time.sleep
    W.httpx.post = rejecting_post
    W.time.sleep = lambda *_a, **_k: None
    try:
        rec = M.submit_work(M.OnchainRoverSink(sidecar_url="http://x", attempts=3),
                            b"x", label="t", vrw_points=50)
        assert posts["n"] == 1                       # not retried
        assert rec.results["validate"]["ok"] is False
    finally:
        W.httpx.post, W.time.sleep = orig_post, orig_sleep


def test_give_feedback_retries_transport_error():
    _patch_walrus()
    posts = {"n": 0}

    def flaky_post(url, json=None, timeout=None):
        if url.endswith("/give-feedback"):
            posts["n"] += 1
            if posts["n"] == 1:
                raise W.httpx.ConnectError("boom")
            return Resp({"tx": "0xfb", "status": "success"})
        return Resp({"ok": True})

    orig_post, orig_sleep = W.httpx.post, W.time.sleep
    W.httpx.post = flaky_post
    W.time.sleep = lambda *_a, **_k: None
    try:
        rec = M.submit_work(M.OnchainRoverSink(sidecar_url="http://x", attempts=3),
                            b"x", label="t", vrw_points=50)
        assert posts["n"] == 2                       # retried once, then succeeded
        assert rec.results["validate"]["tx"] == "0xfb"
    finally:
        W.httpx.post, W.time.sleep = orig_post, orig_sleep


def test_workrecord_ok_property_folds_stage_results():
    art = W.Artifact("0xab", "BLOBZ", "http://u", "bafkrei", "image/jpeg", 1)
    good = W.WorkRecord("e", "r", art, "l", 1,
                        {"start": {"ok": True}, "end": {"tx": "0x1"},
                         "validate": [{"ok": True}, {"tx": "0x2"}]})
    assert good.ok is True
    bad = W.WorkRecord("e", "r", art, "l", 1,
                       {"start": {"task_run_id": "RUN1"},
                        "validate": [{"ok": True}, {"ok": False, "error": "x"}]})
    assert bad.ok is False


def test_onchain_rover_env_precedence_arc_over_sidecar():
    # ARC_SIDECAR_URL wins over the shared SIDECAR_URL (port-4021 collision fix);
    # SOLANA_SIDECAR_URL governs the Solana sink independently.
    import os
    saved = {k: os.environ.get(k) for k in ("ARC_SIDECAR_URL", "SIDECAR_URL", "SOLANA_SIDECAR_URL")}
    try:
        os.environ["ARC_SIDECAR_URL"] = "http://arc:1"
        os.environ["SIDECAR_URL"] = "http://shared:4021"
        os.environ["SOLANA_SIDECAR_URL"] = "http://sol:2"
        assert M.OnchainRoverSink().sidecar_url == "http://arc:1"
        assert M.SolanaRoverSink().sidecar_url == "http://sol:2"
        # Without ARC override, OnchainRoverSink falls back to the shared URL.
        del os.environ["ARC_SIDECAR_URL"]
        assert M.OnchainRoverSink().sidecar_url == "http://shared:4021"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
