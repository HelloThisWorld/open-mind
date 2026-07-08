# Open Mind — local launcher (Windows PowerShell)
# Serves the web UI + REST API on http://127.0.0.1:8077 (local-only).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Write-Host "Starting Open Mind on http://127.0.0.1:8077  (Ctrl+C to stop)" -ForegroundColor Cyan
# --timeout-graceful-shutdown: without it uvicorn waits FOREVER on Ctrl+C for
# in-flight requests to finish; an open SSE job stream never finishes, so the
# server appeared to hang on shutdown. 5s drains politely, then exits.
python -m uvicorn openmind.main:app --host 127.0.0.1 --port 8077 --timeout-graceful-shutdown 5
