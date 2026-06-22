"""LIVE test — real Walrus testnet store + retrieve via the kit's work layer.

No credentials needed (public testnet). Proves store_artifact() actually uploads,
content-addresses (sha256 + IPFS CIDv1), and the blob is retrievable byte-identical.

    .venv/bin/python tests/live/test_live_walrus.py
"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import httpx
import mini_plus_agent_kit.work as W


def main():
    data = b"mini-plus-agent-kit live VRW artifact " + bytes(range(32)) * 4
    art = W.store_artifact(data, content_type="application/octet-stream")  # REAL Walrus PUT + CID
    print("blobId:", art.walrus_blob_id)
    print("ipfs_cid:", art.ipfs_cid)
    print("sha256:", art.sha256)

    assert art.sha256 == "0x" + hashlib.sha256(data).hexdigest()
    assert art.ipfs_cid.startswith("bafkrei")
    got = httpx.get(art.walrus_url, timeout=60).content                    # REAL retrieve
    assert got == data, f"round-trip mismatch {len(got)} vs {len(data)}"
    print(f"round-trip: stored {len(data)} bytes, retrieved identical ✓")
    print("\nLIVE WALRUS PASSED (real testnet store + retrieve, real content addressing)")


if __name__ == "__main__":
    main()
