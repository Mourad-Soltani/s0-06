# S0-09: LLM routing and cost design (built in S1-10)

## Budget
$300 per month, LLM plus infrastructure, in integer micro-USD: 300,000,000. Alert at 80% (240,000,000), and a second
alert at 100%. Exceeding the ceiling is a bug, not a scaling event (product owner ruling).

## Routing: one model per role, chosen by configuration
| Role | LLM use (spec 3.1) | Routed to |
|---|---|---|
| Scout | extraction and classification only | small, cheap model |
| Qualifier | structured scoring | small, cheap model |
| Copywriter | generation with few-shot examples | the strong model, batched |
| Closer | template selection only | small, cheap model |
| Compliance Guard | rule-based plus a classifier | small, cheap model |
| Analyst | statistical, not generative | none (no LLM) |

`RoutingPolicy` requires every role to be listed (None means no LLM) and every routed model to be priced, so a
misconfiguration fails at startup. The model names and prices are NOT in the code. They come from configuration,
filled in from the provider's current price list. Open decision for the owner: the provider, the two model names,
and their prices.

## Cost tracking
- One `core.cost_ledger` row per call (`call_id` unique, so a retry cannot double count), append-only.
- Cost = ceil((input_tokens x input price + output_tokens x output price) / 1,000,000), exact integers, rounded up.
- Infrastructure costs are recorded as `INFRA` rows with an item code.
- `core.cost_status()` sums one UTC calendar month against the ceiling.
- Before a paid call, `require_headroom(estimate)` refuses a call that would pass the ceiling.

## Alert path
At most once per month per threshold (`core.cost_alert` primary key). The alert is marked fired only after the
notifier succeeds, so a failed delivery is retried on the next record. The channel itself is the open decision D2
(the default named in the baseline, email through an ESP to an org address, cannot be met yet: there is no org and
no verified sending domain). Until it is decided, `NotConfiguredNotifier` fails loudly on every attempt, and every
`RecordResult` reports the failed delivery.

## Worked example (ILLUSTRATIVE prices, not real)
If a model cost 3,000,000 micro-USD per million input tokens and 15,000,000 per million output tokens, a call with
2,000 input and 500 output tokens costs ceil((2,000 x 3,000,000 + 500 x 15,000,000) / 1,000,000) = 13,500 micro-USD,
about 1.35 cents. The $300 ceiling would then allow roughly 22,000 such calls a month.
