"""Deterministic template baseline (S3-01): what the Copywriter writes when no model is configured, the monthly budget is
spent, the provider fails, or the model's output breaks the schema. Same brief, same words. No placeholders, no
invented facts. The adapter adds the postal address and the unsubscribe link, so the body does not."""
from __future__ import annotations

from dataclasses import dataclass

from orchestrator.copy_schema import SUBJECT_MAX, DraftBrief, clean_text

TEMPLATE_CONFIDENCE = 0.6


@dataclass(frozen=True)
class TemplateDraft:
    subject: str
    body: str
    confidence: float = TEMPLATE_CONFIDENCE


class TemplateWriter:
    def write(self, brief: DraftBrief) -> TemplateDraft:
        title = clean_text(brief.product.title, 120)
        subject = f"About {title}"
        if len(subject) > SUBJECT_MAX:
            subject = subject[:SUBJECT_MAX - 3].rstrip() + "..."
        offer = ""
        if brief.discount_pct is not None:
            offer = f"Use code {brief.offer_code} for {format(brief.discount_pct.normalize(), 'f')}% off.\n\n"
        if brief.context == "consented_outreach":
            body = (f"Hi,\n\nI thought you might be interested in {title}. You can find it here: {brief.product.checkout_url}\n\n"
                    f"{offer}If you have any questions, just reply to this email.\n\nBest regards")
        else:
            body = (f"Hi,\n\nThanks for getting in touch about {title}. Here is the link again: {brief.product.checkout_url}\n\n"
                    f"{offer}Let me know if there is anything else I can help with.\n\nBest regards")
        return TemplateDraft(subject, body)
