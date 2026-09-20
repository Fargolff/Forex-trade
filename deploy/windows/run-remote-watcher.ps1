param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$ConfigPath = "remote_watcher.yaml",
    [string]$QuorumConfigPath = "watcher_quorum.yaml",
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectRoot

if (Test-Path $QuorumConfigPath) {
    & $PythonExe -m src.watcher_quorum --mode publish --root $ProjectRoot --config $QuorumConfigPath --watcher-config $ConfigPath
} else {
    & $PythonExe -m src.remote_watcher --root $ProjectRoot --config $ConfigPath
}
$code = $LASTEXITCODE
exit $code
