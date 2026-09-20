param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$ConfigPath = "watcher_quorum.yaml",
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectRoot

& $PythonExe -m src.watcher_quorum --mode verify --root $ProjectRoot --config $ConfigPath
$code = $LASTEXITCODE
exit $code
