param(
    [string]$TaskName = "ForexAutoTraderBackup",
    [int]$Hour = 2,
    [int]$Minute = 15
)

$ErrorActionPreference = "Stop"

if ($Hour -lt 0 -or $Hour -gt 23 -or $Minute -lt 0 -or $Minute -gt 59) {
    throw "Hour must be 0-23 and Minute must be 0-59."
}

$Runner = Resolve-Path (Join-Path $PSScriptRoot "run-backup-cycle.ps1")
$At = Get-Date -Hour $Hour -Minute $Minute -Second 0

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`""

$Trigger = New-ScheduledTaskTrigger -Daily -At $At
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Phase 13 verified runtime backup and off-device replication for Forex auto-trader" `
    -Force

Write-Host "Installed backup scheduled task: $TaskName"
Write-Host "Daily schedule: $($At.ToString('HH:mm'))"
Write-Host "Set FOREX_BACKUP_REPLICA_ROOT before the task runs."
Write-Host "Retention deletion remains disabled unless FOREX_BACKUP_APPLY_RETENTION and FOREX_BACKUP_PRUNE_ACK are explicitly set."
