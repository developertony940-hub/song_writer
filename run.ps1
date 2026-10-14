# Launch the song_writer backend on Windows.
# Assumes a virtualenv at backend\.venv (created if missing). Configuration is
# read by the app from LOCALAI_CONFIG / LOCALAI_* environment overrides.
[CmdletBinding()]
param(
    [string]$BindHost = $(if ($env:LOCALAI_HOST) { $env:LOCALAI_HOST } else { "127.0.0.1" }),
    [int]$Port = $(if ($env:LOCALAI_PORT) { [int]$env:LOCALAI_PORT } else { 8000 })
)

Set-Location -LiteralPath $PSScriptRoot\backend

if (-not (Test-Path -LiteralPath ".venv")) {
    Write-Host "Creating virtualenv..."
    py -m venv .venv
}

& ".\.venv\Scripts\Activate.ps1"
python -m pip install -q -r requirements.txt

$env:PYTHONPATH = (Get-Location).Path
python -m uvicorn app.main:app --host $BindHost --port $Port --reload
