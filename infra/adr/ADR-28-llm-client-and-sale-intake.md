# ADR-28: A real language-model client, and the sale-to-lead loop

Status: proposed
Source: Phase 5 (production cutover), drops 1 and 2

## Context
Cutover kit 1 could send email but could not write a draft with a model (the LLM key was loaded and ignored, ADR-27) and could not notice a sale: the
webhook receiver was a development script, nothing read the inbox on a schedule, and Gumroad was never polled. Cost tracking and routing already existed
(S1-10, core.cost_ledger) but had never met a real provider.

## Decision
- **Provider clients** (`orchestrator/llm_providers.py`): `OpenAiLlmClient` (chat completions) and `AnthropicLlmClient` (messages) behind the existing
  `LlmClient` protocol. They return the text and the token counts the provider reports; the Copywriter writes those to the cost ledger at the configured
  price. An answer without usage is refused, because spend that cannot be recorded is not accepted. A call that fails or is refused costs and records
  nothing; an answer that arrives but is not valid JSON is still paid for and still recorded, and the draft falls back to the template.
- **Rate limits.** 429 and transient 5xx/529/network failures are retried at most twice (configurable 0 to 5), honouring Retry-After, with exponential
  back-off; a wait over 20 seconds is not held. When the retries run out the Copywriter writes the template draft (`fallback_reason = LLM_ERROR`): the
  pipeline keeps moving and no draft waits on a provider. Other 4xx errors are never retried; 401/403 map to AuthExpired. No error body is read.
- **Configuration, nothing built in.** `LLM_PROVIDER` (openai or anthropic), a key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or `LLM_API_KEY`, each also as a
  `_FILE` secret), `LLM_MODEL` and the two prices in micro-USD per million tokens. All of them or none; a price of 0 is refused because it would switch
  the budget check off. Only the COPYWRITER role uses the model.
- **Cost control** is the existing machinery: the monthly ceiling in `core.cost_config` (default 300 USD) stops a call before it is made, an 80% and a
  100% alert fire once per month, and the ledger records every call by role, model, tokens and lead. The ledger now has its own database connection, so a
  draft that fails after the provider was paid cannot roll the ledger row back with it. Alerts go to the log as `COST_ALERT` (decision D2 is still open):
  a log line is not a notification and someone must watch for it.
- **What the provider sees:** product title, price, currency, checkout URL, signal kind and the offer if any. Never a name, an address or a lead id.
- **Sale intake.** The webhook receiver (`orchestrator/webhook_server.py`) runs under waitress as its own compose service on a login role that is a member
  of `app_rw` only, with a connection per call (`DsnSignalInbox`), behind a TLS proxy. The Scout runs as a loop in the worker process (its own thread, a
  fresh connection each cycle, the rate governor's buckets kept for the life of the process). Lemon Squeezy and Payhip sales reach it through the inbox;
  Gumroad has no receiver and is polled through its API. A webhook therefore does not call the Scout: the sale is picked up within
  `SCOUT_POLL_SECONDS` (default 30; 0 turns the loop off).
- **What happens to a sale, and what does not.** A sale becomes a lead and a `signal.captured` event; the Qualifier scores it (marked CONSENT_RISK, never
  Tier 2). The Drafter then finds no consented contact and records `DRAFT_SKIPPED / NO_CONTACT`. This is deliberate and unchanged: a marketplace sale carries
  no consent to be emailed (ADR-21). The Copywriter is therefore reached for leads that come through the consented import, not for a bare sale.

## Consequences
- **The product decision this drop surfaces:** a creator's sale will not by itself produce an email. Emailing a buyer needs a legal basis and a way to
  hold the buyer's address, which marketplace signals deliberately do not carry (C9, legal review). Options are a consented capture at checkout, a
  post-purchase message under the transactional context, or keeping the CSV import. Nothing here chooses one.
- A sale attributed to a lead we already contacted (Lemon Squeezy carries our lead id) still creates a new lead and uses a place in the daily cap, because
  conversion attribution (BL-06) is not built. The inbox keeps the attribution, so the fix is later and local.
- The request and response shapes of both providers, and which token-limit parameter each model accepts, were written from their documentation without
  network access; tests pin what this code sends, not what the providers do. A model that rejects `max_completion_tokens` answers 400 and the draft falls
  back to the template: the first live draft will show it.
- `ICP_PRODUCT_IDS` must hold the marketplace's own product ids (Lemon Squeezy's numeric product id, Payhip's product key, Gumroad's id), or a sale scores low.
- waitress is a new dependency. The in-process governor forgets its buckets on restart, which at one request every ten seconds is harmless.

## Revisit when
The first live drafts show the model's real cost per draft (set the ceiling from it), a second model is wanted for another role, the alert channel is
chosen (D2), or the consent question for buyers is decided.
