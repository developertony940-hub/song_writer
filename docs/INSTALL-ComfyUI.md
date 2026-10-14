# ComfyUI install contract — native FP8

This project uses **one** model/workflow contract: native ComfyUI FP8
safetensors. It does **not** use GGUF files or `UnetLoaderGGUF` /
`CLIPLoaderGGUF`. The only non-core node used by the committed video graphs is
`VHS_VideoCombine` from ComfyUI-VideoHelperSuite.

## Install

1. Install a current ComfyUI Portable build in a directory of your choice, bind it to GPU1,
   and start it at `http://127.0.0.1:8188`. This repository does not assume a drive letter or install path.
2. Install VideoHelperSuite using ComfyUI Manager (or clone
   `Kosinkadink/ComfyUI-VideoHelperSuite` into `custom_nodes`) and restart.
   **Do not install ComfyUI-GGUF for this repository's workflows.**
3. Run from the repository root:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\download-models.ps1 -ComfyRoot "C:\path\to\ComfyUI"
   ```

   `-SkipVideo` installs only FLUX.
4. Start ComfyUI with `--lowvram` for Wan on a 24-GB RTX 3090.
5. Start LM Studio with the model whose **exact server ID** is configured as
   `runtime.lmstudio.default_model` (default:
   `qwen3-coder-30b-a3b-instruct`), then start Kokoro at port 9880.
6. Start the backend and require a successful preflight before generation:

   ```text
   GET http://127.0.0.1:8000/api/runtime/preflight
   ```

## Required model files

| Workflow | Native loader contract | Required files |
|---|---|---|
| `flux_dev_fp8` | `UNETLoader`, `DualCLIPLoader`, `VAELoader` | `diffusion_models/flux1-dev-fp8.safetensors`; `text_encoders/clip_l.safetensors`; `text_encoders/t5xxl_fp8_e4m3fn.safetensors`; `vae/ae.safetensors` |
| `wan21_i2v_14b_fp8` | `UNETLoader`, `CLIPLoader`, `VAELoader`, `VHS_VideoCombine` | `diffusion_models/Wan2_1-I2V-14B-480P_fp8_e4m3fn.safetensors`; `text_encoders/umt5-xxl-enc-fp8_e4m3fn.safetensors`; `vae/wan2_1_vae.safetensors` |
| `wan21_t2v_14b_fp8` | same as I2V | `diffusion_models/Wan2_1-T2V-14B_fp8_e4m3fn.safetensors`; shared UMT5 and VAE |

The downloader and committed API graphs use these exact basenames. Do not
substitute older lowercase, GGUF, checkpoint-bundled, or differently named
files without exporting a new API graph and changing all three contracts.

## What preflight checks

Preflight is read-only. It checks project-root writability and >=1 GiB free
space, both FFmpeg binaries, LM Studio `/v1/models` and configured model IDs,
TTS `/health`, ComfyUI `/system_stats`, every enabled API graph, and ComfyUI
`/object_info` for each required class and model filename. It reports every
failure under `checks`; `ready` is true only when all checks pass.

The provided model graphs are native API templates. ComfyUI versions can alter
Wan node inputs or model dropdown inventories; therefore verify the installed
ComfyUI's `object_info` inventory with preflight. If it reports a missing node
or model, export a working native API graph from that exact installation and
keep its IDs used by `comfyui_client.py` (6, 7, 10, 25, 28, and 40 for I2V), or
update that client and tests in the same change.

## Deliberately disabled workflow variants

`flux_dev_fp8_img2img`, `flux_dev_fp8_pulid`, and `upscale_esrgan` are unset in
`settings.toml` rather than pointing at missing/speculative graphs. Requests
for those features fail clearly until a tested exported native workflow is
added and passes preflight. They remain shipped JSON assets and therefore each
has a semantic version in `backend/app/services/provenance.py`; being
versioned records their exact file contract and does not enable them.
