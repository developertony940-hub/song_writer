"""Capability probe for local assistant models.

recommendations_validation.html F6: the app checks that a model id is present
in LM Studio's inventory, which is availability, not capability. Measured
probes showed five loaded ids that answer a trivial prompt with an empty
completion and ``finish_reason=length`` because they are reasoning models
that spend the whole budget on hidden thinking. Those models look available
and silently fail a real draft.

This module asks a model to do the smallest thing the pipeline needs - return
a small JSON object - and reports whether it can. The result is advisory:
the operator can still force a model, but the runtime prefers a capable one.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

PROBE_PROMPT = (
    "Reply with this JSON object and nothing else: "
    '{"lyrics": "[Verse 1]\\nA line", "musical_direction": "short"}'
)
PROBE_MAX_TOKENS = 64


class SupportsChatResult(Protocol):
    async def chat_result(self, prompt: str, **kwargs: Any) -> Any:
        ...


@dataclass(frozen=True)
class CapabilityVerdict:
    """Whether a model can be trusted to produce a structured draft."""

    model: str
    capable: bool
    reason: str
    elapsed_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "capable": self.capable,
            "reason": self.reason,
            "elapsed_ms": self.elapsed_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


async def probe_model_capability(
    client: SupportsChatResult, model: str, *, max_tokens: int = PROBE_MAX_TOKENS,
) -> CapabilityVerdict:
    """Ask one tiny question and judge whether the model can answer it.

    Failure is always reported, never raised: a probe must not break a run.
    """
    try:
        result = await client.chat_result(
            PROBE_PROMPT, model=model, temperature=0.0, max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        return CapabilityVerdict(model=model, capable=False,
                                  reason=f"probe_failed: {type(exc).__name__}")
    text = (getattr(result, "text", "") or "").strip()
    usage = getattr(result, "usage", None)
    elapsed = int(getattr(result, "elapsed_ms", 0) or 0)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    if not text:
        finish = getattr(result, "finish_reason", "") or "unknown"
        return CapabilityVerdict(
            model=model, capable=False, reason="empty_completion",
            elapsed_ms=elapsed, prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict) or "lyrics" not in payload:
        return CapabilityVerdict(
            model=model, capable=False, reason="not_json", elapsed_ms=elapsed,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return CapabilityVerdict(
        model=model, capable=True, reason="ok", elapsed_ms=elapsed,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)


async def probe_candidates(
    client: SupportsChatResult, models: list[str],
) -> dict[str, CapabilityVerdict]:
    """Probe several models and keep the verdicts, best first by capability."""
    verdicts: dict[str, CapabilityVerdict] = {}
    for model in models:
        verdict = await probe_model_capability(client, model)
        verdicts[model] = verdict
        log.info("capability probe: model=%s capable=%s reason=%s",
                 model, verdict.capable, verdict.reason)
    return verdicts


def select_capable(
    verdicts: dict[str, CapabilityVerdict], preferred: list[str],
) -> str | None:
    """Pick the first capable model from the preference order."""
    for model in preferred:
        verdict = verdicts.get(model)
        if verdict is not None and verdict.capable:
            return model
    for verdict in verdicts.values():
        if verdict.capable:
            return verdict.model
    return None
