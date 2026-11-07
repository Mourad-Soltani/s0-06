"""Listing router (S4-03, ADR-26): when a creator sells one product on several marketplaces, decide whose checkout link the draft carries.

The decision, in order:
 1. The lead's own listing. A lead names its product by a marketplace's external id (core.lead.product_external_id). When that id belongs
    to an ACTIVE listing, that listing's marketplace wins, whatever the fees: the person already knows that shop.
 2. Otherwise the most profitable ACTIVE listing: price minus what the marketplace keeps (core.marketplace_fee), in integer minor units,
    the marketplace's cut rounded UP (never flatter a listing). A listing with no usable fee data ranks below every listing with data and the
    decision says FEE_UNKNOWN: the order then rests on names, not on money.
 3. Equal profit is broken by marketplace name, then external id, so a decision never depends on row order.
The result is a ranked list, not one answer: if the first listing cannot be shown (its adapter is missing, the marketplace no longer has the
product, it has no checkout link) the drafter takes the next one and the reason becomes FALLBACK.

`rank` and `net_minor` are pure. `Router` reads the catalog through `PgCatalog` on the caller's connection: reads only, no writes, no events.
Nothing here is personal data: ids, marketplace names, integers and fixed reason codes."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

LEAD_ORIGIN, ONLY_LISTING, MOST_PROFITABLE, TIE_BREAK, FEE_UNKNOWN, FALLBACK = (
    "LEAD_ORIGIN", "ONLY_LISTING", "MOST_PROFITABLE", "TIE_BREAK", "FEE_UNKNOWN", "FALLBACK")
REASONS = (LEAD_ORIGIN, ONLY_LISTING, MOST_PROFITABLE, TIE_BREAK, FEE_UNKNOWN, FALLBACK)
NOT_FOUND_SKIP, NO_ACTIVE_SKIP = "PRODUCT_NOT_FOUND", "NO_ACTIVE_LISTING"        # drafter skip codes

_MARKETPLACE = re.compile(r"^[A-Z][A-Z_]{0,31}$")
_ID = re.compile(r"^[A-Za-z0-9_:.=-]{1,128}$")


@dataclass(frozen=True)
class Listing:
    marketplace: str
    external_id: str


@dataclass(frozen=True)
class Match:
    """A catalog row found by the lead's product id."""
    marketplace: str
    external_id: str
    product_id: str


@dataclass(frozen=True)
class Fee:
    percent_bps: int          # 1% = 100
    fixed_minor: int
    currency: str             # the currency of fixed_minor


@dataclass(frozen=True)
class RouteChoice:
    marketplace: str
    external_id: str
    reason: str
    net_minor: Optional[int]


@dataclass(frozen=True)
class RoutePlan:
    """Either ranked choices (best first) or a drafter skip code, never both."""
    choices: Tuple[RouteChoice, ...] = ()
    skip: Optional[str] = None


@dataclass(frozen=True)
class Route:
    """What a proposal records about its routing. lead_product_id is the id the LEAD named, which keeps the proposal's idempotency key
    stable when a later fee change would pick another listing: one lead, one product, one proposal."""
    marketplace: str
    reason: str
    lead_product_id: str

    def __post_init__(self):
        if not _MARKETPLACE.match(self.marketplace or "") or self.reason not in REASONS or not _ID.match(self.lead_product_id or ""):
            raise ValueError("a route needs a marketplace name, a known reason and the lead's product id")


def net_minor(price_minor: int, currency: str, fee: Optional[Fee]) -> Optional[int]:
    """What the seller keeps from one sale, in minor units; None when it cannot be told (no fee row, or a fixed fee in another currency:
    there is no exchange rate here and none is invented). A percentage-only fee needs no currency match."""
    if fee is None or (fee.fixed_minor and fee.currency != currency):
        return None
    return price_minor - (-(-price_minor * fee.percent_bps // 10_000)) - fee.fixed_minor      # ceil of the marketplace's cut


def rank(listings: Sequence[Listing], *, price_minor: int, currency: str, fees: Mapping[str, Fee],
         origin: Optional[Listing] = None) -> Tuple[RouteChoice, ...]:
    nets: Dict[Listing, Optional[int]] = {l: net_minor(price_minor, currency, fees.get(l.marketplace)) for l in listings}

    def key(l: Listing):
        n = nets[l]
        return (0 if origin is not None and l == origin else 1, 0 if n is not None else 1, -(n or 0), l.marketplace, l.external_id)

    ordered = sorted(listings, key=key)
    if not ordered:
        return ()
    top = ordered[0]
    if origin is not None and top == origin:
        first = LEAD_ORIGIN
    elif len(ordered) == 1:
        first = ONLY_LISTING
    elif any(nets[l] is None for l in ordered):
        first = FEE_UNKNOWN
    elif nets[ordered[1]] == nets[top]:
        first = TIE_BREAK
    else:
        first = MOST_PROFITABLE
    return tuple(RouteChoice(l.marketplace, l.external_id, first if i == 0 else FALLBACK, nets[l]) for i, l in enumerate(ordered))


class PgCatalog:
    """Read-only queries against core.marketplace_listing_ref, core.product, core.marketplace_fee and core.lead (needs app_rw)."""

    def __init__(self, conn):
        self.conn = conn

    def matches(self, external_id: str) -> List[Match]:
        rows = self.conn.execute("SELECT marketplace::text, external_id, product_id FROM core.marketplace_listing_ref WHERE external_id = %s "
                                 "ORDER BY marketplace::text, external_id", (external_id,)).fetchall()
        return [Match(r[0], r[1], str(r[2])) for r in rows]

    def lead_source(self, lead_id) -> Optional[str]:
        row = self.conn.execute("SELECT source_marketplace::text FROM core.lead WHERE id = %s", (lead_id,)).fetchone()
        return row[0] if row else None

    def product(self, product_id: str) -> Optional[Tuple[int, str]]:
        row = self.conn.execute("SELECT base_price_minor, currency FROM core.product WHERE id = %s", (product_id,)).fetchone()
        return (int(row[0]), str(row[1]).strip()) if row else None

    def active_listings(self, product_id: str) -> List[Listing]:
        rows = self.conn.execute("SELECT marketplace::text, external_id FROM core.marketplace_listing_ref WHERE product_id = %s "
                                 "AND status = 'ACTIVE' ORDER BY marketplace::text, external_id", (product_id,)).fetchall()
        return [Listing(r[0], r[1]) for r in rows]

    def fees(self, marketplaces: Sequence[str]) -> Dict[str, Fee]:
        rows = self.conn.execute("SELECT marketplace::text, percent_bps, fixed_minor, currency FROM core.marketplace_fee "
                                 "WHERE marketplace::text = ANY(%s)", (list(marketplaces),)).fetchall()
        return {r[0]: Fee(int(r[1]), int(r[2]), str(r[3]).strip()) for r in rows}


class Router:
    def __init__(self, catalog_factory: Callable[[object], object] = PgCatalog):
        self._factory = catalog_factory

    def plan(self, conn, lead_id, product_id: str) -> RoutePlan:
        cat = self._factory(conn)
        matches = cat.matches(product_id)
        if not matches:
            return RoutePlan(skip=NOT_FOUND_SKIP)                          # the catalog does not know this id
        source = cat.lead_source(lead_id)
        product_ids = {m.product_id for m in matches}
        if len(product_ids) > 1:                                           # the same external id on two marketplaces, for two products
            narrowed = {m.product_id for m in matches if m.marketplace == source}
            if len(narrowed) != 1:
                return RoutePlan(skip=NOT_FOUND_SKIP)                      # ambiguous: guessing here would send the wrong product
            product_ids = narrowed
        (pid,) = product_ids
        mine = [m for m in matches if m.product_id == pid]
        picked = mine if len(mine) == 1 else [m for m in mine if m.marketplace == source]
        origin = Listing(picked[0].marketplace, picked[0].external_id) if len(picked) == 1 else None
        info = cat.product(pid)
        if info is None:
            return RoutePlan(skip=NOT_FOUND_SKIP)
        listings = cat.active_listings(pid)
        if not listings:
            return RoutePlan(skip=NO_ACTIVE_SKIP)
        fees = cat.fees(sorted({l.marketplace for l in listings}))
        choices = rank(listings, price_minor=info[0], currency=info[1], fees=fees, origin=origin if origin in listings else None)
        return RoutePlan(choices=choices)
