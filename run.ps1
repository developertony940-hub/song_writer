# Launch the song_writer backend on Windows.
# Ordinary launch requires a prepared Python 3.12 virtualenv at backend\.venv
# and never creates an environment or installs packages. Configuration is read
# by the app from LOCALAI_CONFIG / LOCALAI_* environment overrides.
# Ordinary launch is non-generating. Use -Music only for an explicitly
# authorized music-enabled process; it enables live generation with an
# experimental capacity override, not verified capacity, and never reloads.
# Use -Setup only to create an empty venv with Python 3.12; dependencies must
# then be installed explicitly.
[CmdletBinding()]
param(
    [string]$BindHost = $(if ($env:LOCALAI_HOST) { $env:LOCALAI_HOST } else { "127.0.0.1" }),
    [int]$Port = $(if ($env:LOCALAI_PORT) { [int]$env:LOCALAI_PORT } else { 7664 }),
    [switch]$Music,
    [switch]$Reload,
    [switch]$CheckOnly,
    [switch]$Setup
)

Set-Location -LiteralPath $PSScriptRoot\backend

$VenvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
$InstallCommand = ".\.venv\Scripts\python.exe -m pip install -r requirements.txt"

function Write-SetupInstructions {
    Write-Host "Backend virtualenv is not ready." -ForegroundColor Red
    Write-Host ""
    Write-Host "Create it with Python 3.12:"
    Write-Host "  py -3.12 -m venv .venv"
    Write-Host ""
    Write-Host "Then install the backend dependencies explicitly:"
    Write-Host "  $InstallCommand"
    Write-Host ""
    Write-Host "Re-run this script after those commands complete."
}

if ($Setup) {
    if (Test-Path -LiteralPath $VenvPython) {
        Write-Host "backend\.venv already exists; no changes made."
        Write-Host "Install or verify dependencies explicitly with:"
        Write-Host "  $InstallCommand"
        exit 0
    }
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Python 3.12 virtualenv creation failed." -ForegroundColor Red
        exit $LASTEXITCODE
    }
    Write-Host "Created backend\.venv with Python 3.12."
    Write-Host "No dependencies were installed."
    Write-Host "Install them explicitly with:"
    Write-Host "  $InstallCommand"
    exit 0
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
    Write-Host "  $InstallCommand"
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

if (-not $Music) {
    $ambient = @($MusicEnvNames | Where-Object {
        [Environment]::GetEnvironmentVariable($_, 'Process') -eq 'true'
    })
    if ($ambient.Count -gt 0) {
        Write-Host "Music authorization environment is already set: $($ambient -join ', ')." -ForegroundColor Red
        Write-Host "Refusing ordinary startup. Use -Music for an explicit music-enabled process."
        exit 1
    }
}

if ($Music) {
    if ($Reload) {
        Write-Host "Music mode cannot be combined with -Reload; no automatic restart is allowed." -ForegroundColor Red
        exit 1
    }
    $env:LOCALAI_MUSIC__LIVE_GENERATION_AUTHORIZED = 'true'
    $env:LOCALAI_MUSIC__CAPACITY_OVERRIDE_AUTHORIZED = 'true'
    Remove-Item Env:LOCALAI_MUSIC__CAPACITY_VERIFIED -ErrorAction SilentlyContinue
    Write-Host "MUSIC MODE: live generation is authorized for this process only." -ForegroundColor Yellow
    Write-Host "Capacity: EXPERIMENTAL OVERRIDE - NOT VERIFIED. This is not capacity proof." -ForegroundColor Yellow
    Write-Host "No reload and no automatic restart. Audio approval remains a separate action."
}

$UvicornArgs = @('-m', 'uvicorn', 'app.main:app', '--host', $BindHost, '--port', $Port)
if ($Reload) {
    $UvicornArgs += '--reload'
}

if ($CheckOnly) {
    Write-Host "Startup preflight passed; no server was started."
    if ($Music) {
        Write-Host "Would enable live generation and the experimental capacity override in the child process."
    } else {
        Write-Host "Ordinary mode: live generation remains disabled and tracked flags remain false."
    }
    Clear-MusicAuthorization
    exit 0
}

$env:PYTHONPATH = (Get-Location).Path
try {
    & $VenvPython @UvicornArgs
} finally {
    Clear-MusicAuthorization
}
exit $LASTEXITCODE
