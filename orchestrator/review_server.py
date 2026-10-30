"""Development entry point for the review API: python -m orchestrator.review_server
Needs DATABASE_URL (a login role that is a member of review_svc) and REVIEW_API_TOKENS_FILE (a JSON object mapping
token to reviewer id). Binds to 127.0.0.1 only. NOT tested here: production runs the ReviewApi WSGI app under a real
server behind a TLS proxy (cutover checklist)."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from wsgiref.simple_server import make_server

from orchestrator.review_api import ReviewApi


def build() -> ReviewApi:
    import psycopg
    from orchestrator.review_service import ReviewQueue, review_audit_hook
    from shared.audit import AuditWriter

    dsn = os.environ["DATABASE_URL"]
    tokens = json.load(open(os.environ["REVIEW_API_TOKENS_FILE"]))

    @contextmanager
    def queue():
        conn = psycopg.connect(dsn, autocommit=True)
        try:
            writer = AuditWriter(conn)
            yield ReviewQueue(conn, audit=review_audit_hook(writer), hasher=writer.hash_payload)
        finally:
            conn.close()

    return ReviewApi(queue, tokens)


if __name__ == "__main__":
    with make_server("127.0.0.1", int(os.environ.get("PORT", "8080")), build()) as server:
        server.serve_forever()
