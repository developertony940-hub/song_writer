"""Focused offline tests for the candidate agent runtime accounting (W1).

Standard library only. The candidate ``runtime.py`` is loaded by file path so
this test cannot fall back to the repository's unmodified module. No provider,
filesystem, GPU or project is touched.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

RUNTIME_PATH = Path(__file__).resolve().parents[1] / "app" / "agents" / "runtime.py"


def _load_runtime():
    spec = importlib.util.spec_from_file_location("candidate_runtime", RUNTIME_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses resolves annotations through sys.modules[cls.__module__].
    sys.modules["candidate_runtime"] = module
    spec.loader.exec_module(module)
    return module


runtime = _load_runtime()
AgentRuntime = runtime.AgentRuntime
RunBudget = runtime.RunBudget
StepGuard = runtime.StepGuard
AgentBudgetExhausted = runtime.AgentBudgetExhausted
AgentStepFailed = runtime.AgentStepFailed


class Usage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class Completion:
    def __init__(self, *, text: str = "{}", usage=None, finish_reason: str = "stop") -> None:
        self.text = text
        self.usage = usage
        self.finish_reason = finish_reason


def test_a_reservation_counts_against_the_cap_before_the_call():
    agent = AgentRuntime(RunBudget(max_tokens=100, max_llm_calls=5))
    agent.reserve(StepGuard(name="first", tokens=80))
    with pytest.raises(AgentBudgetExhausted):
        agent.reserve(StepGuard(name="second", tokens=40))
    assert agent.budget.tokens_reserved == 80
    assert agent.budget.committed_tokens == 80


def test_actual_usage_replaces_the_reservation_and_is_charged_once():
    agent = AgentRuntime(RunBudget(max_tokens=1000))
    guard = StepGuard(name="call", tokens=400)

    async def answer():
        return Completion(text="{}", usage=Usage(100, 50))

    result = asyncio.run(agent.run_step("call", guard, answer))
    assert isinstance(result, Completion)
    assert agent.budget.tokens_in == 100
    assert agent.budget.tokens_out == 50
    assert agent.budget.tokens_reserved == 0
    assert agent.budget.calls == 1
    assert agent.budget.calls_actual == 1
    assert agent.budget.calls_unknown == 0
    assert agent.budget.committed_tokens == 150


def test_unknown_usage_charges_the_reserved_allowance_and_never_frees_it():
    agent = AgentRuntime(RunBudget(max_tokens=10_000))
    guard = StepGuard(name="call", tokens=400)

    async def no_usage():
        return Completion(text="", usage=None, finish_reason="length")

    asyncio.run(agent.run_step("call", guard, no_usage))
    assert agent.budget.calls == 1
    assert agent.budget.calls_unknown == 1
    assert agent.budget.tokens_in == 0
    assert agent.budget.tokens_out == 0
    assert agent.budget.tokens_unknown == 400
    assert agent.budget.tokens_reserved == 0
    assert agent.budget.committed_tokens == 400

    # The unknown charge is not silently released: the headroom is gone.
    with pytest.raises(AgentBudgetExhausted):
        agent.reserve(StepGuard(name="next", tokens=9_700))


def test_a_failed_call_is_charged_as_unknown():
    agent = AgentRuntime(RunBudget(max_tokens=10_000))

    async def boom():
        raise RuntimeError("provider refused")

    with pytest.raises(RuntimeError):
        asyncio.run(agent.run_step("call", StepGuard(name="call", tokens=250), boom))
    assert agent.budget.calls_unknown == 1
    assert agent.budget.tokens_unknown == 250
    assert agent.budget.tokens_reserved == 0


def test_a_timeout_is_charged_as_unknown():
    agent = AgentRuntime(RunBudget(max_tokens=10_000))

    async def never():
        await asyncio.sleep(5)

    with pytest.raises(AgentStepFailed):
        asyncio.run(agent.run_step("stuck", StepGuard(name="stuck", wall_s=1, tokens=300), never))
    assert agent.budget.calls_unknown == 1
    assert agent.budget.tokens_unknown == 300


def test_a_reservation_cannot_be_settled_twice():
    agent = AgentRuntime(RunBudget())
    reservation = agent.reserve(StepGuard(name="once", tokens=100))
    agent.settle(reservation, usage_in=10, usage_out=20)
    with pytest.raises(RuntimeError):
        agent.settle(reservation, usage_in=10, usage_out=20)
    assert agent.budget.calls_actual == 1
    assert agent.budget.tokens_in == 10
    assert agent.budget.tokens_out == 20


def test_derive_resets_usage_but_keeps_the_limits():
    agent = AgentRuntime(RunBudget(max_tokens=777, max_steps=3))
    agent.charge(50, 25)
    fresh = agent.budget.derive()
    assert fresh.max_tokens == 777
    assert fresh.max_steps == 3
    assert fresh.tokens_in == 0
    assert fresh.tokens_out == 0
    assert fresh.calls == 0
    assert fresh.steps == 0


def test_a_missing_or_non_positive_cap_fails_closed():
    with pytest.raises(ValueError):
        RunBudget(max_tokens=0)
    with pytest.raises(ValueError):
        RunBudget(max_tokens=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RunBudget(max_llm_calls=-1)
    with pytest.raises(ValueError):
        RunBudget(max_wall_s=0)


def test_as_dict_reports_each_bucket_distinctly():
    agent = AgentRuntime(RunBudget(max_tokens=1000))
    agent.charge(30, 40)
    report = agent.budget.as_dict()
    for key in ("tokens_in", "tokens_out", "tokens_unknown", "tokens_requested",
                "tokens_reserved", "tokens_actual", "tokens_committed",
                "calls_actual", "calls_unknown", "calls_reserved"):
        assert key in report, key
    assert report["tokens_actual"] == 70
    assert report["tokens_unknown"] == 0
    assert report["tokens_committed"] == 70

