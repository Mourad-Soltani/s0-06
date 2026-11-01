"""Command line for the consented lead intake: python -m orchestrator.lead_intake_cli FILE.csv --actor ID [--source-url URL]
[--ttl-days N] [--dry-run]. Needs DATABASE_URL (a login role that is a member of review_svc). Prints one line per row
(line number and outcome code, never an address) and the totals. Run it with --dry-run first. NOT tested here: the logic
it calls is."""
from __future__ import annotations

import argparse
import os
import sys


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Import consented contacts from a CSV")
    p.add_argument("file")
    p.add_argument("--actor", required=True)
    p.add_argument("--source-url")
    p.add_argument("--ttl-days", type=int, default=365)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    import psycopg
    from orchestrator.lead_intake import ConsentedLeadIntake, IntakeError, intake_audit_hook
    from shared.audit import AuditWriter
    from shared.consent import ConsentService

    with open(a.file, "rb") as fh, psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        writer = AuditWriter(conn)
        intake = ConsentedLeadIntake(conn, ConsentService(conn, lambda act, d: writer.record(d["actor"], act, marketplace=d["marketplace"])),
                                     audit=intake_audit_hook(writer))
        try:
            report = intake.import_csv(fh.read(), actor=a.actor, default_source_url=a.source_url, ttl_days=a.ttl_days, dry_run=a.dry_run)
        except IntakeError as exc:
            print(f"IMPORT REFUSED: {exc.code}", file=sys.stderr)
            return 2
    for r in report.results:
        print(f"line {r.line}: {r.outcome}")
    print("totals:", dict(sorted(report.counts.items())), "(dry run)" if report.dry_run else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
