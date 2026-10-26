"""Offline execution-control tests for the extended-Bridge live path."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import get_settings
from app.config import settings as settings_module
from app.models.schemas import ProjectCreate
from app.services import music_service as music_module
from app.services import project_store as ps
from app.services.music_service import (
    MusicConflict,
    MusicNotAuthorized,
    MusicRecoveryRequired,
    MusicService,
    _file_lock,
    _sha256_bytes,
)
from app.services.replacement_export import (
    ExtendedBridgeBlocked,
    ExtendedBridgeExportService,
)
from app.services.repaint_provider import NativeExecution, NativeRepaintTimeout, RepaintOutput

SR = 48000
CH = 2
SECRET = "fixture-only-live-authorization-secret"


def _setup(tmp_path, monkeypatch):
    base = get_settings()
    monkeypatch.setattr(
        settings_module, "_cached",
        dataclasses.replace(
            base,
            projects_root=tmp_path,
            acceptance=dataclasses.replace(base.acceptance, signing_secret=SECRET),
            music=dataclasses.replace(base.music, repaint_enabled=True),
        ))
    ps._store = None
    music_module._service = None
    service = MusicService(comfy=object(), router=object())
    pid = service.store.create(ProjectCreate(name="live controls", brief="fixture")).id
    service._require_repaint_capacity = lambda _pid: {"approved": True, "measured_free_vram_mib": 20480}
    state = service._load_state(pid)
    state.lifetime_cap = 4
    service._save_state(pid, state)
    service._append_journal(pid, {
        "kind": "attempt_started", "attempt_id": "source-attempt",
        "revision": 3, "submission_cap": 4,
    })
    service._append_journal(pid, {
        "kind": "attempt_completed", "attempt_id": "source-attempt",
        "asset_id": "0f26194c08", "sha256": "d" * 64,
    })
    body = b"frozen multipart fixture body"
    cond = b"frozen conditioning fixture"
    env = {"model": {"sha256": "3f6e0797fad420a39bd33979eb6e840e30989e34a3794e843d23b60ec6e422d7"}}
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    (plan_dir / "native.body").write_bytes(body)
    (plan_dir / "native.conditioning.wav").write_bytes(cond)
    (plan_dir / "native.environment.json").write_text(
        json.dumps(env), encoding="utf-8")
    lyrics = [f"line {n}" for n in range(1, 9)]
    guidance = "fixture guidance"
    request = {
        "provider_kind": "native_ace_step_repaint",
        "content_type": "multipart/form-data; boundary=fixture",
        "serialization": {"boundary": "fixture"},
        "fields": {
            "task_type": "repaint", "prompt": guidance,
            "repainting_start": "3.0", "repainting_end": "51.0",
            "chunk_mask_mode": "explicit", "repaint_mode": "conservative",
            "audio_cover_strength": "1.0", "audio_format": "wav32",
            "lyrics": "\n".join(lyrics), "seed": "20261024",
            "use_random_seed": "true", "bpm": "80", "key_scale": "C major",
            "time_signature": "4", "vocal_language": "en",
        },
        "field_order": [
            "task_type", "prompt", "repainting_start", "repainting_end",
            "chunk_mask_mode", "repaint_mode", "audio_cover_strength",
            "audio_format", "lyrics", "seed", "use_random_seed", "bpm",
            "key_scale", "time_signature", "vocal_language",
        ],
        "conditioning": {
            "relative_path": "native.conditioning.wav", "duration_s": 54.0,
            "source_mapping": {
                "clip_start_s": 182.0, "clip_end_s": 236.0,
                "mask_start_s": 185.0, "mask_end_s": 233.0,
                "clip_offset_s": 182.0, "conditioning_is_full_source": False,
            },
            "settings": {},
        },
        "input_files": [{
            "field": "src_audio", "filename": "native.conditioning.wav",
            "content_type": "audio/wav", "sha256": _sha256_bytes(cond),
            "bytes": len(cond),
        }],
        "request_body_sha256": _sha256_bytes(body),
    }
    (plan_dir / "native.request.json").write_text(
        json.dumps(request), encoding="utf-8")
    plan = {
        "project_id": pid, "plan_id": "fixture-plan", "status": "planned_not_dispatched",
        "provider_contacted": False,
        "source": {"sha256": "d" * 64, "attempt": "519bd4ccf1", "revision": 3},
        "proposal": {"sha256": "e" * 64},
        "native_request": {
            "body_path": "native.body", "body_sha256": _sha256_bytes(body),
            "body_bytes": len(body),
        },
        "conditioning": {"sha256": _sha256_bytes(cond)},
        "lyrics": lyrics, "performance_guidance": guidance,
    }
    (plan_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    loaded = {
        "plan": plan, "request": request, "environment": env,
        "source": {"source_sha256": "d" * 64}, "proposal": {},
        "plan_dir": plan_dir, "plan_sha256": _sha256_bytes((plan_dir / "plan.json").read_bytes()),
        "body_path": plan_dir / "native.body", "body_sha256": _sha256_bytes(body),
        "conditioning_path": plan_dir / "native.conditioning.wav",
        "conditioning_sha256": _sha256_bytes(cond),
        "environment_path": plan_dir / "native.environment.json",
    }
    service._load_extended_bridge_plan = lambda _pid, _dir: loaded
    plan_sha = loaded["plan_sha256"]
    now = datetime.now(timezone.utc)
    auth = {
        "schema": "song_writer.extended_bridge_live_authorization.v1",
        "authorization_id": "fixture-auth-1",
        "launch_id": "fixture-launch-1",
        "issued_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "one_shot": True,
        "no_apply": True,
        "project_id": pid,
        "plan_sha256": plan_sha,
        "request_body_sha256": _sha256_bytes(body),
        "conditioning_sha256": _sha256_bytes(cond),
        "source_sha256": "d" * 64,
        "source_asset_id": "0f26194c08",
        "source_attempt_id": "519bd4ccf1",
        "source_revision": 3,
        "lyrics_sha256": _sha256_bytes("\n".join(lyrics).encode("utf-8")),
        "guidance_sha256": _sha256_bytes(guidance.encode("utf-8")),
        "model_sha256": "3f6e0797fad420a39bd33979eb6e840e30989e34a3794e843d23b60ec6e422d7",
        "limits": {"startups": 1, "model_loads": 1, "submissions": 1, "retries": 0},
        "allowance": {
            "source": "project_lifetime_cap", "cap": 4,
            "prior_submissions": 1, "submission_number": 2,
        },
    }
    auth["signature"] = hmac.new(
        SECRET.encode(), json.dumps(
            {k: v for k, v in auth.items() if k != "signature"},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(),
        hashlib.sha256).hexdigest()
    return service, pid, plan_dir, auth, loaded


class FakeClient:
    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout = timeout
        self.posts = 0
        self.bodies: list[bytes] = []

    def execute_prepared(self, prepared, *, recorder):
        self.posts += 1
        self.bodies.append(prepared.body_path.read_bytes())
        recorder.mark_dispatched()
        if self.timeout:
            raise NativeRepaintTimeout("fixture timeout")
        samples = array("f", [0.0]) * (54 * SR * CH)
        recorder.submission({"status_code": 200, "body": b'{"data":{"task_id":"t"}}',
                             "headers": {}, "observed_at": "fixture"}, "t")
        recorder.status({"status_code": 200, "body": b'{"data":[]}',
                         "headers": {}, "observed_at": "fixture"}, None)
        recorder.output_download({"status_code": 200, "body": b"raw",
                                  "headers": {"content-type": "audio/wav"},
                                  "observed_at": "fixture"}, "http://fixture/audio.wav")
        output = RepaintOutput(samples=samples, sample_rate=SR, channels=CH)
        return NativeExecution(
            output=output, task_id="t", raw_bytes=b"raw",
            output_url="http://fixture/audio.wav",
            output_content_type="audio/wav", output_filename="audio.wav",
            submission={}, statuses=[], client_observed_at="fixture")


def _execute(service, pid, plan_dir, auth, client, monkeypatch):
    export = ExtendedBridgeExportService(music=service)
    fake_package = {
        "package_path": str(plan_dir / "fixture-package.zip"),
        "package_sha256": "a" * 64,
        "hashes": {
            "candidate_master_309s.wav": "b" * 64,
            "review_54s.wav": "c" * 64,
            "candidate_master_309s.mp3": "d" * 64,
        },
    }
    monkeypatch.setattr(export, "package_candidate", lambda *a, **k: fake_package)
    service.claim_extended_bridge_launch(pid, plan_dir, auth)
    return export.execute_live(plan_dir, authorization=auth, client=client)


def test_authorization_claim_charges_existing_cap_only_after_begin(tmp_path, monkeypatch):
    service, pid, plan_dir, auth, _loaded = _setup(tmp_path, monkeypatch)
    claim = service.claim_extended_bridge_launch(pid, plan_dir, auth)
    assert claim["launch_id"] == "fixture-launch-1"
    assert service._journal_state(pid)["submissions"] == 1
    started = service.begin_extended_bridge_live(pid, plan_dir, auth)
    assert started["attempt_id"]
    journal = service._journal_state(pid)
    assert journal["submissions"] == 2
    row = next(item for item in journal["rows"]
               if item.get("kind") == "attempt_started"
               and item.get("attempt_id") == started["attempt_id"])
    assert row["submission_number"] == 2
    assert row["submission_cap"] == 4
    assert row["authorization_id"] == "fixture-auth-1"
    assert row["task"] == "extended_bridge"


def test_duplicate_dispatch_claim_is_rejected(tmp_path, monkeypatch):
    service, pid, plan_dir, auth, _loaded = _setup(tmp_path, monkeypatch)
    service.claim_extended_bridge_launch(pid, plan_dir, auth)
    service.begin_extended_bridge_live(pid, plan_dir, auth)
    with pytest.raises((MusicNotAuthorized, MusicRecoveryRequired)):
        service.begin_extended_bridge_live(pid, plan_dir, auth)
    assert service._journal_state(pid)["submissions"] == 2


def test_execute_live_transmits_saved_bytes_once_and_records(tmp_path, monkeypatch):
    service, pid, plan_dir, auth, _loaded = _setup(tmp_path, monkeypatch)
    client = FakeClient()
    result = _execute(service, pid, plan_dir, auth, client, monkeypatch)
    assert client.posts == 1
    assert client.bodies == [(plan_dir / "native.body").read_bytes()]
    assert result["execution_status"] == "completed"
    assert service._journal_state(pid)["submissions"] == 2
    assert (plan_dir / "provider_output.raw").read_bytes() == b"raw"
    assert any(row.get("kind") == "attempt_completed"
               for row in service._journal_state(pid)["rows"])


def test_execute_live_timeout_is_uncertain_no_retry(tmp_path, monkeypatch):
    service, pid, plan_dir, auth, _loaded = _setup(tmp_path, monkeypatch)
    client = FakeClient(timeout=True)
    with pytest.raises(ExtendedBridgeBlocked):
        _execute(service, pid, plan_dir, auth, client, monkeypatch)
    assert client.posts == 1
    assert service._load_state(pid).recovery_required is True
    assert service._journal_state(pid)["submissions"] == 2
    uncertain = next(row for row in service._journal_state(pid)["rows"]
                     if row.get("kind") == "attempt_uncertain")
    assert uncertain["attempt_id"]
    with pytest.raises((MusicConflict, MusicNotAuthorized, MusicRecoveryRequired)):
        service.claim_extended_bridge_launch(pid, plan_dir, auth)
    recovered = service.recover_extended_bridge(
        pid, uncertain["attempt_id"], True)
    assert recovered["recovery_required"] is False
    assert recovered["allowance_consumed"] is True
    assert service._journal_state(pid)["submissions"] == 2
    assert any(row.get("kind") == "recovery_confirmed"
               for row in service._journal_state(pid)["rows"])


def test_package_failure_preserves_raw_and_does_not_resubmit(tmp_path, monkeypatch):
    service, pid, plan_dir, auth, _loaded = _setup(tmp_path, monkeypatch)
    client = FakeClient()
    export = ExtendedBridgeExportService(music=service)
    monkeypatch.setattr(export, "package_candidate",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fixture package failure")))
    service.claim_extended_bridge_launch(pid, plan_dir, auth)
    result = export.execute_live(plan_dir, authorization=auth, client=client)
    assert client.posts == 1
    assert result["execution_status"] == "completed"
    assert result["packaging_status"] == "failed"
    assert (plan_dir / "provider_output.raw").read_bytes() == b"raw"
    assert any(row.get("kind") == "attempt_completed"
               for row in service._journal_state(pid)["rows"])


def test_invalid_expired_and_reused_authorization_rejected(tmp_path, monkeypatch):
    service, pid, plan_dir, auth, _loaded = _setup(tmp_path, monkeypatch)
    bad = dict(auth)
    bad["signature"] = "0" * 64
    with pytest.raises(MusicNotAuthorized, match="signature"):
        service.claim_extended_bridge_launch(pid, plan_dir, bad)
    expired = dict(auth)
    expired["issued_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    expired["expires_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    expired["signature"] = hmac.new(
        SECRET.encode(), json.dumps(
            {k: v for k, v in expired.items() if k != "signature"},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(),
        hashlib.sha256).hexdigest()
    with pytest.raises(MusicNotAuthorized, match="not currently valid"):
        service.claim_extended_bridge_launch(pid, plan_dir, expired)
    service.claim_extended_bridge_launch(pid, plan_dir, auth)
    with pytest.raises(MusicConflict, match="already claimed"):
        service.claim_extended_bridge_launch(pid, plan_dir, auth)
    assert service._journal_state(pid)["submissions"] == 1


def test_stale_cap_binding_is_rejected(tmp_path, monkeypatch):
    service, pid, plan_dir, auth, _loaded = _setup(tmp_path, monkeypatch)
    service._append_journal(pid, {
        "kind": "attempt_started", "attempt_id": "other-attempt",
        "revision": 3, "submission_cap": 4,
    })
    service._append_journal(pid, {
        "kind": "attempt_completed", "attempt_id": "other-attempt",
        "asset_id": "other-asset", "sha256": "f" * 64,
    })
    with pytest.raises(MusicNotAuthorized, match="allowance"):
        service.claim_extended_bridge_launch(pid, plan_dir, auth)
    assert service._journal_state(pid)["submissions"] == 2


def test_predispatch_persistence_failure_does_not_consume_submission(tmp_path, monkeypatch):
    service, pid, plan_dir, auth, _loaded = _setup(tmp_path, monkeypatch)
    service.claim_extended_bridge_launch(pid, plan_dir, auth)

    def fail(*_args, **_kwargs):
        raise OSError("fixture copy failure")

    monkeypatch.setattr(service, "_copy_exclusive", fail)
    with pytest.raises(MusicConflict, match="dispatch blocked"):
        service.begin_extended_bridge_live(pid, plan_dir, auth)
    assert service._journal_state(pid)["submissions"] == 1


def test_shared_file_lock_is_cross_process(tmp_path):
    lock_path = tmp_path / "accounting.lock"
    log_path = tmp_path / "lock.log"
    script = (
        "import sys,time\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "from pathlib import Path\n"
        "from app.services.music_service import _file_lock\n"
        "p=Path(sys.argv[2]); l=Path(sys.argv[3])\n"
        "with _file_lock(p):\n"
        "    with l.open('a') as f: f.write('start ' + str(time.time()) + '\\n'); f.flush()\n"
        "    time.sleep(0.4)\n"
        "    with l.open('a') as f: f.write('end ' + str(time.time()) + '\\n'); f.flush()\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    first = subprocess.Popen([sys.executable, "-c", script, str(Path(__file__).resolve().parents[1]),
                              str(lock_path), str(log_path)], env=env)
    second = subprocess.Popen([sys.executable, "-c", script, str(Path(__file__).resolve().parents[1]),
                               str(lock_path), str(log_path)], env=env)
    assert first.wait(timeout=20) == 0
    assert second.wait(timeout=20) == 0
    lines = log_path.read_text().splitlines()
    starts = [float(line.split()[1]) for line in lines if line.startswith("start")]
    ends = [float(line.split()[1]) for line in lines if line.startswith("end")]
    assert len(starts) == 2 and len(ends) == 2
    assert max(starts) > min(ends)


class FakeController:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.started = 0
        self.shutdowns = 0
        self.released = False

    def start(self):
        self.started += 1
        if self.fail:
            raise RuntimeError("fixture startup failure")
        return {"status": "loaded_and_held"}

    def shutdown(self):
        self.shutdowns += 1
        self.released = True
        return {"status": "verified-owned-shutdown"}

    def provider_client(self):
        return None


class FakeMusic:
    def __init__(self) -> None:
        self.claims = 0
        self.submissions = 1

    def claim_extended_bridge_launch(self, pid, plan_dir, auth):
        self.claims += 1
        return {"launch_id": auth["launch_id"]}

    def _journal_state(self, pid):
        return {"submissions": self.submissions}


class FakeRunner:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def execute_live(self, plan_dir, *, authorization, client=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("fixture runner failure")
        return {"execution_status": "completed"}


def test_launcher_duplicate_lock_and_startup_failure_are_bounded(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops" / "ace-repaint"))
    from one_shot_extended_bridge import LauncherError, OneShotLauncher

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    (plan_dir / "plan.json").write_text(json.dumps({"project_id": "fixture"}), encoding="utf-8")
    auth = {"launch_id": "fixture-launch", "authorization_id": "fixture-auth"}
    music = FakeMusic()
    controller = FakeController()
    runner = FakeRunner()
    launcher = OneShotLauncher(
        plan_dir=plan_dir, authorization=auth, run_root=tmp_path / "run1",
        music=music, controller=controller, runner=runner)
    outcome = launcher.run()
    assert outcome["submission"]["execution_status"] == "completed"
    assert music.claims == 1 and runner.calls == 1
    assert controller.started == 1 and controller.shutdowns == 1
    with pytest.raises(LauncherError, match="already exists"):
        launcher.run()

    failed = OneShotLauncher(
        plan_dir=plan_dir, authorization=auth, run_root=tmp_path / "run2",
        music=FakeMusic(), controller=FakeController(fail=True), runner=FakeRunner())
    outcome = failed.run()
    assert outcome["status"] == "startup_or_load_failed"
    assert outcome["music_submission_consumed"] is False
    assert outcome["shutdown"]["status"] == "verified-owned-shutdown"
