"""Full-dependency integration tests, using only isolated stores and no provider."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from app.models.music import MusicState
from tests.test_music import _reset, _settings


def _module():
    path = Path(__file__).parents[2] / "ops/prepare_fresh_song.py"
    spec = importlib.util.spec_from_file_location("fresh_song_prepare", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _input(tmp_path):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(dict(schema="song_writer_fresh_draft_v1", name="Fresh test",
                                   lineage={"purpose": "provenance only"},
                                   settings=_settings().model_dump(mode="json"))), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_create_draft_without_allowance_or_provider_and_block_duplicate(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    _reset(root, monkeypatch, live=False)
    input_path, sha = _input(tmp_path)
    receipt = tmp_path / "receipt.json"
    module = _module()
    result = module.prepare(input_path, sha, receipt)
    assert result["status"] == "draft_only_complete"
    state = MusicState.model_validate_json(
        (root / result["project_id"] / "music/.state/state.json").read_text())
    assert state.lifetime_cap is None and state.approval is None
    assert state.preferred_take is None and state.audio_approval is None
    with pytest.raises(FileExistsError):
        module.prepare(input_path, sha, receipt)
    assert len(list(root.glob("*/manifest.json"))) == 1


def test_bad_hash_and_live_enabled_fail_before_creation(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    _reset(root, monkeypatch, live=False)
    input_path, sha = _input(tmp_path)
    receipt = tmp_path / "receipt.json"
    module = _module()
    with pytest.raises(ValueError, match="hash"):
        module.prepare(input_path, "0" * 64, receipt)
    assert not receipt.exists() and not list(root.glob("*/manifest.json"))
    _reset(root, monkeypatch, live=True)
    with pytest.raises(RuntimeError, match="disabled"):
        module.prepare(input_path, sha, receipt)
    assert not receipt.exists() and not list(root.glob("*/manifest.json"))
