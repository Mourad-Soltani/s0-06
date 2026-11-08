"""Real language-model clients behind the LlmClient protocol (ADR-28). The Copywriter, the cost ledger and the routing policy are unchanged: this
file is the seam's other side.

 * `OpenAiLlmClient` (chat completions) and `AnthropicLlmClient` (messages) each send one system message and one user message and return the
   text plus the token counts THE PROVIDER REPORTS. Those counts, not estimates, are what the Copywriter writes to the cost ledger (core.cost_ledger)
   at the configured price. An answer without usage numbers is refused: spend that cannot be recorded is not accepted.
 * Rate limits and transient failures are retried a bounded number of times, honouring Retry-After, with exponential back-off. A wait longer than
   MAX_WAIT_SECONDS is not held (a bus worker is not parked for a minute): the call fails at once. When the retries run out the client raises
   RateLimited / TransportError, and the Copywriter then writes the draft from the standard template (CopyResult.source == TEMPLATE, reason LLM_ERROR).
   Nothing is ever retried after a 4xx other than 429: the request itself is wrong and would be wrong again.
 * Credentials: 401/403 -> AuthExpired and no retry. No error text from the provider is read or kept (an error body can echo the request); every
   exception carries a fixed message. The key appears only in the request header and never in a repr.
 * What is sent: the Copywriter's prompt, which is product facts and the signal kind (orchestrator/copy_schema.py); never a name, an address or a
   lead id. No `user` or `store` field is sent.
Unverified against the live services (no network access when this was written): the request and response shapes below follow each provider's
documented chat API; the unit tests pin what this code sends and expects, not what the providers do. Model names and prices are NOT in the code:
they come from configuration (cutover C7). If a model rejects `max_completion_tokens` or `max_tokens`, the provider answers 400 and the draft falls
back to the template; the first live check (`worker_main check` followed by one real draft) will show it."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

from adapters.errors import AuthExpired, MalformedResponse, RateLimited, Rejected, TransportError
from orchestrator.llm import LlmResponse

MAX_WAIT_SECONDS = 20.0
RETRYABLE = frozenset({429, 500, 502, 503, 504, 529})          # 529 is Anthropic's "overloaded"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


def _default_open(req, timeout):
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None                                      # the key must never follow a redirect to another host
    return urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout)


def _retry_after(exc: urllib.error.HTTPError) -> Optional[float]:
    try:
        value = float(exc.headers.get("Retry-After"))
        return value if value >= 0 else None
    except (TypeError, ValueError, AttributeError):
        return None


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MalformedResponse("invalid token count")
    return value


class _Provider:
    """The shared HTTP part: one JSON POST with bounded retries."""
    name = ""

    def __init__(self, api_key: str, url: str, headers: Dict[str, str], *, opener: Optional[Callable] = None, timeout: float = 30.0,
                 max_retries: int = 2, sleep: Callable[[float], None] = time.sleep):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("an API key is required")
        if not isinstance(max_retries, int) or isinstance(max_retries, bool) or not 0 <= max_retries <= 5:
            raise ValueError("max_retries must be an integer from 0 to 5")
        self._url, self._headers, self._timeout = url, headers, timeout
        self._open, self._max_retries, self._sleep = opener or _default_open, max_retries, sleep

    def __repr__(self) -> str:                                # never the key
        return f"{type(self).__name__}(max_retries={self._max_retries})"

    def _post(self, body: dict) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        attempt = 0
        while True:
            req = urllib.request.Request(self._url, data=data, method="POST",
                                         headers={**self._headers, "Content-Type": "application/json", "Accept": "application/json"})
            try:
                with self._open(req, timeout=self._timeout) as resp:
                    raw = resp.read()
                break
            except urllib.error.HTTPError as exc:             # the body is deliberately not read
                code = exc.code
                if code in (401, 403):
                    raise AuthExpired() from None
                if code not in RETRYABLE:
                    raise Rejected(code) from None
                wait = _retry_after(exc) if code == 429 else None
                error: Exception = RateLimited(wait) if code == 429 else TransportError()
            except (urllib.error.URLError, TimeoutError, OSError):
                wait, error = None, TransportError()
            delay = wait if wait is not None else float(2 ** attempt)
            if attempt >= self._max_retries or delay > MAX_WAIT_SECONDS:
                raise error from None
            self._sleep(delay)
            attempt += 1
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise MalformedResponse("invalid json") from None
        if not isinstance(parsed, dict):
            raise MalformedResponse("unexpected response shape")
        return parsed


def _check_call(model: str, system: str, prompt: str, max_output_tokens: int) -> None:
    if not isinstance(model, str) or not model.strip() or not isinstance(system, str) or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("model and prompt are required")
    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or not 1 <= max_output_tokens <= 32000:
        raise ValueError("max_output_tokens must be an integer from 1 to 32000")


class OpenAiLlmClient(_Provider):
    name = "openai"

    def __init__(self, api_key: str, **kw):
        super().__init__(api_key, OPENAI_URL, {"Authorization": f"Bearer {api_key.strip() if isinstance(api_key, str) else ''}"}, **kw)

    def complete(self, *, model: str, system: str, prompt: str, max_output_tokens: int) -> LlmResponse:
        _check_call(model, system, prompt, max_output_tokens)
        data = self._post({"model": model, "max_completion_tokens": max_output_tokens,
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
        try:
            message = data["choices"][0]["message"]
            text, usage = message.get("content"), data["usage"]
            tokens_in, tokens_out = _count(usage["prompt_tokens"]), _count(usage["completion_tokens"])
        except (KeyError, IndexError, TypeError, AttributeError):
            raise MalformedResponse("missing field") from None
        if not isinstance(text, str) or not text.strip():
            raise MalformedResponse("missing field content")
        return LlmResponse(text=text, input_tokens=tokens_in, output_tokens=tokens_out)


class AnthropicLlmClient(_Provider):
    name = "anthropic"

    def __init__(self, api_key: str, **kw):
        super().__init__(api_key, ANTHROPIC_URL, {"x-api-key": api_key.strip() if isinstance(api_key, str) else "", "anthropic-version": ANTHROPIC_VERSION}, **kw)

    def complete(self, *, model: str, system: str, prompt: str, max_output_tokens: int) -> LlmResponse:
        _check_call(model, system, prompt, max_output_tokens)
        data = self._post({"model": model, "max_tokens": max_output_tokens, "system": system,
                           "messages": [{"role": "user", "content": prompt}]})
        try:
            blocks, usage = data["content"], data["usage"]
            tokens_in, tokens_out = _count(usage["input_tokens"]), _count(usage["output_tokens"])
            text = "".join(b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
        except (KeyError, TypeError):
            raise MalformedResponse("missing field") from None
        if not text.strip():
            raise MalformedResponse("missing field content")
        return LlmResponse(text=text, input_tokens=tokens_in, output_tokens=tokens_out)


PROVIDERS = {"openai": OpenAiLlmClient, "anthropic": AnthropicLlmClient}


def make_llm_client(provider: str, api_key: str, **kw):
    try:
        return PROVIDERS[provider](api_key, **kw)
    except KeyError:
        raise ValueError("unknown provider") from None
