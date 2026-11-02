"""Command line for the reconciliation job: python -m orchestrator.reconciler_cli [--stale-minutes N] [--limit N]. Needs DATABASE_URL (a login role
that is a member of app_rw: the job reads no message bodies) and the channel adapters built from your configuration; until an email provider exists
(cutover C3) this only runs against the fake transport. Run it on a schedule, e.g. every five minutes. NOT tested here: the logic it calls is."""
from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager
from datetime import timedelta


def main(argv=None, channel_adapters=None) -> int:
    p = argparse.ArgumentParser(description="Settle proposals stuck in SENDING")
    p.add_argument("--stale-minutes", type=int, default=15)
    p.add_argument("--limit", type=int, default=50)
    a = p.parse_args(argv)
    import psycopg
    from orchestrator.reconciler import Reconciler

    if not channel_adapters:
        print("No channel adapters configured: build them from your provider settings and call main(channel_adapters=...).", file=sys.stderr)
        return 2

    @contextmanager
    def connect():
        conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
        try:
            yield conn
        finally:
            conn.close()

    for r in Reconciler(connect, channel_adapters, stale_after=timedelta(minutes=a.stale_minutes)).run(a.limit):
        print(f"{r.proposal_id}: {r.outcome}{' (alerted)' if r.alerted else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
