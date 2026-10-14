"""FFmpeg rendering and delivery-master quality control.

The renderer works from a frame-normalized EDL.  It never writes a delivery
pathname until FFmpeg and QC both succeed: work is rendered into a unique
sibling temporary file and atomically promoted with ``Path.replace``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.models.schemas import CAPTION_SAFE_WIDTH, SubtitleClip, TIMELINE_FPS, quantize_timeline_seconds

log = logging.getLogger(__name__)
CAPTION_SAFE_LEFT, CAPTION_SAFE_RIGHT = 72, 1008
CAPTION_SAFE_TOP, CAPTION_SAFE_BOTTOM = 180, 1620
CAPTION_DEFAULT_X, CAPTION_DEFAULT_Y = 540, 1500


class MasterQualityError(RuntimeError):
    """A file is not a valid delivery master."""


class FFmpegProcessTimeout(RuntimeError):
    """An FFmpeg/ffprobe process exceeded its configured hard limit."""


class FFmpegRunner:
    def __init__(self) -> None:
        cfg = get_settings().ffmpeg
        sm = get_settings().social_master
        self.binary, self.ffprobe_binary = cfg.binary, cfg.ffprobe_binary
        self.process_timeout_s = cfg.process_timeout_s
        self.video_codec, self.audio_codec = sm.video_codec, sm.audio_codec
        self.image_format = cfg.default_image_format
        self.sm_width, self.sm_height, self.sm_fps = sm.width, sm.height, sm.fps
        self.sm_vbitrate, self.sm_abitrate = sm.video_bitrate, sm.audio_bitrate
        self.sm_asr, self.sm_ach, self.sm_pix_fmt = sm.audio_sample_rate, sm.audio_channels, sm.pix_fmt
        self.sm_cs, self.sm_cp, self.sm_trc = sm.color_space, sm.color_primaries, sm.color_trc
        self._qc_by_path: dict[str, dict[str, Any]] = {}

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def ffprobe_available(self) -> bool:
        return shutil.which(self.ffprobe_binary) is not None

    async def _communicate(self, proc: asyncio.subprocess.Process, description: str) -> tuple[bytes, bytes]:
        try:
            return await asyncio.wait_for(proc.communicate(), timeout=self.process_timeout_s)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.communicate()
            raise FFmpegProcessTimeout(f"{description} timed out after {self.process_timeout_s}s") from exc

    async def _run(self, args: list[str], cwd: str | None = None) -> tuple[int, str]:
        cmd = [self.binary, *args]
        log.info("FFmpeg: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _stdout, stderr = await self._communicate(proc, "ffmpeg")
        return proc.returncode, (stderr or b"").decode("utf-8", "replace")

    async def _probe_json(self, path: Path) -> dict[str, Any]:
        if not self.ffprobe_available():
            raise MasterQualityError(f"ffprobe is not available: {self.ffprobe_binary}")
        if not path.is_file() or path.stat().st_size == 0:
            raise MasterQualityError(f"media source is missing or empty: {path}")
        proc = await asyncio.create_subprocess_exec(
            self.ffprobe_binary, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await self._communicate(proc, "ffprobe")
        if proc.returncode != 0:
            raise MasterQualityError(f"could not probe media: {stderr.decode('utf-8', 'replace').strip()}")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise MasterQualityError(f"ffprobe returned invalid JSON for {path}") from exc

    async def probe_duration(self, path: Path) -> float:
        probe = await self._probe_json(path)
        try:
            duration = float(probe.get("format", {}).get("duration", 0))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"could not read media duration for {path}") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError(f"media duration must be finite and positive: {path}")
        return duration

    async def _capability_text(self, option: str) -> str:
        proc = await asyncio.create_subprocess_exec(self.binary, "-hide_banner", option,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await self._communicate(proc, f"ffmpeg {option}")
        if proc.returncode != 0: return ""
        return (stdout + stderr).decode("utf-8", "replace")

    async def status(self) -> dict[str, Any]:
        if not self.available():
            return {"online": False, "binary": self.binary, "error": "ffmpeg executable is not discoverable"}
        try:
            rc, out = await self._run(["-version"])
            encoders, filters = await asyncio.gather(self._capability_text("-encoders"), self._capability_text("-filters"))
        except (FFmpegProcessTimeout, OSError) as exc:
            return {"online": False, "binary": self.binary, "error": str(exc)}
        features = {"libx264": "libx264" in encoders, "aac": bool(re.search(r"\baac\b", encoders)),
                    "ass": bool(re.search(r"\bass\b", filters)), "loudnorm": "loudnorm" in filters}
        return {"online": rc == 0, "binary": self.binary, "ffprobe_binary": self.ffprobe_binary,
                "ffprobe_available": self.ffprobe_available(), "version": out.splitlines()[0] if out else "",
                "features": features, "process_timeout_s": self.process_timeout_s,
                "master_spec": f"{self.sm_width}x{self.sm_height}@{self.sm_fps}fps h264/aac"}

    @staticmethod
    def _parse_rate(value: str | None) -> float:
        try:
            if not value:
                return 0.0
            numerator, denominator = value.split("/", 1)
            return float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            return 0.0

    @staticmethod
    def _stream_duration(stream: dict[str, Any]) -> float | None:
        try:
            value = float(stream.get("duration"))
            return value if math.isfinite(value) and value >= 0 else None
        except (ValueError, TypeError):
            return None

    async def _measure_loudness(self, path: Path) -> dict[str, Any]:
        rc, stderr = await self._run(["-i", str(path), "-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"])
        if rc != 0:
            raise MasterQualityError(f"loudness analysis failed:\n{stderr}")
        matches = re.findall(r"\{\s*\"input_i\".*?\}", stderr, flags=re.DOTALL)
        if not matches:
            raise MasterQualityError("loudness analysis produced no loudnorm measurements")
        try:
            stats = json.loads(matches[-1]); integrated, peak = float(stats["input_i"]), float(stats["input_tp"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if '"input_i" : "-inf"' in matches[-1] or '"input_i":"-inf"' in matches[-1]:
                return {"silent": True, "integrated_lufs": None, "true_peak_db": None, "raw": json.loads(matches[-1])}
            raise MasterQualityError("loudness analysis returned unusable measurements") from exc
        if not math.isfinite(integrated) or not math.isfinite(peak):
            if integrated == float("-inf"):
                return {"silent": True, "integrated_lufs": None, "true_peak_db": None, "raw": stats}
            raise MasterQualityError("loudness analysis returned non-finite measurements")
        if not -16.0 <= integrated <= -12.0:
            raise MasterQualityError(f"integrated loudness {integrated:.2f} LUFS is outside -14 ±2 LU")
        if peak > -0.5:
            raise MasterQualityError(f"true peak {peak:.2f} dBTP exceeds -0.5 dBTP")
        return {"silent": False, "integrated_lufs": integrated, "true_peak_db": peak, "raw": stats}

    async def quality_gate(self, path: Path, expected_duration_s: float | None = None) -> dict[str, Any]:
        """Require the full format and normalized-timeline delivery contract."""
        probe = await self._probe_json(path)
        streams = probe.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        errors: list[str] = []
        if video is None:
            errors.append("missing video stream")
        else:
            if video.get("codec_name") != "h264": errors.append(f"video codec is {video.get('codec_name')}, expected h264")
            if (int(video.get("width", 0)), int(video.get("height", 0))) != (self.sm_width, self.sm_height): errors.append("video dimensions do not match delivery profile")
            if video.get("pix_fmt") != self.sm_pix_fmt: errors.append(f"pixel format is {video.get('pix_fmt')}, expected {self.sm_pix_fmt}")
            fps = self._parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
            if abs(fps - self.sm_fps) > 0.02: errors.append(f"frame rate is {fps:g}, expected {self.sm_fps}")
            for key, expected in (("color_space", self.sm_cs), ("color_primaries", self.sm_cp), ("color_transfer", self.sm_trc)):
                if video.get(key) != expected: errors.append(f"{key} is {video.get(key)}, expected {expected}")
        if audio is None:
            errors.append("missing audio stream")
        else:
            if audio.get("codec_name") != "aac": errors.append(f"audio codec is {audio.get('codec_name')}, expected aac")
            if int(audio.get("sample_rate", 0)) != self.sm_asr: errors.append("audio sample rate does not match delivery profile")
            if int(audio.get("channels", 0)) != self.sm_ach: errors.append("audio channels do not match delivery profile")
        try: duration = float(probe.get("format", {}).get("duration", 0))
        except (ValueError, TypeError): duration = 0.0
        if not math.isfinite(duration) or duration <= 0: errors.append("container duration is not finite and positive")
        if expected_duration_s is not None:
            expected = quantize_timeline_seconds(expected_duration_s)
            # Both stream endpoints and container endpoint are checked. The video
            # tolerance is one encoded frame, while the audio/container tolerance
            # permits AAC encoder priming only.
            v_duration = self._stream_duration(video) if video else None
            a_duration = self._stream_duration(audio) if audio else None
            frame_tolerance = 1 / self.sm_fps + 0.001
            if v_duration is None or abs(v_duration - expected) > frame_tolerance:
                errors.append(f"video duration {v_duration} differs from normalized plan {expected:.6f}s")
            if a_duration is None or abs(a_duration - expected) > 0.025:
                errors.append(f"audio duration {a_duration} differs from normalized plan {expected:.6f}s")
            # ISO BMFF/AAC may report encoder-delay/trailing packets at the
            # container level. Permit up to three AAC packets at 48 kHz while
            # retaining stricter stream endpoint checks above.
            if abs(duration - expected) > 0.070:
                errors.append(f"container duration {duration:.6f}s differs from normalized plan {expected:.6f}s")
            frames = video.get("nb_frames") if video else None
            if frames not in (None, "N/A"):
                try:
                    expected_frames = round(expected * self.sm_fps)
                    if int(frames) != expected_frames: errors.append(f"video has {frames} frames, expected {expected_frames}")
                except (ValueError, TypeError):
                    errors.append("video frame count is invalid")
        if errors:
            raise MasterQualityError("delivery master quality gate failed: " + "; ".join(errors))
        loudness = await self._measure_loudness(path)
        metadata = {"passed": True, "expected_duration_s": quantize_timeline_seconds(expected_duration_s) if expected_duration_s is not None else None,
                    "duration_s": duration, "profile": {"width": self.sm_width, "height": self.sm_height, "fps": self.sm_fps,
                    "video_codec": "h264", "audio_codec": "aac", "sample_rate": self.sm_asr, "channels": self.sm_ach,
                    "pixel_format": self.sm_pix_fmt, "color": "rec709"}, "probe": probe, "loudness": loudness}
        self._qc_by_path[str(path.resolve())] = metadata
        return metadata

    def qc_metadata(self, path: Path) -> dict[str, Any] | None:
        return self._qc_by_path.get(str(path.resolve()))

    def _temp_path(self, destination: Path, role: str) -> Path:
        return destination.with_name(f".{destination.stem}.{role}.{uuid.uuid4().hex}{destination.suffix}")

    def _promote(self, source: Path, destination: Path) -> None:
        """Atomically replace destination and transfer its already-passed QC evidence."""
        qc = self.qc_metadata(source)
        if not qc or not qc.get("passed"):
            raise MasterQualityError("refusing to promote output without passed QC")
        source.replace(destination)
        self._qc_by_path.pop(str(source.resolve()), None)
        self._qc_by_path[str(destination.resolve())] = qc

    async def assemble(self, *, video_path: Path | None, audio_path: Path | None, music_path: Path | None,
                       subtitles_path: Path | None, output_path: Path, duration_s: float | None = None) -> Path:
        """Legacy single-video assembly, retained as a compatibility endpoint."""
        if video_path is None or not video_path.is_file():
            raise RuntimeError("FFmpeg assemble: a visual video input is required for a delivery master")
        if duration_s is None: duration_s = await self.probe_duration(video_path)
        duration_s = quantize_timeline_seconds(duration_s)
        if duration_s < 1 / TIMELINE_FPS: raise ValueError("duration_s must be at least one 30-fps frame")
        videos = [{"path": str(video_path), "start_s": 0.0, "end_s": duration_s, "trim_start_s": 0.0}]
        audios = [{"path": str(p), "start_s": 0.0, "end_s": duration_s, "trim_start_s": 0.0, "volume": 1.0}
                  for p in (audio_path, music_path) if p is not None and p.is_file()]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        base = self._temp_path(output_path, "legacy-base")
        muxed: Path | None = None
        try:
            await self.render_composition(video_paths=videos, audio_paths=audios, subtitles=[], total_duration_s=duration_s, output_path=base)
            if subtitles_path is not None and subtitles_path.is_file():
                muxed = self._temp_path(output_path, "subtitle")
                rc, err = await self._run(["-i", str(base), "-i", str(subtitles_path), "-map", "0:v", "-map", "0:a", "-map", "1:s:0",
                                           "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
                                           "-color_range:v", "tv", "-colorspace:v", self.sm_cs,
                                           "-color_primaries:v", self.sm_cp, "-color_trc:v", self.sm_trc,
                                           "-movflags", "+faststart+write_colr", "-y", str(muxed)])
                if rc != 0: raise RuntimeError(f"FFmpeg subtitle mux failed:\n{err}")
                await self.quality_gate(muxed, duration_s)
                self._promote(muxed, output_path)
            else:
                self._promote(base, output_path)
            return output_path
        finally:
            for path in (base, muxed):
                if path and path.exists(): path.unlink(missing_ok=True)
                if path: self._qc_by_path.pop(str(path.resolve()), None)

    async def render_preview_frame(self, video_path: Path, timestamp_s: float, subtitles: list[dict[str, Any]], output_path: Path) -> Path:
        temp = self._temp_path(output_path, "preview")
        args = ["-ss", f"{timestamp_s:.3f}", "-i", str(video_path), "-frames:v", "1", "-vf",
                f"scale={self.sm_width}:{self.sm_height}:force_original_aspect_ratio=decrease,pad={self.sm_width}:{self.sm_height}:(ow-iw)/2:(oh-ih)/2:color=black", "-y", str(temp)]
        try:
            rc, err = await self._run(args)
            if rc != 0: raise RuntimeError(f"FFmpeg frame extraction failed:\n{err}")
            temp.replace(output_path)
            return output_path
        finally:
            temp.unlink(missing_ok=True)

    async def render_composition(self, *, video_paths: list[dict[str, Any]], audio_paths: list[dict[str, Any]],
                                 subtitles: list[dict[str, Any]], total_duration_s: float, output_path: Path, proxy: bool = False) -> Path:
        """Render a normalized 30-fps EDL and atomically promote only QC output."""
        # Proxy renders use a deliberately smaller/faster web profile, while the
        # identical EDL and caption logic is retained for delivery.
        out_width, out_height, out_fps, out_vbitrate = ((540, 960, 15, "900k") if proxy
                                                         else (self.sm_width, self.sm_height, self.sm_fps, self.sm_vbitrate))
        total_duration_s = quantize_timeline_seconds(total_duration_s)
        if total_duration_s < 1 / TIMELINE_FPS: raise ValueError("total_duration_s must be at least one 30-fps frame")
        video_paths = [dict(c) for c in sorted(video_paths, key=lambda c: float(c.get("start_s", 0)))]
        audio_paths = [dict(c) for c in sorted(audio_paths, key=lambda c: float(c.get("start_s", 0)))]

        async def resolve_end(clip: dict[str, Any], label: str) -> None:
            try: start, trim_start = quantize_timeline_seconds(clip.get("start_s", 0)), quantize_timeline_seconds(clip.get("trim_start_s", 0))
            except (TypeError, ValueError) as exc: raise ValueError(f"{label} start values must be numeric and finite") from exc
            if start < 0 or trim_start < 0: raise ValueError(f"{label} start values must be non-negative")
            source_duration = await self.probe_duration(Path(str(clip.get("path", ""))))
            available = source_duration - trim_start
            if available <= 0: raise ValueError(f"{label} trim_start_s is beyond source duration")
            end = total_duration_s if clip.get("end_s") is None else quantize_timeline_seconds(clip["end_s"])
            if end <= start: raise ValueError(f"{label} end_s must be greater than start_s")
            if end > total_duration_s: raise ValueError(f"{label} end_s exceeds total_duration_s")
            if end - start > available + 0.01: raise ValueError(f"{label} range exceeds source duration after trim")
            try: volume = float(clip.get("volume", 1.0))
            except (TypeError, ValueError) as exc: raise ValueError(f"{label} volume must be numeric") from exc
            if not math.isfinite(volume) or not 0 <= volume <= 4: raise ValueError(f"{label} volume must be between 0 and 4")
            clip.update(start_s=start, trim_start_s=trim_start, end_s=end, volume=volume)

        for i, clip in enumerate(video_paths): await resolve_end(clip, f"video clip {i}")
        for i, clip in enumerate(audio_paths): await resolve_end(clip, f"audio clip {i}")
        # Validation is repeated here because direct service consumers can bypass Pydantic.
        normalized_subtitles = [SubtitleClip(**raw).model_dump() for raw in subtitles]
        for sub in normalized_subtitles:
            sub["start_s"], sub["end_s"] = quantize_timeline_seconds(sub["start_s"]), quantize_timeline_seconds(sub["end_s"])
            if sub["end_s"] <= sub["start_s"] or sub["end_s"] > total_duration_s: raise ValueError("subtitle interval must be within composition duration")

        args: list[str] = ["-f", "lavfi", "-t", _time(total_duration_s), "-i", f"color=c=black:s={out_width}x{out_height}:r={out_fps}",
                           "-f", "lavfi", "-t", _time(total_duration_s), "-i", f"anullsrc=r={self.sm_asr}:cl=stereo"]
        for clip in video_paths: args += ["-i", str(clip["path"])]
        for clip in audio_paths: args += ["-i", str(clip["path"])]
        filters = ["[0:v]setpts=PTS-STARTPTS[base0]"]; visual = "base0"
        for i, clip in enumerate(video_paths):
            duration, index = clip["end_s"] - clip["start_s"], 2 + i
            filters.append(f"[{index}:v:0]trim=start={_time(clip['trim_start_s'])}:end={_time(clip['trim_start_s'] + duration)},setpts=PTS-STARTPTS,scale={out_width}:{out_height}:force_original_aspect_ratio=decrease,pad={out_width}:{out_height}:(ow-iw)/2:(oh-ih):color=black,fps={out_fps},setsar=1,setpts=PTS+{_time(clip['start_s'])}/TB[v{i}]")
            filters.append(f"[{visual}][v{i}]overlay=eof_action=pass:shortest=0:repeatlast=0[base{i + 1}]"); visual = f"base{i + 1}"
        filters.append(f"[1:a]atrim=duration={_time(total_duration_s)},asetpts=PTS-STARTPTS[silence]")
        audio_labels = ["[silence]"]
        for j, clip in enumerate(audio_paths):
            duration, index = clip["end_s"] - clip["start_s"], 2 + len(video_paths) + j
            delay_ms = round(clip["start_s"] * 1000)
            filters.append(f"[{index}:a:0]atrim=start={_time(clip['trim_start_s'])}:end={_time(clip['trim_start_s'] + duration)},asetpts=PTS-STARTPTS,aresample={self.sm_asr},aformat=sample_rates={self.sm_asr}:sample_fmts=fltp:channel_layouts=stereo,volume={clip['volume']:.4f},adelay={delay_ms}:all=1[a{j}]")
            audio_labels.append(f"[a{j}]")
        if audio_paths:
            filters.append("".join(audio_labels) + f"amix=inputs={len(audio_labels)}:duration=longest:dropout_transition=0:normalize=0,atrim=duration={_time(total_duration_s)},loudnorm=I=-14:TP=-1:LRA=11,aresample={self.sm_asr},aformat=sample_rates={self.sm_asr}:sample_fmts=fltp:channel_layouts=stereo[aout]")
        else:
            # FFmpeg 9's loudnorm filter can consume a wholly silent, short
            # stream without emitting frames, leaving AAC with an empty queue.
            # Silence needs no gain normalization; retain it directly so every
            # delivery master still has the required finite stereo AAC track.
            filters.append(f"[silence]aresample={self.sm_asr},aformat=sample_rates={self.sm_asr}:sample_fmts=fltp:channel_layouts=stereo[aout]")
        temp = self._temp_path(output_path, "render"); ass_path: Path | None = None
        if normalized_subtitles:
            ass_path = self._temp_path(output_path, "captions").with_suffix(".ass")
            self._write_ass(ass_path, normalized_subtitles, total_duration_s)
            filters.append(f"[{visual}]ass={_escape_filter_path(ass_path.name)}[vout]"); visual = "vout"
        # Carry Rec.709 through the final filter output, H.264 VUI, and MP4 colr
        # atom. Generic output tags alone are not retained reliably by every
        # FFmpeg/libx264 build (notably some Windows FFmpeg 9 distributions).
        filters.append(
            f"[{visual}]setparams=range=tv:colorspace={self.sm_cs}:"
            f"color_primaries={self.sm_cp}:color_trc={self.sm_trc}[vprofile]"
        )
        visual = "vprofile"
        x264_profile = (
            f"colormatrix={self.sm_cs}:colorprim={self.sm_cp}:"
            f"transfer={self.sm_trc}:fullrange=off"
        )
        args += ["-filter_complex", ";".join(filters), "-map", f"[{visual}]", "-map", "[aout]", "-c:v", "libx264",
                 "-x264-params", x264_profile, "-b:v", out_vbitrate, "-r", str(out_fps), "-pix_fmt", self.sm_pix_fmt,
                 "-color_range:v", "tv", "-colorspace:v", self.sm_cs, "-color_primaries:v", self.sm_cp, "-color_trc:v", self.sm_trc,
                 "-c:a", "aac", "-b:a", self.sm_abitrate, "-ar", str(self.sm_asr), "-ac", str(self.sm_ach),
                 "-movflags", "+faststart+write_colr", "-t", _time(total_duration_s), "-y", str(temp)]
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            rc, err = await self._run(args, cwd=str(output_path.parent))
            if rc != 0: raise RuntimeError(f"FFmpeg composition render failed:\n{err}")
            if not temp.is_file() or temp.stat().st_size == 0: raise RuntimeError("FFmpeg composition render completed without an output file")
            if proxy:
                # A proxy is explicitly non-delivery evidence: verify that FFmpeg
                # created bytes, then promote without master QC metadata.
                temp.replace(output_path)
            else:
                await self.quality_gate(temp, total_duration_s)
                self._promote(temp, output_path)
            return output_path
        finally:
            temp.unlink(missing_ok=True)
            self._qc_by_path.pop(str(temp.resolve()), None)
            if ass_path: ass_path.unlink(missing_ok=True)

    @staticmethod
    def _write_ass(ass_path: Path, subtitles: list[dict[str, Any]], total_duration_s: float) -> None:
        header = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n[V4+ Styles]\n"
                  "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n")
        events = "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        styles, dialogue = [], []
        for i, raw in enumerate(subtitles):
            sub = SubtitleClip(**raw)
            start, end = quantize_timeline_seconds(sub.start_s), quantize_timeline_seconds(sub.end_s)
            if end <= start or end > total_duration_s: raise ValueError("subtitle time range must be within composition")
            x = sub.x or (CAPTION_SAFE_LEFT if sub.align == "left" else CAPTION_DEFAULT_X if sub.align == "center" else CAPTION_SAFE_RIGHT)
            y = sub.y or CAPTION_DEFAULT_Y
            primary = _ass_color(sub.color, "&H00FFFFFF")
            background = _ass_color(sub.bg_color, "&H80000000", allow_opacity=True)
            # BorderStyle 3 gives every caption a real opaque/alpha background.
            # Height controls its padding, after the two-line text geometry was
            # validated by SubtitleClip; alignment controls the anchor exactly.
            padding = max(2, (sub.height - math.ceil(sub.font_size * 2)) // 2)
            alignment = {"left": 4, "center": 5, "right": 6}[sub.align]
            style = f"Caption{i}"
            styles.append(f"Style: {style},Arial,{sub.font_size},{primary},{primary},{background},{background},-1,0,0,0,100,100,0,0,3,{padding},0,{alignment},72,72,120,1")
            text = _wrap_ass_caption(sub.text, sub.width, sub.font_size)
            dialogue.append(f"Dialogue: 0,{_fmt_ass_time(start)},{_fmt_ass_time(end)},{style},,0,0,0,,{{\\an{alignment}\\pos({x},{y})}}{text}")
        # Styles and dialogue are constructed independently so untrusted caption
        # text can never alter ASS headers or event records.
        ass_path.write_text(header + "\n".join(styles) + events + "\n".join(dialogue) + "\n", encoding="utf-8")

    async def encode_web_video(self, src: Path, dest: Path) -> Path:
        duration = await self.probe_duration(src)
        return await self.render_composition(video_paths=[{"path": str(src), "start_s": 0, "end_s": duration, "trim_start_s": 0}], audio_paths=[], subtitles=[], total_duration_s=duration, output_path=dest)


def _time(value: float) -> str:
    return f"{value:.6f}"


def _fmt_ass_time(seconds: float) -> str:
    h = int(seconds // 3600); m = int((seconds % 3600) // 60); sec = seconds % 60
    return f"{h}:{m:02d}:{sec:05.2f}"


def _escape_filter_path(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _ass_color(value: str, default: str, *, allow_opacity: bool = False) -> str:
    named = {"white": "FFFFFF", "black": "000000", "red": "FF0000", "green": "00FF00", "blue": "0000FF", "yellow": "FFFF00", "cyan": "00FFFF", "magenta": "FF00FF"}
    raw, sep, opacity = value.lower().strip().partition("@")
    rgb = named.get(raw, raw[1:] if re.fullmatch(r"#[0-9a-f]{6}", raw) else None)
    if rgb is None or (sep and not allow_opacity):
        raise ValueError(f"unsupported ASS color: {value}")
    try: alpha = 0 if not sep else round((1 - float(opacity)) * 255)
    except ValueError as exc: raise ValueError(f"unsupported opacity in color: {value}") from exc
    if not 0 <= alpha <= 255: raise ValueError(f"color opacity must be between 0 and 1: {value}")
    # ASS uses AABBGGRR.
    return f"&H{alpha:02X}{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}"


def _wrap_ass_caption(text: str, width: int, font_size: int) -> str:
    # SubtitleClip already checks the exact same conservative geometry.
    cleaned = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    max_chars = max(8, int(width / (font_size * 0.65)))
    words, lines, current = cleaned.split(" "), [], ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= max_chars: current = candidate
        else: lines.append(current); current = word
    if current: lines.append(current)
    if len(lines) > 2 or any(len(word) > max_chars for word in words):
        raise ValueError("caption cannot be safely rendered in its declared box")
    escaped = [line.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}") for line in lines]
    return "\\N".join(escaped)


_runner: FFmpegRunner | None = None


def get_ffmpeg() -> FFmpegRunner:
    global _runner
    if _runner is None: _runner = FFmpegRunner()
    return _runner
