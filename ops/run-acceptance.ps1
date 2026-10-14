# Run a repeatable Phase 4 production acceptance workflow on Windows.
# The workflow writes JSON and Markdown reports to projects\<id>\reviews.
# Offline mode never contacts LM Studio, ComfyUI, or TTS and is suitable for CI.
# Real mode requires LOCALAI_ACCEPTANCE_SIGNING_SECRET (or the private config
# equivalent). Set it in the current PowerShell session; never commit it.
[CmdletBinding()]
param(
    [ValidateSet("offline", "real")]
    [string]$Mode = "offline",
    [switch]$OperatorApproved,
    [switch]$OperatorRejected,
    [string]$ProjectId
)

if ($Mode -eq "real" -and [string]::IsNullOrWhiteSpace($env:LOCALAI_ACCEPTANCE_SIGNING_SECRET)) {
    Write-Warning "Real acceptance needs LOCALAI_ACCEPTANCE_SIGNING_SECRET. Set a stable private session environment value (or configure acceptance.signing_secret in your private LOCALAI_CONFIG); the runner will fail closed if neither is set."
}

Set-Location -LiteralPath (Join-Path $PSScriptRoot "..\backend")
if (-not (Test-Path -LiteralPath ".venv")) {
    Write-Host "Creating virtualenv..."
    # Use the installed Python launcher version; the backend supports current Python 3 releases.
    py -m venv .venv
}
& ".\.venv\Scripts\Activate.ps1"
python -m pip install -q -r requirements.txt
$args = @("scripts\run_acceptance.py", "--mode", $Mode)
if ($OperatorApproved) { $args += "--operator-approved" }
if ($OperatorRejected) { $args += "--operator-rejected" }
if ($ProjectId) { $args += @("--project-id", $ProjectId) }
python @args
exit $LASTEXITCODE
