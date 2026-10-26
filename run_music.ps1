# Launch only the music-capable song_writer backend on Windows.
# This dedicated launcher always starts the explicitly authorized music
# process: live generation is enabled with the experimental capacity
# override (NOT verified capacity), reload/automatic restart is refused,
# and no environment is created and no packages are installed. Ordinary
# non-generating launch remains run.ps1. Requires the prepared Python 3.12
# virtualenv at backend\.venv; dependencies must be installed explicitly.
# Before starting the backend it checks port 8188 (ComfyUI) and, when
# nothing is listening, starts S:\ComfyUI\run_nvidia_gpu.bat in its own
# window and waits a bounded time for the API to answer. Override with
# -ComfyLauncher / LOCALAI_COMFYUI_LAUNCHER and -ComfyPort. The ComfyUI
# process inherits this shell's environment (pin it with
# CUDA_VISIBLE_DEVICES if it must run on GPU1). -CheckOnly never starts
# ComfyUI.
# The default port 7664 ("SONG" on a phone keypad) avoids the crowded
# 8000 range; pass -Port (or set LOCALAI_PORT) to override.
[CmdletBinding()]
param(
    [string]$BindHost = $(if ($env:LOCALAI_HOST) { $env:LOCALAI_HOST } else { "127.0.0.1" }),
    [int]$Port = $(if ($env:LOCALAI_PORT) { [int]$env:LOCALAI_PORT } else { 7664 }),
    [string]$ComfyLauncher = $(if ($env:LOCALAI_COMFYUI_LAUNCHER) { $env:LOCALAI_COMFYUI_LAUNCHER } else { "S:\ComfyUI\run_nvidia_gpu.bat" }),
    [int]$ComfyPort = 8188,
    [int]$ComfyStartTimeoutS = 120,
    [switch]$CheckOnly
)

Set-Location -LiteralPath $PSScriptRoot\backend

$VenvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"

function Write-SetupInstructions {
    Write-Host "Backend virtualenv is not ready." -ForegroundColor Red
    Write-Host ""
    Write-Host "Create it with Python 3.12:"
    Write-Host "  py -3.12 -m venv .venv"
    Write-Host ""
    Write-Host "Then install the backend dependencies explicitly:"
    Write-Host "  .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    Write-Host ""
    Write-Host "Re-run this script after those commands complete."
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-SetupInstructions
    exit 1
}

$Version = & $VenvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or $Version -ne "3.12") {
    Write-Host "backend\.venv must use Python 3.12 (found: $Version)." -ForegroundColor Red
    Write-SetupInstructions
    exit 1
}

& $VenvPython -c "import fastapi, uvicorn, httpx, pydantic, jinja2, multipart"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Backend dependencies are missing or incomplete." -ForegroundColor Red
    Write-Host "Install them explicitly with:"
    Write-Host "  .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

$MusicEnvNames = @(
    'LOCALAI_MUSIC__LIVE_GENERATION_AUTHORIZED',
    'LOCALAI_MUSIC__CAPACITY_VERIFIED',
    'LOCALAI_MUSIC__CAPACITY_OVERRIDE_AUTHORIZED'
)

function Clear-MusicAuthorization {
    foreach ($name in $MusicEnvNames) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
}

function Test-ComfyUiPort {
    param([int]$Port)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(750)) {
            return $false
        }
        $client.EndConnect($pending)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Start-ComfyUiIfNeeded {
    if (Test-ComfyUiPort -Port $ComfyPort) {
        Write-Host "ComfyUI is already listening on port $ComfyPort." -ForegroundColor Green
        return
    }
    if (-not (Test-Path -LiteralPath $ComfyLauncher)) {
        Write-Warning "Nothing is listening on port $ComfyPort and the ComfyUI launcher was not found: $ComfyLauncher"
        Write-Warning "Start ComfyUI manually at http://127.0.0.1:$ComfyPort before generating."
        return
    }
    Write-Host "ComfyUI is not listening on port $ComfyPort; starting it..." -ForegroundColor Yellow
    Start-Process -FilePath $ComfyLauncher -WorkingDirectory (Split-Path -Parent $ComfyLauncher)
    $deadline = (Get-Date).AddSeconds($ComfyStartTimeoutS)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if (Test-ComfyUiPort -Port $ComfyPort) {
            Write-Host "ComfyUI is ready on port $ComfyPort." -ForegroundColor Green
            return
        }
    }
    Write-Warning "ComfyUI did not answer on port $ComfyPort within $ComfyStartTimeoutS s."
    Write-Warning "The backend will still start and report the runtime blocker; check the ComfyUI window for errors."
}

$env:LOCALAI_MUSIC__LIVE_GENERATION_AUTHORIZED = 'true'
$env:LOCALAI_MUSIC__CAPACITY_OVERRIDE_AUTHORIZED = 'true'
Remove-Item Env:LOCALAI_MUSIC__CAPACITY_VERIFIED -ErrorAction SilentlyContinue
Write-Host "MUSIC-ONLY LAUNCH: live generation is authorized for this process only." -ForegroundColor Yellow
Write-Host "Capacity: EXPERIMENTAL OVERRIDE - NOT VERIFIED. This is not capacity proof." -ForegroundColor Yellow
Write-Host "No reload and no automatic restart. Audio approval remains a separate action."
Write-Host "Website: http://$BindHost`:$Port"

if ($CheckOnly) {
    Write-Host "Startup preflight passed; no server was started."
    Write-Host "Would enable live generation and the experimental capacity override in the child process."
    Write-Host "Would also start ComfyUI ($ComfyLauncher) only if port $ComfyPort is idle."
    Clear-MusicAuthorization
    exit 0
}

Start-ComfyUiIfNeeded

$env:PYTHONPATH = (Get-Location).Path
try {
    & $VenvPython -m uvicorn app.main:app --host $BindHost --port $Port
} finally {
    Clear-MusicAuthorization
}
exit $LASTEXITCODE