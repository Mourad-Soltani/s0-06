"""Lead scoring: pure Python. Names SC*."""
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.scoring import (WEIGHTS, IcpDefinition, RuleScorer, Scores, SignalFeatures, composite, decide)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
ICP = IcpDefinition(frozenset({"gr-1"}))


def scorer():
    return RuleScorer(ICP, now=lambda: NOW)


def feats(kind="SALE", product="gr-1", age=timedelta(hours=1), has_contact=False):
    return SignalFeatures(kind=kind, occurred_at=NOW - age, product_external_id=product, has_contact=has_contact)


def result(f):
    s = scorer().score(f)
    c = composite(s)
    return c, decide(c, s.confidence), s


def test_SC1_composite_is_an_exact_weighted_mean_rounded_half_up():
    assert sum(WEIGHTS.values()) == 100
    assert composite(Scores(100, 100, 100, 100, 100, 1.0)) == 100 and composite(Scores(0, 0, 0, 0, 0, 0.0)) == 0
    assert composite(Scores(90, 50, 50, 80, 80, 0.8)) == 75                  # 7450 / 100 = 74.5 rounds up
    assert composite(Scores(0, 0, 0, 0, 2, 0.5)) == 1                        # exactly 0.5 rounds up
    assert composite(Scores(0, 0, 0, 0, 1, 0.5)) == 0                        # 0.25 rounds down


@pytest.mark.parametrize("comp,conf,expected", [
    (59, 0.99, "NEEDS_REVIEW"), (60, 0.75, "QUALIFIED"), (60, 0.7499, "NEEDS_REVIEW"), (100, 0.74, "NEEDS_REVIEW"),
    (40, 0.99, "NEEDS_REVIEW"), (40, 0.0, "NEEDS_REVIEW"), (39, 0.99, "DISQUALIFIED"), (0, 0.0, "DISQUALIFIED"),
    (39, 0.1, "DISQUALIFIED")])
def test_SC2_decision_table_follows_adr_06(comp, conf, expected):
    assert decide(comp, conf) == expected


@pytest.mark.parametrize("kw", [dict(icp_match=101), dict(icp_match=-1), dict(pain_intensity=True),
                                dict(budget_signal=50.5), dict(confidence=1.01), dict(confidence=-0.1),
                                dict(risk_flags=("not a flag",))])
def test_SC3_scores_are_validated(kw):
    base = dict(icp_match=50, pain_intensity=50, authority_signal=50, timeline_signal=50, budget_signal=50, confidence=0.5)
    base.update(kw)
    with pytest.raises(ValueError):
        Scores(**base)


def test_SC4_signal_features_round_trip_through_a_payload():
    f = feats()
    p = f.to_payload()
    assert SignalFeatures.from_payload(p) == f and "@" not in repr(p)
    assert SignalFeatures.from_payload({**p, "occurred_at": "2026-10-19T10:00:00"}).occurred_at.tzinfo is not None
    assert "product_external_id" not in feats(product=None).to_payload()
    for bad in ({}, {**p, "kind": "WAT"}, {**p, "occurred_at": 5}, {**p, "occurred_at": "never"}):
        with pytest.raises(ValueError):
            SignalFeatures.from_payload(bad)


def test_SC5_the_headline_paths_with_the_placeholder_rules():
    c, status, s = result(feats())                                           # recent sale of an ICP product
    assert (c, status, s.confidence, s.risk_flags) == (75, "QUALIFIED", 0.8, ("CONSENT_RISK",))
    assert result(feats(product="other"))[:2] == (54, "NEEDS_REVIEW")        # off-catalog sale
    assert result(feats(age=timedelta(days=10)))[:2] == (72, "QUALIFIED")    # old sale: timeline drops, still clears
    assert result(feats(kind="SUBSCRIBER"))[:2] == (65, "NEEDS_REVIEW")      # score clears, confidence (0.6) does not
    assert result(feats(kind="QA", product="other", age=timedelta(days=30)))[:2] == (41, "NEEDS_REVIEW")


def test_SC6_recency_boundary_contact_flag_and_the_floor():
    assert scorer().score(feats(age=timedelta(days=7))).timeline_signal == 80
    assert scorer().score(feats(age=timedelta(days=7, seconds=1))).timeline_signal == 50
    assert scorer().score(feats(has_contact=True)).risk_flags == ()
    # the rule scorer cannot disqualify (its lowest composite is 41): only a scorer with real evidence can
    lowest = min(composite(scorer().score(feats(kind=k, product="x", age=timedelta(days=99)))) for k in
                 ("SALE", "REVIEW", "QA", "CART", "SUBSCRIBER", "OFFER_CODE_USE"))
    assert lowest == 41
