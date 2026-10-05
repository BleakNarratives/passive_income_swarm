"""
swarm.contracts — standardized payload envelope (Relay 1)
=========================================================

> "ALL SWARM AGENTS: Standardize emitter schema to match ScoutLoader.write_sidecar
>  signature. Payload MUST include content_sha256 (P2 verification). Reject all
>  ad-hoc JSON without a valid P2 digest."

This module is the contract every emitter in the swarm must speak. An ad-hoc
dict is not telemetry; an :class:`Envelope` is. Anything that cannot present a
valid P2 digest is refused entry at the ingestion boundary — see
:func:`parse` and :class:`~swarm.ingest.Ingestor`.

ADAPTER BOUNDARY
----------------
``core_framework``'s ``ScoutLoader.write_sidecar`` signature is **not available
in this workspace**, so the field names below are the frozen contract the swarm
speaks today. When the framework is reachable, reconcile exactly one thing: the
``Envelope`` field mapping in :meth:`Envelope.to_sidecar`. Everything upstream
and downstream depends only on ``Envelope``.

P2 digest (the definition this repo uses)
-----------------------------------------
    content_sha256 = sha256( canonical_json(payload) )

where ``canonical_json`` is UTF-8, keys sorted, no insignificant whitespace
(``json.dumps(obj, sort_keys=True, separators=(",", ":"))``). Two emitters on
two machines therefore produce the same digest for the same payload, and any
byte of drift is detectable. If ``core_framework``'s P2 canonicalization differs,
swap :func:`content_digest` — nothing else changes.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

__all__ = [
    "SCHEMA",
    "KINDS",
    "Envelope",
    "PayloadError",
    "canonical_json",
    "content_digest",
    "verify_digest",
    "parse",
]

#: The envelope schema this swarm speaks. Bump only with a migration.
SCHEMA = "swarm.payload/1"

#: Legal ``kind`` values. Anything else is refused.
KINDS = ("telemetry", "pheromone", "intel", "skill_update")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class PayloadError(ValueError):
    """Raised when a payload violates the contract — always rejected, never coerced."""


def canonical_json(obj: Any) -> str:
    """Deterministic JSON used for hashing: sorted keys, tight separators, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_digest(payload: Any) -> str:
    """The P2 digest of a payload: bare hex sha256 of its canonical form."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def verify_digest(payload: Any, digest: str) -> bool:
    """True iff ``digest`` is the correct P2 digest for ``payload``."""
    if not isinstance(digest, str) or not _HEX64.match(digest):
        return False
    return content_digest(payload) == digest


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Envelope:
    """A signed-by-digest unit of swarm communication."""

    emitter: str
    kind: str
    payload: Dict[str, Any]
    content_sha256: str
    schema: str = SCHEMA
    created: str = field(default_factory=_now)
    confidence: float = 0.5
    lineage: str = ""
    tags: List[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # -- validation --------------------------------------------------------
    def validate(self) -> "Envelope":
        """Raise :class:`PayloadError` unless this envelope is contract-clean."""
        if self.schema != SCHEMA:
            raise PayloadError(f"unsupported schema '{self.schema}' (expected '{SCHEMA}')")
        if self.kind not in KINDS:
            raise PayloadError(f"unknown kind '{self.kind}' (expected one of {KINDS})")
        if not self.emitter:
            raise PayloadError("envelope missing 'emitter'")
        if not isinstance(self.payload, dict):
            raise PayloadError("'payload' must be a JSON object")
        if not isinstance(self.content_sha256, str) or not _HEX64.match(self.content_sha256):
            raise PayloadError("'content_sha256' must be 64 lowercase hex chars")
        if not verify_digest(self.payload, self.content_sha256):
            raise PayloadError(
                f"P2 digest mismatch for emitter '{self.emitter}': "
                f"expected {content_digest(self.payload)}, got {self.content_sha256}"
            )
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise PayloadError("'confidence' must be within [0,1]")
        return self

    # -- construction ------------------------------------------------------
    @classmethod
    def build(
        cls,
        emitter: str,
        kind: str,
        payload: Dict[str, Any],
        confidence: float = 0.5,
        lineage: str = "",
        tags: Optional[List[str]] = None,
    ) -> "Envelope":
        """Build an envelope, computing the P2 digest for you."""
        return cls(
            emitter=emitter,
            kind=kind,
            payload=dict(payload),
            content_sha256=content_digest(payload),
            confidence=float(confidence),
            lineage=lineage,
            tags=list(tags or []),
        ).validate()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    # -- ADAPTER: reconcile this one method with core_framework ------------
    def to_sidecar(self) -> Dict[str, Any]:
        """Project to the sidecar shape ``ScoutLoader.write_sidecar`` expects.

        **This is the single reconciliation point.** The rest of this repo
        never depends on core_framework's field names, only on ``Envelope``.
        """
        return {
            "schema": self.schema,
            "id": self.id,
            "emitter": self.emitter,
            "kind": self.kind,
            "created": self.created,
            "content_sha256": self.content_sha256,
            "confidence": self.confidence,
            "lineage": self.lineage,
            "tags": self.tags,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Envelope":
        if not isinstance(raw, dict):
            raise PayloadError("payload must be a JSON object")
        missing = [k for k in ("emitter", "kind", "payload", "content_sha256") if k not in raw]
        if missing:
            raise PayloadError(f"ad-hoc JSON rejected — missing required field(s): {missing}")
        env = cls(
            emitter=str(raw["emitter"]),
            kind=str(raw["kind"]),
            payload=dict(raw["payload"]),
            content_sha256=str(raw["content_sha256"]),
            schema=str(raw.get("schema", SCHEMA)),
            created=str(raw.get("created") or _now()),
            confidence=float(raw.get("confidence", 0.5)),
            lineage=str(raw.get("lineage") or ""),
            tags=list(raw.get("tags") or []),
            id=str(raw.get("id") or uuid.uuid4().hex[:12]),
        )
        return env.validate()


def parse(text: str) -> Envelope:
    """Parse-and-verify an emitter payload. Ad-hoc JSON is rejected, not coerced."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise PayloadError(f"not valid JSON: {exc}") from exc
    return Envelope.from_dict(raw)
