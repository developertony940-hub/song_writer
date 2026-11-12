"""Focused offline tests for save-failure records and unknown-usage handling (W2).

The candidate ``pipeline.py`` and ``runtime.py`` are injected into
``sys.modules`` before import so the repository's unmodified modules are never
exercised. The assistant provider, router, service and store are fakes: the
test cannot reach a real project, model, GPU or network.
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
from app.agents.runtime import RunBudget  # noqa: E402
from app.services.lmstudio_client import ChatResult, ChatUsage  # noqa: E402
from app.services.music_service import MusicConflict  # noqa: E402

COMPLETE_BRIEF = (
    "Chord progression: Em -> G -> D -> B7\n"
    "[Verse 1]\nOperator line one,\nOperator line two,\n"
    "Operator line three,\nOperator line four.\n"
)


def _answer() -> str:
    return json.dumps({
        "title": "Save Failure Song",
        "musical_direction": "Southern gothic with fiddle, banjo and a heavy low end.",
        "lyrics": "[Verse 1]\nA line of the song,\nAnother line here.\n",
        "duration_s": 60, "bpm": 120, "time_signature": "4", "key_scale": "E minor",
        "arrangement": {"Verse 1": "Sparse guitar and banjo."},
    })


class FakeRouter:
    async def acquire(self, model, job_id, *, allow_unload=True):
        return None

    async def release(self, model, job_id, unload=False):
        return None


class FakeStore:
    def get(self, pid):
        raise KeyError(pid)


class ScriptedLMS:
    default_model = "test-assistant"

    def __init__(self, answers, *, usage=(100, 300), usage_present=True):
        self.answers = list(answers)
        self.usage = usage
        self.usage_present = usage_present
        self.calls = []

    async def chat_result(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        text = self.answers.pop(0) if self.answers else "{}"
        usage = ChatUsage(*self.usage, self.usage[0] + self.usage[1]) if self.usage_present else None
        return ChatResult(text=text, usage=usage, model="test-assistant",
                          elapsed_ms=25, finish_reason="stop")


class FakeService:
    def __init__(self, state_dir: Path, *, mode: str = "raise") -> None:
        self._dir = state_dir
        self.mode = mode

    def _state_dir(self, pid):
        return self._dir

    def save_draft(self, pid, settings, revision_name=None, *, source="draft"):
        if self.mode == "raise":
            raise MusicConflict("lyrics must contain at least 8 visible characters")
        if self.mode == "empty":
            return {"draft": {}}
        raise AssertionError(f"unexpected save mode {self.mode}")


def _run(tmp_path, service, lms) -> object:
    pipeline = AgentPipeline(service=service, lms=lms, router=FakeRouter(),
                             store=FakeStore(), budget=RunBudget())
    return asyncio.run(pipeline.run("pid-save", COMPLETE_BRIEF))


def test_a_failed_save_leaves_a_ledger_row_and_reraised(tmp_path):
    service = FakeService(tmp_path, mode="raise")
    lms = ScriptedLMS([_answer()])
    try:
        _run(tmp_path, service, lms)
    except MusicConflict:
        pass
    else:
        raise AssertionError("a failed save must not be reported as success")

    rows = read_run_records(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "failed"
    assert any("could not be saved" in note for note in row["notes"])
    # The model call happened before the save, so its cost is recorded.
    assert row["tokens_in"] == 100
    assert row["tokens_out"] == 300
    assert row["calls_actual"] == 1
    assert row["steps"], "the step evidence must survive the save failure"


def test_a_save_that_returns_no_draft_is_not_a_success(tmp_path):
    service = FakeService(tmp_path, mode="empty")
    lms = ScriptedLMS([_answer()])
    result = _run(tmp_path, service, lms)
    assert result.status == "failed"
    assert any("malformed" in note and "state was not verified" in note
               for note in result.notes)
    row = read_run_records(tmp_path)[-1]
    assert row["outcome"] == "failed"
    assert row["draft_version"] is None


def test_an_unknown_provider_outcome_is_not_retried_and_not_freed(tmp_path):
    service = FakeService(tmp_path, mode="raise")
    lms = ScriptedLMS([""], usage_present=False)
    try:
        _run(tmp_path, service, lms)
    except Exception:
        pass
    else:
        raise AssertionError("an unparseable answer must fail the run")

    assert len(lms.calls) == 1, "an unknown outcome must not trigger a retry"
    row = read_run_records(tmp_path)[-1]
    assert row["outcome"] == "failed"
    assert row["calls_unknown"] == 1
    assert row["calls_actual"] == 0
    assert row["tokens_unknown"] == 8192, "the reserved allowance is charged, not freed"
    assert row["tokens_committed"] >= 8192
