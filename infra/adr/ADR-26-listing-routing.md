# ADR-26: Routing a draft to one of a product's marketplaces

Status: proposed
Source: S4-03, S4-04

## Context
A creator can sell one product on several marketplaces (`core.marketplace_listing_ref` has one row per listing). The pipeline knew one product
adapter: the drafter read the product from it and the Closer built the checkout link with it. ADR-23 recorded the gap: a second marketplace needs a
product-to-marketplace mapping. Nothing in the repository said what a marketplace keeps from a sale, and `core.lead.assigned_marketplace` was never used.

## Decision
- **The rule** (`orchestrator/router.py`). 1) If the lead's product id (`core.lead.product_external_id`) is the id of an ACTIVE listing, that listing's
  marketplace wins: the person already knows that shop. 2) Otherwise the ACTIVE listing that leaves the seller the most money. 3) Ties break by
  marketplace name, then external id, so a decision never depends on row order. The result is a ranked list: if the first listing cannot be shown
  (no adapter, the marketplace no longer has it, no checkout link, invalid product) the drafter takes the next, and the reason is FALLBACK.
- **Profit is the seller's own number.** `core.marketplace_fee` (migration 0012) holds a percentage in basis points and a fixed amount per marketplace.
  Net = price - ceil(price x bps / 10000) - fixed, in integer minor units, rounded against the listing. No fee is written into code and there are no seed
  rows. A marketplace with no row, or a fixed fee in another currency (there is no exchange rate), is "unknown": it ranks below every known one and the
  decision is labelled FEE_UNKNOWN, because then the order rests on names, not money. The application role can only read the table.
- **"Originally bought from" is the lead's listing.** The lead stores a product id, not a purchase, so the router uses the listing that id belongs to. For
  a lead who opted in rather than bought, "the marketplace the lead came from" is the honest description, and that is what the reviewer is told.
  An id found on two products is ambiguous and is skipped unless the lead's own source marketplace settles it.
- **Where the decision lives.** In the proposal's payload (`route_marketplace`, `route_reason`), next to `product_external_id`, which becomes the routed
  listing's id. No new column: the Closer already reads that payload, the review API already returns it, and the audit hash already covers it.
  An audit event `ROUTE_DECIDED` (reason code only) records each decision.
- **One lead, one product, one draft.** The proposal's idempotency key uses the product id the LEAD named, not the routed listing, so a later fee change
  cannot produce a second draft. For a lead drafted before routing existed the key is unchanged.
- **The Closer uses the routed marketplace's adapter and no other.** A route with no adapter fails the proposal (`NO_PRODUCT_ADAPTER`) before anything is
  claimed or sent; it never borrows another marketplace's link. A draft with no route behaves exactly as before.
- **Opt-in.** Routing is on only when the drafter gets both a `Router` and `product_adapters`; the Closer takes `product_adapters` separately. Without
  them nothing changes, so the single-marketplace deployment is untouched.
- **The reviewer sees it.** The detail view says where the checkout link points and why, in plain words ("Checkout on Lemon Squeezy: it leaves the most
  money after the marketplace's fees"), or "Checkout marketplace: not recorded" for a draft that was not routed.

## Consequences
- The catalog must be filled (products, listings, fees) before routing is switched on. Until then a routed deployment skips leads with `PRODUCT_NOT_FOUND`
  or `NO_ACTIVE_LISTING`, with a recorded reason, rather than guessing.
- The Guard's link allow-list must contain every routed marketplace's checkout host, or the draft is blocked with LINK_HOST_NOT_ALLOWED.
- Editing a draft through the review API replaces the whole payload. An edit that drops `route_marketplace` closes through the default adapter with the
  lead's product id. The dashboard offers no edit; a client that does must send the route fields back.
- Payhip links carry no lead id (ADR-25), so a Payhip route cannot be attributed. Profit does not account for that; it is a seller decision whether
  attributable sales are worth a lower margin, and the fix is a fee row or the lead's own listing, not code.
- The queue list still shows the draft's channel only; the route is on the detail view. A reporting need would justify a column.
- `core.product.min_margin_pct` is not used. Its meaning (a floor on margin after discounts, or on routing) needs the seller's confirmation.
- `core.lead.assigned_marketplace` is left unused to avoid a second copy of the decision.

## Revisit when
A real fee schedule is entered and the first routed send happens, a product's price differs by marketplace (the router uses the canonical price), or
conversions are attributed (BL-06) and can weigh a marketplace by what it converts, not only by what it keeps.
