"""Compliance Guard rules (S3-02, spec 3.1, 6, 8; ADR-02, ADR-07). Pure Python, deterministic, no network.

Each rule looks at one draft and returns findings. A BLOCK finding means the draft must not go out as written. An
ESCALATE finding means a human must decide. The verdict is BLOCK if any finding blocks, else ESCALATE if any
escalates, else PASS. The rules are strict on purpose: a false block costs a reviewer a minute, a false pass can cost
the sender account. Anything that cannot be checked (no allowed link hosts configured, no discount cap configured)
fails closed. The pattern lists are a starting point to tune against real reviewer rejections."""
from __future__ import annotations

import ipaddress
import re
import urllib.parse
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Callable, FrozenSet, List, Optional, Tuple

from adapters.email_channel.message import split_message

BLOCK, ESCALATE, PASS = "BLOCK", "ESCALATE", "PASS"
OUTREACH, BUYER_INITIATED = "consented_outreach", "buyer_initiated"
SHORTENERS = frozenset({"bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly", "cutt.ly",
                        "shorturl.at"})


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str


@dataclass(frozen=True)
class DraftFacts:
    context: str
    body: object                                   # the whole message: "Subject: ..." line, blank line, text
    has_current_permission: bool
    discount_pct: Optional[Decimal] = None         # the discount the draft actually grants, if any
    confidence: Optional[float] = None             # the Copywriter's own confidence
    risk_flags: Tuple[str, ...] = ()


@dataclass(frozen=True)
class GuardConfig:
    allowed_link_hosts: FrozenSet[str] = frozenset()     # empty means no link is allowed (fail closed)
    max_discount_pct: Optional[Decimal] = None           # ADR-07; None means not configured: any discount escalates
    min_confidence: float = 0.5


# ------------------------------------------------------------------ helpers
def _parts(facts: DraftFacts) -> Tuple[Optional[str], str]:
    """(subject, text). The subject is None when the message does not follow the format."""
    if not isinstance(facts.body, str):
        return None, ""
    try:
        return split_message(facts.body)
    except ValueError:
        return None, facts.body


def _any(patterns, text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


# ------------------------------------------------------------------ rules: each returns a list of Findings
def rule_body(f: DraftFacts, c: GuardConfig) -> List[Finding]:
    if not isinstance(f.body, str) or not f.body.strip():
        return [Finding("MISSING_BODY", BLOCK)]
    return [] if _parts(f)[0] is not None else [Finding("INVALID_MESSAGE_FORMAT", BLOCK)]


def rule_permission(f: DraftFacts, c: GuardConfig) -> List[Finding]:
    return [Finding("NO_CURRENT_PERMISSION", BLOCK)] if f.context == OUTREACH and not f.has_current_permission else []


def rule_discount(f: DraftFacts, c: GuardConfig) -> List[Finding]:
    out: List[Finding] = []
    subject, text = _parts(f)
    mentioned = re.findall(r"(\d{1,3}(?:\.\d+)?)\s*%\s*off", f"{subject or ''} {text}", re.I)
    d = f.discount_pct
    if d is not None:
        if not isinstance(d, Decimal) or not (Decimal(0) < d <= Decimal(100)):
            out.append(Finding("INVALID_DISCOUNT", BLOCK))
        elif c.max_discount_pct is None:
            out.append(Finding("DISCOUNT_CAP_NOT_CONFIGURED", ESCALATE))
        elif d > c.max_discount_pct:
            out.append(Finding("DISCOUNT_ABOVE_CAP", BLOCK))
    for m in mentioned:                                  # the text must promise exactly what the offer grants
        try:
            if d is None or Decimal(m) != d:
                out.append(Finding("DISCOUNT_MISMATCH", BLOCK))
                break
        except InvalidOperation:
            out.append(Finding("DISCOUNT_MISMATCH", BLOCK))
            break
    return out


_PLACEHOLDERS = (r"\{\{.*?\}\}", r"\[\[.*?\]\]", r"%[A-Za-z_]{2,}%", r"\{[a-z_]{2,}\}", r"<[A-Z_]{3,}>")


def rule_placeholders(f: DraftFacts, c: GuardConfig) -> List[Finding]:
    return [Finding("UNRESOLVED_PLACEHOLDER", BLOCK)] if isinstance(f.body, str) and _any(_PLACEHOLDERS, f.body) else []


def rule_subject(f: DraftFacts, c: GuardConfig) -> List[Finding]:
    subject, _ = _parts(f)
    if subject is None:
        return []
    out: List[Finding] = []
    if f.context != BUYER_INITIATED and re.match(r"\s*(re|fwd?|fw)\s*:", subject, re.I):
        out.append(Finding("DECEPTIVE_SUBJECT", BLOCK))                 # a fake reply or forward: deceptive headers
    if re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", subject):
        out.append(Finding("PII_IN_SUBJECT", BLOCK))
    elif re.search(r"(?:\d[\s.()-]?){7,}", subject):
        out.append(Finding("POSSIBLE_PII_IN_SUBJECT", ESCALATE))
    letters = [ch for ch in subject if ch.isalpha()]
    if (len(letters) >= 6 and sum(ch.isupper() for ch in letters) / len(letters) > 0.6) or subject.count("!") > 1:
        out.append(Finding("SHOUTING", ESCALATE))
    if len(subject) > 78:
        out.append(Finding("SUBJECT_TOO_LONG", ESCALATE))
    return out


_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.I)


def rule_links(f: DraftFacts, c: GuardConfig) -> List[Finding]:
    if not isinstance(f.body, str):
        return []
    allowed = tuple(h.lower() for h in c.allowed_link_hosts)
    out: set = set()
    for raw in _URL.findall(f.body):
        bare = raw.lower().startswith("www.")
        if bare or not raw.lower().startswith("https://"):
            out.add(Finding("LINK_NOT_HTTPS", BLOCK))
        try:
            u = urllib.parse.urlsplit("http://" + raw if bare else raw)
            host, creds = (u.hostname or "").lower(), u.username is not None
        except ValueError:
            out.add(Finding("LINK_MALFORMED", BLOCK))
            continue
        if creds:
            out.add(Finding("LINK_HAS_CREDENTIALS", BLOCK))
        try:
            ipaddress.ip_address(host)
            out.add(Finding("LINK_IP_HOST", BLOCK))
            continue
        except ValueError:
            pass
        if host in SHORTENERS:
            out.add(Finding("LINK_SHORTENER", BLOCK))
        if not any(host == h or host.endswith("." + h) for h in allowed):
            out.add(Finding("LINK_HOST_NOT_ALLOWED", BLOCK))
    return sorted(out, key=lambda x: x.code)


_CLAIMS = (r"guaranteed\s+(income|results?|returns?|profits?|success)", r"get\s+rich", r"\brisk[- ]free\b", r"\bno\s+risk\b",
           r"\bcures?\b", r"\bfda[- ]approved\b", r"make\s+\$?\d[\d,]*\s*(a|per|/)\s*(day|week|month)",
           r"100\s*%\s*(safe|guaranteed|effective)")
_URGENCY = (r"\bact\s+now\b", r"\blast\s+chance\b", r"\bonly\s+\d+\s+(left|remaining)\b", r"\bexpires?\s+(today|tonight|soon)\b",
            r"\bhurry\b", r"\bdon'?t\s+miss\s+out\b", r"\bending\s+soon\b")
_SPAM = ("free money", "you are a winner", "click here", "buy now", "limited time offer", "no obligation", "dear friend",
         "congratulations", "100% free", "order now")


def rule_content(f: DraftFacts, c: GuardConfig) -> List[Finding]:
    if not isinstance(f.body, str):
        return []
    out: List[Finding] = []
    if _any(_CLAIMS, f.body):
        out.append(Finding("PROHIBITED_CLAIM", BLOCK))
    if _any(_URGENCY, f.body):
        out.append(Finding("URGENCY_PRESSURE", ESCALATE))
    lowered = f.body.lower()
    if sum(p in lowered for p in _SPAM) >= 2:
        out.append(Finding("SPAM_PHRASES", ESCALATE))
    if _parts(f)[1].count("!") > 4:
        out.append(Finding("SHOUTING", ESCALATE))
    return out


def rule_signals(f: DraftFacts, c: GuardConfig) -> List[Finding]:
    out: List[Finding] = []
    if f.confidence is not None and f.confidence < c.min_confidence:
        out.append(Finding("LOW_CONFIDENCE", ESCALATE))
    if "TOS_RISK" in f.risk_flags:
        out.append(Finding("RISK_FLAG_TOS", ESCALATE))
    return out


RULES: Tuple[Callable[[DraftFacts, GuardConfig], List[Finding]], ...] = (
    rule_body, rule_permission, rule_discount, rule_placeholders, rule_subject, rule_links, rule_content, rule_signals)


def merge(findings) -> Tuple[Finding, ...]:
    """One finding per code, BLOCK winning over ESCALATE, in code order."""
    best = {}
    for x in findings:
        if x.code not in best or x.severity == BLOCK:
            best[x.code] = x
    return tuple(best[k] for k in sorted(best))


def run_rules(facts: DraftFacts, config: GuardConfig) -> Tuple[Finding, ...]:
    return merge(x for rule in RULES for x in rule(facts, config))


def verdict_of(findings) -> str:
    severities = {x.severity for x in findings}
    return BLOCK if BLOCK in severities else ESCALATE if ESCALATE in severities else PASS
