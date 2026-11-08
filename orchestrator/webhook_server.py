"""Entry point for the webhook receiver: python -m orchestrator.webhook_server (ADR-28). Serves orchestrator/webhook_app.WebhookApp with waitress,
a pure-Python production WSGI server, and puts the signals into the inbox that the Scout loop reads.

Settings (environment):
  DATABASE_URL          a login role that is a member of app_rw and nothing more (compose provisions sap_webhook); the receiver never needs pii access
  WEBHOOK_CONFIG_FILE   JSON: {"lemonsqueezy": {"secret": "...", "store_id": 1, "path_token": "..."}, "payhip": {"api_key": "...", "path_token": "..."}}
                        Each path_token is a secret of 16 to 128 characters (letters, digits, _ -): it is part of the URL you give the provider.
  BIND_HOST, PORT       default 127.0.0.1 and 8081. In a container BIND_HOST is 0.0.0.0 and the port is published on the host's loopback only.
Put a TLS proxy in front (cutover C14): the providers call https://your-domain/webhooks/<name>/<path_token>. Gumroad has no receiver: it is polled.
Bad configuration stops the process at start-up naming the setting, never its value. `build` is tested with fakes; the server itself is not."""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Mapping, Optional

from orchestrator.webhook_app import MAX_BODY, WebhookApp
from shared.config import ConfigError

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def build(environ: Optional[Mapping[str, str]] = None, inbox=None) -> WebhookApp:
    env = os.environ if environ is None else environ
    problems = []
    dsn = env.get("DATABASE_URL", "")
    if inbox is None and not dsn.startswith(("postgresql://", "postgres://")):
        problems.append("DATABASE_URL: must be a postgresql:// URL")
    path = env.get("WEBHOOK_CONFIG_FILE", "")
    cfg = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        if not isinstance(cfg, dict):
            raise ValueError
    except (OSError, ValueError):
        problems.append("WEBHOOK_CONFIG_FILE: must be a readable JSON object")
        cfg = {}
    handlers = {}
    if cfg:
        from adapters.lemon_squeezy.webhook import LemonSqueezyWebhook
        from adapters.payhip.webhook import PayhipWebhook
        try:
            if "lemonsqueezy" in cfg:
                c = cfg["lemonsqueezy"]
                if not _TOKEN.match(str(c["path_token"])) or not str(c["secret"]).strip() or int(c["store_id"]) <= 0:
                    raise ValueError
                handlers["lemonsqueezy"] = (LemonSqueezyWebhook(c["secret"], int(c["store_id"])), c["path_token"])
            if "payhip" in cfg:
                c = cfg["payhip"]
                if not _TOKEN.match(str(c["path_token"])) or not str(c["api_key"]).strip():
                    raise ValueError
                handlers["payhip"] = (PayhipWebhook(c["api_key"]), c["path_token"])
        except (KeyError, TypeError, ValueError):
            problems.append("WEBHOOK_CONFIG_FILE: each provider needs its secret or api_key, store_id (Lemon Squeezy) and a path_token of 16 to 128 safe characters")
    if not handlers and not any(p.startswith("WEBHOOK_CONFIG_FILE") for p in problems):
        problems.append("WEBHOOK_CONFIG_FILE: configure at least one of lemonsqueezy, payhip")
    if problems:
        raise ConfigError(problems)
    if inbox is None:
        from orchestrator.inbox_access import DsnSignalInbox
        inbox = DsnSignalInbox(dsn)
    return WebhookApp(inbox, handlers)


def main(environ: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if environ is None else environ
    try:
        app = build(env)
        port = int(env.get("PORT", "8081"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ConfigError as exc:
        for problem in exc.problems:
            print(f"CONFIG ERROR {problem}", file=sys.stderr)
        return 2
    except ValueError:
        print("CONFIG ERROR PORT: must be a port number", file=sys.stderr)
        return 2
    from waitress import serve                 # imported late: a deployment dependency (requirements.txt)
    host = env.get("BIND_HOST", "127.0.0.1")
    print(f"webhook receiver listening on {host}:{port}", flush=True)
    serve(app, host=host, port=port, threads=4, max_request_body_size=MAX_BODY, ident="webhooks", connection_limit=100, channel_timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
