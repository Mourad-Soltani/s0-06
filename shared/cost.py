"""Cost tracker (S1-10): a per-call ledger, a monthly view against the ceiling, and a once-per-month 80% alert.
Design and the routing policy are in docs/S0-09-llm-routing-and-cost-design.md.

Money is integer micro-USD end to end (1 USD = 1,000,000). Estimates round UP, so the tracker never reports less
than a call may have cost. Prices come from configuration: NOTHING here invents a price, and an unpriced model is
an error, never a zero. Alerts are at-least-once: an alert is marked fired only AFTER delivery succeeds, so a
failed notification is retried on the next record instead of being lost. No PII: ids, codes and numbers only.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import List, Mapping, Optional, Protocol

from shared.bus.events import error_code

ROLES = ("SCOUT", "QUALIFIER", "COPYWRITER", "CLOSER", "COMPLIANCE_GUARD", "ANALYST")   # mirrors core.agent_role
_MODEL = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_ITEM = re.compile(r"^[A-Z0-9_]{1,64}$")
_MICRO = 1_000_000


class UnpricedModel(KeyError):
    """No configured price for this model."""


class NoLlmForRole(LookupError):
    """This role is deliberately LLM-free (for example the Analyst)."""


class InvalidRouting(ValueError):
    pass


class BudgetExhausted(RuntimeError):
    """The month's ceiling would be exceeded. Per the product owner, exceeding it is a bug, not a scaling event."""


@dataclass(frozen=True)
class ModelPrice:
    input_micro_per_mtok: int     # micro-USD per million input tokens
    output_micro_per_mtok: int    # micro-USD per million output tokens

    def __post_init__(self):
        if self.input_micro_per_mtok < 0 or self.output_micro_per_mtok < 0:
            raise ValueError("prices must be non-negative")


class PriceBook:
    def __init__(self, prices: Mapping[str, ModelPrice]):
        for model in prices:
            if not _MODEL.match(model):
                raise ValueError(f"invalid model name {model!r}")
        self._prices = dict(prices)

    def __contains__(self, model: str) -> bool:
        return model in self._prices

    def price(self, model: str) -> ModelPrice:
        try:
            return self._prices[model]
        except KeyError:
            raise UnpricedModel(f"no price configured for {model!r}") from None

    def estimate_micro(self, model: str, input_tokens: int, output_tokens: int) -> int:
        """ceil((in * price_in + out * price_out) / 1e6), in exact integer arithmetic."""
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must be non-negative")
        p = self.price(model)
        return -(-(input_tokens * p.input_micro_per_mtok + output_tokens * p.output_micro_per_mtok) // _MICRO)


class RoutingPolicy:
    """Which model each role uses. Every role must be listed explicitly (None = no LLM), and every model must be
    priced, so a misconfiguration fails at startup and not on the first call."""

    def __init__(self, role_models: Mapping[str, Optional[str]], pricebook: PriceBook):
        if set(role_models) != set(ROLES):
            raise InvalidRouting(f"routing must list exactly these roles: {sorted(ROLES)}")
        for role, model in role_models.items():
            if model is not None and model not in pricebook:
                raise InvalidRouting(f"role {role} routes to unpriced model {model!r}")
        self._models = dict(role_models)

    def model_for(self, role: str) -> str:
        if role not in self._models:
            raise InvalidRouting(f"unknown role {role!r}")
        if self._models[role] is None:
            raise NoLlmForRole(role)
        return self._models[role]


@dataclass(frozen=True)
class CostAlert:
    period_start: date
    threshold_pct: int
    spent_micro_usd: int
    ceiling_micro_usd: int
    pct_used: Decimal


class Notifier(Protocol):
    def notify(self, alert: CostAlert) -> None: ...


class NotConfiguredNotifier:
    """Placeholder until an alert channel exists (open decision D2). Fails loudly so nobody assumes alerts go out."""

    def notify(self, alert: CostAlert) -> None:
        raise RuntimeError("alert channel not configured")


class RecordingNotifier:
    def __init__(self):
        self.alerts: List[CostAlert] = []

    def notify(self, alert: CostAlert) -> None:
        self.alerts.append(alert)


def crossed(spent_micro_usd: int, ceiling_micro_usd: int, threshold_pct: int) -> bool:
    """Exact integer test for "spent has reached threshold% of the ceiling". core.cost_status() rounds pct_used to
    2 decimals for display (79.9999997% shows as 80.00), so alerts must never be decided from it."""
    if ceiling_micro_usd <= 0:
        raise ValueError("ceiling must be positive")
    return spent_micro_usd * 100 >= threshold_pct * ceiling_micro_usd


@dataclass(frozen=True)
class CostStatus:
    period_start: date
    spent_micro_usd: int
    ceiling_micro_usd: int
    pct_used: Decimal                   # rounded to 2 decimals for display; alerts use crossed()


@dataclass(frozen=True)
class AlertOutcome:
    threshold_pct: int
    delivered: bool
    error_code: Optional[str] = None


@dataclass(frozen=True)
class RecordResult:
    cost_micro_usd: int
    inserted: bool                      # False when call_id was already recorded (nothing was double counted)
    alerts: tuple = ()                  # AlertOutcome for every alert attempted by this record


class CostTracker:
    """conn must be autocommit. Needs INSERT/SELECT on core.cost_ledger and core.cost_alert, SELECT on
    core.cost_config, and EXECUTE on core.cost_status and core.try_fire_cost_alert (app_rw has them all)."""

    def __init__(self, conn, pricebook: PriceBook, notifier: Notifier):
        self.conn, self.pricebook, self.notifier = conn, pricebook, notifier

    # ---------------------------------------------------------------- recording
    def record_llm_call(self, call_id: uuid.UUID, role: str, model: str, input_tokens: int, output_tokens: int, *,
                        lead_id: Optional[uuid.UUID] = None, trace_id: Optional[uuid.UUID] = None,
                        ts: Optional[datetime] = None) -> RecordResult:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        cost = self.pricebook.estimate_micro(model, input_tokens, output_tokens)   # raises UnpricedModel: nothing written
        row = self.conn.execute(
            "INSERT INTO core.cost_ledger (call_id, ts, category, role, model, input_tokens, output_tokens, "
            "cost_micro_usd, lead_id, trace_id) VALUES (%s, COALESCE(%s, now()), 'LLM', %s::core.agent_role, %s, %s, %s, "
            "%s, %s, %s) ON CONFLICT (call_id) DO NOTHING RETURNING id",
            (call_id, ts, role, model, input_tokens, output_tokens, cost, lead_id, trace_id)).fetchone()
        return RecordResult(cost, row is not None, tuple(self.check_alerts()))

    def record_infra(self, call_id: uuid.UUID, item_code: str, cost_micro_usd: int, *,
                     ts: Optional[datetime] = None) -> RecordResult:
        if not _ITEM.match(item_code or ""):
            raise ValueError("item_code must be an UPPER_SNAKE code")
        if cost_micro_usd < 0:
            raise ValueError("cost must be non-negative")
        row = self.conn.execute(
            "INSERT INTO core.cost_ledger (call_id, ts, category, item_code, cost_micro_usd) "
            "VALUES (%s, COALESCE(%s, now()), 'INFRA', %s, %s) ON CONFLICT (call_id) DO NOTHING RETURNING id",
            (call_id, ts, item_code, cost_micro_usd)).fetchone()
        return RecordResult(cost_micro_usd, row is not None, tuple(self.check_alerts()))

    # ---------------------------------------------------------------- reading
    def status(self, period: Optional[date] = None) -> CostStatus:
        row = (self.conn.execute("SELECT period_start, spent_micro_usd, ceiling_micro_usd, pct_used FROM core.cost_status(%s)",
                                 (period,)) if period else
               self.conn.execute("SELECT period_start, spent_micro_usd, ceiling_micro_usd, pct_used FROM core.cost_status()")
               ).fetchone()
        return CostStatus(*row)

    def require_headroom(self, estimated_micro_usd: int) -> None:
        """Call before a paid call. Spending exactly up to the ceiling is allowed; one micro-USD over is not."""
        st = self.status()
        if st.spent_micro_usd + estimated_micro_usd > st.ceiling_micro_usd:
            raise BudgetExhausted(f"{estimated_micro_usd} would exceed the monthly ceiling")

    # ---------------------------------------------------------------- alerts
    def check_alerts(self) -> List[AlertOutcome]:
        """Fire the configured threshold (80%) and the ceiling (100%) once per month each. The alert is recorded
        as fired only after the notifier succeeds, so a delivery failure is retried on the next call."""
        st = self.status()
        alert_pct = self.conn.execute("SELECT alert_pct FROM core.cost_config").fetchone()[0]
        outcomes = []
        for threshold in sorted({alert_pct, 100}):
            if not crossed(st.spent_micro_usd, st.ceiling_micro_usd, threshold):
                continue
            already = self.conn.execute(
                "SELECT EXISTS (SELECT 1 FROM core.cost_alert WHERE period_start = %s AND pct = %s)",
                (st.period_start, threshold)).fetchone()[0]
            if already:
                continue
            try:
                self.notifier.notify(CostAlert(st.period_start, threshold, st.spent_micro_usd,
                                               st.ceiling_micro_usd, st.pct_used))
            except Exception as exc:  # noqa: BLE001 - delivery failure: report, leave unfired, retry next time
                outcomes.append(AlertOutcome(threshold, False, error_code(exc)))
                continue
            self.conn.execute("SELECT core.try_fire_cost_alert(%s, %s::smallint)", (st.period_start, threshold))
            outcomes.append(AlertOutcome(threshold, True))
        return outcomes
