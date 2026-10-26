"""Rules every real adapter shares in preflight_check, and a health helper. TOS rules specific to one marketplace
stay in that marketplace's adapter."""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Set, Tuple

from adapters.errors import AdapterError, AuthExpired, RateLimited
from shared.models import (ActionType, AdapterCapabilities, AdapterHealth, ComplianceVerdict, Marketplace,
                           ProposedAction, Verdict)

# Spec 6.1 "General": no CAPTCHA, device-verification or account-linking bypass, and no browser automation.
BYPASS_STEPS = frozenset({"browser_automation", "captcha_bypass", "device_verification_bypass",
                          "account_linking_bypass"})


def base_reasons(action: ProposedAction, marketplace: Marketplace, caps: AdapterCapabilities) -> Set[str]:
    reasons: Set[str] = set()
    if action.marketplace != marketplace:
        reasons.add("WRONG_MARKETPLACE")
    reasons.update(step.upper() for step in action.execution_plan if step in BYPASS_STEPS)
    if action.action_type in (ActionType.SEND_MESSAGE, ActionType.REPLY):
        if not caps.supports_outbound_messaging:
            reasons.add("MESSAGING_UNSUPPORTED")
        elif action.payload.get("context") not in caps.allowed_message_contexts:
            reasons.add("CONTEXT_NOT_ALLOWED")
    if action.action_type is ActionType.UPDATE_LISTING and not caps.supports_listing_mutation:
        reasons.add("LISTING_MUTATION_UNSUPPORTED")
    if action.action_type is ActionType.CREATE_OFFER and not caps.supports_offer_codes:
        reasons.add("OFFER_CODES_UNSUPPORTED")
    return reasons


def verdict_for(reasons: Set[str]) -> ComplianceVerdict:
    if reasons:
        return ComplianceVerdict(verdict=Verdict.BLOCK, reason_codes=tuple(sorted(reasons)))
    return ComplianceVerdict(verdict=Verdict.PASS)


def health_from_ping(ping: Callable[[], None], clock: Callable[[], datetime]) -> AdapterHealth:
    """ok=True when the cheapest authenticated call works; otherwise a code that says why (never a message)."""
    try:
        ping()
    except AuthExpired:
        return AdapterHealth(ok=False, checked_at=clock(), detail_code="AUTH_EXPIRED")
    except RateLimited:
        return AdapterHealth(ok=False, checked_at=clock(), detail_code="RATE_LIMITED")
    except AdapterError:
        return AdapterHealth(ok=False, checked_at=clock(), detail_code="DEGRADED")
    return AdapterHealth(ok=True, checked_at=clock())
