"""Consented lead intake (S3-05, ADR-02, ADR-21): a creator uploads a CSV of people who opted in, and each one becomes a
contact with a recorded consent and a lead the pipeline can work. This is the only way a lead gets a contact.

The rules, in the order they are applied to each row:
  * only known columns are accepted (email, name, consented_at, source_url, product_id, kind); an unknown column fails the
    whole file, so a stray phone number column cannot be ingested by accident;
  * a row needs an address; a kind (SUBSCRIBER or SALE); an opaque product id if it has one;
  * consent evidence is the consent date AND an https source URL (the row's, or the import's default). A row without both
    is KEPT QUARANTINED, unreachable, and gets no lead (ADR-02: unclear provenance). A consent older than the expiry window
    is quarantined too: it needs a fresh consent;
  * a consent dated in the future is rejected, and so is any date or URL that is present but malformed;
  * a contact who withdrew at or after the consent date in the row is SKIPPED: an old list never re-subscribes someone who
    opted out. Only a consent dated after the withdrawal restores them;
  * re-running the same file changes nothing (identical consent is reused, one lead per contact and product).
Each row commits on its own (consent, lead and signal.captured event together). The report names line numbers and codes,
never an address or a name. conn must be an autocommit connection with the review_svc role (it reaches pii)."""
from __future__ import annotations

import csv
import hashlib
import io
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from orchestrator.scoring import SignalFeatures
from shared.bus.events import EventType, event_id_for

ALLOWED_COLUMNS = ("email", "name", "consented_at", "source_url", "product_id", "kind")
KINDS = ("SUBSCRIBER", "SALE")
MAX_BYTES, MAX_ROWS = 1_000_000, 5_000
CHANNEL = "EMAIL"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ID = re.compile(r"^[A-Za-z0-9_:.=-]{1,128}$")
_ACTOR = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

# outcomes
IMPORTED, ALREADY_IMPORTED, CONSENT_REFRESHED = "IMPORTED", "ALREADY_IMPORTED", "CONSENT_REFRESHED"
WOULD_IMPORT = "WOULD_IMPORT"
Q_NO_EVIDENCE, Q_EXPIRED = "QUARANTINED_NO_EVIDENCE", "QUARANTINED_EXPIRED"
SKIPPED_WITHDRAWN, SKIPPED_DUPLICATE = "SKIPPED_WITHDRAWN", "SKIPPED_DUPLICATE"
REJECTED = {"email": "REJECTED_INVALID_EMAIL", "kind": "REJECTED_INVALID_KIND", "product": "REJECTED_INVALID_PRODUCT",
            "url": "REJECTED_INVALID_SOURCE_URL", "date": "REJECTED_INVALID_CONSENT_DATE", "future": "REJECTED_CONSENT_IN_FUTURE",
            "row": "REJECTED_INVALID_ROW"}


class IntakeError(ValueError):
    """The file as a whole cannot be imported. The code is fixed text, never file content."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Row:
    line: int
    email: str = ""
    name: Optional[str] = None
    consented_at: Optional[datetime] = None
    source_url: Optional[str] = None
    product_id: Optional[str] = None
    kind: str = "SUBSCRIBER"
    outcome: Optional[str] = None        # set when the row is already decided; None means ready for the database


@dataclass(frozen=True)
class RowResult:
    line: int
    outcome: str


@dataclass(frozen=True)
class ImportReport:
    results: Tuple[RowResult, ...]
    dry_run: bool

    @property
    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in self.results:
            out[r.outcome] = out.get(r.outcome, 0) + 1
        return out


def _parse_time(value: str) -> datetime:
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _https(url: str) -> bool:
    return url.startswith("https://") and " " not in url and len(url) <= 2048


def parse_csv(data, *, now: datetime, ttl_days: int = 365, default_source_url: Optional[str] = None) -> List[Row]:
    """Pure: reads and classifies. Nothing here touches the database."""
    if isinstance(data, (bytes, bytearray)):
        if len(data) > MAX_BYTES:
            raise IntakeError("FILE_TOO_LARGE")
        try:
            data = bytes(data).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise IntakeError("NOT_UTF8") from None
    elif len(data.encode("utf-8")) > MAX_BYTES:
        raise IntakeError("FILE_TOO_LARGE")
    if default_source_url is not None and not _https(default_source_url):
        raise IntakeError("INVALID_DEFAULT_SOURCE_URL")
    reader = csv.reader(io.StringIO(data), strict=True)
    try:
        header = next((r for r in reader if any(c.strip() for c in r)), None)
        if header is None:
            raise IntakeError("EMPTY_FILE")
        header = [h.strip().lower() for h in header]
        if len(set(header)) != len(header):
            raise IntakeError("DUPLICATE_COLUMN")
        if any(h not in ALLOWED_COLUMNS for h in header):
            raise IntakeError("UNKNOWN_COLUMN")
        if "email" not in header:
            raise IntakeError("MISSING_EMAIL_COLUMN")
        rows: List[Row] = []
        seen = set()
        for raw in reader:
            if not any(c.strip() for c in raw):
                continue
            if len(rows) >= MAX_ROWS:
                raise IntakeError("TOO_MANY_ROWS")
            rows.append(_classify(reader.line_num, raw, header, now, ttl_days, default_source_url, seen))
    except csv.Error:
        raise IntakeError("MALFORMED_CSV") from None
    return rows


def _classify(line, raw, header, now, ttl_days, default_url, seen) -> Row:
    if len(raw) != len(header):
        return Row(line, outcome=REJECTED["row"])
    f = {h: v.strip() for h, v in zip(header, raw)}
    email, kind = f.get("email", ""), (f.get("kind") or "SUBSCRIBER").upper()
    product, url, when = f.get("product_id") or None, f.get("source_url") or default_url, f.get("consented_at") or None
    name = _CTRL.sub(" ", f.get("name", ""))[:120].strip() or None

    def done(outcome, **kw):
        return Row(line, email=email, name=name, consented_at=kw.get("consented_at"), source_url=url, product_id=product,
                   kind=kind, outcome=outcome)

    if not _EMAIL.match(email) or len(email) > 254:
        return Row(line, outcome=REJECTED["email"])
    if kind not in KINDS:
        return Row(line, outcome=REJECTED["kind"])
    if product is not None and not _ID.match(product):
        return Row(line, outcome=REJECTED["product"])
    if url is not None and not _https(url):
        return Row(line, outcome=REJECTED["url"])
    parsed = None
    if when is not None:
        try:
            parsed = _parse_time(when)
        except ValueError:
            return Row(line, outcome=REJECTED["date"])
    if parsed is None or url is None:
        return done(Q_NO_EVIDENCE)
    if parsed > now + timedelta(days=1):
        return Row(line, outcome=REJECTED["future"])
    if parsed + timedelta(days=ttl_days) <= now:
        return done(Q_EXPIRED)
    key = (email.lower(), product or "")                 # duplicates are judged among rows that would create a lead
    if key in seen:
        return Row(line, outcome=SKIPPED_DUPLICATE)
    seen.add(key)
    return Row(line, email=email, name=name, consented_at=parsed, source_url=url, product_id=product, kind=kind)


def lead_identity(contact_id, product_id: Optional[str]) -> Tuple[str, str]:
    """(signal_ref, signal_id) for the lead of one contact and product: stable, opaque, unique."""
    ref = "imp_" + hashlib.sha256(f"{contact_id}|{product_id or ''}".encode("utf-8")).hexdigest()[:32]
    return ref, hashlib.sha256(f"INBOUND|{CHANNEL}|{ref}".encode("utf-8")).hexdigest()


def intake_audit_hook(writer) -> Callable[[str, dict], None]:
    def hook(action: str, d: dict) -> None:
        writer.record(d["actor"], action, marketplace=d.get("marketplace"), outcome=d.get("outcome"))
    return hook


class ConsentedLeadIntake:
    def __init__(self, conn, consent, audit: Optional[Callable[[str, dict], None]] = None, publish: Optional[Callable] = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.conn, self.consent, self._audit, self._publish, self._clock = conn, consent, audit, publish, clock

    def import_csv(self, data, *, actor: str, default_source_url: Optional[str] = None, ttl_days: int = 365,
                   dry_run: bool = False) -> ImportReport:
        if not isinstance(actor, str) or not _ACTOR.match(actor):
            raise ValueError("actor must be an opaque id (letters, digits and _:.-)")
        if isinstance(ttl_days, bool) or not isinstance(ttl_days, int) or ttl_days <= 0:
            raise ValueError("ttl_days must be a positive integer")
        rows = parse_csv(data, now=self._clock(), ttl_days=ttl_days, default_source_url=default_source_url)
        results = []
        for row in rows:
            if row.outcome in (Q_NO_EVIDENCE, Q_EXPIRED):
                if not dry_run:
                    self.consent.import_unverified(row.email, name=row.name)          # kept, quarantined, no lead
                results.append(RowResult(row.line, row.outcome))
            elif row.outcome is not None:
                results.append(RowResult(row.line, row.outcome))
            else:
                results.append(RowResult(row.line, self._apply(row, actor, ttl_days, dry_run)))
        if self._audit and not dry_run:
            self._audit("CONSENT_IMPORT_COMPLETED", {"actor": actor, "marketplace": CHANNEL, "outcome": "OK"})
        return ImportReport(tuple(results), dry_run)

    # ------------------------------------------------------------------ one ready row
    def _apply(self, row: Row, actor: str, ttl_days: int, dry_run: bool) -> str:
        with self.conn.transaction():
            found = self.conn.execute("SELECT contact_id FROM pii.contact WHERE lower(email) = lower(%s)", (row.email,)).fetchone()
            contact_id = found[0] if found else None
            perm = None
            if contact_id is not None:
                withdrawn = self.conn.execute(
                    "SELECT max(withdrawn_at) FROM pii.contact_permission WHERE contact_id = %s AND channel = %s::core.marketplace",
                    (contact_id, CHANNEL)).fetchone()[0]
                if withdrawn is not None and withdrawn >= row.consented_at:
                    return SKIPPED_WITHDRAWN                                          # an old list never undoes an opt-out
                same = self.conn.execute(
                    "SELECT id FROM pii.contact_permission WHERE contact_id = %s AND channel = %s::core.marketplace "
                    "AND captured_at = %s AND source_url = %s AND withdrawn_at IS NULL LIMIT 1",
                    (contact_id, CHANNEL, row.consented_at, row.source_url)).fetchone()
                perm = same[0] if same else None
            if dry_run:
                if contact_id is not None:
                    ref, sid = lead_identity(contact_id, row.product_id)
                    if self.conn.execute("SELECT 1 FROM core.lead WHERE signal_id = %s", (sid,)).fetchone():
                        return ALREADY_IMPORTED
                return WOULD_IMPORT
            captured_new = perm is None
            if captured_new:
                perm = self.consent.capture(row.email, CHANNEL, row.source_url, name=row.name, captured_at=row.consented_at,
                                            ttl_days=ttl_days, actor=actor)
            if contact_id is None:
                contact_id = self.conn.execute("SELECT contact_id FROM pii.contact_permission WHERE id = %s", (perm,)).fetchone()[0]
            ref, sid = lead_identity(contact_id, row.product_id)
            lead = self.conn.execute(
                "INSERT INTO core.lead (source_type, source_marketplace, signal_ref, signal_id, contact_id, product_external_id, status) "
                "VALUES ('INBOUND', %s::core.marketplace, %s, %s, %s, %s, 'SIGNAL_CAPTURED') ON CONFLICT (signal_id) DO NOTHING RETURNING id",
                (CHANNEL, ref, sid, contact_id, row.product_id)).fetchone()
            if lead is None:
                return CONSENT_REFRESHED if captured_new else ALREADY_IMPORTED
            features = SignalFeatures(kind=row.kind, occurred_at=row.consented_at, product_external_id=row.product_id, has_contact=True)
            publish = self._publish or _default_publish()
            publish(self.conn, EventType.SIGNAL_CAPTURED, event_id=event_id_for(EventType.SIGNAL_CAPTURED, sid), marketplace=CHANNEL,
                    lead_id=lead[0], payload=features.to_payload(), trace_id=lead[0])
            return IMPORTED


def _default_publish():
    from shared.bus.publisher import publish          # imported late: needs the database driver
    return publish
