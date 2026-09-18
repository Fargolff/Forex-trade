param(
    [string]$TaskName = "ForexAutoTraderSupervisor"
)

$ErrorActionPreference = "Stop"
$Runner = Resolve-Path (Join-Path $PSScriptRoot "run-live-supervisor.ps1")

# This task only launches the wrapper. It does NOT set live.enabled and does
# NOT store FOREX_LIVE_ARM_PHRASE. Those remain explicit operator controls.
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`""

$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 30)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Supervises the guarded Forex auto-trader Phase 9 process" `
    -Force

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Live remains disabled unless config.yaml and FOREX_LIVE_ARM_PHRASE are explicitly configured."
