"""The audit writer (S1-07): the single choke point into audit.event.

What it guarantees:
  * Only this module's SQL path reaches audit.event. The database enforces it: INSERT is revoked from the
    application roles, and audit.record_event() is the only way in (migration 0004).
  * No PII is stored. Payloads and external responses are accepted only to be hashed; the hash is
    sha256(salt || label || 0x00 || data) computed INSIDE the database with a per-lead salt that never leaves it.
    Erasing a lead deletes the salt, which unlinks every hash that lead ever produced.
  * Hashing needs a lead. An event with no lead cannot carry a payload.
  * Salts are generated here with secrets.token_bytes(32) (review checklist A1); the database creates the
    lead's salt on first use and ignores the candidate afterwards.
Everything stored is an opaque id, a code, an enum value or a 32-byte hash.
"""
from __future__ import annotations

import json
import re
import secrets
import uuid
from typing import Any, Callable, Mapping, Optional, Union

_ACTOR = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
_ACTION = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_OUTCOME = re.compile(r"^[A-Z0-9_]{1,64}$")
_VERDICTS = frozenset({"PASS", "BLOCK", "ESCALATE"})
_OUTBOUND = frozenset({"SEND_MESSAGE", "CREATE_OFFER", "REPLY"})     # these must name a marketplace (DB CHECK)

Data = Union[Mapping[str, Any], bytes, str]


def canonical_bytes(data: Data) -> bytes:
    """Stable bytes for hashing: JSON objects with sorted keys and no spaces, str as UTF-8, bytes as given."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, Mapping):
        return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          default=str).encode("utf-8")
    raise TypeError(f"cannot hash {type(data).__name__}")


class AuditWriter:
    """conn must be a psycopg connection whose role can EXECUTE audit.hash_for and audit.record_event
    (app_rw and audit_writer can)."""

    def __init__(self, conn, *, token_bytes: Callable[[int], bytes] = secrets.token_bytes):
        self.conn = conn
        self._token_bytes = token_bytes

    def record(self, actor: str, action_type: str, *, event_id: Optional[uuid.UUID] = None,
               marketplace: Optional[str] = None, lead_id: Optional[uuid.UUID] = None,
               product_id: Optional[uuid.UUID] = None, payload: Optional[Data] = None,
               external_response: Optional[Data] = None, compliance_verdict: Optional[str] = None,
               autonomy_tier: Optional[int] = None, outcome: Optional[str] = None,
               trace_id: Optional[uuid.UUID] = None) -> Optional[int]:
        """Insert one audit event. Returns its sequence number, or None if event_id was already recorded."""
        self._validate(actor, action_type, marketplace, lead_id, payload, external_response,
                       compliance_verdict, autonomy_tier, outcome)
        eid = event_id or uuid.uuid4()
        with self.conn.transaction():
            payload_hash = self._hash(lead_id, "payload", payload)
            response_hash = self._hash(lead_id, "response", external_response)
            row = self.conn.execute(
                "SELECT audit.record_event(%s, %s, %s::core.marketplace, %s, %s, %s, %s::bytea, %s, "
                "%s::smallint, %s, %s::bytea, %s)",
                (eid, actor, marketplace, lead_id, product_id, action_type, payload_hash, compliance_verdict,
                 autonomy_tier, outcome, response_hash, trace_id)).fetchone()
        return row[0]

    def hash_payload(self, lead_id: uuid.UUID, data: Data) -> bytes:
        """The salted hash the database would store for this payload, for callers that keep the hash on another
        row (a proposal's payload_hash). Same function and salt as record(); the salt never leaves the database."""
        return self._hash(lead_id, "payload", data)

    def _hash(self, lead_id, label: str, data: Optional[Data]) -> Optional[bytes]:
        if data is None:
            return None
        return self.conn.execute("SELECT audit.hash_for(%s, %s, %s::bytea, %s::bytea)",
                                 (lead_id, label, canonical_bytes(data), self._token_bytes(32))).fetchone()[0]

    @staticmethod
    def _validate(actor, action_type, marketplace, lead_id, payload, external_response, verdict, tier, outcome):
        if not _ACTOR.match(actor or ""):
            raise ValueError("actor must be an opaque id (letters, digits and _:.-), never an email or name")
        if not _ACTION.match(action_type or ""):
            raise ValueError("action_type must be an UPPER_SNAKE code")
        if outcome is not None and not _OUTCOME.match(outcome):
            raise ValueError("outcome must be an UPPER_SNAKE code")
        if verdict is not None and verdict not in _VERDICTS:
            raise ValueError("compliance_verdict must be PASS, BLOCK or ESCALATE")
        if tier is not None and (isinstance(tier, bool) or tier not in (0, 1, 2)):
            raise ValueError("autonomy_tier must be 0, 1 or 2")
        if action_type in _OUTBOUND and marketplace is None:
            raise ValueError(f"{action_type} events must name a marketplace")
        if lead_id is None and (payload is not None or external_response is not None):
            raise ValueError("a payload or response can only be recorded for a lead (its salt makes erasure unlink it)")


def kill_switch_audit_hook(writer: AuditWriter) -> Callable[[str, dict], None]:
    """Adapter for KillSwitch(audit=...): records KILL / REVIVE as audit events (ids and codes only)."""
    names = {"KILL": "KILL_SWITCH_ENGAGED", "REVIVE": "KILL_SWITCH_CLEARED"}

    def hook(action: str, details: dict) -> None:
        scope = details["scope"]
        writer.record(details["actor"], names[action], marketplace=None if scope == "GLOBAL" else scope,
                      outcome="OK" if not details.get("failed_revocations") else "REVOCATION_FAILED")

    return hook
