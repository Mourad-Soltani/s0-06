"""Single-worker guard (S1-05): one Postgres session-level advisory lock per marketplace-account lane.

The in-process governor is only correct if exactly one worker drives a lane. A second worker calling
acquire() gets LaneBusy immediately instead of silently double-spending the budget. The lock lives and dies
with the SESSION, so a crashed worker releases it automatically.

Rules: hold it on a DEDICATED connection kept open for the worker's lifetime (never a pooled connection that
returns to a pool, where the lock would outlive its owner). Advisory locks are re-entrant within one session,
so the guard protects across connections, not across two objects sharing one connection.
"""
from __future__ import annotations

import zlib


class LaneBusy(Exception):
    """Another session already drives this lane."""


def _key(text: str) -> int:
    """Stable signed 32-bit key (pg_try_advisory_lock(int, int) takes two int4)."""
    k = zlib.crc32(text.encode("utf-8"))
    return k - 2 ** 32 if k >= 2 ** 31 else k


class LaneLock:
    def __init__(self, conn, marketplace, account_id: str):
        self.conn = conn
        self._keys = (_key("lane:" + str(getattr(marketplace, "value", marketplace))), _key(account_id))
        self._held = False

    def acquire(self) -> "LaneLock":
        if self._held:
            raise RuntimeError("this LaneLock is already held")
        got = self.conn.execute("SELECT pg_try_advisory_lock(%s::int4, %s::int4)", self._keys).fetchone()[0]
        if not got:
            raise LaneBusy("another worker already drives this lane")
        self._held = True
        return self

    def release(self) -> None:
        if self._held:
            self.conn.execute("SELECT pg_advisory_unlock(%s::int4, %s::int4)", self._keys)
            self._held = False

    def __enter__(self) -> "LaneLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
