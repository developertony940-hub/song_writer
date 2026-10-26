"""Local, append-only audio edit decisions and CPU-only master exports.

The core intentionally uses only the standard library. No music/generation
service, arrangement data, model, network client, or mutable project manifest
is involved. See docs/MASTER-AUDIO.md for the persistence and threat model.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import threading
import uuid
import wave
from array import array
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MAX_BYTES = 64 * 1024 * 1024
MAX_DURATION = 1800
MAX_TOTAL_SOURCE_DURATION = 7200
MAX_SOURCES = 32
MAX_SECTIONS = 64
MAX_REVISIONS = 500
RATE = 48000
CHANNELS = 2
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SHA_RE = re.compile(r"^[a-f0-9]{64}$")
FORMATS = {".wav": "wav", ".mp3": "mp3", ".flac": "flac", ".ogg": "ogg", ".m4a": "mov", ".aac": "aac"}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class MasterAudioError(Exception):
    def __init__(self, message: str, code: str = "validation", status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


def fail(message, code="validation", status=422):
    raise MasterAudioError(message, code, status)


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sha_file(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def wav_quality(path):
    """Measure actual interleaved master PCM, not recipe gain or source peaks.

    A rail-valued sample is evidence of zero headroom, not proof of clipping.
    Null represents negative-infinity dBFS for digital silence (JSON has no inf).
    """
    peak, full_scale, total = 0, 0, 0
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (CHANNELS, 2, RATE):
            fail("quality measurement PCM format mismatch", "processing_failed", 502)
        expected_samples = audio.getnframes() * CHANNELS
        while block := audio.readframes(65536):
            samples = array("h", block)
            if os.sys.byteorder != "little":
                samples.byteswap()
            low, high = min(samples), max(samples)
            peak = max(peak, abs(low), abs(high))
            full_scale += samples.count(-32768) + samples.count(32767)
            total += len(samples)
    if total != expected_samples or not total:
        fail("quality measurement PCM truncated or empty", "processing_failed", 502)
    peak_dbfs = 20 * math.log10(peak / 32768) if peak else None
    warnings = []
    if full_scale:
        warnings.append("Digital full-scale samples are present: this indicates no sample headroom and possible clipping risk, not proof of clipping. Listen and review gain before release.")
    if peak_dbfs is not None and peak_dbfs > -1:
        warnings.append("Sample peak exceeds -1 dBFS; MP3 encoding or playback reconstruction may create overs. This is not a true-peak measurement.")
    return {"basis": "master_wav_pcm_s16le", "sample_peak_dbfs": peak_dbfs,
            "full_scale_samples": full_scale, "total_samples": total, "warnings": warnings}


def is_link(path):
    # Windows junctions are reparse points but not necessarily symlinks.
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except FileNotFoundError:
        return False


def confined(root: Path, relative: str, *, exists=True):
    """Reject every symlink component, Windows path syntax, and traversal."""
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative or "\x00" in relative:
        fail("invalid relative path")
    parts = relative.split("/")
    if any(p in {"", ".", ".."} for p in parts) or PurePosixPath(relative).is_absolute():
        fail("path must be project-relative without traversal")
    path = root
    for part in parts:
        path = path / part
        if is_link(path):
            fail("symlinks are not allowed", "unsafe_path", 409)
        if path.exists() and not (path.is_dir() or path.is_file()):
            fail("special files are not allowed", "unsafe_path", 409)
    if not path.resolve().is_relative_to(root.resolve()):
        fail("path escapes project", "unsafe_path", 409)
    if exists and not path.is_file():
        fail("file not found", "not_found", 404)
    return path


def read_json(path):
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            fail("oversized evidence record", "evidence_invalid", 409)
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"unreadable evidence record {path.name}: {exc}", "evidence_invalid", 409)


def write_new(path, value):
    """O_EXCL records never replace evidence; partial writes fail closed."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        fail("evidence already exists; no overwrite/retry allowed", "conflict", 409)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
        fail(f"{name} must be a finite number from {low} to {high}")
    return float(value)


def keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        fail("object has missing or unsupported fields")


class MasterAudioService:
    def __init__(self, store=None, settings=None):
        if store is None:
            from app.services.project_store import get_store
            store = get_store()
        if settings is None:
            from app.config import get_settings
            settings = get_settings()
        self.store = store
        self.ffmpeg = settings.ffmpeg.binary
        self.timeout = min(7200, max(1, settings.ffmpeg.process_timeout_s))

    def _project(self, pid):
        if not isinstance(pid, str) or not re.fullmatch(r"[a-f0-9]{12}", pid):
            fail("invalid project id", "not_found", 404)
        root = Path(self.store.root)
        if is_link(root) or is_link(root / pid):
            fail("symlink project root forbidden", "unsafe_path", 409)
        # Current export exposes _dir, not project_dir. Support the public
        # method if a future store adds it; do not invent a new store API.
        try:
            method = getattr(self.store, "project_dir", None) or self.store._dir
            project = Path(method(pid))
        except KeyError:
            fail("project not found", "not_found", 404)
        if project.resolve() != (root.resolve() / pid) or not project.is_dir():
            fail("project escapes store root", "unsafe_path", 409)
        # Resolve trusted root ancestors only after rejecting project symlinks.
        return project.resolve()

    def _root(self, pid):
        project = self._project(pid)
        root = confined(project, "master-audio", exists=False)
        root.mkdir(exist_ok=True)
        for name in ("sources", "revisions", "renders"):
            confined(root, name, exists=False).mkdir(exist_ok=True)
        return root

    @contextmanager
    def _locked(self, pid):
        root = self._root(pid)
        with _locks_guard:
            mutex = _locks.setdefault(str(root), threading.Lock())
        if not mutex.acquire(blocking=False):
            fail("another master-audio operation is active", "busy", 409)
        stream = None
        try:
            path = confined(root, ".lock", exists=False)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                fail("invalid lock file", "unsafe_path", 409)
            stream = os.fdopen(fd, "r+b")
            if os.fstat(fd).st_size == 0:
                stream.write(b"0")
                stream.flush()
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fail("another process holds master-audio lock", "busy", 409)
            yield root
        finally:
            if stream is not None:
                stream.close()  # OS releases lock, even after process death.
            mutex.release()

    def _run(self, argv, directory, evidence):
        """CPU-only argv; bounded stderr goes directly to an evidence file."""
        log = confined(directory, evidence, exists=False)
        write_new(directory / (evidence + ".argv.json"), argv)
        try:
            with log.open("xb") as stderr:
                subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=stderr, check=True, timeout=self.timeout, shell=False)
        except FileNotFoundError:
            fail("configured ffmpeg binary unavailable", "tool_unavailable", 503)
        except (subprocess.SubprocessError, OSError) as exc:
            fail(f"local audio processing failed; evidence preserved ({type(exc).__name__})", "processing_failed", 502)

    def _base(self):
        return [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-threads", "1", "-filter_threads", "1", "-filter_complex_threads", "1"]

    def _records(self, root, kind):
        directory = confined(root, kind, exists=False)
        rows = []
        for item in sorted(directory.iterdir()):
            if kind == "revisions":
                if not re.fullmatch(r"\d{6}\.json", item.name):
                    fail("unexpected revision evidence", "evidence_invalid", 409)
                rows.append(read_json(confined(root, f"{kind}/{item.name}")))
            else:
                if is_link(item) or not item.is_dir():
                    fail("unexpected source/render evidence", "evidence_invalid", 409)
                name = "source.json" if kind == "sources" else "claim.json"
                path = confined(root, f"{kind}/{item.name}/{name}", exists=False)
                if not path.is_file():
                    # Interrupted imports remain visible, never reused silently.
                    rows.append({"status": "uncertain", "source_id" if kind == "sources" else "render_id": item.name})
                else:
                    row = read_json(path)
                    if kind == "renders":
                        result = confined(root, f"{kind}/{item.name}/result.json", exists=False)
                        row = {**row, **read_json(result)} if result.is_file() else {**row, "status": "uncertain"}
                    rows.append(row)
        return rows

    def _revisions(self, root):
        rows = self._records(root, "revisions")
        parent = None
        for i, row in enumerate(rows, 1):
            if row.get("revision") != i or row.get("parent_revision") != i - 1 or row.get("parent_recipe_sha256") != parent:
                fail("revision chain is invalid", "evidence_invalid", 409)
            recipe = {"sections": row.get("sections"), "note": row.get("note")}
            if row.get("recipe_sha256") != digest(recipe):
                fail("revision recipe hash is invalid", "evidence_invalid", 409)
            parent = row["recipe_sha256"]
        return rows

    def _expected(self, root, expected):
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            fail("expected_revision must be a nonnegative integer")
        rows = self._revisions(root)
        if len(rows) != expected:
            fail(f"revision conflict: current revision is {len(rows)}", "revision_conflict", 409)
        return rows

    def _url(self, pid, path):
        return f"/api/projects/{pid}/master-audio/files/{path}"

    def _source(self, pid, root, source_id, verify=True):
        if not isinstance(source_id, str) or not SHA_RE.fullmatch(source_id):
            fail("invalid source id")
        record = read_json(confined(root, f"sources/{source_id}/source.json"))
        if record.get("source_id") != source_id or record.get("sha256") != source_id:
            fail("invalid source binding", "evidence_invalid", 409)
        if verify:
            for key, expected in (("raw_path", source_id), ("pcm_path", record["pcm_sha256"])):
                path = confined(root, record[key])
                if sha_file(path) != expected:
                    fail("immutable source bytes changed", "stale_source", 409)
            original = record.get("original_relative_path")
            if original is not None:
                path = confined(self._project(pid), original)
                if path.stat().st_size > MAX_BYTES or sha_file(path) != source_id:
                    fail("original asset changed; register its new bytes and reconfirm clips", "stale_source", 409)
        return record

    def status(self, pid):
        with self._locked(pid) as root:
            revisions = self._revisions(root)
            sources = self._records(root, "sources")
            for source in sources:
                if source.get("status") == "uncertain":
                    continue
                try:
                    self._source(pid, root, source["source_id"])
                    source["status"] = "ready"
                except MasterAudioError as exc:
                    source["status"], source["error"] = "stale", str(exc)
            return {"schema_version": 1, "revision": len(revisions), "sources": sources,
                    "revisions": revisions, "renders": self._records(root, "renders"),
                    "limits": {"max_bytes": MAX_BYTES, "max_duration_s": MAX_DURATION,
                               "max_sections": MAX_SECTIONS, "max_sources": MAX_SOURCES,
                               "max_revisions": MAX_REVISIONS},
                    "generation": False, "listening_confirmation_is_user_attestation": True}

    def register_source(self, pid, request):
        keys(request, {"relative_path"}, {"relative_path"})
        relative = request["relative_path"]
        with self._locked(pid) as root:
            path = confined(self._project(pid), relative)
            if relative.split("/")[0] not in {"audio", "music"}:
                fail("source must be an existing project audio/ or music/ asset")
            return self._import(pid, root, path, relative)

    def upload_source(self, pid, filename, payload):
        if (not isinstance(filename, str) or not filename.strip() or len(filename) > 255
                or filename in {".", ".."} or any(c in filename for c in ("/", "\\", ":", "\x00"))
                or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
            fail("filename must be an audio basename without separators, drive syntax, or control characters")
        extension = Path(filename).suffix.lower()
        if extension not in FORMATS:
            fail("unsupported uploaded audio extension")
        if not payload or len(payload) > MAX_BYTES:
            fail("empty or oversized upload", "size_limit", 413)
        with self._locked(pid) as root:
            # Upload staging is private; never trust a caller filesystem path.
            staging = confined(root, f"upload-{uuid.uuid4().hex}{extension}", exists=False)
            try:
                with staging.open("xb") as stream:
                    stream.write(payload)
                return self._import(pid, root, staging, None, filename)
            finally:
                staging.unlink(missing_ok=True)

    def _import(self, pid, root, original, relative, original_filename=None):
        extension = original.suffix.lower()
        if extension not in FORMATS:
            fail("supported audio extensions: " + ", ".join(FORMATS))
        size = original.stat().st_size
        if not 0 < size <= MAX_BYTES:
            fail("empty or oversized audio asset", "size_limit", 413)
        source_id = sha_file(original)
        directory = confined(root, f"sources/{source_id}", exists=False)
        if directory.exists():
            if not confined(root, f"sources/{source_id}/source.json", exists=False).is_file():
                fail("source import is incomplete/uncertain; preserve evidence for manual review", "source_uncertain", 409)
            return self._source(pid, root, source_id)
        sources = self._records(root, "sources")
        if len(sources) >= MAX_SOURCES:
            fail("source count limit reached")
        if sum(s.get("duration_s", 0) for s in sources) >= MAX_TOTAL_SOURCE_DURATION:
            fail("total source duration limit reached")
        directory.mkdir()
        raw = directory / ("raw" + extension)
        # Copy through no-follow fd; stat before/after + SHA detect mutable
        # generation output changing while being registered.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(original, flags), "rb") as src, raw.open("xb") as dst:
            before = os.fstat(src.fileno())
            if not stat.S_ISREG(before.st_mode):
                fail("source must be a regular file")
            total = 0
            while block := src.read(1024 * 1024):
                total += len(block)
                if total > MAX_BYTES:
                    fail("source grew beyond byte limit", "size_limit", 413)
                dst.write(block)
            after = os.fstat(src.fileno())
            dst.flush()
            os.fsync(dst.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or sha_file(raw) != source_id or sha_file(original) != source_id:
            fail("source changed during import; evidence retained", "stale_source", 409)
        pcm = directory / "pcm.wav"
        argv = self._base() + ["-protocol_whitelist", "file,pipe", "-f", FORMATS[extension], "-i", str(raw),
                "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1", "-t", str(MAX_DURATION + 0.01),
                "-ar", str(RATE), "-ac", str(CHANNELS), "-c:a", "pcm_s16le", str(pcm)]
        self._run(argv, directory, "decode.stderr")
        try:
            with wave.open(str(pcm), "rb") as audio:
                frames = audio.getnframes()
        except (wave.Error, OSError):
            fail("decoded audio is not valid PCM", "processing_failed", 502)
        duration = frames / RATE
        if not 0 < duration <= MAX_DURATION or duration + sum(s.get("duration_s", 0) for s in sources) > MAX_TOTAL_SOURCE_DURATION:
            fail("decoded source exceeds duration limits; evidence retained")
        record = {"source_id": source_id, "sha256": source_id, "created_at": now(),
                  "original_relative_path": relative, "original_filename": original_filename or original.name,
                  "byte_size": size, "duration_s": duration,
                  "sample_rate": RATE, "channels": CHANNELS, "frames": frames,
                  "raw_path": f"sources/{source_id}/{raw.name}", "pcm_path": f"sources/{source_id}/pcm.wav",
                  "pcm_sha256": sha_file(pcm), "duration_basis": "decoded_pcm_frames", "generation": False}
        record["file_url"] = self._url(pid, record["raw_path"])
        record["pcm_url"] = self._url(pid, record["pcm_path"])
        write_new(directory / "source.json", record)
        return record

    def _sections(self, pid, root, sections):
        if not isinstance(sections, list) or not 1 <= len(sections) <= MAX_SECTIONS:
            fail(f"sections must contain 1 to {MAX_SECTIONS} clips")
        result, seen, sources, total = [], set(), {}, 0
        for clip in sections:
            keys(clip, {"clip_id", "source_id", "start_s", "end_s", "gain_db", "fade_in_s", "fade_out_s", "label", "confirmation"},
                 {"clip_id", "source_id", "start_s", "end_s", "confirmation"})
            clip_id = clip["clip_id"]
            if not isinstance(clip_id, str) or not ID_RE.fullmatch(clip_id) or clip_id in seen:
                fail("clip_id must be safe and unique within recipe")
            seen.add(clip_id)
            sid = clip["source_id"]
            if not isinstance(sid, str):
                fail("invalid source id")
            if sid not in sources:
                sources[sid] = self._source(pid, root, sid)
            source = sources[sid]
            start = number(clip["start_s"], "start_s", 0, source["duration_s"])
            end = number(clip["end_s"], "end_s", 0, source["duration_s"])
            a, b = math.floor(start * RATE + 0.5), math.floor(end * RATE + 0.5)
            if b <= a or b > source["frames"]:
                fail("clip range must contain at least one source PCM frame")
            length = (b - a) / RATE
            gain = number(clip.get("gain_db", 0), "gain_db", -60, 12)
            fade_in = number(clip.get("fade_in_s", 0), "fade_in_s", 0, length)
            fade_out = number(clip.get("fade_out_s", 0), "fade_out_s", 0, length)
            if fade_in + fade_out > length:
                fail("fades may not overlap within a clip")
            label = clip.get("label", "")
            if not isinstance(label, str) or len(label) > 120:
                fail("label must be text up to 120 characters")
            confirmation = clip["confirmation"]
            keys(confirmation, {"confirmed", "source_sha256", "start_s", "end_s"}, {"confirmed", "source_sha256", "start_s", "end_s"})
            if confirmation["confirmed"] is not True or confirmation["source_sha256"] != sid:
                fail("explicit listening confirmation with exact source SHA is required", "confirmation_required", 409)
            if number(confirmation["start_s"], "confirmation.start_s", 0, source["duration_s"]) != start or number(confirmation["end_s"], "confirmation.end_s", 0, source["duration_s"]) != end:
                fail("listening confirmation range is stale", "confirmation_required", 409)
            normalized = {"clip_id": clip_id, "source_id": sid, "source_sha256": sid,
                          "start_s": start, "end_s": end, "start_frame": a, "end_frame": b,
                          "gain_db": gain, "fade_in_s": fade_in, "fade_out_s": fade_out, "label": label}
            normalized["confirmation"] = {**confirmation, "confirmed_at": now(), "clip_sha256": digest(normalized)}
            result.append(normalized)
            total += length
        if total > MAX_DURATION:
            fail("master duration limit exceeded")
        return result, sources, total

    def create_revision(self, pid, request):
        keys(request, {"expected_revision", "sections", "note"}, {"expected_revision", "sections"})
        with self._locked(pid) as root:
            rows = self._expected(root, request["expected_revision"])
            if len(rows) >= MAX_REVISIONS:
                fail("revision limit reached")
            sections, sources, duration = self._sections(pid, root, request["sections"])
            note = request.get("note", "")
            if not isinstance(note, str) or len(note) > 2000:
                fail("note must be text up to 2000 characters")
            record = {"revision": len(rows) + 1, "parent_revision": len(rows),
                      "parent_recipe_sha256": rows[-1]["recipe_sha256"] if rows else None,
                      "created_at": now(), "sections": sections, "note": note, "duration_s": duration,
                      "source_ids": sorted(sources), "generation": False}
            record["recipe_sha256"] = digest({"sections": sections, "note": note})
            write_new(root / "revisions" / f"{record['revision']:06d}.json", record)
            return record

    def render(self, pid, request):
        keys(request, {"expected_revision", "render_id"}, {"expected_revision", "render_id"})
        rid = request["render_id"]
        if not isinstance(rid, str) or not ID_RE.fullmatch(rid):
            fail("render_id must be a safe unique identifier")
        with self._locked(pid) as root:
            rows = self._expected(root, request["expected_revision"])
            if not rows:
                fail("save a confirmed recipe first", "confirmation_required", 409)
            revision = rows[-1]
            prior = self._records(root, "renders")
            if any(r.get("revision") == revision["revision"] or r.get("render_id") == rid for r in prior):
                fail("this revision or render id already has a claim; retries are forbidden", "render_claimed", 409)
            # An interrupted mkdir before claim must also block all rendering:
            # its intended revision cannot safely be reconstructed.
            if any(r.get("revision") is None for r in prior):
                fail("uncertain render claim requires manual evidence review", "render_claimed", 409)
            sources = {sid: self._source(pid, root, sid) for sid in revision["source_ids"]}
            for clip in revision["sections"]:
                without = {k: v for k, v in clip.items() if k != "confirmation"}
                if clip["confirmation"].get("confirmed") is not True or clip["confirmation"].get("clip_sha256") != digest(without):
                    fail("clip confirmation invalid", "confirmation_required", 409)
            directory = confined(root, f"renders/{rid}", exists=False)
            directory.mkdir()
            claim = {"schema_version": 1, "render_id": rid, "revision": revision["revision"],
                     "recipe_sha256": revision["recipe_sha256"], "parent_revision": revision["parent_revision"],
                     "source_ids": revision["source_ids"], "source_hashes": {sid: sources[sid]["sha256"] for sid in sources},
                     "claimed_at": now(), "status": "claimed", "generation": False}
            write_new(directory / "claim.json", claim)
            write_new(directory / "recipe.json", revision)
            try:
                clips = []
                for index, clip in enumerate(revision["sections"]):
                    source = sources[clip["source_id"]]
                    output = directory / f"clip-{index:03d}.wav"
                    length = (clip["end_frame"] - clip["start_frame"]) / RATE
                    filters = [f"atrim=start_sample={clip['start_frame']}:end_sample={clip['end_frame']}",
                               "asetpts=PTS-STARTPTS", f"volume={clip['gain_db']:.9f}dB"]
                    if clip["fade_in_s"]:
                        filters.append(f"afade=t=in:st=0:d={clip['fade_in_s']:.9f}")
                    if clip["fade_out_s"]:
                        filters.append(f"afade=t=out:st={length - clip['fade_out_s']:.9f}:d={clip['fade_out_s']:.9f}")
                    argv = self._base() + ["-protocol_whitelist", "file,pipe", "-f", "wav", "-i", str(confined(root, source["pcm_path"])),
                        "-map", "0:a:0", "-af", ",".join(filters), "-map_metadata", "-1", "-c:a", "pcm_s16le", str(output)]
                    self._run(argv, directory, f"clip-{index:03d}.stderr")
                    clips.append(output)
                wav_path = directory / "master.wav"
                with wav_path.open("xb") as stream:
                    with wave.open(stream, "wb") as master:
                        master.setnchannels(CHANNELS)
                        master.setsampwidth(2)
                        master.setframerate(RATE)
                        for path in clips:
                            with wave.open(str(path), "rb") as audio:
                                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (CHANNELS, 2, RATE):
                                    fail("rendered clip format mismatch", "processing_failed", 502)
                                while block := audio.readframes(65536):
                                    master.writeframesraw(block)
                    stream.flush()
                    os.fsync(stream.fileno())
                with wave.open(str(wav_path), "rb") as audio:
                    frames = audio.getnframes()
                if frames != sum(c["end_frame"] - c["start_frame"] for c in revision["sections"]):
                    fail("master frame count mismatch", "processing_failed", 502)
                quality = wav_quality(wav_path)
                quality_path = directory / "quality.json"
                write_new(quality_path, quality)
                mp3_path = directory / "master.mp3"
                self._run(self._base() + ["-protocol_whitelist", "file,pipe", "-f", "wav", "-i", str(wav_path),
                    "-map", "0:a:0", "-map_metadata", "-1", "-c:a", "libmp3lame", "-b:a", "320k", str(mp3_path)], directory, "mp3.stderr")
                # A present/nonempty MP3 is not proof of a playable master.
                # Decode independently with a bound slightly above the expected
                # duration; permit at most two MP3 frames of encoder padding.
                verified_pcm = directory / "mp3-verified.wav"
                self._run(self._base() + ["-protocol_whitelist", "file,pipe", "-f", "mp3", "-i", str(mp3_path),
                    "-map", "0:a:0", "-map_metadata", "-1", "-t", f"{frames / RATE + 0.25:.9f}",
                    "-ar", str(RATE), "-ac", str(CHANNELS), "-c:a", "pcm_s16le", str(verified_pcm)], directory, "mp3-verify.stderr")
                with wave.open(str(verified_pcm), "rb") as decoded:
                    decoded_frames = decoded.getnframes()
                    if (decoded.getnchannels(), decoded.getsampwidth(), decoded.getframerate()) != (CHANNELS, 2, RATE):
                        fail("MP3 verification format mismatch", "processing_failed", 502)
                if decoded_frames <= 0 or abs(decoded_frames - frames) > 2304:
                    fail("MP3 verification duration mismatch", "processing_failed", 502)
                # Detect external source changes during rendering before any
                # output is declared complete. Failed artifacts remain evidence.
                for sid in sources:
                    self._source(pid, root, sid)
                files = {}
                for kind, path in (("wav", wav_path), ("mp3", mp3_path), ("quality", quality_path)):
                    relative = f"renders/{rid}/{path.name}"
                    files[kind] = {"relative_path": relative, "url": self._url(pid, relative),
                                   "sha256": sha_file(path), "byte_size": path.stat().st_size}
                result = {**claim, "status": "completed", "completed_at": now(), "files": files,
                          "duration_s": frames / RATE, "frames": frames, "sample_rate": RATE, "channels": CHANNELS,
                          "mp3_encoded_from": "master.wav", "mp3_input_sha256": files["wav"]["sha256"],
                          "mp3_bitrate_kbps": 320, "mp3_verified": True, "mp3_decoded_frames": decoded_frames,
                          "recipe_file_sha256": sha_file(directory / "recipe.json"), "quality": quality}
                write_new(directory / "result.json", result)
                return result
            except Exception as exc:
                write_new(directory / "result.json", {**claim, "status": "failed", "failed_at": now(),
                          "error": str(exc), "evidence_preserved": True, "retry_allowed": False})
                if isinstance(exc, MasterAudioError):
                    raise
                fail("master render failed; claim and evidence retained", "processing_failed", 502)

    def peaks(self, pid, source_id, bins=1000):
        if isinstance(bins, bool) or not isinstance(bins, int) or not 16 <= bins <= 4096:
            fail("bins must be an integer from 16 to 4096")
        with self._locked(pid) as root:
            source = self._source(pid, root, source_id)
            bins = min(bins, source["frames"])
            peaks = []
            with wave.open(str(confined(root, source["pcm_path"])), "rb") as audio:
                for i in range(bins):
                    count = (i + 1) * source["frames"] // bins - i * source["frames"] // bins
                    low, high = 0, 0
                    while count:
                        take = min(count, 65536)
                        samples = array("h", audio.readframes(take))
                        if os.sys.byteorder != "little":
                            samples.byteswap()
                        if not samples:
                            fail("PCM unexpectedly truncated", "stale_source", 409)
                        low, high = min(low, min(samples)), max(high, max(samples))
                        count -= take
                    peaks.append([round(low / 32768, 6), round(high / 32768, 6)])
            return {"source_id": source_id, "sha256": source_id, "duration_s": source["duration_s"], "bins": bins,
                    "peaks": peaks, "basis": "decoded_pcm_all_channels_min_max"}

    def file(self, pid, relative):
        """Return only hash-verified source audio or completed master outputs."""
        with self._locked(pid) as root:
            path = confined(root, relative)
            parts = relative.split("/")
            if len(parts) == 3 and parts[0] == "sources":
                record = self._source(pid, root, parts[1])
                if relative in {record["raw_path"], record["pcm_path"]}:
                    return path
            if len(parts) == 3 and parts[0] == "renders" and ID_RE.fullmatch(parts[1]):
                result = read_json(confined(root, f"renders/{parts[1]}/result.json"))
                if result.get("status") == "completed":
                    for output in result["files"].values():
                        if output["relative_path"] == relative and sha_file(path) == output["sha256"]:
                            return path
            fail("file is not a registered deliverable", "not_found", 404)


def get_master_audio_service():
    return MasterAudioService()
