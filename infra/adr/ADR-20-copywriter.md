# ADR-20: Copywriter and the language-model seam

Status: proposed
Source: S3-01

## Context
The Copywriter is the first role that spends money and the first that could say something wrong in the seller's name.
No provider or API key exists yet (cutover item C2), the budget is a hard ceiling (ADR-04), and a model's output is
untrusted text.

## Decision
- **A seam, not a provider.** `LlmClient` is one method. The default client refuses (`LlmNotConfigured`). The real
  provider will implement the protocol and nothing else changes.
- **Routing and budget.** The model comes from the routing policy for the COPYWRITER role (every role listed, every model
  priced). Before a paid call the tracker checks headroom against a deliberately generous size estimate. After the call the
  provider's REPORTED token counts go into the ledger, whatever the text turns out to be: the money is spent.
- **A strict schema, no repair.** The output must be one JSON object with exactly the expected keys and types, bounded
  lengths and no control characters. Anything else is a hard reject (spec 8) with a fixed code. Only the first variant is
  used; A/B testing is deferred.
- **A deterministic template baseline** writes the message when there is no model for the role, the budget is spent,
  the provider is unconfigured or fails, or the output breaks the schema. The reason is recorded in the audit event as
  `FALLBACK_<reason>`. With fallback switched off the same cases raise a typed error. The baseline passes the Compliance
  Guard's rules unchanged, which a test proves.
- **Structured input only.** The model sees a validated brief: product facts, the discount and its code, the context. Never
  raw scraped text, never an address, never an internal id. Seller text is made inert (no control characters, angle brackets
  or backticks, length capped) and travels as one JSON value inside the prompt.
- **The Guard is the backstop.** A model that ignores its instructions still faces the Guard's rules and the adapter's
  preflight before a human sees the draft. Tests feed hostile output through the whole chain.
- **One proposal per lead, product and context.** The idempotency key is derived from them, so a retry never makes a
  second model call, body or event. The model call happens outside the database transaction; the body, the proposal, the
  audit event and `action.proposed` commit together. If a later step fails the spend stays on the ledger: it was real.
- **The tier** is 0 until the shadow-mode gate passes for the marketplace and role (ADR-18), then 1. Never 2 in v1.1.
- **`action.proposed` triggers the Guard** through a bus consumer, so a new draft is judged without a manual call.

## Consequences
- Until a provider is configured every draft comes from the template. That is deliberate and visible in the audit trail.
- The size estimate only gates the budget check; real cost comes from provider usage. A provider that under-reports would
  under-record, so the ledger should be reconciled against the provider's invoice at cutover.
- Leads from marketplace sales carry no contact, so they cannot be drafted yet. A consented-lead intake is needed before
  the loop runs end to end.
- The few-shot examples are fixed. Approved edits should become examples later, through a reviewed change.

## Revisit when
A provider is chosen, the first reviewer rejections show where the template and prompt fall short, or A/B testing starts.
