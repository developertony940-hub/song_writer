"""Self-contained audio helpers for the audiobook slice.

The project's ``FFmpegRunner`` already carries uncommitted local work, so this
slice does not modify it. WAV assembly is pure-Python (deterministic PCM
concatenation with silence); MP3 delivery shells out to the FFmpeg binary named
in configuration. No loudness normalization is applied (deferred to the
delivery specification).
"""
from __future__ import annotations

import hashlib
import subprocess
import wave
from pathlib import Path

from app.config import get_settings


class AudioError(RuntimeError):
    """A chunk or master audio file is missing, malformed, or inconsistent."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _wav_params(path: Path) -> tuple[int, int, int]:
    if not path.is_file() or path.stat().st_size == 0:
        raise AudioError(f"audio file is missing or empty: {path}")
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getnchannels(), handle.getsampwidth(), handle.getframerate()
    except (wave.Error, EOFError) as exc:
        raise AudioError(f"not a readable PCM WAV: {path}") from exc


def write_master_wav(chunk_paths: list[Path], pause_ms: int, output_path: Path) -> Path:
    """Concatenate chunk WAVs with inter-chunk silence into one master WAV.

    All chunks must share channel count, sample width, and rate; mixing formats
    would require a resampler and is deliberately out of scope for this slice.
    """
    if not chunk_paths:
        raise AudioError("no chunks to assemble")
    channels, sampwidth, rate = _wav_params(chunk_paths[0])
    silence_frame = b"\x00" * (sampwidth * channels)
    silence_frames = int(round(rate * (pause_ms / 1000.0))) if pause_ms > 0 else 0
    silence = silence_frame * silence_frames

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp = output_path.with_name(f".{output_path.stem}.tmp{output_path.suffix}")
    try:
        with wave.open(str(temp), "wb") as out:
            out.setnchannels(channels)
            out.setsampwidth(sampwidth)
            out.setframerate(rate)
            for position, path in enumerate(chunk_paths):
                c_channels, c_sampwidth, c_rate = _wav_params(path)
                if (c_channels, c_sampwidth, c_rate) != (channels, sampwidth, rate):
                    raise AudioError(
                        f"chunk {path.name} format {c_channels}ch/{c_sampwidth * 8}bit/{c_rate}Hz "
                        f"does not match {channels}ch/{sampwidth * 8}bit/{rate}Hz"
                    )
                with wave.open(str(path), "rb") as handle:
                    out.writeframes(handle.readframes(handle.getnframes()))
                if silence_frames and position < len(chunk_paths) - 1:
                    out.writeframes(silence)
        temp.replace(output_path)
        return output_path
    finally:
        temp.unlink(missing_ok=True)


def encode_mp3(wav_path: Path, mp3_path: Path) -> Path:
    """Transcode the master WAV to MP3 (delivery copy)."""
    binary = get_settings().ffmpeg.binary
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    temp = mp3_path.with_name(f".{mp3_path.stem}.tmp{mp3_path.suffix}")
    args = [binary, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav_path),
            "-codec:a", "libmp3lame", "-q:a", "2", str(temp)]
    try:
        completed = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=get_settings().ffmpeg.process_timeout_s)
        if completed.returncode != 0:
            raise AudioError("MP3 encode failed: " + completed.stderr.decode("utf-8", "replace").strip())
        if not temp.is_file() or temp.stat().st_size == 0:
            raise AudioError("MP3 encode produced no output")
        temp.replace(mp3_path)
        return mp3_path
    finally:
        temp.unlink(missing_ok=True)
