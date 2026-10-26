"""Portable application configuration with explicit environment overrides.

Checked-in defaults are safe for a clone on Windows, macOS, and Linux. Runtime
operators can either point ``LOCALAI_CONFIG`` at a TOML file or override a
single value with ``LOCALAI_<SECTION>__<KEY>`` (for example,
``LOCALAI_RUNTIME__FFMPEG__BINARY=ffmpeg.exe``). Legacy-friendly aliases such
as ``LOCALAI_PROJECTS_ROOT`` are also supported. Relative ``projects_root``
values in the bundled configuration are repository-relative; when
``LOCALAI_CONFIG`` selects a configuration file, they are relative to that
file's directory.
"""
from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(__file__).with_name("settings.toml")
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_ENV_PREFIX = "LOCALAI_"
_ALIASES = {
    "LOCALAI_PROJECTS_ROOT": ("app", "projects_root"),
    "LOCALAI_PROFILES_ROOT": ("app", "profiles_root"),
    "LOCALAI_HOST": ("app", "host"),
    "LOCALAI_PORT": ("app", "port"),
    "LOCALAI_LMSTUDIO_URL": ("runtime", "lmstudio", "base_url"),
    "LOCALAI_COMFYUI_URL": ("runtime", "comfyui", "base_url"),
    "LOCALAI_TTS_URL": ("runtime", "tts", "base_url"),
    "LOCALAI_FFMPEG_BINARY": ("runtime", "ffmpeg", "binary"),
    "LOCALAI_FFPROBE_BINARY": ("runtime", "ffmpeg", "ffprobe_binary"),
    # Keep the real-acceptance key outside project records. Prefer this shell
    # environment variable rather than placing the secret in a config file.
    "LOCALAI_ACCEPTANCE_SIGNING_SECRET": ("acceptance", "signing_secret"),
}


@dataclass(frozen=True)
class LMStudioCfg:
    base_url: str
    gpu: int
    request_timeout_s: int
    default_model: str
    vision_model: str


@dataclass(frozen=True)
class ComfyUICfg:
    base_url: str
    gpu: int
    poll_interval_s: int
    request_timeout_s: int
    workflow_image: str
    workflow_image_img2img: str = ""
    workflow_image_pulid: str = ""
    workflow_i2v: str = ""
    workflow_t2v: str = ""
    workflow_upscale: str = ""


@dataclass(frozen=True)
class TTSCfg:
    engine: str
    base_url: str
    default_voice: str
    sample_rate: int


@dataclass(frozen=True)
class FFmpegCfg:
    binary: str
    ffprobe_binary: str
    default_video_codec: str
    default_audio_codec: str
    default_image_format: str
    # Applies to every spawned ffmpeg and ffprobe process, including QC.
    process_timeout_s: int = 7200


@dataclass(frozen=True)
class VRAMCfg:
    strategy: str
    gpu0_capacity_mb: int
    gpu1_capacity_mb: int
    reserve_mb: int
    sequential_phasing: bool


@dataclass(frozen=True)
class SocialMasterCfg:
    profile_id: str
    profile_version: str
    width: int
    height: int
    fps: int
    duration_s: float
    video_codec: str
    video_bitrate: str
    audio_codec: str
    audio_bitrate: str
    audio_sample_rate: int
    audio_channels: int
    pix_fmt: str
    color_space: str
    color_primaries: str
    color_trc: str


@dataclass(frozen=True)
class ProvenanceCfg:
    schema_version: int


@dataclass(frozen=True)
class AcceptanceCfg:
    """Local-only key material for tamper-evident real acceptance records."""
    signing_secret: str = ""


@dataclass(frozen=True)
class AudiobookCfg:
    """Phase 1 audiobook slice controls.

    ``live_generation_authorized`` is a server-side hold: while ``False`` the
    API refuses any real (non-fake) TTS render request. Deterministic fake
    synthesis injected by isolated tests is unaffected.
    """
    live_generation_authorized: bool = False


@dataclass(frozen=True)
class MusicCfg:
    """Bounded ACE-Step song workflow controls.

    ``live_generation_authorized`` is a server-side hold. Isolated synthetic
    tests may inject a fake ComfyUI adapter, but the real ComfyUI submission
    path refuses to start while this is false.
    """
    live_generation_authorized: bool = False
    capacity_verified: bool = False
    capacity_override_authorized: bool = False
    engine: str = "ace-step-1.5"
    checkpoint: str = "ace_step_1.5_turbo_aio.safetensors"
    checkpoint_sha256: str = "67b0f43aa5c51c840bd0228e6a935d8ff416ec87e5df2fc0637da17a561252bc"
    workflow: str = "ace_step_1_5_aio"
    max_submission_cap: int = 10
    default_duration_s: float = 30.0
    known_sections: list[str] = field(default_factory=lambda: [
        "intro", "verse", "pre-chorus", "chorus", "post-chorus", "bridge",
        "outro", "inst", "instrumental", "hook", "breakdown", "interlude",
    ])
    preflight_seconds_per_line: float = 6.0
    max_variants: int = 30
    max_critiques: int = 20
    # Section-level repaint (offline integration). Submission stays disabled
    # until a separate explicit authorization; no model is loaded here.
    repaint_enabled: bool = False
    repaint_service_url: str = "http://127.0.0.1:8021"
    repaint_output_format: str = "wav32"
    repaint_crossfade_ms: int = 10
    repaint_min_free_vram_mib: int = 6144
    # Isolated deterministic provider for offline browser walkthroughs. Real
    # submission still requires repaint_enabled + an approved capacity gate.
    repaint_fake_provider: bool = False
    max_revisions: int = 30


@dataclass(frozen=True)
class Settings:
    name: str
    version: str
    host: str
    port: int
    projects_root: Path
    profiles_root: Path
    lmstudio: LMStudioCfg
    comfyui: ComfyUICfg
    tts: TTSCfg
    ffmpeg: FFmpegCfg
    vram: VRAMCfg
    social_master: SocialMasterCfg
    provenance: ProvenanceCfg
    acceptance: AcceptanceCfg
    audiobook: AudiobookCfg
    music: MusicCfg

    @property
    def projects_root_path(self) -> Path:
        return Path(self.projects_root)

    @property
    def profiles_root_path(self) -> Path:
        return Path(self.profiles_root)


def _parse_environment_value(value: str, existing: Any) -> Any:
    """Convert environment values using the TOML default's scalar type."""
    if isinstance(existing, bool):
        normalized = value.strip().lower()
        if normalized not in {"true", "false"}:
            raise ValueError(f"expected true or false, got {value!r}")
        return normalized == "true"
    if isinstance(existing, int) and not isinstance(existing, bool):
        return int(value)
    if isinstance(existing, float):
        return float(value)
    return value


def _set_env_override(data: dict[str, Any], path: tuple[str, ...], value: str, source: str) -> None:
    target: Any = data
    for part in path[:-1]:
        if not isinstance(target, dict) or part not in target:
            raise ValueError(f"{source} targets unknown configuration path {'.'.join(path)}")
        target = target[part]
    key = path[-1]
    if not isinstance(target, dict) or key not in target:
        raise ValueError(f"{source} targets unknown configuration path {'.'.join(path)}")
    target[key] = _parse_environment_value(value, target[key])


def _load_data() -> tuple[dict[str, Any], Path, bool]:
    configured = os.environ.get("LOCALAI_CONFIG")
    uses_external_config = configured is not None
    config_path = Path(configured).expanduser() if uses_external_config else _CONFIG_PATH
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    config_path = config_path.resolve()
    try:
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise RuntimeError(f"cannot read song_writer configuration at {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"song_writer configuration at {config_path} is not a TOML object")
    data = copy.deepcopy(data)
    # Existing private configuration files may predate acceptance signing.
    # Give them a safe empty default so an environment override can still work.
    acceptance = data.setdefault("acceptance", {})
    if not isinstance(acceptance, dict):
        raise RuntimeError("song_writer acceptance configuration must be a TOML table")
    acceptance.setdefault("signing_secret", "")
    # Explicit aliases are easiest for shells and take precedence over generic
    # paths because they are more specific operational intent.
    for env_name, path in _ALIASES.items():
        if env_name in os.environ:
            _set_env_override(data, path, os.environ[env_name], env_name)
    for env_name, value in os.environ.items():
        if not env_name.startswith(_ENV_PREFIX) or env_name in _ALIASES or env_name == "LOCALAI_CONFIG":
            continue
        encoded = env_name[len(_ENV_PREFIX):]
        if "__" not in encoded:
            continue
        path = tuple(part.lower() for part in encoded.split("__"))
        _set_env_override(data, path, value, env_name)
    return data, config_path, uses_external_config


def _portable_path(value: str | Path, config_path: Path, *, uses_external_config: bool) -> Path:
    """Resolve a configured project root without depending on launch CWD.

    The bundled defaults remain repository-relative for a portable checkout.
    An explicitly selected ``LOCALAI_CONFIG`` file is a self-contained local
    configuration, so its relative paths are anchored beside that TOML file.
    """
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if path.is_absolute():
        return path
    base = config_path.parent if uses_external_config else _REPOSITORY_ROOT
    return (base / path).resolve()


def _build(data: dict[str, Any], config_path: Path, *, uses_external_config: bool) -> Settings:
    app = data["app"]
    rt = data["runtime"]
    return Settings(
        name=app["name"], version=app["version"], host=app["host"], port=app["port"],
        projects_root=_portable_path(
            app["projects_root"], config_path, uses_external_config=uses_external_config,
        ),
        profiles_root=_portable_path(
            app["profiles_root"], config_path, uses_external_config=uses_external_config,
        ),
        lmstudio=LMStudioCfg(**rt["lmstudio"]),
        comfyui=ComfyUICfg(**rt["comfyui"]),
        tts=TTSCfg(**rt["tts"]),
        ffmpeg=FFmpegCfg(**rt["ffmpeg"]),
        vram=VRAMCfg(**data["vram"]),
        social_master=SocialMasterCfg(**data["social_master"]),
        provenance=ProvenanceCfg(**data["provenance"]),
        acceptance=AcceptanceCfg(**data["acceptance"]),
        audiobook=AudiobookCfg(**data.get("audiobook", {})),
        music=MusicCfg(**data.get("music", {})),
    )


_cached: Settings | None = None


def get_settings() -> Settings:
    global _cached
    if _cached is None:
        data, config_path, uses_external_config = _load_data()
        _cached = _build(data, config_path, uses_external_config=uses_external_config)
    return _cached


def reload_settings() -> Settings:
    global _cached
    _cached = None
    return get_settings()
