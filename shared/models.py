from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

class Marketplace(Enum):
    GUMROAD = "gumroad"
    ETSY = "etsy"

class ActionType(Enum):
    SEND_MESSAGE = "send_message"
    REPLY = "reply"
    UPDATE_LISTING = "update_listing"
    CREATE_OFFER = "create_offer"

class ListingStatus(Enum):
    ACTIVE = "active"
    PAUSED = "paused"

class Verdict(Enum):
    PASS = "pass"
    BLOCK = "block"

@dataclass(frozen=True)
class Money:
    amount_minor: int
    currency: str

@dataclass(frozen=True)
class Discount:
    kind: str
    percent: Optional[Decimal] = None
    fixed_minor: Optional[int] = None

@dataclass(frozen=True)
class ProposedAction:
    action_type: ActionType
    marketplace: Marketplace
    payload: dict = field(default_factory=dict)
    execution_plan: tuple = ()

@dataclass(frozen=True)
class Signal:
    kind: str
    marketplace: Marketplace
    product_external_id: Optional[str]
    signal_ref: str
    occurred_at: datetime

@dataclass(frozen=True)
class ProductSnapshot:
    external_id: str
    title: str
    price: Money
    status: ListingStatus
    checkout_url: Optional[str]
    last_synced_at: datetime
    description: str = ""

@dataclass(frozen=True)
class Verification:
    verdict: Verdict
    reason_codes: tuple = ()

@dataclass(frozen=True)
class Health:
    ok: bool
    detail_code: Optional[str]
    checked_at: datetime

@dataclass(frozen=True)
class Capabilities:
    supports_offer_codes: bool = False
    supports_checkout_url: bool = False
    supports_outbound_messaging: bool = False
    supports_listing_mutation: bool = False
    supports_acp_checkout: bool = False

@dataclass(frozen=True)
class OfferResult:
    code: str
    checkout_url: str
