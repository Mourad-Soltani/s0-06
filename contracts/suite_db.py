"""The database-backed contract: a kill switch halts a marketplace's queue within the deadline, leaves other
marketplaces running, and the queue resumes on revive. Needs a database with every migration applied."""
from __future__ import annotations

import threading
import time

from shared.bus.events import EventType
from shared.bus.publisher import publish
from shared.bus.runner import run_loop
from shared.bus.store import PgOutboxStore
from shared.bus.worker import Worker
from shared.killswitch import KillSwitch, PgKillStore


class _NoRevoker:
    def revoke(self, marketplace: str) -> None:
        return None


def _wait_until(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def check_kill_switch_halts_queue(db, marketplace: str, other: str = "PAYHIP", window: float = 1.0,
                                  deadline: float = 5.0) -> None:
    import psycopg
    seen = {marketplace: [], other: []}
    lock, stop, errs = threading.Lock(), threading.Event(), []

    def record(conn, ev):
        with lock:
            seen[ev.marketplace].append(ev.event_id)

    def loop():
        try:
            with psycopg.connect(db.dsn, autocommit=True) as conn:
                w = Worker(PgOutboxStore(conn), "contract-worker")
                w.subscribe(EventType.LEAD_SCORED, "contract", record)
                run_loop(w, stop, poll_interval=0.1)
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    t = threading.Thread(target=loop)
    t.start()
    try:
        for m in (marketplace, other):
            publish(db.conn, EventType.LEAD_SCORED, marketplace=m)
        assert _wait_until(lambda: len(seen[marketplace]) == 1 and len(seen[other]) == 1, deadline), \
            "baseline events were not processed"
        ks = KillSwitch(PgKillStore(db.conn), _NoRevoker())
        ks.kill(marketplace, "contract-test", "CONTRACT")
        killed_at = time.monotonic()
        for m in (marketplace, other):
            publish(db.conn, EventType.LEAD_SCORED, marketplace=m)
        assert _wait_until(lambda: len(seen[other]) == 2, deadline), "an unrelated marketplace must keep running"
        assert time.monotonic() - killed_at <= deadline, f"queues took longer than {deadline}s to settle"
        time.sleep(window)
        assert len(seen[marketplace]) == 1, f"{marketplace} kept processing after it was killed"
        pending = db.conn.execute(
            "SELECT count(*) FROM core.outbox_event WHERE marketplace=%s::core.marketplace AND status='PENDING' "
            "AND attempts=0", (marketplace,)).fetchone()[0]
        assert pending == 1, "a paused event must stay PENDING with no attempt consumed"
        ks.revive(marketplace, "contract-test")
        assert _wait_until(lambda: len(seen[marketplace]) == 2, deadline), f"{marketplace} did not resume within {deadline}s"
    finally:
        stop.set()
        t.join(10)
    assert not errs, errs
