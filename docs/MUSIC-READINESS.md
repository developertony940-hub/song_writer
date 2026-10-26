# Read-only music readiness

`GET /api/projects/{pid}/music/readiness` is an observation, **not** generation,
authorization, a capacity reservation, or audio/listening acceptance. Register
`app.routers.music_readiness.router` with the existing FastAPI application.
This slice does not change `main.py`, the frontend, configuration, services or
host state. It adds no POST dispatch endpoint.

## Response contract (schema version 1)

Top-level keys:

- `schema_version`: `1`; `project_id`: URL id; `read_only`: `true`.
- `ready`, `can_generate`: `false`. They deliberately remain false until a
  separately designed physical mapping/ownership/exclusivity verification
  exists. Existing configuration flags cannot prove those facts.
- `status`: `blocked` when a definite blocker exists, otherwise `unknown`.
- `blockers`: array of `{code, message}`. Includes approval, live hold, finite
  lifetime cap, accounting recovery, scheduler hold/lease/model-switch,
  endpoint/queue, mapping, ownership, exclusivity and workflow-verification
  findings. Unknown is not success.
- `approvals`: `{draft_version, lyrics_sha256, settings_sha256, record,
  hashes_match, audio_approval}`. Nullable values are preserved. `record` is
  the persisted approval including its variant plan and note. Hash matching
  recomputes exact draft hashes with the existing service hashing functions;
  returned digest fields are the recorded draft digests, not new approvals.
- `submission`: `{lifetime_cap, effective_cap, used, remaining,
  current_attempt, recovery_required, orphans, open_uncertain}`. Cap is the
  minimum approved/lifetime cap; `remaining` is null when no cap exists.
  Attempt and approval objects retain the existing music-state schemas.
- `runtime`: `{endpoint, online, system_stats, queue, policy, device_mapping,
  ownership, exclusive_capacity, router, observation_errors}`.
  - `system_stats`: endpoint JSON object or null. No free-memory threshold
    implies readiness. `online` only means a well-shaped stats observation.
  - `queue`: `{known, running, pending}`; counts are null when unknown. Queued
    workflows/prompts are not echoed.
  - `policy`: `{model, slot, configured_device_index, capacity_verified,
    capacity_override_authorized, live_generation_authorized}`. Model/slot
    come from the existing scheduler profile, not a newly invented policy.
  - `device_mapping`: `{status, reason, devices}`. `status` is `mismatch` for
    conflicting configured/scheduler/endpoint device indexes or a non-CUDA
    endpoint; otherwise `unknown`. ComfyUI's CUDA index may be process-local,
    so index/name equality alone never establishes physical identity.
  - `ownership`, `exclusive_capacity`: `{status: "unknown", reason}`.
    Cooperative leases cannot exclude external GPU processes. Empty queue
    plus low memory usage is not exclusive ownership evidence.
  - `router`: current scheduler status plus `holds_unreadable`.
  - `observation_errors`: endpoint path and exception class only.
- `input_review`: `{issues, duration}`. Issues preserve the existing
  `review_music_inputs` issue-array API and codes. Duration is null without
  a draft; otherwise it has the documented heuristic fields below.
- `captures`: `[{attempt_id, kind, artifacts}]`, one entry per journal row
  with existing `_path`/`_sha256` fields. These include retained request,
  environment, response/history/outcome/audio/native captures when present.
  References are not fetched, recreated, or newly hash-verified here.
- `actions`: `{generation: {method: "POST", path:
  "/api/music/{pid}/generate", enabled: false, reason}, dispatch_added: false}`.
  This names only the existing guarded route; the UI must not promise that
  pressing a button will generate when mapping/capacity are unknown.
- `limitations`: human-readable snapshot/evidence boundaries.

Missing/invalid project ids return 404. Corrupt state, invalid schema or
inconsistent journal accounting returns 409 rather than fake ready/empty
state. Endpoint failures return a diagnostic snapshot with blockers. A
readiness observation is not atomic with a future generation request: the
existing generation route must retain all its approval/cap/recovery guards.

## Read-only implementation boundary

The adapter reads existing `manifest.json`, `music/.state/state.json` and
`music/.state/attempts.jsonl` without the convenience `ProjectStore` or
`MusicService` readers that create directories. It validates `MusicState`,
reuses the service's actual journal consistency logic and draft hash
functions, and only reads the model router. No saves, migration writes,
leases, holds, approvals, cap changes, model loads/unloads, process launch,
queue dispatch or cancel calls are performed. No existing artifacts are
modified. The only HTTP operations are GET `/system_stats` and GET `/queue`
against the configured ComfyUI endpoint, with a five-second per-call timeout
and redirects disabled for the default client. An injected async client
needs `get()`, with response `raise_for_status()` and `json()`; an
`httpx.AsyncClient(transport=httpx.MockTransport(...))` is also suitable.
The endpoint never probes the native repaint service or other hosts.

## Duration diagnostic

`estimate_lyric_duration` returns:
`method`, `lyric_lines`, `words`, `syllables_estimate`, `tempo_bpm_range`,
`tempo_source`, `syllables_per_beat_range`, `words_per_second_range`,
`instrumental_sections`, `instrumental_beats_per_section_range`,
`vocal_seconds_range`, `instrumental_seconds_range`, `total_seconds_range`,
`uncertainty`, `legacy_seconds_per_line_used`.

This replaces a fixed six-seconds-per-line guess with an intentionally broad,
transparent envelope:

- Unicode word-token count and English-oriented vowel-group syllable estimate.
- Explicit settings BPM when supplied; otherwise direction `N BPM`; otherwise
  a 60–140 BPM uncertainty range. Existing service preflight calls do not
  supply the settings BPM, so they use direction/default range; the new
  readiness snapshot uses the actual saved settings BPM.
- 0.5–2 syllables/beat and 1–4 words/second; vocal lower/upper bounds use the
  maximum of the corresponding word and syllable estimates.
- Each explicitly instrumental section receives 8–16 beats of allowance.
  Intro/outro are not presumed instrumental without an exact directive.
  Instrumental directives are intent, not proven model behavior; any words
  present are still counted. No timing directives are rewritten or invented.
- `lyric_density_high` only when even the optimistic bound exceeds the target.
  `lyric_density_low` only when an explicit BPM is available and the target
  exceeds twice the broad upper bound. Absence of warnings is not fit proof.
- The `seconds_per_line` argument remains accepted for callers but is no
  longer used. Non-density section/conflict issue codes remain compatible.

Non-English syllables, melisma, rests, repeats, expressive timing and omitted
lyrics remain uncertain. The diagnostic never rewrites text, arrangement,
approved settings, input hashes or approval records. It does not guarantee
all lyrics will be sung or assess the generated audio.

## Offline validation

From `backend`, without installing dependencies:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_music_readiness.py' -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_music_input_review.py' -v
```

These run actual dependency-free duration/readiness/GET-observation helpers,
temporary-filesystem no-mkdir checks, and the existing limited AST-extracted
service contract checks. They do not exercise dependency-backed FastAPI
routing, live GPUs/services, model installation, or listening acceptance.
