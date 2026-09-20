param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$ConfigPath = "remote_watcher.yaml",
    [string]$TaskName = "Forex Remote Liveness Watcher",
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$runner = Join-Path $ProjectRoot "deploy\windows\run-remote-watcher.ps1"
if (-not (Test-Path $runner)) {
    throw "Remote watcher runner not found: $runner"
}

$taskCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runner`" -ProjectRoot `"$ProjectRoot`" -ConfigPath `"$ConfigPath`" -PythonExe `"$PythonExe`""
& schtasks.exe /Create /TN $TaskName /SC MINUTE /MO 1 /TR $taskCommand /F | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Failed to create scheduled task '$TaskName' (exit $LASTEXITCODE)."
}

Write-Host "Created scheduled task '$TaskName' to run the independent remote watcher every minute."
Write-Host "For unattended use, configure the task with a dedicated service account that can read the replica root."
