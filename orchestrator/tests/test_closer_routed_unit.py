"""The Closer with routing (S4-03): a routed draft is closed with ITS marketplace's checkout link, never another's; an unroutable one fails
without sending; a draft without a route behaves as before. No database. Names CLR*."""
import uuid
from decimal import Decimal
from urllib.parse import urlencode

import pytest

from orchestrator.closer import SENT, Closer
from orchestrator.guard import ComplianceGuard
from orchestrator.guard_rules import GuardConfig
from orchestrator.tests.test_closer_unit import (ATTR, BODY, CONTACT, KEY, LEAD, PERM, PID, Channel, Conn, PassAdapter, Products, row)

LS_PLAIN = "https://buy.example.test/ls/ls-1"
LS_ATTR = f"{LS_PLAIN}?{urlencode({'checkout[custom][ref]': str(LEAD)})}"
LS_BODY = f"Subject: Your guide is ready\n\nHi,\n\nHere it is: {LS_PLAIN}\n\nThanks!"
ROUTE = {"route_marketplace": "LEMON_SQUEEZY", "route_reason": "MOST_PROFITABLE"}
GUARD = ComplianceGuard({"EMAIL": PassAdapter()}, GuardConfig(allowed_link_hosts=frozenset({"shop.example.test", "buy.example.test"}),
                                                              max_discount_pct=Decimal("20")))


class LsProducts:
    def __init__(self):
        self.asked = []

    def build_checkout_url(self, pid, params):
        self.asked.append((pid, dict(params)))
        return f"https://buy.example.test/ls/{pid}" + (f"?{urlencode({f'checkout[custom][{k}]': v for k, v in sorted(params.items())})}" if params else "")


def routed_row(**kw):
    r = list(row(product="ls-1", body=kw.pop("body", LS_BODY), payload_extra=kw.pop("payload_extra", ROUTE), **kw))
    r[-1] = "gr-1"                                                         # the lead's own product id: a different listing on another marketplace
    return tuple(r)


def build(r, adapters, *, default=None):
    log, audits, pubs = [], [], []
    conn, channel = Conn(log, r), Channel(log)

    class Connect:
        def __enter__(self_inner):
            return conn

        def __exit__(self_inner, *a):
            return False

    closer = Closer(lambda: Connect(), default or Products(), {"EMAIL": channel}, GUARD, product_adapters=adapters,
                    audit_factory=lambda c: (lambda a, d: audits.append((a, d))), publish=lambda c, et, **k: pubs.append((et, k)))
    return closer, conn, channel, log, audits, pubs


def test_CLR1_a_routed_draft_gets_its_own_marketplaces_attributed_link_and_the_default_adapter_is_not_used():
    ls, default = LsProducts(), LsProducts()
    closer, conn, channel, log, audits, pubs = build(routed_row(), {"LEMON_SQUEEZY": ls}, default=default)
    assert closer.process(PID).outcome == SENT
    (contact, body, key), = channel.sent
    assert (contact, key) == (str(CONTACT), KEY) and body == LS_BODY.replace(LS_PLAIN, LS_ATTR) and "gr-1" not in body
    assert ls.asked == [("ls-1", {}), ("ls-1", {"ref": str(LEAD)})] and default.asked == []
    assert log == ["begin", "begin", "claim", "commit", "commit", "send", "begin", "finish:SENT", "commit"]      # the same claim-then-send order
    assert pubs[0][1]["payload"]["link"] == "ATTRIBUTED"


def test_CLR2_a_route_with_no_adapter_fails_the_draft_before_anything_is_sent_and_never_borrows_another_marketplaces_link():
    default = LsProducts()
    closer, conn, channel, log, audits, pubs = build(routed_row(), {"GUMROAD": LsProducts()}, default=default)
    assert closer.process(PID).outcome == "NO_PRODUCT_ADAPTER"
    assert channel.sent == [] and default.asked == [] and "claim" not in log and "failed_before_claim" in log and pubs == []
    assert audits == [("CLOSER_FAILED", {"marketplace": "EMAIL", "lead_id": LEAD, "tier": 1, "outcome": "NO_PRODUCT_ADAPTER"})]


@pytest.mark.parametrize("route", [5, "", "lemon_squeezy", ["LEMON_SQUEEZY"], {"x": 1}])
def test_CLR3_a_route_that_is_not_a_known_marketplace_name_fails_the_draft(route):
    closer, conn, channel, log, audits, pubs = build(routed_row(payload_extra={"route_marketplace": route}), {"LEMON_SQUEEZY": LsProducts()})
    assert closer.process(PID).outcome == "NO_PRODUCT_ADAPTER" and channel.sent == [] and "claim" not in log


def test_CLR4_a_draft_without_a_route_uses_the_default_adapter_even_when_routing_is_configured():
    ls = LsProducts()
    closer, conn, channel, log, audits, pubs = build(row(), {"LEMON_SQUEEZY": ls})
    assert closer.process(PID).outcome == SENT
    assert f"?ref={LEAD}" in channel.sent[0][1] and ls.asked == [] and channel.sent[0][1].replace(f"?ref={LEAD}", "") == BODY


def test_CLR5_the_final_text_of_a_routed_draft_is_still_checked_before_it_is_claimed():
    guard_without_ls = ComplianceGuard({"EMAIL": PassAdapter()}, GuardConfig(allowed_link_hosts=frozenset({"shop.example.test"}), max_discount_pct=Decimal("20")))
    log, audits = [], []
    conn, channel = Conn(log, routed_row()), Channel(log)

    class Connect:
        def __enter__(self_inner):
            return conn

        def __exit__(self_inner, *a):
            return False

    closer = Closer(lambda: Connect(), Products(), {"EMAIL": channel}, guard_without_ls, product_adapters={"LEMON_SQUEEZY": LsProducts()},
                    audit_factory=lambda c: (lambda a, d: audits.append((a, d))), publish=lambda c, et, **k: None)
    assert closer.process(PID).outcome == "FINAL_CHECK_LINK_HOST_NOT_ALLOWED" and channel.sent == [] and "claim" not in log


def test_CLR6_the_default_arguments_keep_every_existing_caller_working():
    closer = Closer(lambda: None, Products(), {}, GUARD)
    assert closer.product_adapters == {} and closer.products is not None
