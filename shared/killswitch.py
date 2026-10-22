"""Kill switch service (spec 6.3, S1-08). The DB already blocks sends (CP003) and emits adapter.killed
atomically (migration 0002); this adds credential revocation and the audit hook, and makes queues pause
(migration 0003). The store is injected, so the orchestration is unit-testable without a database.

Order matters: the switch is flipped FIRST and commits, so new sends are refused immediately. Revocation
comes after and is best-effort: a failed revocation is reported, never silently ignored, and never undoes
the kill. Re-running kill retries revocation. Reviving does NOT restore credentials; that is a human step.
No PII: scopes are enum names, actors and reason codes are opaque ids and codes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol, Tuple

from shared.bus.events import error_code

GLOBAL = "GLOBAL"


class CredentialRevoker(Protocol):
    def revoke(self, marketplace: str) -> None: ...


class NotConfiguredRevoker:
    """Stand-in until the secrets manager exists (S0-04). Fails loudly so nobody believes credentials were revoked."""

    def revoke(self, marketplace: str) -> None:
        raise RuntimeError("secrets manager not configured")


class KillStore(Protocol):
    def get_state(self, scope: str) -> bool: ...
    def set_state(self, scope: str, killed: bool, actor: str, reason_code: Optional[str]) -> None: ...
    def marketplaces(self) -> List[str]: ...
    def states(self) -> List[Tuple[str, bool]]: ...


@dataclass(frozen=True)
class KillResult:
    scope: str
    changed: bool                                          # False when the switch was already in that state
    failed_revocations: Tuple[Tuple[str, str], ...] = ()   # (marketplace, error class code)
    audit_error_code: Optional[str] = None

    @property
    def fully_applied(self) -> bool:
        return not self.failed_revocations and self.audit_error_code is None


class PgKillStore:
    """conn must be autocommit so each call commits on its own."""

    def __init__(self, conn):
        self.conn = conn

    def get_state(self, scope: str) -> bool:
        if scope == GLOBAL:
            row = self.conn.execute("SELECT killed FROM core.kill_switch WHERE is_global").fetchone()
        else:
            row = self.conn.execute("SELECT killed FROM core.kill_switch WHERE marketplace = %s::core.marketplace",
                                    (scope,)).fetchone()
        if row is None:
            raise ValueError(f"unknown kill-switch scope {scope!r}")
        return row[0]

    def set_state(self, scope: str, killed: bool, actor: str, reason_code: Optional[str]) -> None:
        self.conn.execute("SELECT core.set_kill_switch(%s, %s, %s, %s)", (scope, killed, actor, reason_code))

    def marketplaces(self) -> List[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT marketplace::text FROM core.kill_switch WHERE NOT is_global ORDER BY id").fetchall()]

    def states(self) -> List[Tuple[str, bool]]:
        return [(r[0], r[1]) for r in self.conn.execute(
            "SELECT CASE WHEN is_global THEN 'GLOBAL' ELSE marketplace::text END, killed "
            "FROM core.kill_switch ORDER BY id").fetchall()]


class KillSwitch:
    def __init__(self, store: KillStore, revoker: CredentialRevoker,
                 audit: Optional[Callable[[str, dict], None]] = None):
        self.store, self.revoker, self.audit = store, revoker, audit

    def _targets(self, scope: str) -> List[str]:
        known = self.store.marketplaces()
        if scope == GLOBAL:
            return known
        if scope not in known:
            raise ValueError(f"unknown kill-switch scope {scope!r}")   # before anything changes
        return [scope]

    def _audit(self, action: str, details: dict) -> Optional[str]:
        if self.audit is None:
            return None
        try:
            self.audit(action, details)
        except Exception as exc:  # noqa: BLE001 - the switch has already moved; report, never undo
            return error_code(exc)
        return None

    def kill(self, scope: str, actor: str, reason_code: str) -> KillResult:
        targets = self._targets(scope)
        was_killed = self.store.get_state(scope)
        self.store.set_state(scope, True, actor, reason_code)       # 1. flip: sends are refused from here on
        failed = []
        for marketplace in targets:                                 # 2. revoke, best effort, always attempted
            try:
                self.revoker.revoke(marketplace)
            except Exception as exc:  # noqa: BLE001
                failed.append((marketplace, error_code(exc)))
        audit_err = self._audit("KILL", {"scope": scope, "actor": actor, "reason_code": reason_code,
                                         "changed": not was_killed, "failed_revocations": len(failed)})
        return KillResult(scope, changed=not was_killed, failed_revocations=tuple(failed), audit_error_code=audit_err)

    def revive(self, scope: str, actor: str) -> KillResult:
        self._targets(scope)
        was_killed = self.store.get_state(scope)
        self.store.set_state(scope, False, actor, None)
        audit_err = self._audit("REVIVE", {"scope": scope, "actor": actor, "changed": was_killed})
        return KillResult(scope, changed=was_killed, audit_error_code=audit_err)

    def status(self) -> List[Tuple[str, bool]]:
        return self.store.states()
