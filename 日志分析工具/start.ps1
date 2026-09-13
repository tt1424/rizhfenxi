$ErrorActionPreference = "Stop"
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path (Split-Path -Parent (Split-Path -Parent $appDir)) ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}

Set-Location -LiteralPath $appDir
& $python server.py
