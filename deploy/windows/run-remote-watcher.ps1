param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$ConfigPath = "remote_watcher.yaml",
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectRoot

& $PythonExe -m src.remote_watcher --root $ProjectRoot --config $ConfigPath
$code = $LASTEXITCODE
exit $code
