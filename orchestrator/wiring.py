"""The whole pipeline on one bus (BL-03). Registering these four consumers is what turns the separate parts into a product:

    signal.captured -> qualifier -> lead.scored, lead.qualified
    lead.qualified  -> drafter   -> action.proposed          (the Copywriter writes the draft)
    action.proposed -> compliance_guard -> compliance.passed (a verdict; the draft now waits in the review queue)
    action.approved -> closer    -> action.sent              (published by the review service when a reviewer approves)

signal.captured comes from the Scout or the consented-lead intake; action.approved comes from an approval. PIPELINE records the same
chain as data, and a test keeps it honest."""
from __future__ import annotations

from typing import Tuple

# (consumes, consumer, publishes)
PIPELINE: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("signal.captured", "qualifier", ("lead.scored", "lead.qualified")),
    ("lead.qualified", "drafter", ("action.proposed",)),
    ("action.proposed", "compliance_guard", ("compliance.passed",)),
    ("action.approved", "closer", ("action.sent",)),
)
EXTERNAL_SOURCES = frozenset({"signal.captured", "action.approved"})        # events that start a stage from outside the bus chain


def register_pipeline(worker, *, qualifier, drafter, guard_consumer, closer) -> None:
    for consumer in (qualifier, drafter, guard_consumer, closer):
        consumer.register(worker)
