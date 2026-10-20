"""The MarketplaceAdapter contract (spec 4.1, 4.2). Marketplace-specific business logic never goes here or
in a subclass: adapters declare capabilities, map schemas, enforce their marketplace's TOS, and nothing else.

Method names are the spec's camelCase mapped to snake_case (listProducts -> list_products, ...).

Two guarantees beyond the spec's method list:
  1. Capability gating. Optional operations are public methods here that check the DECLARED capability and
     raise UnsupportedCapability before any hook (and so any side effect) runs. Adapters implement the
     underscore hooks only for capabilities they declare.
  2. Declared, not inferred. A concrete adapter whose declared capabilities and implemented hooks disagree
     fails at class-definition time, so the mismatch cannot reach a running system.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # annotations only: keeps this module importable without pydantic
    from shared.models import (AdapterCapabilities, AdapterHealth, ComplianceVerdict, Conversation, Discount,
                               DraftResult, ListingInput, ListingResult, ListingStatus, Marketplace, Message,
                               Money, OfferResult, ProductSnapshot, ProposedAction, SendResult, Signal)


class UnsupportedCapability(Exception):
    """Raised before any side effect when a call needs a capability the adapter did not declare."""


# declared capability -> the hooks an adapter must implement (and may only implement) when it is True
_GATES: Dict[str, Tuple[str, ...]] = {
    "supports_listing_mutation": ("_create_or_update_listing", "_update_price", "_update_status"),
    "supports_offer_codes": ("_create_offer_code",),
    "supports_checkout_url": ("_build_checkout_url",),
    "supports_outbound_messaging": ("_list_conversations", "_read_conversation", "_draft_reply", "_send_reply"),
}


class MarketplaceAdapter(ABC):
    marketplace: ClassVar["Marketplace"]
    capabilities: ClassVar["AdapterCapabilities"]

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "capabilities" not in cls.__dict__:      # abstract intermediates declare nothing yet
            return
        if "marketplace" not in cls.__dict__:
            raise TypeError(f"{cls.__name__} declares capabilities but not marketplace")
        for flag, hooks in _GATES.items():
            enabled = getattr(cls.capabilities, flag)
            implemented = [h for h in hooks if getattr(cls, h) is not getattr(MarketplaceAdapter, h)]
            if enabled and len(implemented) != len(hooks):
                missing = [h for h in hooks if h not in implemented]
                raise TypeError(f"{cls.__name__} declares {flag}=True but does not implement {missing}")
            if not enabled and implemented:
                raise TypeError(f"{cls.__name__} implements {implemented} but declares {flag}=False")

    # ------------------------------------------------------------------ always required
    @abstractmethod
    def list_products(self) -> List["ProductSnapshot"]: ...

    @abstractmethod
    def get_product(self, external_id: str) -> "ProductSnapshot": ...

    @abstractmethod
    def fetch_signals(self, since: datetime) -> List["Signal"]: ...

    @abstractmethod
    def preflight_check(self, action: "ProposedAction") -> "ComplianceVerdict":
        """The marketplace's TOS enforcement point. Must not perform the action or any side effect."""

    @abstractmethod
    def health_check(self) -> "AdapterHealth": ...

    # ------------------------------------------------------------------ capability-gated
    def create_or_update_listing(self, listing: "ListingInput") -> "ListingResult":
        self._require("supports_listing_mutation")
        return self._create_or_update_listing(listing)

    def update_price(self, external_id: str, price: "Money") -> None:
        self._require("supports_listing_mutation")
        self._update_price(external_id, price)

    def update_status(self, external_id: str, status: "ListingStatus") -> None:
        self._require("supports_listing_mutation")
        self._update_status(external_id, status)

    def create_offer_code(self, product_external_id: str, code: str, discount: "Discount",
                          expires_at: Optional[datetime]) -> "OfferResult":
        self._require("supports_offer_codes")
        return self._create_offer_code(product_external_id, code, discount, expires_at)

    def build_checkout_url(self, product_external_id: str, params: Dict[str, str]) -> str:
        self._require("supports_checkout_url")
        return self._build_checkout_url(product_external_id, params)

    def list_conversations(self, since: datetime) -> List["Conversation"]:
        self._require("supports_outbound_messaging")
        return self._list_conversations(since)

    def read_conversation(self, conversation_id: str) -> List["Message"]:
        self._require("supports_outbound_messaging")
        return self._read_conversation(conversation_id)

    def draft_reply(self, conversation_id: str, body: str) -> "DraftResult":
        """Saves a draft. Never sends."""
        self._require("supports_outbound_messaging")
        self._check_length(body)
        return self._draft_reply(conversation_id, body)

    def send_reply(self, conversation_id: str, body: str, idempotency_key: uuid.UUID) -> "SendResult":
        """Same key twice must produce one external message (contract test)."""
        self._require("supports_outbound_messaging")
        self._check_length(body)
        return self._send_reply(conversation_id, body, idempotency_key)

    # ------------------------------------------------------------------ hooks (adapters override per capability)
    def _create_or_update_listing(self, listing): raise NotImplementedError
    def _update_price(self, external_id, price): raise NotImplementedError
    def _update_status(self, external_id, status): raise NotImplementedError
    def _create_offer_code(self, product_external_id, code, discount, expires_at): raise NotImplementedError
    def _build_checkout_url(self, product_external_id, params): raise NotImplementedError
    def _list_conversations(self, since): raise NotImplementedError
    def _read_conversation(self, conversation_id): raise NotImplementedError
    def _draft_reply(self, conversation_id, body): raise NotImplementedError
    def _send_reply(self, conversation_id, body, idempotency_key): raise NotImplementedError

    # ------------------------------------------------------------------ guards
    def _require(self, flag: str) -> None:
        if not getattr(self.capabilities, flag):
            raise UnsupportedCapability(f"{self.marketplace.value} does not declare {flag}")

    def _check_length(self, body: str) -> None:
        limit = self.capabilities.max_message_length
        if limit is not None and len(body) > limit:
            raise ValueError(f"message exceeds {self.marketplace.value} limit of {limit} characters")
