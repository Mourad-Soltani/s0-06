"""Canonical, marketplace-agnostic types shared by the orchestrator (L1) and the adapters (L2).

Every model is frozen and rejects unknown fields, so a malformed payload is a hard failure at the boundary
(spec section 8), not something downstream code has to guess about.
PII: Message.body is personal data. It is excluded from repr, and it must never be logged, put on the event
bus or written to audit rows (checklist B1). Identifiers and codes below are validated to opaque alphabets.
"""
from __future__ import annotations

import enum
from decimal import Decimal
from typing import Annotated, Any, Literal, Mapping, Optional, Tuple

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

# Opaque alphabets. SignalRef matches the DB CHECK on core.lead.signal_ref; ReasonCode matches the DB code CHECKs.
SignalRef = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_:.-]{1,128}$")]
ReasonCode = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9_]{1,64}$")]
Context = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]      # e.g. buyer_initiated
PlanStep = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]     # e.g. browser_automation
ExternalId = Annotated[str, StringConstraints(min_length=1, max_length=256)]        # marketplace-assigned, opaque
OfferCode = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class Marketplace(str, enum.Enum):
    """Mirrors core.marketplace in the database (a test compares the two). EMAIL is an outbound channel."""
    GUMROAD = "GUMROAD"
    ETSY = "ETSY"
    SHOPIFY = "SHOPIFY"
    AMAZON = "AMAZON"
    FIVERR = "FIVERR"
    PAYHIP = "PAYHIP"
    LEMON_SQUEEZY = "LEMON_SQUEEZY"
    ACP = "ACP"
    UCP = "UCP"
    EMAIL = "EMAIL"


class ListingStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class ActionType(str, enum.Enum):
    SEND_MESSAGE = "SEND_MESSAGE"
    CREATE_OFFER = "CREATE_OFFER"
    UPDATE_LISTING = "UPDATE_LISTING"
    REPLY = "REPLY"


class Verdict(str, enum.Enum):
    PASS = "PASS"
    BLOCK = "BLOCK"
    ESCALATE = "ESCALATE"


class RateModel(str, enum.Enum):
    NONE = "NONE"                          # no documented limit
    TOKEN_BUCKET = "TOKEN_BUCKET"
    FIXED_WINDOW = "FIXED_WINDOW"
    COST_BASED = "COST_BASED"              # e.g. Shopify's leaky bucket measured in points


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Money(_Model):
    amount_minor: int = Field(ge=0)        # minor units (cents), never floats
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class Discount(_Model):
    """ADR-12: PERCENT uses percent only, FIXED uses fixed_minor only (minor units of the product currency)."""
    kind: Literal["PERCENT", "FIXED"]
    percent: Optional[Decimal] = Field(default=None, ge=0, le=100, decimal_places=2)
    fixed_minor: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _exactly_the_right_field(self) -> "Discount":
        if self.kind == "PERCENT" and (self.percent is None or self.fixed_minor is not None):
            raise ValueError("PERCENT discount needs percent and no fixed_minor")
        if self.kind == "FIXED" and (self.fixed_minor is None or self.percent is not None):
            raise ValueError("FIXED discount needs fixed_minor and no percent")
        return self


class ProductSnapshot(_Model):
    external_id: ExternalId
    title: str = Field(min_length=1, max_length=512)
    description: str = Field(max_length=20000)
    price: Money
    status: ListingStatus
    checkout_url: Optional[str] = None
    last_synced_at: AwareDatetime


class ListingInput(_Model):
    external_id: Optional[ExternalId] = None     # None = create
    title: str = Field(min_length=1, max_length=512)
    description: str = Field(max_length=20000)
    price: Money
    status: ListingStatus = ListingStatus.DRAFT


class ListingResult(_Model):
    external_id: ExternalId
    status: ListingStatus
    checkout_url: Optional[str] = None


class OfferResult(_Model):
    code: OfferCode
    checkout_url: str
    expires_at: Optional[AwareDatetime] = None


class Conversation(_Model):
    conversation_id: ExternalId
    context: Context                               # must be one of the adapter's allowed_message_contexts
    updated_at: AwareDatetime


class Message(_Model):
    message_id: ExternalId
    sender: Literal["BUYER", "SELLER"]
    body: str = Field(repr=False)                  # PII: kept out of repr
    sent_at: AwareDatetime


class DraftResult(_Model):
    draft_id: ExternalId


class SendResult(_Model):
    external_message_id: ExternalId
    deduplicated: bool = False                     # True when the idempotency key matched an earlier send


class Signal(_Model):
    signal_ref: SignalRef                          # opaque id, never a handle or email (survives erasure)
    kind: Literal["SALE", "REVIEW", "QA", "CART", "SUBSCRIBER", "OFFER_CODE_USE"]
    marketplace: Marketplace
    product_external_id: Optional[ExternalId] = None
    occurred_at: AwareDatetime


class ProposedAction(_Model):
    action_type: ActionType
    marketplace: Marketplace
    payload: Mapping[str, Any]
    execution_plan: Tuple[PlanStep, ...] = ()      # declared steps, so preflight can reject bypass plans


class ComplianceVerdict(_Model):
    verdict: Verdict
    reason_codes: Tuple[ReasonCode, ...] = ()

    @model_validator(mode="after")
    def _blocks_explain_themselves(self) -> "ComplianceVerdict":
        if self.verdict is not Verdict.PASS and not self.reason_codes:
            raise ValueError("BLOCK and ESCALATE verdicts need at least one reason code")
        return self


class AdapterHealth(_Model):
    ok: bool
    checked_at: AwareDatetime
    detail_code: Optional[ReasonCode] = None


class AdapterCapabilities(_Model):
    """Declared, never inferred: every flag is required. The orchestrator's autonomy tiers read these."""
    supports_outbound_messaging: bool
    supports_offer_codes: bool
    supports_checkout_url: bool
    supports_listing_mutation: bool
    supports_acp_checkout: bool
    max_message_length: Optional[int] = Field(default=None, ge=1)
    allowed_message_contexts: Tuple[Context, ...]
    rate_limit_model: RateModel
    requires_idempotency_key: bool

    @model_validator(mode="after")
    def _messaging_fields_agree_with_the_flag(self) -> "AdapterCapabilities":
        if self.supports_outbound_messaging:
            if not self.allowed_message_contexts:
                raise ValueError("an adapter that messages must declare allowed_message_contexts")
        elif self.allowed_message_contexts or self.max_message_length is not None:
            raise ValueError("message limits and contexts require supports_outbound_messaging")
        return self
