"""Verifiable Robotic Work — robot data → on-chain, through one ``WorkSink``.

A robot run produces an *artifact* (a clip or photo). The kit stores it once on
Walrus (a public URL) and computes its IPFS CID + sha256, then emits the work to
one or more sinks:

* :class:`BitRobotSink` — the canonical BitRobot path (docs.bitrobot.ai):
  ``POST /subnets/{id}/events`` with ``register_resource`` → ``task_start`` →
  ``task_end {raw_data_uri, raw_data_cid}`` → ``task_validate {vrw_points}`` →
  Subnet Points → Bolts; the resource is an Entity NFT on Solana.
* :class:`OnchainRoverSink` — the Arc/EVM stack (Clanker 500): register the
  ``(sha256, blobId)`` proof with the sidecar (``POST /proof``), which ``settle.ts``
  anchors on Arc via ``ReputationRegistry.giveFeedback``.
* :class:`SolanaRoverSink` — the Solana stack (Clanker 5000): the same ``/proof`` +
  ``/give-feedback`` HTTP surface, but the native-Solana sidecar anchors it on the
  ``clanker5000`` Anchor program (``give_feedback``: a per-agent reputation PDA with
  ``feedback_uri = walrus://blobId`` and ``feedback_hash = sha256``). Returns a
  Solana signature + explorer link.

One artifact, every ledger. Combine sinks with :class:`MultiSink` (e.g. BitRobot +
Solana from one robot run).
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

# Default attempts for bounded retry-with-backoff (see ``_retry``). One attempt
# means single-shot (no retry); env override keeps it tunable in the field.
WALRUS_PUT_ATTEMPTS = int(os.environ.get("WALRUS_PUT_ATTEMPTS", "3"))
SIDECAR_POST_ATTEMPTS = int(os.environ.get("SIDECAR_POST_ATTEMPTS", "3"))
RETRY_BACKOFF = float(os.environ.get("RETRY_BACKOFF", "0.5"))


def _is_transport_error(exc: Exception) -> bool:
    """True for retryable transport failures (connect/timeout/read), not for an
    HTTP error *response* (``raise_for_status`` → ``HTTPStatusError``).

    ``httpx.RequestError`` is the base of every transport-level error; an HTTP
    error status is a *definitive* answer from the server (e.g. a chain rejection
    behind the sidecar) and must NOT be retried. Falls back gracefully when the
    httpx stub lacks these classes.
    """
    status_err = getattr(httpx, "HTTPStatusError", None)
    if status_err is not None and isinstance(exc, status_err):
        return False
    request_err = getattr(httpx, "RequestError", None)
    if request_err is not None:
        return isinstance(exc, request_err)
    # Stub httpx without typed exceptions: treat anything not a status error as
    # transport-level so the idempotent retry path still exercises.
    return True


def _retry(fn, *, attempts: int, backoff: float = RETRY_BACKOFF, sleep=time.sleep):
    """Call ``fn`` up to ``attempts`` times, retrying only transport errors.

    Backoff is exponential (``backoff * 2**i``). A non-transport error (HTTP
    status / chain rejection) is re-raised immediately, never retried. The
    success path returns ``fn()``'s value unchanged. ``sleep`` is injectable so
    tests need not actually wait.
    """
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - re-raised below
            if not _is_transport_error(e) or i == attempts - 1:
                raise
            last = e
            sleep(backoff * (2 ** i))
    assert last is not None  # unreachable: loop either returns or raises
    raise last


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    """POST ``payload`` and return the decoded JSON body (raises on error status)."""
    r = httpx.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


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


def walrus_put(
    data: bytes, epochs: int = 5, timeout: float = 60.0, attempts: int = WALRUS_PUT_ATTEMPTS
) -> str:
    """Store bytes on Walrus → blobId (handles both publisher response shapes).

    Idempotent: a re-PUT of already-stored bytes returns ``alreadyCertified``, so
    transport failures are retried with bounded backoff (``attempts``). HTTP error
    statuses are not retried. ``attempts=1`` is single-shot.
    """

    def _put() -> str:
        r = httpx.put(
            f"{WALRUS_PUBLISHER}/v1/blobs", params={"epochs": epochs}, content=data, timeout=timeout
        )
        r.raise_for_status()
        j = r.json()
        if "newlyCreated" in j:
            return j["newlyCreated"]["blobObject"]["blobId"]
        return j["alreadyCertified"]["blobId"]

    return _retry(_put, attempts=attempts)


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
        attempts: int = SIDECAR_POST_ATTEMPTS,
    ):
        # SIDECAR_URL collides with the Solana sidecar on port 4021, so prefer the
        # Arc-specific ARC_SIDECAR_URL, then fall back to the shared SIDECAR_URL.
        self.sidecar_url = (
            sidecar_url or os.environ.get("ARC_SIDECAR_URL")
            or os.environ.get("SIDECAR_URL", "http://localhost:4021")
        ).rstrip("/")
        self.robot = robot
        self.skill = skill
        self.score = score
        self.anchor = anchor
        self.timeout = timeout
        self.attempts = attempts
        self._pending: dict[str, tuple[Artifact, str]] = {}

    def register_resource(self, name, subtype, owner, metadata) -> dict:
        return {"ok": True, "note": "onchain-rover registers agents via ENS/ERC-8004, not here"}

    def task_start(self, event_id: str, **kw) -> dict:
        return {"ok": True, "task_run_id": event_id}

    def task_end(self, run_ref: str, artifact: Artifact, *, label: str = "agent work", **kw) -> dict:
        self._pending[run_ref] = (artifact, label)
        try:
            return _retry(lambda: _post_json(
                f"{self.sidecar_url}/proof",
                {"blobId": artifact.walrus_blob_id, "sha256": artifact.sha256, "label": label},
                self.timeout,
            ), attempts=self.attempts)
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
            # Retry only transport errors; a chain rejection comes back as an HTTP
            # error status and is surfaced (not retried) by ``_retry``.
            return _retry(lambda: _post_json(
                f"{self.sidecar_url}/give-feedback",
                {"robot": self.robot, "skill": self.skill, "score": score,
                 "blobId": artifact.walrus_blob_id, "sha256": sha},
                self.timeout,
            ), attempts=self.attempts)  # { tx, status, explorer, ... }
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


def solana_explorer_tx(signature: str, cluster: str = "devnet") -> str:
    """Solana Explorer URL for a transaction signature (mainnet drops the cluster)."""
    url = f"https://explorer.solana.com/tx/{signature}"
    return url if cluster == "mainnet-beta" else f"{url}?cluster={cluster}"


class SolanaRoverSink(WorkSink):
    """Anchor robot work on the ``clanker5000`` Solana program (the Clanker 5000 stack).

    The Solana counterpart of :class:`OnchainRoverSink` (Arc/EVM). The
    onchain-rover-solana sidecar (port 4021) is *native-Solana* — its chain backend
    drives the ``clanker5000`` Anchor program — and serves the same HTTP surface:

    * ``task_end``  → ``POST /proof {blobId, sha256, label}`` (the tracker the UI reads).
    * ``task_validate`` → ``POST /give-feedback {robot, skill, score, blobId, sha256}``,
      which calls ``settle.giveFeedback`` → ``clanker5000.give_feedback``: a per-agent
      reputation PDA storing ``feedback_uri = walrus://{blobId}`` and ``feedback_hash =
      sha256`` (32 bytes). Score is 0–100; ``≥ 70`` clears the program's attestation
      threshold (``ATTESTATION_THRESHOLD``). Returns the Solana transaction signature,
      surfaced here with an explorer link and a ``verified`` flag.

    The robot's on-chain agent PDA is resolved sidecar-side from ``robot`` — register
    it once via the sidecar's ``/register-agent`` (→ ``clanker5000.register_agent``).
    ``sha256`` may be sent 0x-prefixed or bare (the Solana client strips ``0x``).
    Set ``anchor=False`` to register the proof without the chain write.
    """

    def __init__(
        self,
        sidecar_url: str | None = None,
        *,
        robot: str = "guard",
        skill: str = "deliver",
        score: int | None = None,
        anchor: bool = True,
        cluster: str = "devnet",
        timeout: float = 30.0,
        attempts: int = SIDECAR_POST_ATTEMPTS,
    ):
        # Solana sidecar keeps its own SOLANA_SIDECAR_URL; the shared SIDECAR_URL
        # (port 4021) is only a last-resort fallback for backward compatibility.
        self.sidecar_url = (
            sidecar_url or os.environ.get("SOLANA_SIDECAR_URL")
            or os.environ.get("SIDECAR_URL", "http://localhost:4021")
        ).rstrip("/")
        self.robot = robot
        self.skill = skill
        self.score = score
        self.anchor = anchor
        self.cluster = cluster
        self.timeout = timeout
        self.attempts = attempts
        self._pending: dict[str, tuple[Artifact, str]] = {}

    def register_resource(self, name, subtype, owner, metadata) -> dict:
        return {"ok": True,
                "note": "register via the sidecar /register-agent → clanker5000.register_agent"}

    def task_start(self, event_id: str, **kw) -> dict:
        return {"ok": True, "task_run_id": event_id}

    def task_end(self, run_ref: str, artifact: Artifact, *, label: str = "agent work", **kw) -> dict:
        self._pending[run_ref] = (artifact, label)
        try:
            return _retry(lambda: _post_json(
                f"{self.sidecar_url}/proof",
                {"blobId": artifact.walrus_blob_id, "sha256": artifact.sha256, "label": label},
                self.timeout,
            ), attempts=self.attempts)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def task_validate(self, run_ref: str, vrw_points: int) -> dict:
        artifact, label = self._pending.pop(run_ref, (None, "agent work"))
        if not self.anchor:
            return {"ok": True, "note": "anchor disabled"}
        if artifact is None:
            return {"ok": False, "error": "no artifact recorded for this run"}
        # clanker5000 stores a 0-100 reputation value; map VRW points unless overridden.
        score = self.score if self.score is not None else max(0, min(100, int(vrw_points)))
        # the Solana client strips a leading 0x; send bare hex for consistency.
        sha = artifact.sha256[2:] if artifact.sha256.startswith("0x") else artifact.sha256
        try:
            # Retry only transport errors; a definitive chain rejection (HTTP error
            # status) is surfaced immediately, not retried.
            out = _retry(lambda: _post_json(
                f"{self.sidecar_url}/give-feedback",
                {"robot": self.robot, "skill": self.skill, "score": score,
                 "blobId": artifact.walrus_blob_id, "sha256": sha},
                self.timeout,
            ), attempts=self.attempts)
            sig = out.get("tx") or out.get("signature")
            if sig and "explorer" not in out:
                out["explorer"] = solana_explorer_tx(sig, self.cluster)
            out.setdefault("verified", score >= 70)   # clanker5000 ATTESTATION_THRESHOLD
            return out
        except Exception as e:
            return {"ok": False, "error": str(e)}


class MultiSink(WorkSink):
    """Fan a single piece of work out to several sinks.

    Each sink is called independently: a failure in one sink is captured as
    ``{"ok": False, "error": ...}`` in its slot so the remaining sinks still run
    (one ledger being down must not abort the rest of the fan-out).
    """

    def __init__(self, *sinks: WorkSink):
        self.sinks = list(sinks)

    def _fan(self, method: str, *a, **k) -> list:
        out = []
        for s in self.sinks:
            try:
                out.append(getattr(s, method)(*a, **k))
            except Exception as e:  # noqa: BLE001 - isolate one sink's failure
                out.append({"ok": False, "error": str(e)})
        return out

    def register_resource(self, *a, **k):
        return self._fan("register_resource", *a, **k)

    def task_start(self, *a, **k):
        return self._fan("task_start", *a, **k)

    def task_end(self, *a, **k):
        return self._fan("task_end", *a, **k)

    def task_validate(self, *a, **k):
        return self._fan("task_validate", *a, **k)


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

    @property
    def ok(self) -> bool:
        """True unless some stage result explicitly reports ``ok == False``.

        Each stage value is either a single response (a sink's dict) or a list of
        responses (a :class:`MultiSink` fan-out). A result is failing only if a
        dict in it carries ``ok`` set to a falsy value; results without an ``ok``
        key (e.g. a successful ``{"tx": ...}``) count as passing.
        """
        def _stage_ok(result: Any) -> bool:
            items = result if isinstance(result, list) else [result]
            for item in items:
                if isinstance(item, dict) and "ok" in item and not item["ok"]:
                    return False
            return True

        return all(_stage_ok(r) for r in self.results.values())


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
