# Local nondestructive master-audio editor

This is a **CPU-only editor/exporter**, not generation, repaint, transcription,
automatic section detection, or an approval of artistic quality. It never reads
song arrangement timings or changes song state, generation caps, take preferences,
the project manifest, asset registry, or existing audio. Intended section labels
are user text, not evidence of audible boundaries. Actual bounds come only from
decoded PCM and an explicit listening attestation.

## Application integration

`backend/app/main.py` mounts the master-audio and readiness routers and serves
the editor at `/mastering`. Open it from the Music panel's **Open mastering
workspace** link, or visit `/mastering?project=<project-id>` directly.
Fetching source does not reload an already-running nonreloading backend;
loading the updated application remains a separately controlled local step.
Do not run the generation-enabled music launcher merely to view this editor.

The supplied store
has `get_store()._dir(pid)`, **not** `project_dir(pid)`; the service uses the former
after validating the 12-lowercase-hex project ID, store root, and project path.
It also supports `project_dir` if a future store supplies it. Configuration is
`get_settings().ffmpeg.binary` and `.process_timeout_s` (bounded at 7200 seconds).
No ffprobe, AI service, model, network client, download, or installation is used.
The installed ffmpeg must include the `libmp3lame` encoder.

## Exact HTTP contract

Base: `/api/projects/{pid}/master-audio`. All JSON writes reject unknown fields.
There is no request `project_id`: the URL determines the project.

### Read editor state

`GET <base>` returns:

```json
{"schema_version":1,"revision":0,"sources":[],"revisions":[],"renders":[],
 "limits":{"max_bytes":67108864,"max_duration_s":1800,"max_sections":64,
           "max_sources":32,"max_revisions":500},
 "generation":false,"listening_confirmation_is_user_attestation":true}
```

`revision` is the latest integer edit revision; an empty editor is revision 0.
Each source reports `status: ready|stale|uncertain`; stale entries include `error`.
An interrupted render without a terminal result is reported as `uncertain`.
GET may return 409 while a write/another read holds the project lock. Malformed
evidence fails closed rather than being silently ignored or reset.

### Import existing project audio

`POST <base>/sources` JSON:

```json
{"relative_path":"music/take.mp3"}
```

The file must already exist beneath the project's `audio/` or `music/` folder.
Traversal, absolute/Windows drive paths, symlinks, junctions, special files, and
unsupported formats are refused. Supported extensions: `.wav`, `.mp3`, `.flac`,
`.ogg`, `.m4a`, `.aac`. Caller-supplied arbitrary filesystem paths are never used.

Response is a source record with:
`source_id`, `sha256` (both full raw-byte SHA-256), `created_at`,
`original_relative_path`, `original_filename`, `byte_size`, `duration_s`, `sample_rate:48000`,
`channels:2`, `frames`, `raw_path`, `pcm_path`, `pcm_sha256`, `file_url`,
`pcm_url`, `duration_basis:"decoded_pcm_frames"`, `generation:false`.
Raw source bytes are copied unchanged. Reimporting identical bytes deduplicates
and verifies the existing registration, never overwrites it.

`POST <base>/sources/upload?filename=take.mp3` accepts a **raw body**, not multipart;
use `Content-Type: application/octet-stream`. Unicode and spaces in basenames are
accepted; separators, drive syntax, control characters, and `.`/`..` are refused.
The original basename is retained as `original_filename`, never used to construct
a storage path (staging uses a UUID and validated extension). Identical-byte
deduplication keeps the first registration's original filename. The extension
selects the audio demuxer. Upload is bounded while streaming the request into memory;
uploaded sources have `original_relative_path:null`. Failed staging is removed;
any created source evidence remains preserved. Prefer project-asset registration
for files already present to avoid uploading a second copy.

### Save an immutable edit revision

`POST <base>/revisions` JSON:

```json
{
  "expected_revision": 0,
  "note": "First listened edit",
  "sections": [
    {
      "clip_id": "verse-1",
      "source_id": "<64 lowercase hex raw source SHA>",
      "start_s": 0,
      "end_s": 12.5,
      "gain_db": 0,
      "fade_in_s": 0.02,
      "fade_out_s": 0.02,
      "label": "Verse 1",
      "confirmation": {
        "confirmed": true,
        "source_sha256": "<same SHA>",
        "start_s": 0,
        "end_s": 12.5
      }
    }
  ]
}
```

Required clip fields are `clip_id,source_id,start_s,end_s,confirmation`.
Gain/fades default to zero and label/note default to empty text. Sections are
concatenated in array order, with no implicit gaps, overlap, crossfade, or time
stretch. Split by creating two clips with distinct IDs and adjacent source
ranges. Reorder by changing array order. Repeated and overlapping **source**
ranges are permitted; each output clip is independently bounded by its source.

Confirmation must contain literal boolean `true` and exact source hash/range;
strings, integers, stale hashes, and changed bounds are refused. The server adds
`confirmed_at` and a `clip_sha256` covering **all normalized clip edit fields**.
A revision contains `revision`, `parent_revision`, `parent_recipe_sha256`,
`recipe_sha256`, `created_at`, `sections`, `source_ids`, `duration_s`, `note`,
`generation:false`. It never mutates an earlier revision. Submitted seconds are
retained; additional `start_frame/end_frame` record nearest 48 kHz frame rounding.

**Frontend:** do not round-trip a returned section wholesale: server-only fields
`source_sha256,start_frame,end_frame` and the enriched confirmation are not input
fields. Serialize the input shape above. Clear the listening checkbox when a
clip source/range/edit changes, and require explicit fresh confirmation on save;
do not manufacture confirmation from a label or intended arrangement. The server
can verify binding, not that a human actually listened. Existing confirmations
are immutable revision evidence, not a general authorization for new edits.

### Render exactly one attempt per revision

`POST <base>/renders` JSON:

```json
{"expected_revision":1,"render_id":"master-unique-client-id"}
```

`render_id` and `clip_id` match `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`. Rendering is
synchronous (offloaded from the ASGI event loop) and holds the cross-process
project lock. A claim is persisted **before** any export processing. Any previous
claim for this revision or render ID rejects retry, whether completed, failed,
or uncertain. A request timeout/disconnect is not permission to resubmit. Read
state and inspect evidence. There is no automatic recovery, retry, overwrite,
or deletion endpoint. A consciously saved new revision is a separate version.

Processing: verify raw/original/PCM hashes; check clip attestations; render each
trim/gain/fade through ffmpeg; concatenate exact stereo PCM frames into a new
exclusive `master.wav`; encode **that WAV**, not the source MP3, to 320 kbps MP3;
independently decode the MP3 with a bounded duration and verify nonempty PCM plus
duration agreement (at most two 1152-sample MP3 frames of padding); reverify source
hashes; persist the completion manifest. Duration and master
frame count are checked against the recipe. MP3 encoder delay/padding may affect
container duration; WAV frame count is authoritative.

Success includes `status:"completed"`, `render_id`, `revision`, `recipe_sha256`,
`parent_revision`, `source_ids`, `source_hashes`, `claimed_at`, `completed_at`,
`files:{wav:{relative_path,url,sha256,byte_size},mp3:{...}}`, `duration_s`,
`frames`, `sample_rate`, `channels`, `mp3_encoded_from:"master.wav"`,
`mp3_input_sha256`, `mp3_bitrate_kbps:320`, `mp3_verified:true`,
`mp3_decoded_frames`, `recipe_file_sha256`, and `quality`. `files.quality` uses the
same `{relative_path,url,sha256,byte_size}` shape and binds the immutable
`quality.json` bytes. The quality object is:

```json
{"basis":"master_wav_pcm_s16le","sample_peak_dbfs":-8.72,
 "full_scale_samples":0,"total_samples":96000,"warnings":[]}
```

Measurements scan the actual rendered WAV in bounded PCM chunks, across both
channels. `total_samples` counts channel samples, not frames. Full-scale counts
both signed 16-bit rails (−32768 and +32767). Sample peak is
`20*log10(max(abs(sample))/32768)`; digital silence uses JSON `null` for −∞ dBFS.
Warnings flag full-scale samples and sample peaks above −1 dBFS. Full-scale
samples indicate zero headroom and **possible risk, not proof of clipping**;
MP3/playback reconstruction can overshoot. This is not true-peak, LUFS, or a
clipping detector. UI should display the warnings, not silently imply safety.
No automatic normalization, limiter, source rejection, or source mutation occurs.
Quality is persisted before MP3 encoding, so it remains evidence if encoding fails.

Failures append a terminal failed
result, preserve raw/partial outputs/logs/argv/recipe/claim, and forbid retry.
Process death leaves claim evidence uncertain and still blocks retry.

### Listening and waveform

Use each source's **`pcm_url` for listening/audition** so waveform and audition
positions use the same decoded 48 kHz timeline, not compressed-container offsets.

`GET <base>/files/{relative_path}` delivers only registered, currently hash-valid
source raw/PCM, completed WAV/MP3 masters, or their hash-bound quality JSON.
Use returned URLs; other evidence JSON,
logs, intermediate clips, and failed outputs are not public deliverables. Existing
masters remain historical deliverables even if the original asset later changes;
old sources cannot be used for new edits or renders after such a change.

`GET <base>/sources/{source_id}/peaks?bins=1000` accepts 16–4096 bins and returns
`{source_id,sha256,duration_s,bins,peaks:[[min,max],...],basis}`. It streams the
verified decoded PCM in bounded chunks, aggregating extrema across both channels.
Peaks are amplitudes, not recognized sections, loudness analysis, or transcripts.

Errors use `detail:{code,message}`: 422 validation, 409 stale source/conflict/
confirmation/busy/evidence errors, 404 missing, 413 oversized, 503 missing binary,
502 processing failure. FastAPI query validation may use its standard 422 shape.

## Persistence, limits, and security

All new state is confined to `<project>/master-audio/`:
`sources/<raw-SHA>/{raw.ext,pcm.wav,source.json,...}`,
`revisions/000001.json`, and
`renders/<render_id>/{claim.json,recipe.json,result.json,master.wav,master.mp3,...}`.
JSON records use exclusive creation and fsync; ffmpeg uses `-n`, argv arrays,
`shell=False`, fixed demuxers, `file,pipe` protocol whitelist, one CPU thread,
and configured subprocess deadlines. No AI/GPU initialization or network calls
occur. Each ffmpeg invocation uses `subprocess.run(timeout=...)`, which kills and
waits for that child on timeout; the deadline is per invocation, not an overall
render deadline across all clips/encode/verification. Failure appends a terminal
record without overwriting the claim, argv, logs, quality, or partial output. No
process-group or unrelated process termination is attempted.
The lock is a persistent file with POSIX `flock` or Windows byte locking;
an in-process lock also protects threads. OS lock release is automatic on crash;
the file is never unlinked to force recovery. Actual Windows locking/junction
behavior requires validation on the target host; tests here run on Linux only.

Limits: 64 MiB raw source/upload; 32 sources; 1800 seconds per source/master;
7200 total registered source seconds; 64 sections; 500 revisions; 256 KiB JSON
request; gain −60 to +12 dB; fades cannot overlap. Rejected/interrupted import
directories count toward the source count; manual evidence review is intentional.
Decoded PCM is 48 kHz stereo **16-bit**, including for higher-resolution inputs.
Gain can clip; there is no limiter, LUFS normalization, dithering control, or
crossfade. Sources remain untouched so later higher-resolution export is possible.

Local project storage must be trusted and not concurrently modified by hostile
processes. Symlink/reparse checks and hash verification reject preexisting unsafe
paths and ordinary generation-source changes; they are not an OS sandbox against
an administrator racing filesystem changes between validation and open. Source
hashes are not signatures against an adversary rewriting all evidence. No auth/
CSRF policy is added here; retain the application's localhost-only deployment.
Failed evidence and intermediate clip WAVs are retained and consume disk; there
is no quota manager or cleanup API. Back up evidence before manual intervention.

## Isolated verification

From `backend/`:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_master_audio -v
```

The service and ordinary validation tests need only the Python standard library,
not FastAPI/Pydantic. Real system-ffmpeg synthetic tests skip if unavailable;
nothing is installed. Fixtures use temporary projects only. Router runtime tests
require the application's already-installed dependencies; they were unavailable
in the supplied sandbox. Nothing is applied to actual projects or the live host.
