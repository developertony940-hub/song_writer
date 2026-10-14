"""Focused Phase 1 timeline, caption, and delivery-QC tests."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def runner():
    from app.services.ffmpeg_runner import FFmpegRunner
    value = FFmpegRunner()
    value.binary = value.ffprobe_binary = "ffmpeg"
    value.ffprobe_binary = "ffprobe"
    value.sm_width, value.sm_height, value.sm_fps = 160, 320, 15
    value.sm_vbitrate, value.sm_abitrate = "300k", "64k"
    return value


@pytest.fixture(autouse=True)
def require_ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for delivery integration tests")


def make_media(path: Path, color: str | None = None, audio: bool = False) -> None:
    if audio:
        args = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1", "-y", str(path)]
    else:
        args = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=160x320:r=15:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(path)]
    subprocess.run(args, check=True)


def rgb_at(video: Path, seconds: float) -> tuple[int, int, int]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{seconds:.3f}", "-i", str(video), "-frames:v", "1", "-vf", "crop=2:2:80:160", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    return tuple(raw[:3])


def pcm_energy_at(video: Path, seconds: float) -> int:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(seconds), "-t", "0.05", "-i", str(video), "-map", "0:a:0", "-f", "s16le", "-ac", "1", "-ar", "48000", "-"], check=True, capture_output=True).stdout
    return sum(abs(int.from_bytes(raw[i:i + 2], "little", signed=True)) for i in range(0, len(raw) - 1, 2))


def test_timeline_places_clips_delays_audio_and_covers_gaps_with_canvas(runner, tmp_path: Path):
    red, blue, tone = tmp_path / "red.mp4", tmp_path / "blue.mp4", tmp_path / "tone.wav"
    make_media(red, "red"); make_media(blue, "blue"); make_media(tone, audio=True)
    out = tmp_path / "timeline.mp4"
    run(runner.render_composition(
        video_paths=[
            {"path": str(red), "start_s": 0, "end_s": 1, "trim_start_s": 0},
            # This overlaps red; the later clip must be visible on top.
            {"path": str(blue), "start_s": .5, "end_s": 1.5, "trim_start_s": 0},
        ],
        audio_paths=[{"path": str(tone), "start_s": 1, "end_s": 2, "trim_start_s": 0, "volume": 1}],
        subtitles=[], total_duration_s=2.5, output_path=out,
    ))
    early, overlap, gap = rgb_at(out, .25), rgb_at(out, .75), rgb_at(out, 2.0)
    assert early[0] > early[2] * 2, (early, overlap, gap)
    assert overlap[2] > overlap[0] * 2, (early, overlap, gap)
    assert max(gap) < 8, (early, overlap, gap)
    assert pcm_energy_at(out, .25) < 1000
    assert pcm_energy_at(out, 1.25) > 10000
    assert pcm_energy_at(out, 2.25) < 1000
    qc = runner.qc_metadata(out)
    assert qc and qc["passed"] and qc["duration_s"] == pytest.approx(2.5, abs=.15)
    assert qc["profile"]["video_codec"] == "h264"


def test_ass_caption_escaping_wrapping_and_schema_range_validation(tmp_path: Path):
    from pydantic import ValidationError
    from app.models.schemas import SubtitleClip
    from app.services.ffmpeg_runner import FFmpegRunner

    with pytest.raises(ValidationError, match="end_s must be greater"):
        SubtitleClip(start_s=2, end_s=2, text="bad")
    with pytest.raises(ValidationError, match="unbroken token"):
        SubtitleClip(start_s=0, end_s=1, text="x" * 100, width=200, font_size=48)
    with pytest.raises(ValidationError, match="background opacity"):
        SubtitleClip(text="bad color", bg_color="#010203@2")
    ass = tmp_path / "captions.ass"
    FFmpegRunner._write_ass(ass, [{
        "start_s": 0, "end_s": 2, "text": "Slash \\ command and {control}",
        "x": 0, "y": 0, "width": 936, "font_size": 48,
    }], 3)
    text = ass.read_text(encoding="utf-8")
    dialogue = next(line for line in text.splitlines() if line.startswith("Dialogue:"))
    assert "\\{control\\}" in dialogue
    assert "\\\\ command" in dialogue
    assert dialogue.count("\\N") <= 1
    assert "\\pos(" in dialogue


def test_quality_gate_rejects_an_invalid_master(runner, tmp_path: Path):
    wrong = tmp_path / "wrong.mp4"
    make_media(wrong, "red")
    with pytest.raises(Exception, match="quality gate failed"):
        run(runner.quality_gate(wrong, 1))
