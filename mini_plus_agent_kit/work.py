"""Verifiable Robotic Work — robot data → on-chain, through one ``WorkSink``.

A robot run produces an *artifact* (a clip or photo). The kit stores it once on
Walrus (a public URL) and computes its IPFS CID + sha256, then emits the work to
one or more sinks:

* :class:`BitRobotSink` — the canonical BitRobot path (docs.bitrobot.ai):
  ``POST /subnets/{id}/events`` with ``register_resource`` → ``task_start`` →
  ``task_end {raw_data_uri, raw_data_cid}`` → ``task_validate {vrw_points}`` →
  Subnet Points → Bolts; the resource is an Entity NFT on Solana.
* :class:`OnchainRoverSink` — your existing stack: register the ``(sha256,
  blobId)`` proof with the sidecar (``POST /proof``), which your ``settle.ts``
  anchors on Arc/Solana via ``giveFeedback`` / ``settleRaceOnChain``.

One artifact, both ledgers. Combine sinks with :class:`MultiSink`.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

WALRUS_PUBLISHER = os.environ.get(
    "WALRUS_PUBLISHER", "https://publisher.walrus-testnet.walrus.space"
)
WALRUS_AGGREGATOR = os.environ.get(
    "WALRUS_AGGREGATOR", "https://aggregator.walrus-testnet.walrus.space"
)
IPFS_CHUNK = 1 << 20  # 1 MiB — matches docs `--chunker=size-1048576`


# --------------------------------------------------------------------------- #
# Content addressing
# --------------------------------------------------------------------------- #
def _b32_multibase(raw: bytes) -> str:
    return "b" + base64.b32encode(raw).decode("ascii").lower().rstrip("=")


def cid_v1_raw(data: bytes) -> str:
    """CIDv1 (raw codec, sha2-256) for a single block ≤ 1 MiB.

    Bytes are ``0x01 0x55 0x12 0x20 || sha256``; multibase base32 → ``bafkrei…``.
    Equivalent to ``ipfs add --raw-leaves -Q`` for content that fits one block.
    """
    digest = hashlib.sha256(data).digest()
    return _b32_multibase(bytes([0x01, 0x55, 0x12, 0x20]) + digest)


def ipfs_cid(data: bytes) -> str | None:
    """IPFS CID for ``data`` matching the docs' chunking.

    ≤ 1 MiB → computed in-process (single raw block). Larger → shell out to
    ``ipfs add -n --raw-leaves --chunker=size-1048576 -Q`` (the documented
    command) so the UnixFS DAG root is correct; returns ``None`` if the ``ipfs``
    CLI is unavailable (callers may then omit the CID — verification simply
    skips, per the BitRobot docs).
    """
    if len(data) <= IPFS_CHUNK:
        return cid_v1_raw(data)
    try:  # pragma: no cover - external CLI
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            path = f.name
        out = subprocess.run(
            ["ipfs", "add", "-n", "--raw-leaves", "--chunker=size-1048576", "-Q", path],
            capture_output=True, text=True, timeout=120,
        )
        os.unlink(path)
        return out.stdout.strip() or None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Artifact
# --------------------------------------------------------------------------- #
@dataclass
class Artifact:
    """A stored piece of robot data, content-addressed for either ledger."""

    sha256: str
    walrus_blob_id: str
    walrus_url: str
    ipfs_cid: str | None
    content_type: str
    byte_length: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "walrus_blob_id": self.walrus_blob_id,
            "walrus_url": self.walrus_url,
            "ipfs_cid": self.ipfs_cid,
            "content_type": self.content_type,
            "byte_length": self.byte_length,
        }


def walrus_put(data: bytes, epochs: int = 5, timeout: float = 60.0) -> str:
    """Store bytes on Walrus → blobId (handles both publisher response shapes)."""
    r = httpx.put(
        f"{WALRUS_PUBLISHER}/v1/blobs", params={"epochs": epochs}, content=data, timeout=timeout
    )
    r.raise_for_status()
    j = r.json()
    if "newlyCreated" in j:
        return j["newlyCreated"]["blobObject"]["blobId"]
    return j["alreadyCertified"]["blobId"]


def store_artifact(data: bytes, content_type: str = "image/jpeg") -> Artifact:
    """Store ``data`` on Walrus and content-address it (sha256 + IPFS CID)."""
    blob_id = walrus_put(data)
    return Artifact(
        sha256="0x" + hashlib.sha256(data).hexdigest(),
        walrus_blob_id=blob_id,
        walrus_url=f"{WALRUS_AGGREGATOR}/v1/blobs/{blob_id}",
        ipfs_cid=ipfs_cid(data),
        content_type=content_type,
        byte_length=len(data),
    )


# --------------------------------------------------------------------------- #
# Sinks
# --------------------------------------------------------------------------- #
class WorkSink(ABC):
    """Where Verifiable Robotic Work is recorded."""

    @abstractmethod
    def register_resource(self, name: str, subtype: str, owner: str, metadata: dict) -> dict: ...
    @abstractmethod
    def task_start(self, event_id: str, **kw) -> dict: ...
    @abstractmethod
    def task_end(self, run_ref: str, artifact: Artifact, **kw) -> dict: ...
    @abstractmethod
    def task_validate(self, run_ref: str, vrw_points: int) -> dict: ...


class BitRobotSink(WorkSink):
    """BitRobot subnet events API (docs.bitrobot.ai)."""

    def __init__(
        self,
        subnet_id: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        owner: str | None = None,
        resource_name: str | None = None,
        resource_subtype: str = "frodobot",
        timeout: float = 30.0,
    ):
        self.subnet_id = subnet_id or os.environ["BITROBOT_SUBNET_ID"]
        self.api_key = api_key or os.environ["BITROBOT_API_KEY"]  # brb_...
        self.base_url = (base_url or os.environ.get("BITROBOT_API_URL", "https://api.bitrobot.ai")).rstrip("/")
        self.owner = owner or os.environ.get("BITROBOT_OWNER")  # Solana wallet
        # Default resource this sink attributes work to (an Entity NFT).
        self.resource_name = resource_name or os.environ.get("BITROBOT_RESOURCE_NAME")
        self.resource_subtype = resource_subtype
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=timeout,
        )

    def register(self, name: str | None = None, *, subtype: str | None = None,
                 owner: str | None = None, symbol: str = "ROVER",
                 description: str = "", image: str = "") -> dict:
        """Register this robot as an on-chain Entity NFT (one-time setup).

        Returns ``{resource_id, ent_address}``. Subsequent ``task_start`` calls on
        this sink attribute work to it (via ``resource_name`` + ``resource_subtype``).
        """
        name = name or self.resource_name
        if not name:
            raise ValueError("register() needs a resource name (or BITROBOT_RESOURCE_NAME)")
        owner = owner or self.owner
        if not owner:
            raise ValueError("register() needs a Solana owner wallet (or BITROBOT_OWNER)")
        self.resource_name = name
        return self.register_resource(name, subtype or self.resource_subtype, owner,
                                      {"name": name, "symbol": symbol,
                                       "description": description, "image": image})

    def _event(self, payload: dict) -> dict:
        r = self._http.post(f"/subnets/{self.subnet_id}/events", json=payload)
        r.raise_for_status()
        return r.json()

    def register_resource(self, name: str, subtype: str, owner: str, metadata: dict) -> dict:
        return self._event({
            "event_type": "register_resource",
            "resource_name": name,
            "resource_subtype": subtype,
            "owner": owner,
            "ent_metadata": metadata,
        })

    def task_start(self, event_id: str, *, resource_name: str | None = None,
                   resource_subtype: str | None = None, task_id: str | None = None,
                   resource_id: str | None = None, **kw) -> dict:
        body: dict[str, Any] = {"event_type": "task_start", "event_id": event_id,
                                "started_at": _now_iso()}
        if resource_id:
            body["resource_id"] = resource_id
        else:
            body.update(resource_name=resource_name or self.resource_name,
                        resource_subtype=resource_subtype or self.resource_subtype,
                        ent_owner=self.owner)
        if task_id:
            body["task_id"] = task_id
        return self._event(body)

    def task_end(self, run_ref: str, artifact: Artifact, **kw) -> dict:
        body = {"event_type": "task_end", "task_run_id": run_ref,
                "raw_data_uri": artifact.walrus_url, "ended_at": _now_iso()}
        if artifact.ipfs_cid:
            body["raw_data_cid"] = artifact.ipfs_cid
        return self._event(body)

    def task_validate(self, run_ref: str, vrw_points: int) -> dict:
        return self._event({"event_type": "task_validate", "task_run_id": run_ref,
                            "vrw_points": vrw_points})

    def close(self) -> None:
        self._http.close()


class OnchainRoverSink(WorkSink):
    """Your stack: register the proof, then anchor it on Arc via the sidecar.

    * ``task_end``  → ``POST /proof {blobId, sha256, label}`` (the tracker the UI
      and settle flow read).
    * ``task_validate`` → ``POST /give-feedback {robot, skill, score, blobId,
      sha256}`` (sidecar index.ts:597), which calls ``settle.giveFeedback`` →
      ``ReputationRegistry.giveFeedback`` on Arc. Key custody stays in the sidecar.

    The robot's on-chain ``agentId`` is resolved sidecar-side from ``robot``.
    Set ``anchor=False`` to only register the proof without the chain write.
    """

    def __init__(
        self,
        sidecar_url: str | None = None,
        *,
        robot: str = "guard",
        skill: str = "deliver",
        score: int | None = None,
        anchor: bool = True,
        timeout: float = 30.0,
    ):
        self.sidecar_url = (sidecar_url or os.environ.get("SIDECAR_URL", "http://localhost:4021")).rstrip("/")
        self.robot = robot
        self.skill = skill
        self.score = score
        self.anchor = anchor
        self.timeout = timeout
        self._pending: dict[str, tuple[Artifact, str]] = {}

    def register_resource(self, name, subtype, owner, metadata) -> dict:
        return {"ok": True, "note": "onchain-rover registers agents via ENS/ERC-8004, not here"}

    def task_start(self, event_id: str, **kw) -> dict:
        return {"ok": True, "task_run_id": event_id}

    def task_end(self, run_ref: str, artifact: Artifact, *, label: str = "agent work", **kw) -> dict:
        self._pending[run_ref] = (artifact, label)
        try:
            r = httpx.post(
                f"{self.sidecar_url}/proof",
                json={"blobId": artifact.walrus_blob_id, "sha256": artifact.sha256, "label": label},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def task_validate(self, run_ref: str, vrw_points: int) -> dict:
        artifact, label = self._pending.pop(run_ref, (None, "agent work"))
        if not self.anchor:
            return {"ok": True, "note": "anchor disabled"}
        if artifact is None:
            return {"ok": False, "error": "no artifact recorded for this run"}
        # ReputationRegistry stores a 0-100 score; map VRW points unless overridden.
        score = self.score if self.score is not None else max(0, min(100, int(vrw_points)))
        # settle.giveFeedback re-adds the 0x prefix to sha256 — send the bare hex.
        sha = artifact.sha256[2:] if artifact.sha256.startswith("0x") else artifact.sha256
        try:
            r = httpx.post(
                f"{self.sidecar_url}/give-feedback",
                json={"robot": self.robot, "skill": self.skill, "score": score,
                      "blobId": artifact.walrus_blob_id, "sha256": sha},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()  # { tx, status, explorer, ... }
        except Exception as e:
            return {"ok": False, "error": str(e)}


class RaceProofSink(WorkSink):
    """Settle a RaceMarket race on Arc with the agent's captured finish proof.

    ``task_validate`` posts the stored ``(sha256, blobId)`` to the sidecar's
    ``POST /race/settle {raceId, winnerIdx, sha256, blobId}`` route, which calls
    ``settle.settleRaceOnChain`` → ``RaceMarket.settle`` (judge = guard). Use when
    the agent IS the race oracle and you want *its* finish frame to settle the
    market (vs ``/race/finish``, which re-captures the guard's own photo).

    ``winner_idx`` is the winning racer's index; ``race_id=None`` lets the sidecar
    use its current ``onChainRaceId``.
    """

    def __init__(self, sidecar_url: str | None = None, *, winner_idx: int,
                 race_id: int | None = None, timeout: float = 30.0):
        self.sidecar_url = (sidecar_url or os.environ.get("SIDECAR_URL", "http://localhost:4021")).rstrip("/")
        self.winner_idx = winner_idx
        self.race_id = race_id
        self.timeout = timeout
        self._pending: dict[str, tuple[Artifact, str]] = {}

    def register_resource(self, name, subtype, owner, metadata) -> dict:
        return {"ok": True, "note": "races register via RaceMarket.openRace, not here"}

    def task_start(self, event_id: str, **kw) -> dict:
        return {"ok": True, "task_run_id": event_id}

    def task_end(self, run_ref: str, artifact: Artifact, *, label: str = "race finish", **kw) -> dict:
        self._pending[run_ref] = (artifact, label)
        return {"ok": True, "stored": artifact.walrus_blob_id}

    def task_validate(self, run_ref: str, vrw_points: int) -> dict:
        artifact, label = self._pending.pop(run_ref, (None, "race finish"))
        if artifact is None:
            return {"ok": False, "error": "no artifact recorded for this run"}
        # settleRaceOnChain tolerates 0x or bare hex; send bare for consistency.
        sha = artifact.sha256[2:] if artifact.sha256.startswith("0x") else artifact.sha256
        body: dict[str, Any] = {"winnerIdx": self.winner_idx, "sha256": sha,
                                "blobId": artifact.walrus_blob_id, "label": label}
        if self.race_id is not None:
            body["raceId"] = self.race_id
        try:
            r = httpx.post(f"{self.sidecar_url}/race/settle", json=body, timeout=self.timeout)
            r.raise_for_status()
            return r.json()  # { tx, status, explorer, ... }
        except Exception as e:
            return {"ok": False, "error": str(e)}


class MultiSink(WorkSink):
    """Fan a single piece of work out to several sinks."""

    def __init__(self, *sinks: WorkSink):
        self.sinks = list(sinks)

    def register_resource(self, *a, **k):
        return [s.register_resource(*a, **k) for s in self.sinks]

    def task_start(self, *a, **k):
        return [s.task_start(*a, **k) for s in self.sinks]

    def task_end(self, *a, **k):
        return [s.task_end(*a, **k) for s in self.sinks]

    def task_validate(self, *a, **k):
        return [s.task_validate(*a, **k) for s in self.sinks]


# --------------------------------------------------------------------------- #
# Task helper
# --------------------------------------------------------------------------- #
@dataclass
class WorkRecord:
    event_id: str
    run_ref: str
    artifact: Artifact
    label: str
    vrw_points: int
    results: dict = field(default_factory=dict)


def submit_work(
    sink: WorkSink,
    data: bytes,
    *,
    label: str,
    vrw_points: int = 100,
    content_type: str = "image/jpeg",
    resource_name: str | None = None,
    resource_subtype: str | None = None,
    task_id: str | None = None,
    event_id: str | None = None,
) -> WorkRecord:
    """Store ``data`` once and run the full task lifecycle on ``sink``.

    start → store(Walrus + CID + sha256) → end(raw_data_uri/cid) → validate.
    Returns a :class:`WorkRecord` with the artifact and per-stage responses.
    """
    event_id = event_id or f"mpak-{uuid.uuid4().hex}"
    artifact = store_artifact(data, content_type=content_type)
    start = sink.task_start(event_id, resource_name=resource_name,
                            resource_subtype=resource_subtype, task_id=task_id)
    run_ref = _run_ref(start, event_id)
    end = sink.task_end(run_ref, artifact, label=label)
    validate = sink.task_validate(run_ref, vrw_points)
    return WorkRecord(event_id, run_ref, artifact, label, vrw_points,
                      {"start": start, "end": end, "validate": validate})


def _run_ref(start_response: Any, fallback: str) -> str:
    if isinstance(start_response, dict):
        return start_response.get("task_run_id") or fallback
    if isinstance(start_response, list):  # MultiSink — key off BitRobot's id
        for r in start_response:
            if isinstance(r, dict) and r.get("task_run_id"):
                return r["task_run_id"]
    return fallback


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
