"""Local FFmpeg integration coverage using generated valid media and ffprobe."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

# Developers without local FFmpeg can still run the unit/configuration suite.
# CI installs both binaries so this real-media regression coverage always runs there.
if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    pytestmark = pytest.mark.skip(reason="FFmpeg and ffprobe are required for synthetic-media integration tests")


def run(coro):
    return asyncio.run(coro)


def ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-v", "error", *args], check=True)


def probe_streams(path: Path) -> list[dict[str, str]]:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        check=True, text=True, capture_output=True,
    )
    return json.loads(completed.stdout)["streams"]


@pytest.fixture()
def runner():
    from app.services.ffmpeg_runner import FFmpegRunner
    value = FFmpegRunner()
    value.binary, value.ffprobe_binary = "ffmpeg", "ffprobe"
    # Small media keeps the real-codec integration test fast while still
    # exercising filters, muxing, subtitles, and duration behavior.
    value.sm_width, value.sm_height, value.sm_fps = 160, 320, 15
    value.sm_vbitrate, value.sm_abitrate = "300k", "64k"
    return value


@pytest.fixture()
def media(tmp_path: Path) -> dict[str, Path]:
    video = tmp_path / "video.mp4"
    voice = tmp_path / "voice.wav"
    music = tmp_path / "music.wav"
    subtitles = tmp_path / "short.vtt"
    ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=160x320:r=15:d=1.2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(video)])
    ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000:duration=2.4", "-y", str(voice)])
    ffmpeg(["-f", "lavfi", "-i", "sine=frequency=220:sample_rate=24000:duration=2.4", "-y", str(music)])
    subtitles.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:00.300\nshort cue\n", encoding="utf-8")
    return {"video": video, "voice": voice, "music": music, "subs": subtitles}


@pytest.mark.parametrize("voice,music,subs", [
    (False, False, False), (True, False, False), (False, True, False), (False, False, True),
    (True, True, False), (True, False, True), (False, True, True), (True, True, True),
])
def test_real_assemble_optional_tracks_do_not_let_subtitles_truncate_master(
    runner, media: dict[str, Path], tmp_path: Path, voice: bool, music: bool, subs: bool,
):
    output = tmp_path / f"master-{voice}-{music}-{subs}.mp4"
    run(runner.assemble(video_path=media["video"], audio_path=media["voice"] if voice else None,
                        music_path=media["music"] if music else None,
                        subtitles_path=media["subs"] if subs else None, output_path=output))
    assert output.is_file() and output.stat().st_size > 0
    duration = run(runner.probe_duration(output))
    assert 1.1 <= duration <= 1.35, duration
    streams = probe_streams(output)
    visual = [stream for stream in streams if stream["codec_type"] == "video"]
    audio = [stream for stream in streams if stream["codec_type"] == "audio"]
    assert len(visual) == 1
    assert visual[0]["codec_name"] == "h264"
    assert visual[0]["pix_fmt"] == "yuv420p"
    # Require explicit signaling in the encoded stream/container. Some FFmpeg
    # builds drop generic output tags unless the filter, x264 VUI, and MP4 colr
    # paths all carry the delivery profile.
    assert visual[0]["color_range"] == "tv"
    assert visual[0]["color_space"] == "bt709"
    assert visual[0]["color_primaries"] == "bt709"
    assert visual[0]["color_transfer"] == "bt709"
    # The delivery profile deliberately inserts a silent AAC track when no
    # narration/music is supplied, so every social master remains stream-safe.
    assert len(audio) == 1
    assert audio[0]["codec_name"] == "aac"
    assert audio[0]["channels"] == 2


def test_composition_omitted_end_is_derived_from_source_and_invalid_range_is_validation_error(
    runner, media: dict[str, Path], tmp_path: Path,
):
    output = tmp_path / "composition.mp4"
    run(runner.render_composition(
        video_paths=[{"path": str(media["video"]), "start_s": 0, "end_s": None, "trim_start_s": 0}],
        audio_paths=[], subtitles=[], total_duration_s=1.2, output_path=output,
    ))
    assert output.is_file()
    assert 1.1 <= run(runner.probe_duration(output)) <= 1.35
    with pytest.raises(ValueError, match="exceeds source duration"):
        run(runner.render_composition(
            video_paths=[{"path": str(media["video"]), "start_s": 0, "end_s": 2, "trim_start_s": 0}],
            audio_paths=[], subtitles=[], total_duration_s=2, output_path=tmp_path / "bad.mp4",
        ))
