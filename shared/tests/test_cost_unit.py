"""Cost helpers: no database. Names are CU*."""
import pytest

from shared.cost import (ROLES, BudgetExhausted, InvalidRouting, ModelPrice, NoLlmForRole, NotConfiguredNotifier,
                         PriceBook, RecordingNotifier, RoutingPolicy, UnpricedModel, CostAlert, crossed)

BOOK = PriceBook({"small": ModelPrice(300_000, 1_500_000), "strong": ModelPrice(3_000_000, 15_000_000)})


def test_CU1_estimate_is_exact_integer_arithmetic_rounded_up():
    assert BOOK.estimate_micro("strong", 2_000, 500) == 13_500                 # exact: 6,000 + 7,500
    assert BOOK.estimate_micro("small", 1, 0) == 1                              # 0.3 micro rounds UP to 1
    assert BOOK.estimate_micro("small", 0, 0) == 0
    assert BOOK.estimate_micro("strong", 10 ** 12, 10 ** 12) == 18 * 10 ** 12  # no float, no overflow
    for i in (0, 1, 7, 999, 12_345):
        for o in (0, 1, 3, 4_321):
            for m, p in (("small", (300_000, 1_500_000)), ("strong", (3_000_000, 15_000_000))):
                assert BOOK.estimate_micro(m, i, o) * 1_000_000 >= i * p[0] + o * p[1]   # never under-reports


def test_CU2_unpriced_models_and_bad_inputs_are_errors_not_zeros():
    with pytest.raises(UnpricedModel):
        BOOK.estimate_micro("mystery", 10, 10)
    for bad in ((-1, 0), (0, -1)):
        with pytest.raises(ValueError):
            BOOK.estimate_micro("small", *bad)
    with pytest.raises(ValueError):
        ModelPrice(-1, 0)
    with pytest.raises(ValueError):
        PriceBook({"bad name": ModelPrice(1, 1)})
    assert "small" in BOOK and "mystery" not in BOOK


GOOD = {"SCOUT": "small", "QUALIFIER": "small", "COPYWRITER": "strong", "CLOSER": "small",
        "COMPLIANCE_GUARD": "small", "ANALYST": None}


def test_CU3_routing_resolves_models_and_the_analyst_has_none():
    policy = RoutingPolicy(GOOD, BOOK)
    assert policy.model_for("COPYWRITER") == "strong" and policy.model_for("SCOUT") == "small"
    with pytest.raises(NoLlmForRole):
        policy.model_for("ANALYST")
    with pytest.raises(InvalidRouting):
        policy.model_for("INTERN")


def test_CU4_routing_misconfiguration_fails_at_construction():
    missing = {k: v for k, v in GOOD.items() if k != "CLOSER"}
    extra = {**GOOD, "INTERN": "small"}
    unpriced = {**GOOD, "COPYWRITER": "mystery"}
    for bad in (missing, extra, unpriced):
        with pytest.raises(InvalidRouting):
            RoutingPolicy(bad, BOOK)
    assert len(ROLES) == 6


def test_CU5_notifiers():
    from datetime import date
    from decimal import Decimal
    alert = CostAlert(date(2026, 10, 1), 80, 240_000_000, 300_000_000, Decimal("80.00"))
    rec = RecordingNotifier()
    rec.notify(alert)
    assert rec.alerts == [alert]
    with pytest.raises(RuntimeError):
        NotConfiguredNotifier().notify(alert)
    assert issubclass(BudgetExhausted, RuntimeError)


def test_CU6_threshold_crossing_uses_exact_integers_not_the_rounded_percentage():
    assert crossed(239_999_999, 300_000_000, 80) is False        # 79.9999997%: the DB's 2-decimal pct reads 80.00
    assert crossed(240_000_000, 300_000_000, 80) is True
    assert crossed(299_999_999, 300_000_000, 100) is False and crossed(300_000_000, 300_000_000, 100) is True
    assert crossed(0, 300_000_000, 80) is False and crossed(10 ** 15, 300_000_000, 100) is True
    with pytest.raises(ValueError):
        crossed(1, 0, 80)
