"""What the reviewer sees, as plain data (S3-04). Pure: no Streamlit, no network. Text shown to a person is written here so it
can be tested and changed in one place. Nothing here displays an address, a name or a consent reference."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

REJECT_REASONS: Tuple[Tuple[str, str], ...] = (
    ("TONE_OFF", "The tone is off"), ("INACCURATE", "It says something inaccurate"), ("WRONG_PRODUCT", "Wrong product for this person"),
    ("TOO_PUSHY", "Too pushy"), ("OFF_BRAND", "It does not sound like us"), ("NEEDS_REWRITE", "Needs a rewrite"), ("OTHER", "Another reason"))

_ERRORS = {
    "UNAUTHENTICATED": "Your token was not accepted. Please sign in again.", "NOT_FOUND": "That item no longer exists.",
    "NOT_PENDING": "Someone already decided this one.", "NOT_APPROVABLE": "The checks have not passed, so this cannot be approved.",
    "EDIT_UNAVAILABLE": "Editing is not available on this server.", "INVALID_REQUEST": "The request was not accepted.",
    "UNREACHABLE": "The review service cannot be reached. Check the address and try again.", "BAD_RESPONSE": "The review service sent something unexpected.",
    "INTERNAL": "The review service had a problem. Try again in a moment."}

_VERDICT = {"PASS": "Checks passed", "BLOCK": "Blocked by the checks", "ESCALATE": "Needs your judgement", None: "Not checked yet"}
_FLAGS = {"CONSENT_RISK": "No consent on record for this lead", "TOS_RISK": "Might break a marketplace rule"}
_SOURCES = {"INBOUND": "An imported contact", "MARKETPLACE_SIGNAL": "A marketplace signal", "COMMUNITY": "A community post",
            "REFERRAL": "A referral"}
FINDING_LABELS = {
    "MISSING_BODY": "The message is empty", "INVALID_MESSAGE_FORMAT": "The message does not follow the required format",
    "NO_CURRENT_PERMISSION": "There is no current consent for this contact", "INVALID_DISCOUNT": "The discount is not valid",
    "DISCOUNT_ABOVE_CAP": "The discount is above your cap", "DISCOUNT_CAP_NOT_CONFIGURED": "A discount is offered but no cap is set",
    "DISCOUNT_MISMATCH": "The text promises a different discount than the offer grants", "UNRESOLVED_PLACEHOLDER": "It still contains a template placeholder",
    "DECEPTIVE_SUBJECT": "The subject pretends to be a reply or forward", "PII_IN_SUBJECT": "The subject contains an email address",
    "POSSIBLE_PII_IN_SUBJECT": "The subject may contain a phone or order number", "SHOUTING": "Too many capitals or exclamation marks",
    "SUBJECT_TOO_LONG": "The subject is too long", "LINK_NOT_HTTPS": "A link is not secure (https)", "LINK_SHORTENER": "A link uses a URL shortener",
    "LINK_HAS_CREDENTIALS": "A link contains a login", "LINK_IP_HOST": "A link points at a bare IP address",
    "LINK_HOST_NOT_ALLOWED": "A link goes to a site that is not allowed", "LINK_MALFORMED": "A link is malformed",
    "PROHIBITED_CLAIM": "It makes a claim that is not allowed", "URGENCY_PRESSURE": "It uses pressure language", "SPAM_PHRASES": "It uses spam-like phrases",
    "LOW_CONFIDENCE": "The writer was not confident", "RISK_FLAG_TOS": "The lead carries a marketplace-rule risk", "NO_ADAPTER": "No adapter vouches for this channel",
    "MISSING_CONSENT_REF": "The message does not cite the consent it relies on", "CONTEXT_NOT_ALLOWED": "This kind of message is not allowed on this channel",
    "MESSAGE_TOO_LONG": "The message is too long", "MESSAGING_UNSUPPORTED": "This channel cannot send messages",
    "WRONG_MARKETPLACE": "The message is for a different channel", "LISTING_MUTATION_UNSUPPORTED": "This channel cannot change listings",
    "OFFER_CODES_UNSUPPORTED": "This channel cannot create offer codes", "BROWSER_AUTOMATION": "It would need browser automation, which is not allowed",
    "CAPTCHA_BYPASS": "It would need a CAPTCHA bypass, which is not allowed", "DEVICE_VERIFICATION_BYPASS": "It would need a device-check bypass, which is not allowed",
    "ACCOUNT_LINKING_BYPASS": "It would need an account-linking bypass, which is not allowed"}
_SCORES = (("icp_match_score", "Fit with your ideal customer"), ("pain_intensity", "Need"), ("authority_signal", "Decision power"),
           ("timeline_signal", "Timing"), ("budget_signal", "Budget"))


def friendly_error(code: Optional[str]) -> str:
    return _ERRORS.get(code or "", "Something went wrong. Please try again.")


def _when(iso: Any) -> str:
    try:
        return datetime.fromisoformat(str(iso)).strftime("%d %b %H:%M UTC")
    except ValueError:
        return "unknown time"


def _pct(x: Any) -> Optional[int]:
    return round(x * 100) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


@dataclass(frozen=True)
class QueueRow:
    proposal_id: str
    label: str
    marketplace: str
    tier: int
    verdict: Optional[str]
    confidence_pct: Optional[int]


def queue_rows(actions: List[Dict[str, Any]]) -> List[QueueRow]:
    rows = []
    for a in actions:
        pid = a.get("proposal_id")
        if not isinstance(pid, str):
            continue
        verdict, tier, mp = a.get("compliance_verdict"), a.get("autonomy_tier", 0), str(a.get("marketplace", "?"))
        rows.append(QueueRow(pid, f"{_when(a.get('created_at'))} · {mp} · {_VERDICT.get(verdict, 'Unknown')} · tier {tier}", mp, tier,
                             verdict, _pct(a.get("confidence"))))
    return rows


@dataclass(frozen=True)
class Finding:
    code: str
    label: str
    severity: str


@dataclass(frozen=True)
class DetailView:
    proposal_id: str
    title: str
    subject: str
    body: str
    source_label: str
    offer_line: Optional[str]
    verdict: Optional[str]
    verdict_label: str
    findings: Tuple[Finding, ...]
    scores: Tuple[Tuple[str, str], ...]
    score_headline: str
    lead_summary: Tuple[str, ...]
    risk_flags: Tuple[str, ...]
    tier: int
    tier_note: str
    writer_confidence_pct: Optional[int]
    can_approve: bool
    approve_blocked_reason: Optional[str]


def split_message(body: Any) -> Tuple[str, str]:
    """('subject', 'text') from 'Subject: ...' + blank line + text; the whole body when it does not follow the format."""
    if isinstance(body, str):
        head, sep, rest = body.partition("\n\n")
        if head.startswith("Subject:") and sep and "\n" not in head:
            return head[len("Subject:"):].strip(), rest
        return "(no subject)", body
    return "(no subject)", ""


def detail_view(d: Dict[str, Any]) -> DetailView:
    payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
    subject, body = split_message(payload.get("body"))
    verdict, status, tier = d.get("compliance_verdict"), d.get("status"), d.get("autonomy_tier", 0)
    source = payload.get("source")
    source_label = {"LLM": "Written by the model", "TEMPLATE": "Written from the standard template (no model was used)"}.get(source, "Source unknown")
    pct, code = payload.get("discount_pct"), payload.get("offer_code")
    offer = f"{pct}% off with code {code}" if pct and code else None
    lead = d.get("lead") if isinstance(d.get("lead"), dict) else None
    scores, headline, summary, flags = (), "Not scored", (), ()
    if lead:
        scores = tuple((label, f"{lead[k]}/100" if isinstance(lead.get(k), int) else "not available") for k, label in _SCORES)
        headline = f"{lead['composite_score']}/100" if isinstance(lead.get("composite_score"), int) else "Not scored"
        lc = _pct(lead.get("confidence"))
        summary = tuple(x for x in (f"Source: {_SOURCES.get(lead.get('source_type'), 'Unknown')}",
                                    f"Product: {lead['product_external_id']}" if lead.get("product_external_id") else None,
                                    f"Scoring confidence: {lc}%" if lc is not None else None) if x)
        flags = tuple(_FLAGS.get(f, f.replace("_", " ").lower()) for f in (lead.get("risk_flags") or []))
    findings = tuple(Finding(f["rule_code"], FINDING_LABELS.get(f["rule_code"], f["rule_code"].replace("_", " ").lower()), f.get("severity", "BLOCK"))
                     for f in (d.get("findings") or []) if isinstance(f, dict) and isinstance(f.get("rule_code"), str))
    can, why = True, None
    if status != "PROPOSED":
        can, why = False, "Someone already decided this one."
    elif verdict is None:
        can, why = False, "The checks have not run yet."
    elif verdict == "BLOCK":
        can, why = False, "The checks blocked this draft. Reject it, or have it edited and re-checked."
    elif verdict == "ESCALATE":
        can, why = False, "The checks cannot decide this one. Reject it, or have it edited and re-checked."
    note = ("Shadow mode: approving records your decision for the quality gate. Nothing is sent." if tier == 0
            else "Approving makes this message eligible to be sent.")
    return DetailView(proposal_id=str(d.get("proposal_id", "")), title=f"Draft for {d.get('marketplace', '?')} ({str(d.get('channel', '?')).lower()})",
                      subject=subject, body=body, source_label=source_label, offer_line=offer, verdict=verdict,
                      verdict_label=_VERDICT.get(verdict, "Unknown"), findings=findings, scores=scores, score_headline=headline,
                      lead_summary=summary, risk_flags=flags, tier=tier, tier_note=note, writer_confidence_pct=_pct(d.get("confidence")),
                      can_approve=can, approve_blocked_reason=why)
