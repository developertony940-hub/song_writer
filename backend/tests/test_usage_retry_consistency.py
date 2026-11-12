"""Item 1: retry eligibility and settlement must share one definition of usage.

Candidate ``pipeline.py`` and ``runtime.py`` are injected into ``sys.modules``
before import. The provider, router, service and store are fakes; no project,
model, GPU, network or audio is touched.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import app.agents  # noqa: F401 - ensure the package exists before injection

CANDIDATE_AGENTS = Path(__file__).resolve().parents[1] / "app" / "agents"


def _inject(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_inject("app.agents.runtime", CANDIDATE_AGENTS / "runtime.py")
_inject("app.agents.pipeline", CANDIDATE_AGENTS / "pipeline.py")

from app.agents.pipeline import AgentPipeline  # noqa: E402
from app.agents.records import read_run_records  # noqa: E402
from app.agents.runtime import AgentRuntime, RunBudget, StepGuard, known_usage  # noqa: E402

COMPLETE_BRIEF = (
    "Chord progression: Em -> G -> D -> B7\n"
    "[Verse 1]\nOperator line one,\nOperator line two,\n"
    "Operator line three,\nOperator line four.\n"
)


def _answer() -> str:
    return json.dumps({
        "title": "Usage Song",
        "musical_direction": "Southern gothic with fiddle, banjo and a heavy low end.",
        "lyrics": "[Verse 1]\nA line of the song,\nAnother line here.\n",
        "duration_s": 60, "bpm": 120, "time_signature": "4", "key_scale": "E minor",
        "arrangement": {"Verse 1": "Sparse guitar and banjo."},
    })


class Usage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class Completion:
    def __init__(self, *, text, usage, finish_reason="stop"):
        self.text = text
        self.usage = usage
        self.finish_reason = finish_reason
        self.model = "fake-model"
        self.elapsed_ms = 10


class FakeRouter:
    async def acquire(self, model, job_id, *, allow_unload=True):
        return None

    async def release(self, model, job_id, unload=False):
        return None


class FakeStore:
    def get(self, pid):
        raise KeyError(pid)


class ScriptedLMS:
    default_model = "fake-model"

    def __init__(self, completions):
        self.completions = list(completions)
        self.calls = 0

    async def chat_result(self, prompt, **kwargs):
        self.calls += 1
        if not self.completions:
            raise AssertionError("the provider was called more often than scripted")
        return self.completions.pop(0)


class ValidService:
    def __init__(self, state_dir):
        self._dir = state_dir
        self.save_calls = 0

    def _state_dir(self, pid):
        return self._dir

    def save_draft(self, pid, settings, revision_name=None, *, source="draft"):
        self.save_calls += 1
        return {"draft": {"version": 1, "lyrics_sha256": "a" * 64,
                          "settings_sha256": "b" * 64}}


def _run(tmp_path, lms, *, budget=None):
    pipeline = AgentPipeline(service=ValidService(tmp_path), lms=lms,
                             router=FakeRouter(), store=FakeStore(),
                             budget=budget or RunBudget())
    return asyncio.run(pipeline.run("pid-usage", COMPLETE_BRIEF))


def _assert_unknown_and_not_retried(tmp_path, lms, result):
    assert lms.calls == 1, "an unknown/invalid outcome must not be retried"
    assert result.usage["calls_actual"] == 0
    assert result.usage["calls_unknown"] == 1
    assert result.usage["tokens_unknown"] == 8192
    row = read_run_records(tmp_path)[-1]
    assert row["calls_unknown"] == 1
    assert row["tokens_unknown"] == 8192


def test_length_with_absent_usage_is_not_retried(tmp_path):
    lms = ScriptedLMS([Completion(text=_answer(), usage=None, finish_reason="length")])
    result = _run(tmp_path, lms)
    assert result.status == "saved"
    _assert_unknown_and_not_retried(tmp_path, lms, result)


def test_length_with_partial_usage_is_not_retried(tmp_path):
    lms = ScriptedLMS([Completion(text=_answer(), usage=Usage(100, None),
                                  finish_reason="length")])
    result = _run(tmp_path, lms)
    assert result.status == "saved"
    _assert_unknown_and_not_retried(tmp_path, lms, result)


def test_length_with_negative_usage_is_not_retried(tmp_path):
    lms = ScriptedLMS([Completion(text=_answer(), usage=Usage(100, -5),
                                  finish_reason="length")])
    result = _run(tmp_path, lms)
    assert result.status == "saved"
    _assert_unknown_and_not_retried(tmp_path, lms, result)


def test_length_with_boolean_usage_is_not_retried(tmp_path):
    lms = ScriptedLMS([Completion(text=_answer(), usage=Usage(True, 10),
                                  finish_reason="length")])
    result = _run(tmp_path, lms)
    assert result.status == "saved"
    _assert_unknown_and_not_retried(tmp_path, lms, result)


def test_length_with_string_usage_is_not_retried(tmp_path):
    lms = ScriptedLMS([Completion(text=_answer(), usage=Usage("100", "10"),
                                  finish_reason="length")])
    result = _run(tmp_path, lms)
    assert result.status == "saved"
    _assert_unknown_and_not_retried(tmp_path, lms, result)


def test_length_with_valid_usage_is_retried_within_existing_caps(tmp_path):
    lms = ScriptedLMS([
        Completion(text=_answer(), usage=Usage(100, 10), finish_reason="length"),
        Completion(text=_answer(), usage=Usage(100, 10), finish_reason="stop"),
    ])
    result = _run(tmp_path, lms, budget=RunBudget(max_llm_calls=2))
    assert result.status == "saved"
    assert lms.calls == 2
    assert result.usage["calls_actual"] == 2
    assert result.usage["calls_unknown"] == 0
    assert result.usage["tokens_in"] == 200
    assert result.usage["tokens_out"] == 20


def test_known_usage_rejects_partial_and_invalid_counts():
    assert known_usage(None) is None
    assert known_usage(Usage(1, None)) is None
    assert known_usage(Usage(None, 1)) is None
    assert known_usage(Usage(-1, 1)) is None
    assert known_usage(Usage(True, 1)) is None
    assert known_usage(Usage("1", 1)) is None
    assert known_usage(Usage(0, 0)) == (0, 0)
    assert known_usage(Usage(7, 9)) == (7, 9)


def test_settle_cannot_promote_invalid_counts_to_known_zero():
    agent = AgentRuntime(RunBudget())
    reservation = agent.reserve(StepGuard(name="x", tokens=100))
    outcome = agent.settle(reservation, usage_in=-1, usage_out=5, known=True)
    assert outcome["known"] is False
    assert agent.budget.calls_unknown == 1
    assert agent.budget.tokens_unknown == 100
    assert agent.budget.tokens_in == 0
    assert agent.budget.tokens_out == 0


def test_settle_still_cannot_be_used_twice():
    agent = AgentRuntime(RunBudget())
    reservation = agent.reserve(StepGuard(name="x", tokens=100))
    agent.settle(reservation, usage_in=10, usage_out=20)
    try:
        agent.settle(reservation, usage_in=10, usage_out=20)
    except RuntimeError:
        pass
    else:
        raise AssertionError("a settled reservation must not settle twice")
    assert agent.budget.calls_actual == 1
