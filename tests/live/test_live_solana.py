"""LIVE — SolanaRoverSink over real httpx against an emulated Clanker 5000 sidecar.

No stubs: a stdlib HTTP server implements the native-Solana sidecar contract
(`POST /proof`, `POST /give-feedback` → clanker5000.give_feedback returning a Solana
signature), and the REAL SolanaRoverSink anchors a robot artifact end to end over
real sockets. Verifies the on-the-wire payload (walrus blob + bare sha256), the
returned signature, the explorer link, and the ≥70 attestation-threshold flag.

    .venv/bin/python tests/live/test_live_solana.py
"""

import hashlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mini_plus_agent_kit.work import SolanaRoverSink, Artifact

SIG = "5xVHe1bgk9rJqP2sTtq0n8rkq9b3z6mWnAa4yDcF7gH2cD3eF4gH5jK6mN7pQ8rS9tU"
GIVE_FEEDBACK = {}


class Sidecar(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/proof":
            out = {"ok": True}
        elif self.path == "/give-feedback":
            GIVE_FEEDBACK.update(body)
            # mirrors solana-chain.giveFeedbackOnChain return: an own Anchor sig
            out = {"agentId": "1", "score": body.get("score"), "tx": SIG}
        else:
            out = {"error": "not found"}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Sidecar)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    raw = b"rover finish frame"
    art = Artifact(sha256="0x" + hashlib.sha256(raw).hexdigest(), walrus_blob_id="BLOBZ",
                   walrus_url="https://agg/v1/blobs/BLOBZ", ipfs_cid="bafkreitest",
                   content_type="image/jpeg", byte_length=len(raw))

    sink = SolanaRoverSink(sidecar_url=base, robot="guard", skill="deliver", cluster="devnet")
    end = sink.task_end("run1", art, label="courier delivery @ checkpoint")
    val = sink.task_validate("run1", vrw_points=100)

    print(f"clanker5000 sidecar (emulated) at {base}")
    print(f"  /proof          -> {end}")
    print(f"  /give-feedback  -> tx {val['tx'][:12]}…  verified={val['verified']}")
    print(f"  explorer        -> {val['explorer']}")
    print(f"  on-wire payload -> robot={GIVE_FEEDBACK['robot']} skill={GIVE_FEEDBACK['skill']} "
          f"score={GIVE_FEEDBACK['score']} blobId={GIVE_FEEDBACK['blobId']}")

    assert end.get("ok") is True
    assert GIVE_FEEDBACK["robot"] == "guard" and GIVE_FEEDBACK["skill"] == "deliver"
    assert GIVE_FEEDBACK["blobId"] == "BLOBZ"
    assert GIVE_FEEDBACK["sha256"] == art.sha256[2:]            # bare hex on the wire
    assert val["tx"] == SIG and val["verified"] is True          # 100 ≥ 70 threshold
    assert val["explorer"] == f"https://explorer.solana.com/tx/{SIG}?cluster=devnet"
    srv.shutdown()
    print("\nLIVE SOLANA PASSED (real httpx → clanker5000 give_feedback, signature + explorer)")


if __name__ == "__main__":
    main()
