"""Kill switch CLI: reachable without a deploy (spec 6.3).
  python -m shared.killswitch_cli kill GLOBAL --actor ops-1 --reason INCIDENT_2026_10
  python -m shared.killswitch_cli revive GUMROAD --actor ops-1
  python -m shared.killswitch_cli status
DSN from --dsn or DATABASE_URL. Exit codes: 0 done, 2 kill applied but a revocation or audit step failed
(printed loudly), 1 error. An HTTP API needs a framework decision and is not part of this ticket."""
from __future__ import annotations

import argparse
import os
import sys
from typing import Callable, List, Optional

from shared.killswitch import KillSwitch, NotConfiguredRevoker, PgKillStore


def _default_service(dsn: str) -> KillSwitch:
    import psycopg  # imported late so the module loads without a database driver
    conn = psycopg.connect(dsn, autocommit=True)
    return KillSwitch(PgKillStore(conn), NotConfiguredRevoker())


def main(argv: Optional[List[str]] = None, *, make_service: Callable[[str], KillSwitch] = _default_service,
         out: Callable[[str], None] = print) -> int:
    p = argparse.ArgumentParser(prog="killswitch")
    p.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("kill")
    k.add_argument("scope")
    k.add_argument("--actor", required=True)
    k.add_argument("--reason", required=True)
    r = sub.add_parser("revive")
    r.add_argument("scope")
    r.add_argument("--actor", required=True)
    sub.add_parser("status")
    args = p.parse_args(argv)
    if not args.dsn:
        out("error: no --dsn and DATABASE_URL is not set")
        return 1
    try:
        ks = make_service(args.dsn)
        if args.cmd == "status":
            for scope, killed in ks.status():
                out(f"{scope:<14} {'KILLED' if killed else 'running'}")
            return 0
        res = ks.kill(args.scope, args.actor, args.reason) if args.cmd == "kill" else ks.revive(args.scope, args.actor)
    except Exception as exc:  # noqa: BLE001
        out(f"error: {type(exc).__name__}")
        return 1
    out(f"{args.cmd} {res.scope}: {'changed' if res.changed else 'already in that state'}")
    for marketplace, code in res.failed_revocations:
        out(f"CREDENTIALS NOT REVOKED for {marketplace} ({code}): revoke by hand")
    if res.audit_error_code:
        out(f"AUDIT NOT RECORDED ({res.audit_error_code}): record by hand")
    return 0 if res.fully_applied else 2


if __name__ == "__main__":
    sys.exit(main())
