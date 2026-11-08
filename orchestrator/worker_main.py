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

    def __init__(self, worker, closer, email, adapters, default_marketplace: str, copywriter=None):
        self.worker, self.closer, self.email, self.adapters, self.default_marketplace = worker, closer, email, adapters, default_marketplace
        self.copywriter = copywriter


class LogNotifier:
    """Cost alerts go to the log until an alert channel is chosen (decision D2). A log line is NOT a push alert: someone must watch for COST_ALERT.
    Numbers only; the alert is recorded as delivered once the line is written."""

    def notify(self, alert) -> None:
        log.error("COST_ALERT threshold_pct=%s spent_micro_usd=%s ceiling_micro_usd=%s", alert.threshold_pct, alert.spent_micro_usd, alert.ceiling_micro_usd)


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


def build_product_adapters(s: Settings, signals=None) -> Dict[str, object]:
    """signals: where Lemon Squeezy and Payhip adapters read webhook signals from (the inbox). The Scout needs it; drafting and closing do not."""
    adapters: Dict[str, object] = {}
    if s.gumroad_token:
        from adapters.gumroad.adapter import GumroadAdapter
        from adapters.gumroad.transport import HttpGumroadTransport
        adapters["GUMROAD"] = GumroadAdapter(HttpGumroadTransport(s.gumroad_token.reveal()))
    if s.lemon_squeezy_key:
        from adapters.lemon_squeezy.adapter import LemonSqueezyAdapter
        from adapters.lemon_squeezy.transport import HttpLemonSqueezyTransport
        adapters["LEMON_SQUEEZY"] = LemonSqueezyAdapter(HttpLemonSqueezyTransport(s.lemon_squeezy_key.reveal(), s.lemon_squeezy_store_id), signals)
    if s.payhip_key:
        from adapters.payhip.adapter import PayhipAdapter
        from adapters.payhip.transport import HttpPayhipTransport
        adapters["PAYHIP"] = PayhipAdapter(HttpPayhipTransport(s.payhip_key.reveal()), load_payhip_catalog(s.payhip_catalog_file), signals)
    return adapters


def build(s: Settings, connect: Callable, bus_conn, *, product_adapters: Optional[Mapping[str, object]] = None, email_transport=None,
          cost_conn=None, llm_client=None) -> Parts:
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
    from shared.cost import CostTracker, ModelPrice, PriceBook, RoutingPolicy

    transport = email_transport or PostmarkEmailTransport(s.postmark_token.reveal(), s.email_from, PgAddressResolver(connect),
                                                          message_stream=s.postmark_stream, enable_lookup=s.postmark_lookup)
    email = EmailChannelAdapter(transport, EmailConfig(s.postal_address, s.unsubscribe_base_url))
    adapters = dict(product_adapters) if product_adapters is not None else build_product_adapters(s)
    default_marketplace = s.marketplaces[0]
    default_adapter = adapters[default_marketplace]
    guard = ComplianceGuard({"EMAIL": email}, GuardConfig(allowed_link_hosts=s.allowed_link_hosts, max_discount_pct=s.max_discount_pct))

    # The cost ledger gets its OWN connection: the bus worker handles an event inside a transaction, and a ledger row written inside it would be
    # rolled back with it if the handler failed after the provider had already been paid.
    if s.llm_configured:
        from orchestrator.llm_providers import make_llm_client
        prices = PriceBook({s.llm_model: ModelPrice(s.llm_input_price, s.llm_output_price)})
        routes = {**NO_MODEL, "COPYWRITER": s.llm_model}
        client = llm_client or make_llm_client(s.llm_provider, s.llm_key.reveal(), max_retries=s.llm_max_retries)
    else:
        prices, routes, client = PriceBook({}), NO_MODEL, NotConfiguredLlmClient()
    copywriter = Copywriter(RoutingPolicy(routes, prices), CostTracker(cost_conn if cost_conn is not None else bus_conn, prices, LogNotifier()), client)
    routed = s.routing_enabled
    drafter = DraftingConsumer(default_writer_factory(copywriter), default_adapter, router=Router() if routed else None,
                               product_adapters=adapters if routed else None)
    closer = Closer(connect, default_adapter, {"EMAIL": email}, guard, product_adapters=adapters if routed else None)
    worker = Worker(PgOutboxStore(bus_conn), s.worker_id)
    register_pipeline(worker, qualifier=Qualifier(RuleScorer(IcpDefinition(s.icp_product_ids)), PgLeadRepo()), drafter=drafter,
                      guard_consumer=GuardConsumer(guard), closer=closer)
    return Parts(worker, closer, email, adapters, default_marketplace, copywriter)


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


SCOUT_ACCOUNT = "default"           # one seller, one account per marketplace in this deployment


class ScoutLoop:
    """Reads new sales and turns each into a lead and a signal.captured event (orchestrator/scout.py). Gumroad is polled through its API; Lemon
    Squeezy and Payhip sales arrive through the webhook receiver into the inbox, which the same Scout reads, so a sale appears here within one poll
    interval (SCOUT_POLL_SECONDS). The rate governor's buckets live as long as the process; the database connection is opened afresh each cycle, so
    a database restart heals itself. The Scout's own throttles apply: it pauses while the review queue is deep and stops at a daily cap (ADR-03)."""

    def __init__(self, s: Settings, connect: Callable, inbox, *, adapters: Optional[Mapping[str, object]] = None, governor=None):
        from shared.governor import BucketConfig, InProcessGovernor, lane
        self._connect = connect
        self.lanes = {(m, SCOUT_ACCOUNT): a for m, a in (adapters if adapters is not None else build_product_adapters(s, signals=inbox)).items()}
        self.governor = governor or InProcessGovernor({lane(m, a): BucketConfig(capacity=3, refill_per_second=0.1) for (m, a) in self.lanes})

    def run_once(self):
        from orchestrator.lead_store import PgLeadStore
        from orchestrator.scout import Scout
        with self._connect() as conn:
            return Scout(PgLeadStore(conn), self.governor, self.lanes).run_once()


def scout_serve(loop, stop: threading.Event, *, interval: float) -> None:
    """Run the Scout every `interval` seconds until `stop`. A failing cycle is logged by error class and the loop goes on."""
    from shared.bus.events import error_code
    while not stop.is_set():
        try:
            for r in loop.run_once():
                if r.status != "ok" or r.new_leads:
                    log.info("scout %s/%s: %s new=%d seen=%d%s", r.marketplace, r.account_id, r.status, r.new_leads, r.seen,
                             f" error={r.error_code}" if r.error_code else "")
        except Exception as exc:  # noqa: BLE001 - the class name only
            log.error("scout cycle failed: %s", error_code(exc))
        stop.wait(interval)


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
    with psycopg.connect(dsn, autocommit=True) as bus_conn, psycopg.connect(dsn, autocommit=True) as cost_conn:
        try:
            parts = build(s, connect, bus_conn, cost_conn=cost_conn)
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
        log.info("drafting: %s", f"{s.llm_provider} model {s.llm_model} (token costs recorded, ceiling enforced)" if s.llm_configured
                 else "standard template only (no language model configured)")
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        log.info("worker %s started: marketplaces=%s routing=%s stream=%s", s.worker_id, ",".join(s.marketplaces), s.routing_enabled, s.postmark_stream)
        scout_thread = None
        if s.scout_poll_seconds > 0:
            from orchestrator.inbox_access import DsnSignalInbox
            loop = ScoutLoop(s, connect, DsnSignalInbox(dsn))
            scout_thread = threading.Thread(target=scout_serve, args=(loop, stop), kwargs={"interval": s.scout_poll_seconds}, name="scout", daemon=True)
            scout_thread.start()
            log.info("scout started: lanes=%s every %gs", ",".join(sorted(m for m, _ in loop.lanes)), s.scout_poll_seconds)
        serve(parts, stop, poll_interval=s.poll_interval, sweep_seconds=s.sweep_seconds)
        if scout_thread is not None:
            scout_thread.join(timeout=10)
        log.info("worker %s stopped", s.worker_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
