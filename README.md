# song_writer

Local FastAPI orchestration for copy, images, video, audio, and FFmpeg delivery masters. Every project has a filesystem-backed manifest and asset registry.

## Song section editing and master revisions

The Music panel links to **Open mastering workspace** at `/mastering`.
This local, nondestructive editor provides waveform interval playback, section
trim/split/reorder, gain/fades, undo/redo, immutable edit revisions, and lossless
WAV plus once-encoded MP3 masters. Originals are preserved. It does not generate
missing lyrics or authorize AI submissions. See [the editor guide](docs/MASTER-AUDIO.md)
and [readiness limits](docs/MUSIC-READINESS.md).

Syncing this source does not update a running nonreloading backend. A controlled
local reload/restart requires separate permission. No dependency installation,
model loading, or generation is needed simply to fetch these changes.

## Runtime model

- **GPU0:** LM Studio and Kokoro. song_writer serializes its own GPU0 jobs, but LM Studio's OpenAI-compatible API has no verified model-unload endpoint; its reported GPU residency is therefore **advisory**.
- **GPU1:** ComfyUI FLUX/Wan jobs are serialized by an application lease. `/free` is requested between models when appropriate, but a request acknowledgement is not represented as a measured VRAM release.
- **CPU:** FFmpeg creates H.264/AAC `.mp4` social masters (1080x1920, 30 fps by default).

The router does not control external GPU processes.

## Project layout

```text
projects/{id}/
  manifest.json
  assets.json
  copy/ images/ video/ audio/ music/ final/ prompts/
```

## Setup

1. Copy `backend/app/config/settings.example.toml` outside the repository (or to the gitignored `backend/app/config/settings.local.toml`), customize local runtime URLs/model IDs/project root, and set `LOCALAI_CONFIG` to that file. With `LOCALAI_CONFIG`, a relative `app.projects_root` is resolved from the selected TOML file's directory, not the repository or launch directory; use an absolute path when that is not desired. The bundled `settings.toml` continues to resolve its relative project root from the repository root. For a one-off setting, use environment overrides such as `LOCALAI_PROJECTS_ROOT`, `LOCALAI_FFMPEG_BINARY=ffmpeg.exe`, or `LOCALAI_RUNTIME__LMSTUDIO__DEFAULT_MODEL=exact-loaded-model-id`.
2. Install backend requirements and run Uvicorn from `backend`:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\pip install -r requirements.txt
   .\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 7664
   ```

   On Windows, `./run.ps1` performs the same backend setup and honors `LOCALAI_HOST` / `LOCALAI_PORT` (or its `-BindHost` / `-Port` parameters). `-BindHost` avoids PowerShell's reserved `$Host` automatic variable. `./run_music.ps1` launches the same backend in the explicitly authorized music-only mode (live generation with the experimental capacity override, not verified capacity; same default port 7664, `-Port` to override).

3. Install TTS separately: `ops/requirements-tts-kokoro.txt` supports the local/offline Kokoro server (install a PyTorch wheel for the selected CUDA/CPU platform first); `ops/requirements-tts-edge.txt` is an optional networked fallback. For the committed native-FP8 ComfyUI graphs, see [`docs/INSTALL-ComfyUI.md`](docs/INSTALL-ComfyUI.md). The model downloader verifies basic safetensors structure; operators should pin and checksum models for fully reproducible installations.

## Review-gated stages

Stages are `copy → images → video → audio → final`. Workers alone may set `running`, `succeeded`, or `failed`; the public stage route only accepts `pending` and `approved`. Approval requires the stage to have succeeded, persisted file evidence, and all earlier stages to be approved. `run-project` rechecks file evidence before honoring a persisted `succeeded` state.

## API summary

| Method | Path | Purpose |
|---|---|---|
| GET, POST | `/api/projects` | List or create projects |
| GET | `/api/projects/{pid}` | Read project metadata |
| PATCH | `/api/projects/{pid}/mode` | Set `manual` or `auto` mode |
| PATCH | `/api/projects/{pid}/stage` | Review transition: only `pending` / `approved` |
| PUT | `/api/projects/{pid}/composition` | Save an editable composition |
| GET | `/api/projects/{pid}/assets?kind=` | List assets |
| GET | `/api/projects/{pid}/assets/{aid}` | Download asset |
| POST | `/api/generate/copy`, `/image`, `/video`, `/audio` | Generate an asset |
| POST | `/api/generate/assemble` | Assemble a simple master |
| POST | `/api/generate/assemble-composition` | Render the Phase-0 sequential composition |
| POST | `/api/generate/preview-frame` | Render a composition preview frame |
| POST | `/api/generate/artifact`, `/image-plan`, `/run-project`, `/upscale` | Supporting workflow actions |
| GET | `/api/runtime/status`, `/vram`, `/models`, `/lmstudio/models`, `/preflight` | Runtime diagnostics |

## Reproducibility and CI

Every generated asset records `meta.provenance`: schema version, versioned prompt identifier and hashes, selected model names, workflow names/versions/content hashes, and—on rendered output—the versioned delivery-profile fingerprint. This is evidence of the exact local configuration/workflow contract without copying secrets or models into project manifests. Bump a prompt/workflow/profile version with every intentional incompatible semantic change.

GitHub Actions installs only the pinned backend test dependencies and FFmpeg, then runs the deterministic offline suite. Media integration tests generate tiny color bars and sine waves at test time; they never require ComfyUI, LM Studio, TTS, a GPU, models, or checked-in media.

## Phase 4: production acceptance and operational readiness

Phase 4 closes the source-only audit limitation with a repeatable representative project. It preserves the distinction between a technical acceptance and a human creative sign-off: the workflow cannot claim to see, hear, or judge an unavailable model output.

### Deterministic offline / CI acceptance

On Windows, from the repository root:

```powershell
.\ops\run-acceptance.ps1 -Mode offline
```

This **does not contact** LM Studio, ComfyUI, or TTS. It creates deterministic color-video, PNG, and sine-audio fixtures, then drives the ordinary fingerprinted VideoPlan, reviewed candidate links, timed captions, proxy, approved-preview, master render, provenance, FFmpeg `ffprobe`, loudness, and final-stage evidence gates. It prints a project ID and writes both artifact-friendly JSON and readable Markdown to `projects\<project-id>\reviews\phase4-acceptance-*.{json,md}`. A `passed_technical` result proves the local pipeline and master contract, not AI creative quality.

For an already activated venv, the exact equivalent is:

```powershell
Set-Location backend
python scripts\run_acceptance.py --mode offline
```

### Workstation acceptance with real configured services

First make the services available and configure their exact URLs/model IDs in your private local TOML. Real acceptance also requires a stable, high-entropy signing secret. Prefer a private process environment variable so it is neither committed nor written into any project manifest, review record, or report:

```powershell
# Generate once, retain it in an approved local secret manager, then set it
# for this PowerShell session before every resume of this real acceptance run.
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$env:LOCALAI_ACCEPTANCE_SIGNING_SECRET = [Convert]::ToBase64String($bytes)
.\ops\run-acceptance.ps1 -Mode real
```

`acceptance.signing_secret` in a gitignored private `LOCALAI_CONFIG` is also supported when process-environment secret management is unavailable. Never put a real secret in the tracked example/default TOML or Git. Keep the same secret for the lifetime of a pending real run: an absent or changed secret fails closed with an actionable error and cannot be rotated in place.

The run verifies the secret before making any generation request. A failure produces structured diagnostics and does not create or generate an acceptance project. A ready first run creates a **new HMAC-SHA256-signed real acceptance-run record**, generates only image candidates, and returns `pending_operator_candidate_review`; it does not fabricate candidate review, approve a stage, create a proxy, or render a master. Each state signature canonically binds the project/run IDs, original real mode, complete transition history, prior signature value, proxy binding, and eventual operator approval record.

Use the local UI to inspect/select one real image candidate per scene, then resume **the exact returned real run** (ordinary projects and offline runs are rejected):

```powershell
.\ops\run-acceptance.ps1 -Mode real -ProjectId <project-id>
```

The next invocation creates motion candidates and again waits for the operator to select them. A subsequent no-flag resume generates the tagged audio and an exact recorded proxy, returning `pending_operator_proxy_review`. Review the proxy for framing, anatomy/motion, unwanted text, brand continuity, factual claims, caption readability/safe space, and audio intelligibility. Only after viewing the exact recorded bytes may the operator attest:

```powershell
.\ops\run-acceptance.ps1 -Mode real -ProjectId <project-id> -OperatorApproved
```

Each signed transition also records a canonical evidence snapshot: registered asset IDs, paths, byte hashes, media kinds, complete registry metadata (including provenance/source markers), candidate review/selection records, and current VideoPlan/composition linkage. Before later generation, proxy approval, master rendering, and certification, the runner compares that snapshot with the live registry and files. This rejects changed or deleted bound files, paths, metadata/provenance, fixture markers, candidate evidence, and plan/composition linkage; replacing proxy or source bytes requires a new proxy review. The completion audit independently reruns FFmpeg `ffprobe` and loudness QC and binds QC to the current master SHA-256.

This protection is limited to **post-transition project-file tampering by an actor without the signing secret**. It does not claim to protect against arbitrary process/configuration compromise, signing-secret access, or races while a trusted in-process generation/review result is being captured and signed. Use OS account/ACL separation and a protected secret store for stronger threat models.

To reject rather than approve the recorded proxy, preserving a truthful remediation path:

```powershell
.\ops\run-acceptance.ps1 -Mode real -ProjectId <project-id> -OperatorRejected
```

This invalidates preview approval and returns `pending_operator_remediation`; revise/review candidates in the UI and resume without a flag to create a new proxy. `passed_technical_operator_attested` is a technical result plus an operator's proxy-review attestation—not a substitute for final delivery review. Keep the reports and immutable run record with the workstation record. Generated project media/reports remain excluded from Git.
