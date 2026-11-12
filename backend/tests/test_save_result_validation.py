"""Item 2: validate every save return and record malformed outcomes honestly.

Candidate ``pipeline.py``/``runtime.py`` are injected before import. All
dependencies are fakes; no project, model, router, network or audio is used.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

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
from app.services.music_service import MusicConflict  # noqa: E402

COMPLETE_BRIEF = (
    "Chord progression: Em -> G -> D -> B7\n"
    "[Verse 1]\nOperator line one,\nOperator line two,\n"
    "Operator line three,\nOperator line four.\n"
)


def _answer() -> str:
    return json.dumps({
        "title": "Save Validation Song",
        "musical_direction": "Southern gothic with fiddle, banjo and a heavy low end.",
        "lyrics": "[Verse 1]\nA line of the song,\nAnother line here.\n",
        "duration_s": 60, "bpm": 120, "time_signature": "4", "key_scale": "E minor",
        "arrangement": {"Verse 1": "Sparse guitar and banjo."},
    })


class Usage:
    def __init__(self, prompt_tokens=100, completion_tokens=300):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class Completion:
    def __init__(self, text):
        self.text = text
        self.usage = Usage()
        self.finish_reason = "stop"
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


class FakeService:
    def __init__(self, state_dir, payload=None, *, raises=None):
        self._dir = state_dir
        self.payload = payload
        self.raises = raises
        self.save_calls = 0

    def _state_dir(self, pid):
        return self._dir

    def save_draft(self, pid, settings, revision_name=None, *, source="draft"):
        self.save_calls += 1
        if self.raises is not None:
            raise self.raises
        return self.payload


def _run(tmp_path, service):
    pipeline = AgentPipeline(service=service, lms=ScriptedLMS([Completion(_answer())]),
                             router=FakeRouter(), store=FakeStore(), budget=RunBudget())
    return asyncio.run(pipeline.run("pid-save", COMPLETE_BRIEF))


VALID = {"draft": {"version": 1, "lyrics_sha256": "a" * 64, "settings_sha256": "b" * 64}}

MALFORMED = {
    "none": None,
    "list": ["not", "a", "mapping"],
    "no_draft_key": {"preflight": []},
    "draft_str": {"draft": "not-a-mapping"},
    "draft_list": {"draft": [1, 2, 3]},
    "version_missing": {"draft": {"lyrics_sha256": "a"}},
    "version_none": {"draft": {"version": None}},
    "version_zero": {"draft": {"version": 0}},
    "version_negative": {"draft": {"version": -1}},
    "version_bool": {"draft": {"version": True}},
    "version_string": {"draft": {"version": "1"}},
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_a_malformed_save_response_is_a_recorded_failure(tmp_path, name):
    service = FakeService(tmp_path, MALFORMED[name])
    result = _run(tmp_path, service)
    assert service.save_calls == 1, "a save must not be retried"
    assert result.status == "failed"
    assert any("malformed" in note for note in result.notes)
    assert any("state was not verified" in note for note in result.notes)
    row = read_run_records(tmp_path)[-1]
    assert row["outcome"] == "failed"
    assert any("malformed" in note for note in row["notes"])


def test_a_valid_save_is_a_success(tmp_path):
    service = FakeService(tmp_path, VALID)
    result = _run(tmp_path, service)
    assert result.status == "saved"
    assert result.draft_version == 1
    assert result.lyrics_sha256 == "a" * 64
    row = read_run_records(tmp_path)[-1]
    assert row["outcome"] == "saved"
    assert row["draft_version"] == 1


def test_a_known_save_conflict_is_recorded_and_reraised(tmp_path):
    service = FakeService(tmp_path, raises=MusicConflict("revision limit reached"))
    try:
        _run(tmp_path, service)
    except MusicConflict:
        pass
    else:
        raise AssertionError("a save conflict must still be raised")
    assert service.save_calls == 1
    row = read_run_records(tmp_path)[-1]
    assert row["outcome"] == "failed"
    assert any("could not be saved" in note for note in row["notes"])


def test_a_ledger_write_failure_is_surfaced_not_claimed(tmp_path):
    service = FakeService(tmp_path, VALID)
    with patch("app.agents.records.append_run_record",
               side_effect=OSError("disk is full")):
        result = _run(tmp_path, service)
    assert result.status == "partial", "a saved draft without a durable ledger is partial"
    assert any("audit ledger could not be written" in note for note in result.notes)


def test_no_ledger_row_is_claimed_when_the_project_is_unknown():
    class UnknownService(FakeService):
        def _state_dir(self, pid):
            raise KeyError(pid)

    pipeline = AgentPipeline(service=UnknownService(Path("unused"), VALID),
                             lms=ScriptedLMS([Completion(_answer())]),
                             router=FakeRouter(), store=FakeStore(), budget=RunBudget())
    result = asyncio.run(pipeline.run("pid-unknown", COMPLETE_BRIEF))
    assert result.status == "partial"
    assert any("project state directory is unknown" in note for note in result.notes)
