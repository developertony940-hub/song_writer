"""Item 3: expose the unit and time guarantees the budget does NOT make.

The candidate ``runtime.py`` is loaded by path. Standard library only. These
tests do not assert a hard total-token or total-wall guarantee; they prove the
limits are admissions, not upper bounds.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

RUNTIME_PATH = Path(__file__).resolve().parents[1] / "app" / "agents" / "runtime.py"


def _load_runtime():
    spec = importlib.util.spec_from_file_location("candidate_runtime_scope", RUNTIME_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["candidate_runtime_scope"] = module
    spec.loader.exec_module(module)
    return module


rt = _load_runtime()


class Usage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class Completion:
    def __init__(self, usage):
        self.usage = usage
        self.finish_reason = "stop"
        self.model = "fake-model"
        self.elapsed_ms = 1


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


def test_prompt_plus_output_can_exceed_the_output_reservation():
    agent = rt.AgentRuntime(rt.RunBudget(max_tokens=100_000))
    guard = rt.StepGuard(name="call", tokens=50)

    async def answer():
        # The output cap is 50, but the provider reports more on both sides.
        return Completion(Usage(100, 100))

    asyncio.run(agent.run_step("call", guard, answer))
    budget = agent.budget
    assert budget.tokens_requested == 50, "the reservation is the output cap"
    assert budget.tokens_in == 100
    assert budget.tokens_out == 100
    assert budget.tokens == 200
    assert budget.committed_tokens == 200
    assert budget.committed_tokens > budget.tokens_requested, (
        "an output reservation does not upper-bound total token consumption")


def test_a_step_admitted_before_the_deadline_can_finish_after_it():
    clock = FakeClock(1000.0)
    rt.time = clock
    try:
        budget = rt.RunBudget(max_wall_s=10)
        budget.started = clock.now
        agent = rt.AgentRuntime(budget)
        clock.now = 1009.0  # admitted with one second left on the run

        async def slow_step():
            clock.now += 5.0  # the step itself takes five seconds
            return Completion(Usage(10, 10))

        asyncio.run(agent.run_step("slow", rt.StepGuard(name="slow", wall_s=6, tokens=100),
                                   slow_step))
        assert agent.steps[-1]["status"] == "ok"
        assert budget.elapsed_s > budget.max_wall_s, (
            "the run deadline is checked at admission only; a step can finish after it")
    finally:
        import time as real_time
        rt.time = real_time


def test_the_run_deadline_refuses_a_step_that_starts_after_it():
    clock = FakeClock(1000.0)
    rt.time = clock
    try:
        budget = rt.RunBudget(max_wall_s=10)
        budget.started = clock.now
        agent = rt.AgentRuntime(budget)
        clock.now = 1011.0  # already past the deadline

        async def answer():
            return Completion(Usage(1, 1))

        try:
            asyncio.run(agent.run_step("late", rt.StepGuard(name="late", tokens=10), answer))
        except rt.AgentBudgetExhausted:
            pass
        else:
            raise AssertionError("a step starting after the deadline must be refused")
    finally:
        import time as real_time
        rt.time = real_time
