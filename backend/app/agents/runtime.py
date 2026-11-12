"""Budgeted runtime for the agent pipeline.

recommendations_validation.html Section 7: a run is bounded by seven
independent limits, and the runtime - never the model - decides what runs
next. Budgets are read-only to agents, retries are counted, and a model that
stops answering ends the run inside its declared wall clock.

The runtime never raises out of a step unless the whole run must stop, and it
always leaves a run record behind so an aborted run is still auditable.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


class AgentBudgetExhausted(RuntimeError):
    """Raised when a run or a step cannot continue within its limits."""

    def __init__(self, message: str, *, scope: str = "run") -> None:
        super().__init__(message)
        self.scope = scope


class AgentStepFailed(RuntimeError):
    """Raised when a step fails but the run may continue."""


def _valid_count(value: Any) -> bool:
    """A token count is valid only when it is a real, non-negative integer."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def known_usage(usage: Any) -> tuple[int, int] | None:
    """The validated (prompt, completion) counts, or ``None`` when unknown.

    Usage is known only when both counts are present, integral, non-negative
    and not booleans. Anything else is treated as unknown, so a partial or
    invalid object can never be silently recorded as a known zero. This single
    predicate is used by both settlement and the pipeline's retry decision.
    """
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    if not _valid_count(prompt) or not _valid_count(completion):
        return None
    return int(prompt), int(completion)


@dataclass
class RunBudget:
    """The hard limits of one run, plus its measured and unknown usage.

    Limits and usage share one object for backwards compatibility, but they
    are accounted separately: ``tokens_in``/``tokens_out`` are measured
    (actual), ``tokens_unknown`` is the reserved allowance charged when a
    provider returned no usage, and ``tokens_reserved`` is the allowance
    held by a step that has not settled yet. ``derive()`` returns a fresh
    usage slate with the same limits so one run can never consume the next
    run's allowance.

    ``tokens_requested`` sums the per-step *output* caps (``StepGuard.tokens``,
    sent to the provider as ``max_tokens``). It is **not** a total-token
    reservation: settlement charges prompt plus completion usage, so actual
    consumption can exceed the output reservation.
    """

    max_steps: int = 14
    max_llm_calls: int = 12
    max_tokens: int = 128_000
    max_wall_s: int = 900
    max_repair_rounds: int = 2
    max_failures_per_agent: int = 2

    tokens_in: int = 0
    tokens_out: int = 0
    tokens_unknown: int = 0
    tokens_requested: int = 0
    tokens_reserved: int = 0
    calls: int = 0
    calls_actual: int = 0
    calls_unknown: int = 0
    calls_reserved: int = 0
    steps: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        # Fail closed: a missing, zero, negative or boolean cap is not a cap.
        for name in ("max_steps", "max_llm_calls", "max_tokens", "max_wall_s"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")
        for name in ("max_repair_rounds", "max_failures_per_agent"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer, got {value!r}")

    def derive(self) -> "RunBudget":
        """The same limits with a fresh usage slate."""
        return RunBudget(
            max_steps=self.max_steps,
            max_llm_calls=self.max_llm_calls,
            max_tokens=self.max_tokens,
            max_wall_s=self.max_wall_s,
            max_repair_rounds=self.max_repair_rounds,
            max_failures_per_agent=self.max_failures_per_agent,
        )

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started

    @property
    def tokens(self) -> int:
        """Measured (actual) tokens only; unknown usage is reported separately."""
        return self.tokens_in + self.tokens_out

    @property
    def committed_tokens(self) -> int:
        """Actual + unknown + in-flight reservation: what enforcement counts."""
        return self.tokens + self.tokens_unknown + self.tokens_reserved

    @property
    def committed_calls(self) -> int:
        return self.calls + self.calls_reserved

    def as_dict(self) -> dict[str, int]:
        return {
            "max_steps": self.max_steps,
            "max_llm_calls": self.max_llm_calls,
            "max_tokens": self.max_tokens,
            "max_wall_s": self.max_wall_s,
            "max_repair_rounds": self.max_repair_rounds,
            "steps": self.steps,
            "calls": self.calls,
            "calls_actual": self.calls_actual,
            "calls_unknown": self.calls_unknown,
            "calls_reserved": self.calls_reserved,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens": self.tokens,
            "tokens_actual": self.tokens,
            "tokens_unknown": self.tokens_unknown,
            "tokens_requested": self.tokens_requested,
            "tokens_reserved": self.tokens_reserved,
            "tokens_committed": self.committed_tokens,
            "elapsed_s": int(self.elapsed_s),
        }


class StepGuard:
    """One step's declared limits.

    ``tokens`` is the provider's *output* cap (sent as ``max_tokens``). It is
    reserved against the run's token cap as a conservative allowance, but it
    does not upper-bound prompt + completion usage; settlement charges what
    the provider reports. ``wall_s`` is a per-step timeout applied separately
    from the run's ``max_wall_s`` deadline, which is checked at admission only.
    """

    def __init__(self, *, name: str, wall_s: int = 60, tokens: int = 4000,
                 model_calls: int = 1) -> None:
        self.name = name
        self.wall_s = wall_s
        self.tokens = tokens
        self.model_calls = model_calls


class AgentRuntime:
    """Runs steps inside a budget and records what each one cost."""

    def __init__(self, budget: RunBudget | None = None,
                 run_id: str | None = None) -> None:
        self.budget = budget or RunBudget()
        self.run_id = run_id or uuid.uuid4().hex[:10]
        self.steps: list[dict[str, Any]] = []
        self._seen_hashes: list[str] = []
        self._reservations: dict[str, tuple[int, int]] = {}

    # -- accounting ----------------------------------------------------
    def note_step(self, name: str, *, status: str, detail: dict[str, Any] | None = None,
                  artifact_hash: str = "", elapsed_ms: int = 0) -> None:
        self.steps.append({
            "step": len(self.steps) + 1,
            "name": name,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "artifact_sha256": artifact_hash,
            **(detail or {}),
        })

    def charge(self, usage_in: int, usage_out: int) -> None:
        """Record one measured call directly (no reservation was taken)."""
        self.budget.tokens_in += max(0, int(usage_in))
        self.budget.tokens_out += max(0, int(usage_out))
        self.budget.calls += 1
        self.budget.calls_actual += 1

    def reserve(self, guard: StepGuard) -> str:
        """Hold a step's declared allowance before its call is made.

        Returns a reservation id that must be passed to ``settle`` exactly
        once. The allowance is counted against the caps while in flight, so
        two concurrent steps cannot spend the same headroom.
        """
        self.check(guard)
        reservation = uuid.uuid4().hex[:12]
        self._reservations[reservation] = (guard.model_calls, guard.tokens)
        self.budget.calls_reserved += guard.model_calls
        self.budget.tokens_reserved += guard.tokens
        self.budget.tokens_requested += guard.tokens
        return reservation

    def settle(self, reservation: str, *, usage_in: int | None = None,
               usage_out: int | None = None, known: bool | None = None) -> dict[str, Any]:
        """Release a reservation and record the call's usage exactly once.

        A settled reservation cannot be settled again, so a call is never
        charged twice. When the provider did not report usable usage
        (``known`` is false, or a count is missing, negative, boolean or not
        an integer) the reserved allowance is charged to ``tokens_unknown``
        instead of being silently released or recorded as a known zero.
        """
        try:
            calls, tokens = self._reservations.pop(reservation)
        except KeyError as exc:
            raise RuntimeError(
                f"reservation {reservation!r} was already settled or never made") from exc
        self.budget.calls_reserved -= calls
        self.budget.tokens_reserved -= tokens
        valid = _valid_count(usage_in) and _valid_count(usage_out)
        if known is None:
            known = valid
        elif known and not valid:
            # Invalid or partial counts are never promoted to known zero usage.
            known = False
        if known:
            actual_in = int(usage_in)
            actual_out = int(usage_out)
            self.budget.tokens_in += actual_in
            self.budget.tokens_out += actual_out
            self.budget.calls += 1
            self.budget.calls_actual += 1
            return {"known": True, "tokens_in": actual_in, "tokens_out": actual_out}
        # Unknown outcome: consume the reserved allowance, never free it.
        self.budget.tokens_unknown += tokens
        self.budget.calls += 1
        self.budget.calls_unknown += 1
        return {"known": False, "tokens_unknown": tokens}

    def note_failure(self, agent: str) -> None:
        self.budget.failures[agent] = self.budget.failures.get(agent, 0) + 1

    def circuit_open(self, agent: str) -> bool:
        """True when this agent has failed too often in this run."""
        return self.budget.failures.get(agent, 0) >= self.budget.max_failures_per_agent

    def no_progress(self, artifact_hash: str) -> bool:
        """True when the last two steps produced no new artifact."""
        if not artifact_hash:
            return False
        if self._seen_hashes and self._seen_hashes[-1] == artifact_hash:
            return True
        self._seen_hashes.append(artifact_hash)
        return False

    # -- guards ---------------------------------------------------------
    def check(self, guard: StepGuard) -> None:
        """Refuse to start a step that cannot fit inside the budget."""
        if self.budget.steps >= self.budget.max_steps:
            raise AgentBudgetExhausted(
                f"run exceeded {self.budget.max_steps} steps", scope="run")
        if self.budget.committed_calls + guard.model_calls > self.budget.max_llm_calls:
            raise AgentBudgetExhausted(
                f"run exceeded {self.budget.max_llm_calls} model calls", scope="run")
        if self.budget.committed_tokens + guard.tokens > self.budget.max_tokens:
            raise AgentBudgetExhausted(
                f"run exceeded {self.budget.max_tokens} tokens", scope="run")
        if self.budget.elapsed_s > self.budget.max_wall_s:
            raise AgentBudgetExhausted(
                f"run exceeded {self.budget.max_wall_s}s wall clock", scope="run")

    async def run_step(
        self, name: str, guard: StepGuard,
        action: Callable[[], Awaitable[Any]],
        *, on_error: Callable[[Exception], bool] | None = None,
    ) -> Any:
        """Run one step inside its timeout and charge its usage.

        A failure is re-raised so the caller can decide between continuing
        and stopping. The step is recorded either way.
        """
        reservation = self.reserve(guard)
        self.budget.steps += 1
        started = time.monotonic()
        try:
            async with asyncio.timeout(guard.wall_s):
                result = await action()
        except TimeoutError as exc:
            self.settle(reservation, known=False)
            self.note_step(name, status="timeout", elapsed_ms=int((time.monotonic() - started) * 1000))
            self.note_failure(name)
            raise AgentStepFailed(f"step '{name}' exceeded {guard.wall_s}s") from exc
        except Exception as exc:
            self.settle(reservation, known=False)
            self.note_step(name, status="failed", detail={"error": str(exc)[:300]},
                           elapsed_ms=int((time.monotonic() - started) * 1000))
            self.note_failure(name)
            if on_error is not None and on_error(exc):
                return None
            raise
        elapsed = int((time.monotonic() - started) * 1000)
        parsed = known_usage(getattr(result, "usage", None))
        if parsed is None:
            # No usage, or partial/invalid usage: charge the reserved allowance
            # as unknown rather than letting it escape the budget.
            self.settle(reservation, known=False)
        else:
            self.settle(reservation, usage_in=parsed[0], usage_out=parsed[1],
                        known=True)
        detail: dict[str, Any] = {}
        if isinstance(result, dict):
            detail = {k: v for k, v in result.items()
                      if isinstance(v, (str, int, float, bool)) and k != "text"}
        else:
            for attribute in ("model", "finish_reason", "elapsed_ms"):
                value = getattr(result, attribute, None)
                if isinstance(value, (str, int, float, bool)):
                    detail[attribute] = value
        self.note_step(name, status="ok", detail=detail, elapsed_ms=elapsed)
        return result


def hash_text(text: str) -> str:
    """A content hash used for progress detection and memoisation keys."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
