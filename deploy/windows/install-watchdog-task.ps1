param(
    [string]$TaskName = "ForexAutoTraderWatchdog"
)

$ErrorActionPreference = "Stop"
$Runner = Resolve-Path (Join-Path $PSScriptRoot "run-watchdog.ps1")

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`""

$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 30)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Independent Phase 10 heartbeat watchdog for the Forex auto-trader" `
    -Force

Write-Host "Installed watchdog scheduled task: $TaskName"
Write-Host "For real external monitoring, run this task on a second machine and point it to a shared/remote heartbeat source."
