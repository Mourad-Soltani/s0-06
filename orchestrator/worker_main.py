"""Production entry point for the pipeline (cutover kit 1, ADR-27): python -m orchestrator.worker_main {run | check | reconcile}.

  run        the event-bus worker: the whole chain on one bus (qualifier, drafter, guard, closer), plus a periodic Closer sweep so an approved draft
             that was held (kill switch, rate limit) goes out after the hold ends. Runs until SIGTERM, finishing the event in hand.
  check      start-up self-test: settings are valid, the database answers, the email provider and each configured marketplace accept our credentials.
             Prints one line per check (name and OK/FAIL, never a value or an error message). Sends nothing. Exit code 0 only if all pass.
  reconcile  one pass of the reconciliation job (BL-04, ADR-24) with the real email adapter. Run it from a scheduler, e.g. every five minutes.

All settings come from the environment (shared/config.py). This module is the one place that knows which concrete classes make up the product;
everything it builds already has its own tests. NOT tested here: the live services. `build` is tested with fakes, `serve` with a fake worker."""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from contextlib import contextmanager
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from shared.config import ConfigError, Settings, load_settings

log = logging.getLogger("worker")
ROLES = ("SCOUT", "QUALIFIER", "COPYWRITER", "CLOSER", "COMPLIANCE_GUARD", "ANALYST")
NO_MODEL = {role: None for role in ROLES}        # until a model is chosen and priced (C2, C7) every draft is written from the standard template


class Parts:
    """What `build` returns: the worker to run and the pieces the other commands need."""

    def __init__(self, worker, closer, email, adapters, default_marketplace: str):
        self.worker, self.closer, self.email, self.adapters, self.default_marketplace = worker, closer, email, adapters, default_marketplace


def make_connect(dsn: str):
    """A context manager that yields a fresh autocommit connection and closes it. The Closer and the address resolver each use their own
    (ADR-23: the Closer must never share the bus worker's transaction)."""
    @contextmanager
    def connect():
        import psycopg                      # imported late: needs the database driver
        conn = psycopg.connect(dsn, autocommit=True)
        try:
            yield conn
        finally:
            conn.close()
    return connect


def load_payhip_catalog(path: str):
    """The seller's catalogue: a JSON list of {"key", "name", "price_minor", "currency"?, "description"?}. Payhip cannot list products itself."""
    from adapters.payhip.adapter import PayhipProduct
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return [PayhipProduct(key=r["key"], name=r["name"], price_minor=int(r["price_minor"]), currency=r.get("currency", "USD"),
                              description=r.get("description", "")) for r in raw]
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigError(["PAYHIP_CATALOG_FILE: must be a readable JSON list of {key, name, price_minor}"]) from None


def build_product_adapters(s: Settings) -> Dict[str, object]:
    adapters: Dict[str, object] = {}
    if s.gumroad_token:
        from adapters.gumroad.adapter import GumroadAdapter
        from adapters.gumroad.transport import HttpGumroadTransport
        adapters["GUMROAD"] = GumroadAdapter(HttpGumroadTransport(s.gumroad_token.reveal()))
    if s.lemon_squeezy_key:
        from adapters.lemon_squeezy.adapter import LemonSqueezyAdapter
        from adapters.lemon_squeezy.transport import HttpLemonSqueezyTransport
        adapters["LEMON_SQUEEZY"] = LemonSqueezyAdapter(HttpLemonSqueezyTransport(s.lemon_squeezy_key.reveal(), s.lemon_squeezy_store_id))
    if s.payhip_key:
        from adapters.payhip.adapter import PayhipAdapter
        from adapters.payhip.transport import HttpPayhipTransport
        adapters["PAYHIP"] = PayhipAdapter(HttpPayhipTransport(s.payhip_key.reveal()), load_payhip_catalog(s.payhip_catalog_file))
    return adapters


def build(s: Settings, connect: Callable, bus_conn, *, product_adapters: Optional[Mapping[str, object]] = None, email_transport=None) -> Parts:
    from adapters.email_channel.adapter import EmailChannelAdapter
    from adapters.email_channel.message import EmailConfig
    from adapters.email_channel.postmark import PostmarkEmailTransport
    from orchestrator.address_resolver import PgAddressResolver
    from orchestrator.closer import Closer
    from orchestrator.copywriter import Copywriter
    from orchestrator.drafter import DraftingConsumer, default_writer_factory
    from orchestrator.guard import ComplianceGuard
    from orchestrator.guard_consumer import GuardConsumer
    from orchestrator.guard_rules import GuardConfig
    from orchestrator.lead_store import PgLeadRepo
    from orchestrator.llm import NotConfiguredLlmClient
    from orchestrator.qualifier import Qualifier
    from orchestrator.router import Router
    from orchestrator.scoring import IcpDefinition, RuleScorer
    from orchestrator.wiring import register_pipeline
    from shared.bus.store import PgOutboxStore
    from shared.bus.worker import Worker
    from shared.cost import CostTracker, NotConfiguredNotifier, PriceBook, RoutingPolicy

    transport = email_transport or PostmarkEmailTransport(s.postmark_token.reveal(), s.email_from, PgAddressResolver(connect),
                                                          message_stream=s.postmark_stream, enable_lookup=s.postmark_lookup)
    email = EmailChannelAdapter(transport, EmailConfig(s.postal_address, s.unsubscribe_base_url))
    adapters = dict(product_adapters) if product_adapters is not None else build_product_adapters(s)
    default_marketplace = s.marketplaces[0]
    default_adapter = adapters[default_marketplace]
    guard = ComplianceGuard({"EMAIL": email}, GuardConfig(allowed_link_hosts=s.allowed_link_hosts, max_discount_pct=s.max_discount_pct))

    prices = PriceBook({})
    copywriter = Copywriter(RoutingPolicy(NO_MODEL, prices), CostTracker(bus_conn, prices, NotConfiguredNotifier()), NotConfiguredLlmClient())
    routed = s.routing_enabled
    drafter = DraftingConsumer(default_writer_factory(copywriter), default_adapter, router=Router() if routed else None,
                               product_adapters=adapters if routed else None)
    closer = Closer(connect, default_adapter, {"EMAIL": email}, guard, product_adapters=adapters if routed else None)
    worker = Worker(PgOutboxStore(bus_conn), s.worker_id)
    register_pipeline(worker, qualifier=Qualifier(RuleScorer(IcpDefinition(s.icp_product_ids)), PgLeadRepo()), drafter=drafter,
                      guard_consumer=GuardConsumer(guard), closer=closer)
    return Parts(worker, closer, email, adapters, default_marketplace)


def serve(parts, stop: threading.Event, *, poll_interval: float, sweep_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
    """Claim and handle events until `stop` is set. A failed sweep is logged by error class and retried at the next interval; a failure of the
    bus itself ends the process so the supervisor restarts it with a fresh connection."""
    from shared.bus.events import error_code
    next_sweep = clock() + sweep_seconds
    while not stop.is_set():
        if parts.worker.run_once().claimed == 0:
            stop.wait(poll_interval)
        if sweep_seconds and clock() >= next_sweep:
            next_sweep = clock() + sweep_seconds
            try:
                swept = parts.closer.sweep()
                if swept:
                    log.info("sweep handled %d approved proposals", len(swept))
            except Exception as exc:  # noqa: BLE001 - the class name only: a message could carry an address
                log.error("sweep failed: %s", error_code(exc))


def run_checks(s: Settings, bus_conn, parts) -> List[Tuple[str, bool]]:
    from shared.bus.events import error_code
    results: List[Tuple[str, bool]] = []

    def attempt(name: str, fn: Callable[[], bool]) -> None:
        try:
            results.append((name, bool(fn())))
        except Exception as exc:  # noqa: BLE001
            log.error("check %s raised %s", name, error_code(exc))
            results.append((name, False))

    attempt("database", lambda: bus_conn.execute("SELECT 1").fetchone()[0] == 1)
    attempt("email_provider", lambda: parts.email.health_check().ok)
    for marketplace, adapter in sorted(parts.adapters.items()):
        attempt(f"marketplace_{marketplace.lower()}", lambda a=adapter: a.health_check().ok)
    return results


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)


def main(argv: Optional[Sequence[str]] = None, environ: Optional[Mapping[str, str]] = None, env_file: Optional[str] = ".env") -> int:
    parser = argparse.ArgumentParser(description="Sales agent pipeline worker")
    parser.add_argument("command", choices=("run", "check", "reconcile"))
    parser.add_argument("rest", nargs=argparse.REMAINDER, help="reconcile: options for the reconciliation job (--stale-minutes N --limit N)")
    args = parser.parse_args(argv)
    _setup_logging()
    try:
        s = load_settings(environ, env_file=env_file)
    except ConfigError as exc:
        for problem in exc.problems:
            print(f"CONFIG ERROR {problem}", file=sys.stderr)
        return 2
    import psycopg                                          # imported late: needs the database driver
    dsn = s.database_url.reveal()
    connect = make_connect(dsn)
    with psycopg.connect(dsn, autocommit=True) as bus_conn:
        try:
            parts = build(s, connect, bus_conn)
        except ConfigError as exc:
            for problem in exc.problems:
                print(f"CONFIG ERROR {problem}", file=sys.stderr)
            return 2
        if args.command == "check":
            results = run_checks(s, bus_conn, parts)
            for name, ok in results:
                print(f"{name}: {'OK' if ok else 'FAIL'}")
            return 0 if all(ok for _, ok in results) else 1
        if args.command == "reconcile":
            from orchestrator.reconciler_cli import main as reconcile_main
            os.environ["DATABASE_URL"] = dsn                 # the job reads its own connection string
            return reconcile_main(args.rest, channel_adapters={"EMAIL": parts.email})
        if s.llm_configured:
            log.warning("an LLM key is configured but no provider client exists yet (C2): drafts are written from the standard template")
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        log.info("worker %s started: marketplaces=%s routing=%s stream=%s", s.worker_id, ",".join(s.marketplaces), s.routing_enabled, s.postmark_stream)
        serve(parts, stop, poll_interval=s.poll_interval, sweep_seconds=s.sweep_seconds)
        log.info("worker %s stopped", s.worker_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
