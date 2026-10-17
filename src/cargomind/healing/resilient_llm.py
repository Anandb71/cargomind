"""ResilientLlm: a self-healing model chain for ADK agents.

Each agent gets a ``ResilientLlm`` whose ``chain`` is tried in order, e.g.
``OpenAI Responses tier -> OpenAI backup -> OfflineLlm``. An explicit
``CARGOMIND_PROVIDER=multi`` retains the legacy vendor chain. Per candidate:

* skipped while its circuit breaker is open (the last link is never skipped);
* chaos may inject a transient fault (never into the offline engine);
* transient errors are retried in place with short backoff;
* any other failure (missing key, auth, 4xx, ...) falls through to the next
  link, with the request rolled back to its pristine state first (the same
  snapshot/restore approach as ADK's ``FallbackModel``);
* once a candidate yields its first response the turn belongs to it.

Every retry/fallback/recovery is journaled, and each response is stamped with
``custom_metadata = {"served_by": <model>, "fallbacks": <n>, "tier": <tier>}``.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, NamedTuple

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

from cargomind import config
from cargomind.healing import chaos, journal
from cargomind.healing.breaker import get_breaker

if TYPE_CHECKING:
    from google.adk.models._capabilities import LlmCapabilities

logger = logging.getLogger(__name__)

TIERS = ("fast", "pro", "vision", "critic")

# Private LlmRequest attributes a model may edit (mirrors ADK's FallbackModel).
_SNAPSHOT_PRIVATE = (
    "_dynamic_instructions",
    "_is_managed_agent",
    "_has_static_instruction",
    "_static_instruction_prefix_end_index",
)
_MISSING = object()


class _RequestSnapshot(NamedTuple):
    """The parts of an LlmRequest a model edits in place, kept for rollback.

    ``tools_dict`` is deliberately excluded: it holds live tool objects (MCP
    tools reach a threading.Lock that deepcopy refuses) and models never edit it.
    """

    contents: list[types.Content]
    config: types.GenerateContentConfig
    live_connect_config: types.LiveConnectConfig
    private: dict[str, Any]

    @classmethod
    def of(cls, req: LlmRequest) -> _RequestSnapshot:
        private: dict[str, Any] = {}
        for name in _SNAPSHOT_PRIVATE:
            value = getattr(req, name, _MISSING)
            if value is not _MISSING:
                private[name] = copy.deepcopy(value)
        return cls(
            contents=[c.model_copy(deep=True) for c in req.contents],
            config=req.config.model_copy(deep=True),
            live_connect_config=(
                req.live_connect_config.model_copy(deep=True)
                if req.live_connect_config
                else types.LiveConnectConfig()
            ),
            private=private,
        )

    def restore(self, req: LlmRequest) -> None:
        """Rolls `req` back in place (the caller holds the same object).

        Copies again so the snapshot stays pristine for further restores.
        """
        req.contents = [c.model_copy(deep=True) for c in self.contents]
        req.config = self.config.model_copy(deep=True)
        req.live_connect_config = self.live_connect_config.model_copy(deep=True)
        for name, value in self.private.items():
            setattr(req, name, copy.deepcopy(value))


def _is_offline(llm: BaseLlm) -> bool:
    return type(llm).__name__ == "OfflineLlm" or str(llm.model).startswith("offline")


def _short(error: BaseException) -> str:
    text = str(error).strip().replace("\n", " ")
    return f"{type(error).__name__}: {text}"[:200] if text else type(error).__name__


class ResilientLlm(BaseLlm):
    """Tries each model in ``chain`` until one answers; never gives up silently."""

    model: str = "resilient"
    chain: list[BaseLlm] = Field(min_length=1)
    tier: str = "fast"
    retry_backoff: tuple[float, ...] = (0.4, 1.0)
    """Sleep before each in-place retry of a transient error (len = max retries)."""

    @property
    def capabilities(self) -> LlmCapabilities:
        """The primary model's capabilities (the request is built for it)."""
        return self.chain[0].capabilities

    def describe(self) -> list[dict[str, Any]]:
        """Chain + breaker state, for the dashboard."""
        out = []
        for llm in self.chain:
            b = get_breaker(f"model:{llm.model}")
            out.append({"model": llm.model, "kind": type(llm).__name__, "breaker": b.state})
        return out

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        last = len(self.chain) - 1
        fallbacks = 0
        last_error: BaseException | None = None
        recovered_from: list[str] = []

        for index, candidate in enumerate(self.chain):
            component = f"model:{candidate.model}"
            breaker = get_breaker(component)
            if not breaker.allow() and index < last:
                logger.info("ResilientLlm[%s]: skipping %s (breaker open)", self.tier, component)
                fallbacks += 1
                recovered_from.append(f"{candidate.model} (breaker open)")
                continue

            pristine = _RequestSnapshot.of(llm_request)
            offline = _is_offline(candidate)
            max_attempts = 1 + len(self.retry_backoff)
            response_yielded = False
            error: BaseException | None = None

            for attempt in range(max_attempts):
                if attempt:
                    pristine.restore(llm_request)
                llm_request.model = candidate.model
                try:
                    if not offline:
                        chaos.maybe_fail(component)
                    async with aclosing(
                        candidate.generate_content_async(llm_request, stream)
                    ) as agen:
                        async for response in agen:
                            if not response_yielded:
                                response_yielded = True
                                breaker.success()
                                if fallbacks or attempt or recovered_from:
                                    journal.record(
                                        "recovered",
                                        f"llm:{self.tier}",
                                        detail="; ".join(recovered_from) or f"{candidate.model} retried",
                                        action=f"served by {candidate.model}"
                                        + (f" after {attempt} retr{'y' if attempt == 1 else 'ies'}" if attempt else ""),
                                        outcome="recovered",
                                    )
                            meta = dict(response.custom_metadata or {})
                            meta.setdefault("served_by", candidate.model)
                            meta.update(
                                fallbacks=fallbacks,
                                tier=self.tier,
                            )
                            response.custom_metadata = meta
                            yield response
                    if not response_yielded:
                        # A model that ends its turn silently still answered.
                        breaker.success()
                    return
                except Exception as exc:
                    if response_yielded:
                        # Part of this model's turn already reached the caller.
                        raise
                    error = exc
                    if _is_permanent(exc):
                        # quota exhausted / bad key: retrying wastes seconds - trip the breaker now
                        for _ in range(int(getattr(config, "BREAKER_THRESHOLD", 3))):
                            breaker.failure()
                        break
                    if attempt + 1 < max_attempts and chaos.is_transient(exc):
                        delay = self.retry_backoff[attempt]
                        journal.record(
                            "retry",
                            component,
                            detail=_short(exc),
                            action=f"retry {attempt + 1}/{max_attempts - 1} in {delay:g}s",
                            outcome="retrying",
                        )
                        recovered_from.append(f"{candidate.model}: {_short(exc)[:80]}")
                        if delay > 0:
                            await asyncio.sleep(delay)
                        continue
                    break

            # Candidate exhausted (non-transient error or retries used up).
            assert error is not None
            last_error = error
            breaker.failure()
            pristine.restore(llm_request)
            nxt = self._next_name(index)
            journal.record(
                "fallback",
                component,
                detail=_short(error),
                action=f"→ {nxt}" if nxt else "→ (none: chain exhausted)",
                outcome="failed" if nxt is None else "falling_back",
            )
            logger.warning(
                "ResilientLlm[%s]: %s failed (%s); falling back to %s",
                self.tier,
                candidate.model,
                _short(error),
                nxt,
            )
            fallbacks += 1
            recovered_from.append(f"{candidate.model}: {_short(error)[:80]}")

        if last_error is None:  # pragma: no cover - only if every link was skipped
            last_error = RuntimeError(f"ResilientLlm[{self.tier}]: no model available")
        raise last_error

    def _next_name(self, index: int) -> str | None:
        """Name of the next candidate that will actually be tried after `index`."""
        last = len(self.chain) - 1
        for j in range(index + 1, len(self.chain)):
            llm = self.chain[j]
            if j == last or get_breaker(f"model:{llm.model}").allow():
                return llm.model
        return None


# ---------------------------------------------------------------- factory
_TIER_GEMINI = {
    "fast": lambda: config.GEMINI_FAST,
    "pro": lambda: config.GEMINI_PRO,
    "critic": lambda: config.GEMINI_PRO,
    "vision": lambda: config.GEMINI_VISION,
}


_PERMANENT_MARKERS = (
    "resource_exhausted", "quota", "api key", "api_key", "not scoped", "permission_denied",
    "permission denied", "invalid x-api-key", "authentication", "unauthorized", "billing",
    "model not found", "not_found", "does not exist",
)


def _is_permanent(exc: BaseException) -> bool:
    """Errors a retry cannot fix (quota exhausted, bad/unscoped key, unknown model)."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in _PERMANENT_MARKERS)


def _make_gemini(model_id: str) -> BaseLlm:
    from google.adk.models.google_llm import Gemini

    kwargs: dict[str, Any] = {"model": model_id}
    if "retry_options" in Gemini.model_fields:
        kwargs["retry_options"] = types.HttpRetryOptions(attempts=1)  # ResilientLlm owns retries
    return Gemini(**kwargs)


def _make_second_vendors() -> list[BaseLlm]:
    """Every configured non-Google vendor (Claude, then GPT) via LiteLLM."""
    if config.FORCE_OFFLINE:
        return []
    ids = []
    if config.ANTHROPIC_API_KEY:
        ids.append(config.CLAUDE_MODEL)
    if config.OPENAI_API_KEY:
        ids.append(config.OPENAI_MODEL)
    out: list[BaseLlm] = []
    for model_id in ids:
        try:
            from google.adk.models.lite_llm import LiteLlm

            out.append(LiteLlm(model=model_id))
        except Exception:
            logger.warning("Could not build LiteLlm(%s); skipping", model_id, exc_info=True)
    return out


def _make_second_vendor() -> BaseLlm | None:
    """Back-compat: first configured second vendor."""
    vendors = _make_second_vendors()
    return vendors[0] if vendors else None


def _make_offline(policy: Any) -> BaseLlm:
    from cargomind.healing.offline_llm import OfflineLlm  # integrator-owned

    return OfflineLlm(policy=policy) if policy is not None else OfflineLlm()


def build_model(tier: str = "fast", policy: Any = None) -> ResilientLlm:
    """Builds the resilient model chain for an agent tier.

    Args:
      tier: ``fast`` | ``pro`` | ``vision`` | ``critic``.
      policy: ``OfflinePolicy`` for the final rule-engine link.

    Returns:
      OpenAI tier + OpenAI backup + ``OfflineLlm`` by default. Legacy ``multi``
      mode uses Gemini plus cross-provider fallback, with the critic vendor
      first. The deterministic offline link is always last.
    """
    if tier not in _TIER_GEMINI:
        raise ValueError(f"unknown tier {tier!r}; expected one of {TIERS}")
    chain: list[BaseLlm] = []
    if config.PROVIDER == "openai":
        if config.OPENAI_API_KEY and not config.FORCE_OFFLINE:
            from cargomind.healing.openai_responses import OpenAIResponsesLlm

            primary = {"fast": config.OPENAI_FAST, "pro": config.OPENAI_PRO,
                       "critic": config.OPENAI_CRITIC, "vision": config.OPENAI_VISION}[tier].removeprefix("openai/")
            for model_id in dict.fromkeys((primary, config.OPENAI_FALLBACK.removeprefix("openai/"))):
                chain.append(OpenAIResponsesLlm(
                    model="openai/" + model_id,
                    reasoning_effort="none" if model_id == "gpt-6-luna" else "low",
                    timeout_seconds=config.OPENAI_TIMEOUT_S,
                    max_output_tokens=config.OPENAI_MAX_OUTPUT_TOKENS,
                ))
        chain.append(_make_offline(policy))
        return ResilientLlm(chain=chain, tier=tier)
    if config.online_gemini():
        primary = _TIER_GEMINI[tier]()
        chain.append(_make_gemini(primary))
        backup = getattr(config, "GEMINI_BACKUP", None)
        if backup and backup != primary:
            chain.append(_make_gemini(backup))
    seconds = _make_second_vendors()
    if tier == "critic":
        chain[0:0] = seconds  # independent vendor(s) judge the Gemini planner
    else:
        chain.extend(seconds)
    chain.append(_make_offline(policy))
    return ResilientLlm(chain=chain, tier=tier)
