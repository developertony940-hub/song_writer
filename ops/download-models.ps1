<#
.SYNOPSIS
Downloads the native-FP8 model contract used by the committed ComfyUI API
workflows. Paths are passed to Python via environment variables, never
interpolated into Python source.
#>
[CmdletBinding()]
param(
  [switch]$SkipVideo,
  # Set this argument or LOCALAI_COMFYUI_ROOT to your ComfyUI Portable root.
  [string]$ComfyRoot = $env:LOCALAI_COMFYUI_ROOT,
  [string]$ComfyPython,
  [string]$ModelsRoot
)
$ErrorActionPreference = 'Stop'

if (-not $ComfyRoot -and (-not $ComfyPython -or -not $ModelsRoot)) {
  throw "Set -ComfyRoot (or LOCALAI_COMFYUI_ROOT), or provide both -ComfyPython and -ModelsRoot. No machine-specific path is assumed."
}
if (-not $ComfyPython) { $ComfyPython = Join-Path $ComfyRoot "python_embeded\python.exe" }
if (-not $ModelsRoot) { $ModelsRoot = Join-Path $ComfyRoot "ComfyUI\models" }
if (-not (Test-Path -LiteralPath $ComfyPython -PathType Leaf)) {
  throw "Comfy Python was not found: $ComfyPython"
}

function Invoke-HfDownload {
  param([string]$Repo, [string]$Filename, [string]$DestDir, [string]$LocalName = "")
  New-Item -ItemType Directory -Path $DestDir -Force | Out-Null
  $targetName = if ($LocalName) { $LocalName } else { [IO.Path]::GetFileName($Filename) }
  if ([IO.Path]::GetFileName($targetName) -ne $targetName) { throw "LocalName must be a basename" }
  $destFull = [IO.Path]::GetFullPath($DestDir)
  $target = [IO.Path]::GetFullPath((Join-Path $destFull $targetName))
  if ((Split-Path -Parent $target) -ne $destFull) { throw "Download target escapes destination" }

  # The first eight bytes of a safetensors file are a little-endian JSON-header
  # length. This catches empty/partial/corrupt files without inventing hashes
  # for upstream files whose revisions have not been pinned by the operator.
  $code = @'
import os, shutil, struct
from pathlib import Path
from huggingface_hub import hf_hub_download
repo = os.environ["LOCALAI_HF_REPO"]
filename = os.environ["LOCALAI_HF_FILENAME"]
dest = Path(os.environ["LOCALAI_HF_DEST"]).resolve()
target = Path(os.environ["LOCALAI_HF_TARGET"]).resolve()
if target.parent != dest or target.name in ("", ".", ".."):
    raise SystemExit("unsafe destination")
def verify(path):
    if not path.is_file() or path.stat().st_size <= 8:
        return False
    with path.open("rb") as fh:
        raw = fh.read(8)
    header = struct.unpack("<Q", raw)[0]
    return header > 1 and header <= path.stat().st_size - 8
if not verify(target):
    downloaded = Path(hf_hub_download(repo_id=repo, filename=filename, local_dir=str(dest))).resolve()
    if not downloaded.is_file():
        raise SystemExit("download did not produce a file")
    if downloaded != target:
        shutil.move(str(downloaded), str(target))
if not verify(target):
    raise SystemExit("downloaded file failed safetensors structural verification")
print(target)
'@
  $env:LOCALAI_HF_REPO = $Repo
  $env:LOCALAI_HF_FILENAME = $Filename
  $env:LOCALAI_HF_DEST = $destFull
  $env:LOCALAI_HF_TARGET = $target
  & $ComfyPython -c $code
  if ($LASTEXITCODE -ne 0) { throw "Download failed or verification failed: $Repo/$Filename" }
  Write-Host "DONE: $target" -ForegroundColor Green
}

Invoke-HfDownload -Repo "Comfy-Org/flux1-dev" -Filename "split_files/diffusion_models/flux1-dev-fp8.safetensors" -DestDir "$ModelsRoot\diffusion_models" -LocalName "flux1-dev-fp8.safetensors"
Invoke-HfDownload -Repo "Comfy-Org/flux1-dev" -Filename "split_files/text_encoders/clip_l.safetensors" -DestDir "$ModelsRoot\text_encoders" -LocalName "clip_l.safetensors"
Invoke-HfDownload -Repo "Comfy-Org/flux1-dev" -Filename "split_files/text_encoders/t5xxl_fp8_e4m3fn.safetensors" -DestDir "$ModelsRoot\text_encoders" -LocalName "t5xxl_fp8_e4m3fn.safetensors"
Invoke-HfDownload -Repo "Comfy-Org/flux1-dev" -Filename "split_files/vae/ae.safetensors" -DestDir "$ModelsRoot\vae" -LocalName "ae.safetensors"

if (-not $SkipVideo) {
  Invoke-HfDownload -Repo "Kijai/WanVideo_comfy" -Filename "Wan2_1-I2V-14B-480P_fp8_e4m3fn.safetensors" -DestDir "$ModelsRoot\diffusion_models"
  Invoke-HfDownload -Repo "Kijai/WanVideo_comfy" -Filename "Wan2_1-T2V-14B_fp8_e4m3fn.safetensors" -DestDir "$ModelsRoot\diffusion_models"
  Invoke-HfDownload -Repo "Kijai/WanVideo_comfy" -Filename "umt5-xxl-enc-fp8_e4m3fn.safetensors" -DestDir "$ModelsRoot\text_encoders"
  Invoke-HfDownload -Repo "Wan-AI/Wan2.1-T2V-14B-diffusers" -Filename "vae/diffusion_pytorch_model.safetensors" -DestDir "$ModelsRoot\vae" -LocalName "wan2_1_vae.safetensors"
}
Write-Host "Restart ComfyUI, then call GET /api/runtime/preflight before queuing jobs." -ForegroundColor Green
