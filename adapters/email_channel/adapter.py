"""EmailChannelAdapter (S2-02). The only outbound channel in v1.1 (ADR-08), so it carries the strictest preflight:
consent reference for outreach, a valid message format, and a footer and unsubscribe header it adds itself.
It does NOT decide who may be contacted: the database send gate does (ADR-11). It has no catalog, no offers and
no checkout. No email service provider is chosen yet (cutover item C3), so it talks to an EmailTransport.
Idempotency: a repeated key never reaches the transport, and the key is passed to the transport for providers that
support one; a lost response after a provider accepted a send is the one case this cannot rule out."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from adapters import policy
from adapters.base import MarketplaceAdapter
from adapters.email_channel import message as msg
from adapters.errors import NotFound
from shared.models import (ActionType, AdapterCapabilities, AdapterHealth, ComplianceVerdict, Conversation, DraftResult,
                           Marketplace, Message, ProductSnapshot, ProposedAction, RateModel, SendResult, Signal)

_OPAQUE = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
OUTREACH = "consented_outreach"
MAX_BODY = 10_000


class EmailChannelAdapter(MarketplaceAdapter):
    marketplace = Marketplace.EMAIL
    capabilities = AdapterCapabilities(
        supports_outbound_messaging=True, supports_offer_codes=False, supports_checkout_url=False,
        supports_listing_mutation=False, supports_acp_checkout=False, max_message_length=MAX_BODY,
        allowed_message_contexts=(OUTREACH, "buyer_initiated", "order_related"),
        rate_limit_model=RateModel.TOKEN_BUCKET, requires_idempotency_key=True)

    def __init__(self, transport: msg.EmailTransport, config: msg.EmailConfig, *,
                 clock: Optional[Callable[[], datetime]] = None):
        self.transport, self.config = transport, config
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sent: Dict[uuid.UUID, SendResult] = {}
        self._draft_ids = 0

    # ------------------------------------------------------------------ always required
    def list_products(self) -> List[ProductSnapshot]:
        return []                                       # email has no catalog

    def get_product(self, external_id: str) -> ProductSnapshot:
        raise NotFound()

    def fetch_signals(self, since: datetime) -> List[Signal]:
        return []                                       # no signal kind fits email; replies arrive as conversations

    def preflight_check(self, action: ProposedAction) -> ComplianceVerdict:
        """Pure policy: no transport call, no side effect."""
        reasons = policy.base_reasons(action, self.marketplace, self.capabilities)
        if action.action_type in (ActionType.SEND_MESSAGE, ActionType.REPLY):
            if action.payload.get("context") == OUTREACH:
                ref = action.payload.get("consent_ref")
                if not (isinstance(ref, str) and _OPAQUE.match(ref)):
                    reasons.add("MISSING_CONSENT_REF")       # outreach must cite a ContactPermission id
            body = action.payload.get("body")
            if isinstance(body, str):
                if len(body) > MAX_BODY:
                    reasons.add("MESSAGE_TOO_LONG")
                try:
                    msg.split_message(body)
                except ValueError:
                    reasons.add("INVALID_MESSAGE_FORMAT")
        return policy.verdict_for(reasons)

    def health_check(self) -> AdapterHealth:
        return policy.health_from_ping(self.transport.ping, self._clock)

    # ------------------------------------------------------------------ declared capability: messaging
    def _list_conversations(self, since: datetime) -> List[Conversation]:
        return [msg.conversation(t) for t in self.transport.list_threads(since)]

    def _read_conversation(self, conversation_id: str) -> List[Message]:
        return [msg.message(m) for m in self.transport.read_thread(conversation_id)]

    def _draft_reply(self, conversation_id: str, body: str) -> DraftResult:
        msg.split_message(body)                          # a draft that could never be sent is refused now
        self._draft_ids += 1
        return DraftResult(draft_id=f"draft-{self._draft_ids}")   # held by the orchestrator; nothing leaves

    def _send_reply(self, conversation_id: str, body: str, idempotency_key: uuid.UUID) -> SendResult:
        email = msg.compose(body, self.config, conversation_id)  # raises ValueError before any side effect
        if idempotency_key in self._sent:
            return self._sent[idempotency_key].model_copy(update={"deduplicated": True})
        provider_id = self.transport.send(email, str(idempotency_key))
        result = SendResult(external_message_id=str(provider_id))
        self._sent[idempotency_key] = result
        return result
