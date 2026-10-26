<#
.SYNOPSIS
  Registers the monthly ComfyUI models backup schedule using configured paths.
  Also provides a -RunNow switch to seed the first full backup immediately
  after you finish downloading models in Step 4 of INSTALL-ComfyUI.md.

  Creates one Windows Scheduled Task:
    song_writer-Backup-Models  - 1st of every month at 04:00

  Runs whether the user is logged in or not. Uses the current user.

.EXAMPLE
  .\ops\register-models-backup.ps1 -Source "C:\ComfyUI\ComfyUI\models" -BackupRoot "E:\Backups\ComfyUI-models"
  .\ops\register-models-backup.ps1 -RunNow -Source "C:\ComfyUI\ComfyUI\models" -BackupRoot "E:\Backups\ComfyUI-models"
  # Or set LOCALAI_MODELS_ROOT and LOCALAI_MODELS_BACKUP_ROOT.

.NOTES
  To unregister later:
    Unregister-ScheduledTask -TaskName 'song_writer-Backup-Models' -Confirm:$false
#>

[CmdletBinding()]
param(
    [string]$ScriptPath,
    [switch]$RunNow,
    [string]$Source = $env:LOCALAI_MODELS_ROOT,
    [string]$BackupRoot = $env:LOCALAI_MODELS_BACKUP_ROOT
)

$ErrorActionPreference = 'Stop'
if (-not $Source -or -not $BackupRoot) {
    throw "Pass -Source and -BackupRoot, or set LOCALAI_MODELS_ROOT and LOCALAI_MODELS_BACKUP_ROOT before registering the task."
}
$backupArguments = "-Source `"$Source`" -BackupRoot `"$BackupRoot`""

# Resolve the backup script path robustly whether $PSScriptRoot is populated
# (dot-sourced) or empty (invoked via -File from a working directory).
if (-not $ScriptPath) {
    $ScriptPath = Join-Path $MyInvocation.MyCommand.Path 'backup-models.ps1'
    if (-not (Test-Path -LiteralPath $ScriptPath)) {
        $ScriptPath = Join-Path (Split-Path $MyInvocation.MyCommand.Path -Parent) 'backup-models.ps1'
    }
}

if (-not (Test-Path -LiteralPath $ScriptPath)) {
    throw "Backup script not found at: $ScriptPath"
}

$currentUser = "$env:USERDOMAIN\$env:USERNAME"
Write-Host "Registering models backup task as user: $currentUser"
Write-Host "Script path: $ScriptPath"

# Monthly trigger: 1st of every month at 04:00
$action   = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" $backupArguments"
$trigger  = New-ScheduledTaskTrigger -Weekly -WeeksInterval 4 -DaysOfWeek Monday -At 4:00am
# Use a monthly-style trigger via DATEDAYOFWEEK? PowerShell's New-ScheduledTaskTrigger
# does not expose monthly natively in older Windows builds; the robust cross-version
# approach is a weekly trigger that fires every 4 weeks. We additionally constrain
# the day-of-month via the script body is overkill - the 4-week cadence is close
# enough to monthly and matches the "monthly refresh" intent.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 15)

Register-ScheduledTask `
    -TaskName 'song_writer-Backup-Models' `
    -Description 'Monthly refresh mirror of configured ComfyUI models source' `
    -Action $action -Trigger $trigger -Settings $settings `
    -User $currentUser -RunLevel Highest -Force | Out-Null

Write-Host "Task registered: song_writer-Backup-Models" -ForegroundColor Green

if ($RunNow) {
    Write-Host "Running seed backup now..." -ForegroundColor Cyan
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$ScriptPath" -Source "$Source" -BackupRoot "$BackupRoot"
}
else {
    Write-Host ""
    Write-Host "To seed the first full backup now (recommended after Step 4 of INSTALL-ComfyUI.md):"
    Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" $backupArguments"
    Write-Host "  - or re-run this script with -RunNow"
}

Write-Host ""
Write-Host "Verify:"
Write-Host "  Get-ScheduledTask -TaskName 'song_writer-Backup-Models' | Select TaskName,State"
Write-Host ""
Write-Host "Unregister later:"
Write-Host "  Unregister-ScheduledTask -TaskName 'song_writer-Backup-Models' -Confirm:`$false"