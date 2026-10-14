<#
.SYNOPSIS
  Registers the weekly-full + daily-differential backup schedule for
  LocalAIContent projects using the configured source and backup root.

  Creates two Windows Scheduled Tasks:
    LocalAIContent-Backup-Full   - every Sunday 03:00, weekly
    LocalAIContent-Backup-Diff  - Monday-Saturday 03:00, daily

  Runs whether the user is logged in or not. Uses the current user.

.EXAMPLE
  .\ops\register-backup-schedule.ps1 -Source "C:\LocalAIContent\projects" -BackupRoot "E:\Backups\LocalAIContent"
  # Or set LOCALAI_PROJECTS_ROOT and LOCALAI_PROJECTS_BACKUP_ROOT.

.NOTES
  To unregister later:
    Unregister-ScheduledTask -TaskName 'LocalAIContent-Backup-Full'  -Confirm:$false
    Unregister-ScheduledTask -TaskName 'LocalAIContent-Backup-Diff'  -Confirm:$false
#>

[CmdletBinding()]
param(
    [string]$ScriptPath,
    [string]$Source = $env:LOCALAI_PROJECTS_ROOT,
    [string]$BackupRoot = $env:LOCALAI_PROJECTS_BACKUP_ROOT
)

$ErrorActionPreference = 'Stop'
if (-not $Source -or -not $BackupRoot) {
    throw "Pass -Source and -BackupRoot, or set LOCALAI_PROJECTS_ROOT and LOCALAI_PROJECTS_BACKUP_ROOT before registering tasks."
}
$backupArguments = "-Source `"$Source`" -BackupRoot `"$BackupRoot`""

# Resolve the backup script path robustly whether $PSScriptRoot is populated
# (dot-sourced) or empty (invoked via -File from a working directory).
if (-not $ScriptPath) {
    $ScriptPath = Join-Path (Split-Path $MyInvocation.MyCommand.Path -Parent) 'backup-projects.ps1'
}

if (-not (Test-Path -LiteralPath $ScriptPath)) {
    throw "Backup script not found at: $ScriptPath"
}

$currentUser = "$env:USERDOMAIN\$env:USERNAME"
Write-Host "Registering backup tasks as user: $currentUser"
Write-Host "Script path: $ScriptPath"

# Full task: weekly Sunday 03:00
$fullAction   = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -Mode full $backupArguments"
$fullTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 3:00am
$fullSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName 'LocalAIContent-Backup-Full' `
    -Description 'Weekly FULL mirror of configured LocalAIContent project source' `
    -Action $fullAction -Trigger $fullTrigger -Settings $fullSettings `
    -User $currentUser -RunLevel Highest -Force | Out-Null

# Diff task: daily Monday-Saturday 03:00
$diffAction   = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -Mode diff $backupArguments"
$diffTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday,Saturday -At 3:00am
$diffSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName 'LocalAIContent-Backup-Diff' `
    -Description 'Daily DIFFERENTIAL mirror of configured LocalAIContent project source' `
    -Action $diffAction -Trigger $diffTrigger -Settings $diffSettings `
    -User $currentUser -RunLevel Highest -Force | Out-Null

Write-Host "Both tasks registered." -ForegroundColor Green
Write-Host ""
Write-Host "Verify:"
Write-Host "  Get-ScheduledTask -TaskName 'LocalAIContent-Backup-*' | Select TaskName,State,Author"
Write-Host ""
Write-Host "Run the full backup now to seed the configured backup root:"
Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -Mode full $backupArguments"
Write-Host ""
Write-Host "Unregister later:"
Write-Host "  Unregister-ScheduledTask -TaskName 'LocalAIContent-Backup-Full' -Confirm:`$false"
Write-Host "  Unregister-ScheduledTask -TaskName 'LocalAIContent-Backup-Diff' -Confirm:`$false"