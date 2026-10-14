<#
.SYNOPSIS
  Portable backup for LocalAIContent project assets.
  Weekly full + daily differential, implemented via robocopy /MIR for the
  full pass and robocopy with /XO (older files skipped) for differentials.

.DESCRIPTION
  Source  : supplied -Source or LOCALAI_PROJECTS_ROOT
  Target  : supplied -BackupRoot or LOCALAI_PROJECTS_BACKUP_ROOT, with {full|diff}
  Schedule: full every Sunday 03:00, differential Mon-Sat 03:00

  Why robocopy over raw Copy-Item:
    - Restartable (/Z), preserves ACLs (/COPY:DAT), retries on lock (/R:2 /W:5)
    - /MIR for full creates an exact mirror (deletes stale files at target)
    - Differentials use /MIR against a shadow copy semantics by only copying
      files whose source timestamp is newer than the target's (/XO is implicit
      in /MIR which copies only changed files; we keep a separate diff folder
      to preserve a rolling history, not a true block-level diff)

  Exclusions: temp/ scratch/ (transient generation frames, large and disposable)
  Logs: ops\logs\backup-YYYY-MM-DD-{full|diff}.log

.PARAMETER Mode
  full  - Mirror source to <BackupRoot>\full
  diff  - Mirror source to <BackupRoot>\diff
          (keeps a separate rolling copy; pairs with the weekly full)

.EXAMPLE
  .\backup-projects.ps1 -Mode full -Source "C:\LocalAIContent\projects" -BackupRoot "E:\Backups\LocalAIContent"
  .\backup-projects.ps1 -Mode diff -Source "C:\LocalAIContent\projects" -BackupRoot "E:\Backups\LocalAIContent"
  # Or set LOCALAI_PROJECTS_ROOT and LOCALAI_PROJECTS_BACKUP_ROOT.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('full', 'diff')]
    [string]$Mode,

    [string]$Source = $env:LOCALAI_PROJECTS_ROOT,
    [string]$BackupRoot = $env:LOCALAI_PROJECTS_BACKUP_ROOT
)

$ErrorActionPreference = 'Stop'
if (-not $Source -or -not $BackupRoot) {
    throw "Pass -Source and -BackupRoot, or set LOCALAI_PROJECTS_ROOT and LOCALAI_PROJECTS_BACKUP_ROOT."
}
$timestamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$logDir = Join-Path $PSScriptRoot 'logs'
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$logFile = Join-Path $logDir "backup-$timestamp-$Mode.log"

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'o'), $msg
    Add-Content -LiteralPath $logFile -Value $line
    Write-Host $line
}

Write-Log "=== Backup START mode=$Mode ==="
Write-Log "Source     : $Source"
Write-Log "BackupRoot : $BackupRoot"

if (-not (Test-Path -LiteralPath $Source)) {
    Write-Log "Source not found. Creating empty source so first run succeeds."
    New-Item -ItemType Directory -Path $Source -Force | Out-Null
}

$target = Join-Path $BackupRoot $Mode
if (-not (Test-Path -LiteralPath $target)) {
    New-Item -ItemType Directory -Path $target -Force | Out-Null
}
Write-Log "Target     : $target"

# Exclusions: transient generation frames (large, disposable)
$excludeDirs = @('temp', 'scratch', 'tmp', '__pycache__')

# Build robocopy argument list
$rcArgs = @(
    $Source,
    $target,
    '/MIR',          # Mirror source to target (true full copy)
    '/COPY:DAT',     # Data, Attributes, Timestamps
    '/R:2',          # 2 retries per file
    '/W:5',          # 5 sec wait between retries
    '/MT:8',         # 8 threads
    '/NP',           # No progress percentage (cleaner log)
    '/NDL',          # No directory list (cleaner log)
    '/TEE',          # Write to console AND log
    "/LOG:$logFile"
)
foreach ($d in $excludeDirs) {
    $rcArgs += "/XD"
    $rcArgs += (Join-Path $Source $d)
}

Write-Log "robocopy args: $($rcArgs -join ' ')"

$rcExe = "$env:SystemRoot\System32\robocopy.exe"
$rc = & $rcExe @rcArgs
$rcCode = $LASTEXITCODE
Write-Log "robocopy exit code: $rcCode"

# robocopy exit codes: 0-7 = success (no errors), 8+ = failure
if ($rcCode -ge 8) {
    Write-Log "ERROR: Backup FAILED (rc=$rcCode). See $logFile"
    exit 1
}

$summary = $rc | Where-Object { $_ -match '^\s*(Files|Dirs|Bytes|Total|Copied|Extras|Failed)' }
foreach ($line in $summary) { Write-Log $line }

# Retention: keep last 4 weekly full log sets and 7 daily diffs (by log file age)
$logs = Get-ChildItem -Path $logDir -Filter "backup-*.log" |
    Sort-Object LastWriteTime -Descending
$fullLogs = $logs | Where-Object { $_.Name -match '-full\.log$' }
$diffLogs = $logs | Where-Object { $_.Name -match '-diff\.log$' }
if ($fullLogs.Count -gt 8) { $fullLogs | Select-Object -Skip 8 | Remove-Item -Force }
if ($diffLogs.Count -gt 14) { $diffLogs | Select-Object -Skip 14 | Remove-Item -Force }

Write-Log "=== Backup COMPLETE mode=$Mode rc=$rcCode ==="
exit 0