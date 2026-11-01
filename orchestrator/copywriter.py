"""The Copywriter (S3-01, spec 3.1): turns a structured brief into one draft. It routes to the model the routing policy
names for the role, checks the monthly budget BEFORE a paid call, records the provider's reported usage in the cost
ledger as soon as a call returns (the money is spent whatever the text is worth), and accepts the output only if it
passes the strict schema. When there is no usable model output it writes the deterministic template instead and says
why in `fallback_reason`; with `fallback_to_template=False` it raises CopyError instead. Only the first variant is
used: A/B testing is deferred (ADR-20)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from orchestrator.copy_schema import FEW_SHOT, DraftBrief, SchemaViolation, build_prompt, parse_llm_output
from orchestrator.llm import LlmClient, LlmNotConfigured
from orchestrator.templates import TemplateWriter
from shared.cost import BudgetExhausted, CostTracker, NoLlmForRole, RoutingPolicy

ROLE = "COPYWRITER"


class CopyError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CopyResult:
    subject: str
    body: str
    confidence: float
    source: str                          # "LLM" or "TEMPLATE"
    model: Optional[str] = None
    fallback_reason: Optional[str] = None

    @property
    def message(self) -> str:
        return f"Subject: {self.subject}\n\n{self.body}"


@dataclass(frozen=True)
class CopywriterConfig:
    max_output_tokens: int = 600
    fallback_to_template: bool = True


def estimate_tokens(text: str) -> int:
    """A deliberately generous size estimate (three bytes per token) used ONLY to check the budget before a call."""
    return -(-len(text.encode("utf-8")) // 3)


class Copywriter:
    def __init__(self, policy: RoutingPolicy, tracker: CostTracker, llm: LlmClient, templates: Optional[TemplateWriter] = None,
                 config: Optional[CopywriterConfig] = None, few_shot=FEW_SHOT, new_call_id: Callable[[], uuid.UUID] = uuid.uuid4):
        self.policy, self.tracker, self.llm = policy, tracker, llm
        self.templates, self.config, self.few_shot, self._new_call_id = templates or TemplateWriter(), config or CopywriterConfig(), few_shot, new_call_id

    def draft(self, brief: DraftBrief, *, trace_id: Optional[uuid.UUID] = None) -> CopyResult:
        try:
            model = self.policy.model_for(ROLE)
        except NoLlmForRole:
            return self._template(brief, "NO_LLM_FOR_ROLE")
        system, prompt = build_prompt(brief, self.few_shot)
        estimate = self.tracker.pricebook.estimate_micro(model, estimate_tokens(system) + estimate_tokens(prompt),
                                                         self.config.max_output_tokens)
        try:
            self.tracker.require_headroom(estimate)
        except BudgetExhausted:
            return self._template(brief, "BUDGET_EXHAUSTED")
        try:
            resp = self.llm.complete(model=model, system=system, prompt=prompt, max_output_tokens=self.config.max_output_tokens)
        except LlmNotConfigured:
            return self._template(brief, "LLM_NOT_CONFIGURED")
        except Exception:  # noqa: BLE001 - any provider failure falls back; nothing is known to have been spent
            return self._template(brief, "LLM_ERROR")
        self.tracker.record_llm_call(self._new_call_id(), ROLE, model, resp.input_tokens, resp.output_tokens,
                                     lead_id=brief.lead_id, trace_id=trace_id or brief.lead_id)   # a ledger failure is loud
        try:
            variants, confidence = parse_llm_output(resp.text)
        except SchemaViolation as exc:
            return self._template(brief, f"SCHEMA_{exc.code}")
        return CopyResult(variants[0].subject, variants[0].body, confidence, "LLM", model)

    def _template(self, brief: DraftBrief, reason: str) -> CopyResult:
        if not self.config.fallback_to_template:
            raise CopyError(reason)
        t = self.templates.write(brief)
        return CopyResult(t.subject, t.body, t.confidence, "TEMPLATE", None, reason)
