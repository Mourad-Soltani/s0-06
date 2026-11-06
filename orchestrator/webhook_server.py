"""Development entry point for the webhook receiver: python -m orchestrator.webhook_server
Needs DATABASE_URL (a login role that is a member of app_rw) and WEBHOOK_CONFIG_FILE, a JSON object such as
{"lemonsqueezy": {"secret": "...", "store_id": 1, "path_token": "..."}, "payhip": {"api_key": "...", "path_token": "..."}}.
Binds to 127.0.0.1 only. NOT tested here: production runs the WebhookApp under a real server behind a TLS proxy."""
from __future__ import annotations

import json
import os
from wsgiref.simple_server import make_server

from orchestrator.webhook_app import WebhookApp


def build() -> WebhookApp:
    import psycopg
    from adapters.lemon_squeezy.webhook import LemonSqueezyWebhook
    from adapters.payhip.webhook import PayhipWebhook
    from shared.signal_inbox import PgSignalInbox

    cfg = json.load(open(os.environ["WEBHOOK_CONFIG_FILE"]))
    handlers = {}
    if "lemonsqueezy" in cfg:
        c = cfg["lemonsqueezy"]
        handlers["lemonsqueezy"] = (LemonSqueezyWebhook(c["secret"], c["store_id"]), c["path_token"])
    if "payhip" in cfg:
        c = cfg["payhip"]
        handlers["payhip"] = (PayhipWebhook(c["api_key"]), c["path_token"])
    return WebhookApp(PgSignalInbox(psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)), handlers)


if __name__ == "__main__":
    with make_server("127.0.0.1", int(os.environ.get("PORT", "8081")), build()) as server:
        server.serve_forever()
