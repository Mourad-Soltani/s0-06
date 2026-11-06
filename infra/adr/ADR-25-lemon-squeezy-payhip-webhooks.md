# ADR-25: Lemon Squeezy and Payhip adapters, and signals that arrive by webhook

Status: proposed
Source: S4-01, S4-02

## Context
Both marketplaces were checked against their public documentation before any code was written. They are less alike than the spec assumed.
Lemon Squeezy has a full JSON:API (products, variants, discounts, orders) with a documented limit of 300 requests a minute, and HMAC-signed
webhooks. Payhip's public API covers only coupons and license keys: it cannot list products or sales, and its webhook is the only way to learn
of a sale. Polling Payhip for signals, as first proposed, is not possible.

## Decision
- **Signals arrive by webhook into an inbox.** A receiver writes signals to `core.signal_inbox` (ids, a kind and a time; never a buyer's address or
  name). Each adapter's `fetch_signals` reads that table, so the Scout still pulls and nothing downstream changes. Writes are idempotent per
  marketplace and reference, so a provider's retry creates nothing new. The table is append-only for the application role.
- **The receiver** (`orchestrator/webhook_app.py`) is a small standard-library WSGI app. A request is `POST /webhooks/<name>/<token>`. It checks the
  route and the secret path token first (constant time; a wrong name or token is an identical 404), then size, then the provider's own signature.
  Anything accepted or deliberately ignored is a 200, so providers stop retrying. It runs with the application role alone.
- **Lemon Squeezy authentication is real**: the hex HMAC-SHA256 of the raw body under the webhook's signing secret. An `order_created` event from the
  configured store becomes a SALE signal; test-mode orders and other events are ignored. If the checkout link carried a lead id as custom data
  (`checkout[custom][ref]`, which the Closer sets), it is stored as the attributed lead.
- **Payhip authentication is weak by design**: the payload's `signature` is the SHA-256 of the API key, a constant. Anyone who has seen one
  payload can forge another. The handler checks it anyway, and the receiver adds a secret URL token, a freshness window (a replayed old event is
  ignored) and idempotency per item. A Payhip signal is a hint, not proof of payment. A forged event can at worst create a spurious lead with no
  contact, which nothing can message.
- **Payhip is a smaller adapter and says so.** Products come from a catalogue the seller keeps. Offer codes are single-product percent coupons only:
  fixed amounts (the unit is unconfirmed) and expiry are refused, not guessed. The checkout link honours only `coupon`: Payhip returns nothing to us,
  so a lead id would be shared with it for no benefit, and every other parameter is dropped. A Payhip sale therefore cannot be attributed to a lead.
- **Lemon Squeezy offers more**: discounts limited to the product's variants, with expiry. Codes are restricted to letters and digits until the
  platform's rule is confirmed. Checkout links carry custom data and the discount in the documented query form.
- **Rate limits.** Lemon Squeezy: declared as a fixed window; the lane is 300 with a refill of 5 per second; the transport reads the remaining allowance
  from the response headers and maps a 429 to a back-off of the platform's Retry-After, or a minute. Payhip publishes no limit, so its lane (30 in a
  burst, then one every two seconds) is our own conservative choice.
- **Unverified against the live platforms** (mappers fail loudly rather than guess): Lemon Squeezy's attribute names, the discount body and the
  pagination links; its custom-data and discount query syntax; Payhip's coupon field names, its product-page URL form and its 429 behaviour.

## Consequences
- Production needs a public HTTPS endpoint for the receiver, with the signing secrets and path tokens held in the secrets manager (cutover items).
- A Payhip webhook is a weaker source than a Lemon Squeezy one. If forged leads ever matter, the answer is to cross-check against Payhip's own records
  by hand, since it offers no API for it.
- Conversions on Payhip cannot be tied to a lead through the link. Matching a buyer's address to a contact would work but is a privacy decision for the
  legal review (C9); it is not done.
- Lemon Squeezy shares the lead id with the platform as custom data. That is pseudonymous but linkable, and is on the legal review list with the keyed token.

## Revisit when
The accounts exist and the spike report is verified (C10), Payhip adds sales or product endpoints, or conversions need to be attributed.
