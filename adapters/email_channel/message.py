"""Message format, footer and transport types for the email channel.

Format (the adapter's send_reply/draft_reply body): a first line `Subject: <text>`, a blank line, then the text.
The adapter appends the CAN-SPAM / PECR footer itself (postal address and an unsubscribe link) and sets the
List-Unsubscribe header, so no message can leave without them."""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Protocol, Tuple

from adapters.errors import MalformedResponse
from shared.models import Conversation, Message

SUBJECT_MAX = 200
FORMAT_HELP = "message must be 'Subject: <text>', a blank line, then the text"


@dataclass(frozen=True)
class EmailConfig:
    postal_address: str            # CAN-SPAM requires a valid physical postal address in every commercial email
    unsubscribe_base_url: str      # an https endpoint that honours ?thread=<id>; the one-click form (RFC 8058) is an ESP/endpoint item

    def __post_init__(self):
        if len(self.postal_address.strip()) < 10 or "\n" in self.postal_address or "\r" in self.postal_address:
            raise ValueError("postal_address must be a single line with a real address")
        if not self.unsubscribe_base_url.startswith("https://") or " " in self.unsubscribe_base_url:
            raise ValueError("unsubscribe_base_url must be an https URL")


@dataclass(frozen=True)
class OutboundEmail:
    thread_id: str                 # opaque contact-thread id; the transport resolves it to an address
    subject: str
    text: str                      # body plus footer
    headers: Mapping[str, str] = field(default_factory=dict)


class EmailTransport(Protocol):
    def ping(self) -> None: ...
    def list_threads(self, since: datetime) -> List[Dict[str, Any]]: ...
    def read_thread(self, thread_id: str) -> List[Dict[str, Any]]: ...
    def send(self, email: OutboundEmail, idempotency_key: str) -> str: ...      # returns the provider's message id


def split_message(body: str) -> Tuple[str, str]:
    head, sep, rest = body.partition("\n")
    if not sep or not head.startswith("Subject:"):
        raise ValueError(FORMAT_HELP)
    subject = head[len("Subject:"):].strip()
    blank, sep2, text = rest.partition("\n")
    if not subject or len(subject) > SUBJECT_MAX or any(ord(c) < 32 for c in subject) or blank != "" or not sep2 \
            or not text.strip():
        raise ValueError(FORMAT_HELP)
    return subject, text


def unsubscribe_url(config: EmailConfig, thread_id: str) -> str:
    base = config.unsubscribe_base_url
    return f"{base}{'&' if '?' in base else '?'}thread={urllib.parse.quote(thread_id, safe='')}"


def compose(body: str, config: EmailConfig, thread_id: str) -> OutboundEmail:
    subject, text = split_message(body)
    url = unsubscribe_url(config, thread_id)
    footer = f"\n\n--\n{config.postal_address.strip()}\nTo stop receiving these emails: {url}\n"
    return OutboundEmail(thread_id=thread_id, subject=subject, text=text.rstrip() + footer,
                         headers={"List-Unsubscribe": f"<{url}>"})


def _time(value: Any) -> datetime:
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise MalformedResponse("invalid timestamp") from None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def conversation(raw: Mapping[str, Any]) -> Conversation:
    try:
        return Conversation(conversation_id=raw["thread_id"], context=raw["context"], updated_at=_time(raw["updated_at"]))
    except KeyError as exc:
        raise MalformedResponse(f"missing field {exc.args[0]}") from None


def message(raw: Mapping[str, Any]) -> Message:
    try:
        if raw["sender"] not in ("BUYER", "SELLER"):
            raise MalformedResponse("invalid field sender")
        return Message(message_id=raw["message_id"], sender=raw["sender"], body=raw["body"], sent_at=_time(raw["sent_at"]))
    except KeyError as exc:
        raise MalformedResponse(f"missing field {exc.args[0]}") from None
