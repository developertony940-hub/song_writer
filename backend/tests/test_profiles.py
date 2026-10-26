"""Isolated tests for versioned artist production profiles.

Every take is synthetic: a fake ComfyUI adapter returns deterministic WAV bytes.
No live service or model is contacted.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import io
import json
import wave
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.config import settings as settings_module
from app.main import create_app
from app.models.music import MusicSettings
from app.models.schemas import ProjectCreate
from app.services import music_service as music_module
from app.services import profile_service as profile_module
from app.services import profile_store as profile_store_module
from app.services import project_store as ps
from app.services.music_service import MusicService
from app.services.profile_service import ProfileService


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00\x00" * 1200)
    return buffer.getvalue()


class FakeRouter:
    async def acquire(self, model: str, job_id: str, *, allow_unload: bool = True) -> None:
        return None

    async def release(self, model: str, job_id: str, unload: bool = False) -> None:
        return None

    def held(self, model: str):
        return None


class FakeComfy:
    def __init__(self) -> None:
        self.journal_path: Path | None = None

    async def status(self) -> dict:
        return {"online": True, "base_url": "fake", "gpus": []}

    async def object_info(self) -> dict:
        return {}

    async def submit_music(self, workflow: str, patches: dict) -> str:
        if self.journal_path is not None:
            rows = [
                json.loads(line)
                for line in self.journal_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert any(row.get("kind") == "attempt_started" for row in rows)
        return "prompt-profile-1"

    async def wait_music(self, prompt_id: str, *, should_cancel=None, cancel_timeout_s: float = 10.0) -> dict:
        return {"outputs": {"8": {"audio": [{"filename": "fake.wav", "subfolder": "", "type": "output"}]}}}

    async def fetch_music(self, result: dict, *, prompt_id: str = "") -> dict:
        return {"filename": "fake.wav", "bytes": _wav_bytes(), "prompt_id": prompt_id}

    async def cancel_prompt(self, prompt_id: str) -> dict:
        return {"interrupt_status": 200, "queue_status": 200}


def _ready_runtime():
    async def runtime() -> dict:
        return {
            "ready": True,
            "offline_ready": True,
            "capacity_verified": True,
            "capacity_override_authorized": False,
            "capacity_ready": True,
            "capacity_label": "verified",
            "checkpoint_available": True,
            "workflow_valid": True,
            "live_ready": True,
            "errors": [],
        }
    return runtime


def _reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = get_settings()
    monkeypatch.setattr(
        settings_module,
        "_cached",
        dataclasses.replace(
            base,
            projects_root=tmp_path / "projects",
            profiles_root=tmp_path / "profiles",
            music=dataclasses.replace(
                base.music,
                live_generation_authorized=True,
                max_revisions=30,
            ),
        ),
    )
    ps._store = None
    music_module._service = None
    profile_module._service = None
    profile_store_module._store = None


def _settings() -> MusicSettings:
    return MusicSettings(
        musical_direction="Profile warm acoustic storyteller",
        lyrics="[verse]\nA quiet room, a borrowed light.\n",
        duration_s=12.0,
        language="en",
        bpm=100,
        time_signature="4",
        key_scale="C major",
        generate_audio_codes=True,
        seed=7,
        temperature=0.9,
        top_p=0.95,
        cfg_scale=2.5,
    )


def _changed_settings() -> MusicSettings:
    return MusicSettings(
        musical_direction="Current bright synth direction",
        lyrics="[verse]\nA different room, a different light.\n",
        duration_s=24.0,
        language="en",
        bpm=120,
        time_signature="3",
        key_scale="D major",
        generate_audio_codes=True,
        seed=99,
        arrangement={"Intro": "0:00-0:08"},
        temperature=0.7,
        top_p=0.8,
        cfg_scale=4.0,
    )


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _reset(tmp_path, monkeypatch)
    service = MusicService(comfy=FakeComfy(), router=FakeRouter())
    service.runtime = _ready_runtime()
    pid = service.store.create(ProjectCreate(name="profiles", brief="synthetic")).id
    service.save_draft(pid, _settings())
    service.approve(pid, "profile source approval", 2)
    service.comfy.journal_path = service._journal_path(pid)
    asyncio.run(service.generate(pid))
    takes = service.takes(pid)["takes"]
    assert takes and takes[0]["asset_id"]
    asset_id = takes[0]["asset_id"]
    music_module._service = service
    profile_service = ProfileService(
        store=profile_store_module.get_profile_store(), music=service)
    profile_module._service = profile_service
    client = TestClient(create_app())
    return client, service, pid, asset_id


def _create_profile(client: TestClient, pid: str, asset_id: str, *, name: str = "Warm preset"):
    return client.post("/api/profiles", json={
        "project_id": pid,
        "asset_id": asset_id,
        "name": name,
        "artist_description": "User-added warm production notes",
        "vocal_description": "User-added close-mic baritone",
        "production_defaults": {"language": "en", "temperature": 0.85},
    })


def test_profile_capture_and_reload_persistence(tmp_path, monkeypatch):
    client, service, pid, asset_id = _setup(tmp_path, monkeypatch)
    response = _create_profile(client, pid, asset_id)
    assert response.status_code == 200, response.text
    body = response.json()
    profile_id = body["profile"]["id"]
    version = body["version"]
    assert body["profile"]["latest_version"] == 1
    assert body["profile"]["version_count"] == 1
    assert version["version"] == 1
    assert version["label"] == "production preset; singer identity not verified"
    assert version["provenance_gaps"] == []
    assert version["source"]["project_id"] == pid
    assert version["source"]["asset_id"] == asset_id
    assert version["source"]["revision"] == 1
    assert version["source"]["engine"] == get_settings().music.engine
    assert version["source"]["checkpoint"] == get_settings().music.checkpoint
    assert version["source"]["checkpoint_sha256"] == get_settings().music.checkpoint_sha256
    assert version["source"]["workflow"] == get_settings().music.workflow
    assert version["source"]["settings_source"] == "journal"
    take = service.takes(pid)["takes"][0]
    assert version["source"]["audio_sha256"] == take["audio_sha256"]
    assert version["recorded_settings"]["bpm"] == 100
    assert version["recorded_settings"]["musical_direction"] == "Profile warm acoustic storyteller"

    listing = client.get("/api/profiles").json()
    assert listing["label"] == "production preset; singer identity not verified"
    assert listing["reference_conditioning"] == "disabled"
    assert [item["id"] for item in listing["profiles"]] == [profile_id]

    # Simulate process restart: the durable files must still answer.
    profile_module._service = None
    profile_store_module._store = None
    reloaded = client.get(f"/api/profiles/{profile_id}")
    assert reloaded.status_code == 200, reloaded.text
    again = reloaded.json()
    assert again["version"]["version"] == 1
    assert again["version"]["recorded_settings"]["bpm"] == 100
    assert again["versions"] == [1]


def test_profile_versions_are_immutable_and_do_not_touch_project_or_take(tmp_path, monkeypatch):
    client, service, pid, asset_id = _setup(tmp_path, monkeypatch)
    created = _create_profile(client, pid, asset_id).json()
    profile_id = created["profile"]["id"]
    store = profile_store_module.get_profile_store()
    v1_path = store.root / profile_id / "versions" / "0001.json"
    v1_bytes = v1_path.read_bytes()
    before_state = service._load_state(pid).model_dump(mode="json")
    before_takes = service.takes(pid)

    response = client.put(f"/api/profiles/{profile_id}", json={
        "name": "Warm preset revised",
        "artist_description": "Revised user-added notes",
        "vocal_description": "Revised vocal notes",
        "production_defaults": {"language": "en", "temperature": 0.5},
    })
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["profile"]["latest_version"] == 2
    assert updated["profile"]["version_count"] == 2

    v1 = client.get(f"/api/profiles/{profile_id}/versions/1").json()["version"]
    assert v1["artist_description"] == "User-added warm production notes"
    assert v1["production_defaults"]["temperature"] == 0.85
    assert v1_path.read_bytes() == v1_bytes
    latest = client.get(f"/api/profiles/{profile_id}/versions/2").json()["version"]
    assert latest["artist_description"] == "Revised user-added notes"
    assert latest["recorded_settings"]["bpm"] == 100
    assert latest["source"] == v1["source"]

    after_state = service._load_state(pid).model_dump(mode="json")
    assert after_state == before_state
    assert service.takes(pid) == before_takes


def test_preview_exclusions_lyrics_preservation_approval_and_caps(tmp_path, monkeypatch):
    client, service, pid, asset_id = _setup(tmp_path, monkeypatch)
    created = _create_profile(client, pid, asset_id).json()
    profile_id = created["profile"]["id"]
    service.save_draft(pid, _changed_settings())
    service.approve(pid, "cap stays monotone", 2)
    from_old_take = _create_profile(
        client, pid, asset_id, name="Old take snapshot").json()
    assert from_old_take["version"]["recorded_settings"]["bpm"] == 100
    assert from_old_take["version"]["recorded_settings"]["musical_direction"] == \
        "Profile warm acoustic storyteller"
    assert from_old_take["version"]["recorded_settings"]["lyrics"] != \
        _changed_settings().lyrics
    before_takes = service.takes(pid)["takes"]
    assert service._load_state(pid).lifetime_cap == 2

    response = client.post(
        f"/api/profiles/{profile_id}/versions/1/preview", json={
            "project_id": pid,
            "exclude": ["bpm", "key_scale", "time_signature", "seed", "duration_s", "arrangement"],
            "apply_defaults": False,
            "acknowledge_unverified": False,
        })
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["compatibility"]["status"] == "supported"
    assert preview["unsupported_fields"] == []
    proposed = preview["proposed_settings"]
    assert proposed["lyrics"] == _changed_settings().lyrics
    assert proposed["bpm"] == 120
    assert proposed["key_scale"] == "D major"
    assert proposed["seed"] == 99
    assert proposed["duration_s"] == 24.0
    assert proposed["arrangement"] == {"Intro": "0:00-0:08"}
    assert proposed["musical_direction"] == "Profile warm acoustic storyteller"
    changed_fields = {item["field"] for item in preview["changes"]}
    assert "musical_direction" in changed_fields
    assert not changed_fields & {"bpm", "key_scale", "time_signature", "seed", "duration_s", "arrangement"}
    assert {item["field"] for item in preview["excluded"]} == {
        "bpm", "key_scale", "time_signature", "seed", "duration_s", "arrangement"}
    assert "lyrics from the current project" in preview["preserved"]

    loaded = client.post(
        f"/api/profiles/{profile_id}/versions/1/load", json={
            "project_id": pid,
            "exclude": ["bpm", "key_scale", "time_signature", "seed", "duration_s", "arrangement"],
            "apply_defaults": False,
            "acknowledge_unverified": False,
            "preview_token": preview["preview_token"],
            "confirm": True,
        })
    assert loaded.status_code == 200, loaded.text
    result = loaded.json()["draft"]
    assert result["approval"] is None
    assert result["audio_approval"] is None
    assert result["lifetime_cap"] == 2
    assert result["draft"]["settings"]["bpm"] == 120
    assert result["draft"]["settings"]["seed"] == 99
    assert result["draft"]["settings"]["musical_direction"] == "Profile warm acoustic storyteller"
    assert result["draft"]["settings"]["lyrics"] == _changed_settings().lyrics
    assert result["draft"]["version"] == 3
    assert service.takes(pid)["takes"] == before_takes


def test_stale_preview_and_explicit_confirmation_are_required(tmp_path, monkeypatch):
    client, service, pid, asset_id = _setup(tmp_path, monkeypatch)
    created = _create_profile(client, pid, asset_id).json()
    profile_id = created["profile"]["id"]
    service.save_draft(pid, _changed_settings())
    preview = client.post(
        f"/api/profiles/{profile_id}/versions/1/preview", json={
            "project_id": pid, "exclude": ["bpm"], "apply_defaults": False,
        }).json()
    unconfirmed = client.post(
        f"/api/profiles/{profile_id}/versions/1/load", json={
            "project_id": pid, "exclude": ["bpm"], "apply_defaults": False,
            "preview_token": preview["preview_token"], "confirm": False,
        })
    assert unconfirmed.status_code == 409
    assert "confirmation" in unconfirmed.json()["detail"]
    service.save_draft(pid, _changed_settings().model_copy(update={
        "musical_direction": "Stale preview direction",
    }))
    stale = client.post(
        f"/api/profiles/{profile_id}/versions/1/load", json={
            "project_id": pid, "exclude": ["bpm"], "apply_defaults": False,
            "preview_token": preview["preview_token"], "confirm": True,
        })
    assert stale.status_code == 409
    assert "stale" in stale.json()["detail"]


def test_unsupported_defaults_are_reported_not_silently_dropped(tmp_path, monkeypatch):
    client, service, pid, asset_id = _setup(tmp_path, monkeypatch)
    created = _create_profile(client, pid, asset_id).json()
    profile_id = created["profile"]["id"]
    updated = client.put(f"/api/profiles/{profile_id}", json={
        "name": "Unsupported defaults",
        "artist_description": "",
        "vocal_description": "",
        "production_defaults": {"bpm": 133, "language": "en"},
    })
    assert updated.status_code == 200, updated.text
    version = updated.json()["version"]["version"]
    preview_response = client.post(
        f"/api/profiles/{profile_id}/versions/{version}/preview", json={
            "project_id": pid, "exclude": [], "apply_defaults": True,
        })
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["unsupported_fields"] == ["bpm"]
    assert preview["proposed_settings"]["bpm"] == _settings().bpm
    refused = client.post(
        f"/api/profiles/{profile_id}/versions/{version}/load", json={
            "project_id": pid, "exclude": [], "apply_defaults": True,
            "preview_token": preview["preview_token"], "confirm": True,
        })
    assert refused.status_code == 409
    assert "unsupported profile fields" in refused.json()["detail"]


def test_provider_mismatch_refuses_load_without_substitution(tmp_path, monkeypatch):
    client, service, pid, asset_id = _setup(tmp_path, monkeypatch)
    created = _create_profile(client, pid, asset_id).json()
    profile_id = created["profile"]["id"]
    base = get_settings()
    monkeypatch.setattr(
        settings_module, "_cached",
        dataclasses.replace(
            base, music=dataclasses.replace(base.music, engine="different-engine")))
    preview = client.post(
        f"/api/profiles/{profile_id}/versions/1/preview", json={
            "project_id": pid, "exclude": [], "apply_defaults": False,
        })
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["compatibility"]["status"] == "unsupported"
    assert any("engine" in issue for issue in body["compatibility"]["issues"])
    refused = client.post(
        f"/api/profiles/{profile_id}/versions/1/load", json={
            "project_id": pid, "exclude": [], "apply_defaults": False,
            "acknowledge_unverified": True,
            "preview_token": body["preview_token"], "confirm": True,
        })
    assert refused.status_code == 409
    assert "provider compatibility failed" in refused.json()["detail"]


def test_unverified_identity_requires_explicit_acknowledgement(tmp_path, monkeypatch):
    client, service, pid, asset_id = _setup(tmp_path, monkeypatch)
    created = _create_profile(client, pid, asset_id).json()
    profile_id = created["profile"]["id"]
    store = profile_store_module.get_profile_store()
    latest = store.get_latest(profile_id)
    unverified = latest.model_copy(update={
        "version": latest.version + 1,
        "name": "Missing checkpoint identity",
        "created_at": datetime.now(timezone.utc),
        "source": latest.source.model_copy(update={"checkpoint": None}),
    })
    store.add_version(unverified)
    service.save_draft(pid, _changed_settings())
    service.approve(pid, "unverified load invalidates", 2)
    preview = client.post(
        f"/api/profiles/{profile_id}/versions/2/preview", json={
            "project_id": pid, "exclude": ["bpm"], "apply_defaults": False,
        }).json()
    assert preview["compatibility"]["status"] == "unverified"
    refused = client.post(
        f"/api/profiles/{profile_id}/versions/2/load", json={
            "project_id": pid, "exclude": ["bpm"], "apply_defaults": False,
            "acknowledge_unverified": False,
            "preview_token": preview["preview_token"], "confirm": True,
        })
    assert refused.status_code == 409
    assert "unverified" in refused.json()["detail"]
    accepted = client.post(
        f"/api/profiles/{profile_id}/versions/2/load", json={
            "project_id": pid, "exclude": ["bpm"], "apply_defaults": False,
            "acknowledge_unverified": True,
            "preview_token": preview["preview_token"], "confirm": True,
        })
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["draft"]["approval"] is None


def test_legacy_take_without_settings_is_not_reconstructed(tmp_path, monkeypatch):
    client, service, pid, asset_id = _setup(tmp_path, monkeypatch)
    meta = service.store.get_asset(pid, asset_id)
    music_meta = dict(meta.meta.get("music") or {})
    music_meta.pop("effective_settings", None)
    service.store.update_asset_meta(pid, asset_id, music=music_meta)
    journal_path = service._journal_path(pid)
    rows = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for row in rows:
        if row.get("kind") == "attempt_started":
            row.pop("effective_settings", None)
    journal_path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    response = _create_profile(client, pid, asset_id, name="Legacy gap profile")
    assert response.status_code == 200, response.text
    version = response.json()["version"]
    assert version["recorded_settings"] is None
    assert version["source"]["settings_source"] is None
    assert any("no effective settings" in gap for gap in version["provenance_gaps"])
    preview = client.post(
        f"/api/profiles/{version['profile_id']}/versions/1/preview", json={
            "project_id": pid, "exclude": [], "apply_defaults": False,
        })
    assert preview.status_code == 409
    assert "no recorded take settings" in preview.json()["detail"]


def test_browser_artist_profile_controls_are_present():
    root = Path(__file__).resolve().parents[2]
    html = (root / "frontend" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (root / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    for element_id in [
        "profile-take", "profile-name", "profile-artist", "profile-vocal",
        "profile-default-direction", "profile-default-language",
        "profile-default-temperature", "profile-default-topp", "profile-default-cfg",
        "profile-save", "profile-update", "profile-list", "profile-details",
        "profile-version", "profile-exclude-bpm", "profile-exclude-key",
        "profile-exclude-timesig", "profile-exclude-seed", "profile-exclude-duration",
        "profile-exclude-arrangement", "profile-apply-defaults",
        "profile-ack-unverified", "profile-preview", "profile-load",
        "profile-preview-out",
    ]:
        assert f'id="{element_id}"' in html
    assert "Production preset; singer identity not verified" in html
    assert "Reference conditioning is disabled" in html
    assert "app.js?v=46" in html
    for symbol in [
        "refreshProfiles", "saveProfileFromTake", "updateProfileVersion",
        "previewProfileLoad", "loadProfileIntoProject", "profileExclusions",
        "clearProfilePreview", "updateProfileLoadEnabled",
    ]:
        assert (f"\nfunction {symbol}" in js
                or f"\nasync function {symbol}" in js
                or f"\n{symbol} =" in js)
    assert "\nlet profilePreviewState" in js
    assert '\ndocument.getElementById("profile-save")' in js
    assert "/profiles/" in js
