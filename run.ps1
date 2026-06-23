# Open Mind — local launcher (Windows PowerShell)
# Serves the web UI + REST API on http://127.0.0.1:8077 (local-only).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Write-Host "Starting Open Mind on http://127.0.0.1:8077  (Ctrl+C to stop)" -ForegroundColor Cyan
python -m uvicorn openmind.main:app --host 127.0.0.1 --port 8077
