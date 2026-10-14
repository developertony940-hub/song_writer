<#
.SYNOPSIS
  Portable one-time full + monthly refresh backup for a configured ComfyUI models directory.

.DESCRIPTION
  ComfyUI model files are large (40-60 GB) and re-downloadable, so this is a
  low-frequency safety net, not a daily backup. Strategy:
    - First run seeds the configured backup root with a full mirror.
    - Subsequent runs use /MIR which only copies new/changed files
      (fast when models are unchanged - typically minutes, not hours).

  Excludes output/ and any *.lock / *.tmp scratch files.
  Logs: ops\logs\models-backup-YYYY-MM-dd_HHmmss.log

.PARAMETER Mode
  (default, no param needed - always does the same /MIR pass)
  Kept for symmetry with backup-projects.ps1; optional.

.EXAMPLE
  .\backup-models.ps1 -Source "C:\ComfyUI\ComfyUI\models" -BackupRoot "E:\Backups\ComfyUI-models"
  # Or set LOCALAI_MODELS_ROOT and LOCALAI_MODELS_BACKUP_ROOT.
#>

[CmdletBinding()]
param(
    [string]$Source = $env:LOCALAI_MODELS_ROOT,
    [string]$BackupRoot = $env:LOCALAI_MODELS_BACKUP_ROOT
)

$ErrorActionPreference = 'Stop'
if (-not $Source -or -not $BackupRoot) {
    throw "Pass -Source and -BackupRoot, or set LOCALAI_MODELS_ROOT and LOCALAI_MODELS_BACKUP_ROOT."
}
$timestamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$logDir = Join-Path $PSScriptRoot 'logs'
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$logFile = Join-Path $logDir "models-backup-$timestamp.log"

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'o'), $msg
    Add-Content -LiteralPath $logFile -Value $line
    Write-Host $line
}

Write-Log "=== Models backup START ==="
Write-Log "Source     : $Source"
Write-Log "BackupRoot : $BackupRoot"

if (-not (Test-Path -LiteralPath $Source)) {
    Write-Log "WARN: Source not found ($Source)."
    Write-Log "This is expected before ComfyUI is installed. Nothing to back up yet."
    Write-Log "=== Models backup SKIPPED (no source) ==="
    exit 0
}

if (-not (Test-Path -LiteralPath $BackupRoot)) {
    New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
}
Write-Log "Target     : $BackupRoot"

# Measure source size for log context
$srcSize = (Get-ChildItem -Path $Source -Recurse -File -ErrorAction SilentlyContinue |
    Measure-Object -Property Length -Sum).Sum
Write-Log ("Source size : {0:N1} GB" -f ($srcSize / 1GB))

# Robocopy args - /MIR handles both full (first run) and refresh (later runs)
# by only copying files that are new/changed at the source.
$rcArgs = @(
    $Source,
    $BackupRoot,
    '/MIR',          # Mirror source -> target (full on first, incremental after)
    '/COPY:DAT',     # Data, Attributes, Timestamps
    '/R:3',          # 3 retries (models are large; locks possible during download)
    '/W:10',         # 10 sec between retries
    '/MT:4',         # 4 threads (fewer than projects: large files, less contention)
    '/NP',           # No progress percentage
    '/NDL',          # No directory list
    '/TEE',
    "/LOG:$logFile"
)

# Exclude transient scratch: output folders, lock/tmp files
$rcArgs += '/XD'
$rcArgs += (Join-Path $Source 'output')
$rcArgs += '/XF'
$rcArgs += '*.lock'
$rcArgs += '*.tmp'
$rcArgs += '*.part'
$rcArgs += '*.download'

Write-Log "robocopy args: $($rcArgs -join ' ')"

$rcExe = "$env:SystemRoot\System32\robocopy.exe"
$rc = & $rcExe @rcArgs
$rcCode = $LASTEXITCODE
Write-Log "robocopy exit code: $rcCode"

# robocopy: 0-7 = success, 8+ = failure
if ($rcCode -ge 8) {
    Write-Log "ERROR: Models backup FAILED (rc=$rcCode). See $logFile"
    exit 1
}

# Capture the summary lines
$summary = $rc | Where-Object { $_ -match '^\s*(Files|Dirs|Bytes|Total|Copied|Skipped|Mismatch|FAILED|Extras)' }
foreach ($line in $summary) { Write-Log $line }

# Retention: keep last 6 model backup logs (bi-monthly history)
$logs = Get-ChildItem -Path $logDir -Filter "models-backup-*.log" |
    Sort-Object LastWriteTime -Descending
if ($logs.Count -gt 6) {
    $logs | Select-Object -Skip 6 | Remove-Item -Force
}

Write-Log "=== Models backup COMPLETE rc=$rcCode ==="
exit 0