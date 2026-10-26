"""Offline regression tests for portable configuration and asset provenance."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


def test_environment_overrides_are_typed_and_bundled_project_root_is_portable(monkeypatch):
    from app.config import reload_settings
    from app.config import settings as module

    # Do not use monkeypatch.setattr for this singleton: its normal teardown
    # would restore the post-reload cache and leak this test's settings to the
    # rest of the suite. Restore the cache explicitly before env teardown.
    original_cache = module._cached
    try:
        monkeypatch.delenv("LOCALAI_CONFIG", raising=False)
        monkeypatch.setenv("LOCALAI_PROJECTS_ROOT", "test-projects")
        monkeypatch.setenv("LOCALAI_RUNTIME__FFMPEG__PROCESS_TIMEOUT_S", "17")
        monkeypatch.setenv("LOCALAI_RUNTIME__COMFYUI__POLL_INTERVAL_S", "5")
        monkeypatch.setenv("LOCALAI_ACCEPTANCE_SIGNING_SECRET", "private-test-secret")
        changed = reload_settings()
        assert changed.projects_root == (module._REPOSITORY_ROOT / "test-projects").resolve()
        assert changed.ffmpeg.process_timeout_s == 17
        assert changed.comfyui.poll_interval_s == 5
        assert changed.acceptance.signing_secret == "private-test-secret"
    finally:
        module._cached = original_cache


def test_external_config_relative_projects_root_is_anchored_to_config_directory(tmp_path: Path, monkeypatch):
    from app.config import reload_settings
    from app.config import settings as module

    external_config = tmp_path / "operator-config" / "local.toml"
    external_config.parent.mkdir()
    external_config.write_text(
        module._CONFIG_PATH.read_text(encoding="utf-8").replace(
            'projects_root = "projects"', 'projects_root = "operator-projects"', 1,
        ),
        encoding="utf-8",
    )
    original_cache = module._cached
    try:
        monkeypatch.delenv("LOCALAI_PROJECTS_ROOT", raising=False)
        monkeypatch.setenv("LOCALAI_CONFIG", str(external_config))
        changed = reload_settings()
        assert changed.projects_root == (external_config.parent / "operator-projects").resolve()
    finally:
        # This must happen before monkeypatch restores LOCALAI_CONFIG. It keeps
        # a failed assertion from leaving the external configuration cached.
        module._cached = original_cache


def test_launch_script_uses_non_reserved_bind_host_parameter():
    launcher = Path(__file__).resolve().parents[2] / "run.ps1"
    content = launcher.read_text(encoding="utf-8")
    assert "[string]$BindHost" in content
    assert ("--host $BindHost" in content) or ("'--host', $BindHost" in content)
    assert "[string]$Host" not in content


def test_music_launch_mode_is_explicit_and_non_reloading():
    launcher = Path(__file__).resolve().parents[2] / "run.ps1"
    content = launcher.read_text(encoding="utf-8")
    assert "[switch]$Music" in content
    assert "[switch]$Reload" in content
    assert "LOCALAI_MUSIC__CAPACITY_OVERRIDE_AUTHORIZED = 'true'" in content
    assert "Music mode cannot be combined with -Reload" in content
    assert "Capacity: EXPERIMENTAL OVERRIDE - NOT VERIFIED" in content


def test_music_launcher_starts_comfyui_only_when_port_is_idle():
    launcher = Path(__file__).resolve().parents[2] / "run_music.ps1"
    content = launcher.read_text(encoding="utf-8")
    assert "$ComfyPort = 8188" in content
    assert "S:\\ComfyUI\\run_nvidia_gpu.bat" in content
    assert "[string]$ComfyLauncher" in content
    assert "function Test-ComfyUiPort" in content
    assert "function Start-ComfyUiIfNeeded" in content
    assert content.count("Start-Process") == 1
    assert "Start-Process -FilePath $ComfyLauncher" in content
    assert "Split-Path -Parent $ComfyLauncher" in content
    assert "ComfyUI is already listening" in content
    assert "ComfyUI did not answer" in content
    assert "Would also start ComfyUI" in content
    check_only_at = content.index("if ($CheckOnly)")
    launch_call_at = content.index("\nStart-ComfyUiIfNeeded\n")
    assert check_only_at < launch_call_at
    assert launch_call_at < content.index("& $VenvPython -m uvicorn")


def test_all_shipped_workflows_have_semantic_provenance_versions():
    from app.services.provenance import WORKFLOWS_DIR, WORKFLOW_VERSIONS, workflow_record

    shipped = {path.stem for path in WORKFLOWS_DIR.glob("*.json")}
    assert set(WORKFLOW_VERSIONS) == shipped
    for name in shipped:
        record = workflow_record(name)
        assert re.fullmatch(r"\d+\.\d+\.\d+", record["version"])
        assert len(record["sha256"]) == 64


def test_provenance_versions_prompts_workflows_models_and_render_profile():
    from app.services.provenance import asset_provenance

    value = asset_provenance(
        prompt_id="image.generate", rendered_prompt="a deterministic test prompt",
        models={"image": "flux_dev_fp8"}, workflows=["flux_dev_fp8"],
        include_render_profile=True,
    )
    assert value["schema_version"] == 1
    assert value["prompt"]["version"] == "1.0.0"
    assert value["prompt"]["rendered_sha256"] == hashlib.sha256(b"a deterministic test prompt").hexdigest()
    assert value["models"] == {"image": "flux_dev_fp8"}
    workflow = value["workflows"][0]
    assert workflow["name"] == "flux_dev_fp8"
    assert workflow["path"] == "comfyui/workflows/flux_dev_fp8.json"
    assert workflow["version"] == "1.0.0"
    assert len(workflow["sha256"]) == 64
    profile = value["render_profile"]
    assert profile["id"] == "vertical-social-h264-aac-rec709"
    assert profile["version"] == "1.0.0"
    assert len(profile["fingerprint"]) == 64


def test_final_registration_persists_versioned_render_provenance(tmp_path: Path, monkeypatch):
    from app.models.schemas import AssetKind
    from app.services.orchestrator import Orchestrator

    class Store:
        def save_asset(self, project_id, kind, filename, content, meta):
            self.args = project_id, kind, filename, content, meta
            return "registered"

        def update_stage(self, project_id, stage, state):
            self.updated = project_id, stage, state

    class FFmpeg:
        def qc_metadata(self, path):
            return {"passed": True, "duration_s": 1.0}

    output = tmp_path / "master.mp4"
    output.write_bytes(b"synthetic-media-fixture")
    orch = Orchestrator.__new__(Orchestrator)
    orch.store, orch.ffmpeg = Store(), FFmpeg()
    result = orch._register_final("project", output, {"composition": True})
    assert result == "registered"
    project_id, kind, filename, content, meta = orch.store.args
    assert (project_id, kind, filename, content) == ("project", AssetKind.final, "master.mp4", None)
    assert meta["qc"]["passed"] is True
    assert meta["provenance"]["prompt"]["id"] == "render.composition"
    assert meta["provenance"]["render_profile"]["version"] == "1.0.0"
