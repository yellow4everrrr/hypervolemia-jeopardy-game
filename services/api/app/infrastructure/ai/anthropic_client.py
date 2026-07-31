"""Anthropic API adapter.

The only module in the codebase that talks to a language model. Everything above it —
the evidence bundle, the contract, the validator — is pure and testable without a
network, which is what makes the fabrication tests in ``tests/unit/ai/`` possible.

Three choices worth stating:

**Structured output, not parsed prose.** The response shape is enforced with
``output_config.format``, so a claim always arrives with its citations attached rather
than being teased out of a paragraph afterwards.

**The system prompt is cached.** It is long, constant, and sent on every request; the
evidence goes in the user turn *after* it. That ordering is what makes the cache hit —
a per-trader system prompt would never be reused.

**A refusal is a result, not an exception.** Safety classifiers can decline a request
and return HTTP 200 with ``stop_reason == "refusal"``. Reading ``content[0]``
unconditionally would raise an IndexError and read as an outage.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import anthropic

from app.ai.prompts import OUTPUT_SCHEMA, PROMPT_VERSION, SYSTEM_PROMPT, build_user_message
from app.core.config import Settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: The coach reasons over a large evidence payload and its output is read closely. The
#: default is the most capable model; the setting exists so a deployment can trade
#: quality for cost without a code change.
DEFAULT_MODEL = "claude-opus-5"

#: Effort governs how much the model thinks before answering. Interpreting a
#: significance-controlled evidence set is exactly the kind of work that benefits, and
#: this is not a latency-critical path — a coaching report is requested, not polled.
DEFAULT_EFFORT = "high"

#: Room for the analysis plus the thinking that precedes it. On Claude Opus 5 thinking
#: is on by default and `max_tokens` caps thinking *and* response together, so a budget
#: sized only for the answer would truncate mid-sentence.
DEFAULT_MAX_TOKENS = 16_000

#: Per-million-token rates used to record what an analysis cost. Stored alongside the
#: row rather than recomputed later, because the rate for a model changes over time and
#: a historical cost recomputed at today's rate is not what was actually spent.
PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5": (Decimal("3"), Decimal("15")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
}


@dataclass
class CoachResponse:
    """What came back, with the accounting needed to audit it."""

    output: dict[str, Any] = field(default_factory=dict)
    model: str = DEFAULT_MODEL
    prompt_version: str = PROMPT_VERSION
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: int = 0
    stop_reason: str | None = None
    refused: bool = False
    refusal_category: str | None = None
    error: str | None = None

    @property
    def cost_usd(self) -> Decimal | None:
        rates = PRICING.get(self.model)
        if rates is None:
            return None
        input_rate, output_rate = rates
        million = Decimal(1_000_000)
        return (
            Decimal(self.input_tokens) * input_rate / million
            + Decimal(self.output_tokens) * output_rate / million
        ).quantize(Decimal("0.000001"))

    @property
    def succeeded(self) -> bool:
        return not self.refused and self.error is None and bool(self.output)


class AnthropicCoach:
    """Sends an evidence bundle to the model and returns structured claims."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: anthropic.AsyncAnthropic | None = None,
        model: str | None = None,
    ) -> None:
        self._model = model or getattr(settings, "ai_model", None) or DEFAULT_MODEL
        api_key = getattr(settings, "anthropic_api_key", None)
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key.get_secret_value() if api_key else None
        )

    async def analyse(
        self,
        bundle_payload: dict[str, Any],
        *,
        question: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str = DEFAULT_EFFORT,
    ) -> CoachResponse:
        """One analysis. Never raises for a model-side outcome — it reports one."""
        started = time.perf_counter()

        # Assembled as a plain mapping: `output_config.format.schema` is a JSON Schema
        # document, which is dynamic by nature and does not fit the SDK's TypedDicts.
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            # Thinking is on by default on Opus 5; stated explicitly so the intent
            # survives a model change to one where it is not.
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": effort,
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
            "system": [
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # The evidence follows in the user turn, so this prefix is
                    # byte-identical on every request and reads from cache.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": build_user_message(bundle_payload, question),
                }
            ],
        }

        try:
            message = await self._client.messages.create(**request)
        except anthropic.APIStatusError as exc:
            logger.warning("coach.api_error", status=exc.status_code, message=exc.message)
            raise ExternalServiceError(
                f"the coaching model returned {exc.status_code}",
                details={"provider": "anthropic"},
            ) from exc
        except anthropic.APIConnectionError as exc:
            logger.warning("coach.connection_error", error=str(exc))
            raise ExternalServiceError(
                "could not reach the coaching model", details={"provider": "anthropic"}
            ) from exc

        elapsed = int((time.perf_counter() - started) * 1000)
        response = CoachResponse(
            model=message.model or self._model,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cache_read_tokens=getattr(message.usage, "cache_read_input_tokens", 0) or 0,
            latency_ms=elapsed,
            stop_reason=message.stop_reason,
        )

        # Checked before touching `content`: on a refusal the list is empty, and
        # indexing it would surface a safety outcome as an IndexError.
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            response.refused = True
            response.refusal_category = getattr(details, "category", None)
            logger.warning("coach.refused", category=response.refusal_category)
            return response

        text = next(
            (block.text for block in message.content if block.type == "text"), None
        )
        if text is None:
            response.error = "model returned no text content"
            return response

        try:
            response.output = json.loads(text)
        except json.JSONDecodeError as exc:
            # `output_config.format` makes this near-impossible, but a truncated
            # response (max_tokens) can still cut the JSON mid-object.
            response.error = f"could not parse structured output: {exc}"
            logger.warning(
                "coach.unparseable", stop_reason=message.stop_reason, length=len(text)
            )

        logger.info(
            "coach.analysed",
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cached=response.cache_read_tokens,
            latency_ms=elapsed,
        )
        return response
