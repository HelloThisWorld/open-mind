@echo off
cd /d "%~dp0"
echo Starting Open Mind on http://127.0.0.1:8077  (Ctrl+C to stop)
rem --timeout-graceful-shutdown: without it uvicorn waits forever on Ctrl+C for
rem in-flight requests (an open SSE job stream never finishes) - 5s then exit.
python -m uvicorn openmind.main:app --host 127.0.0.1 --port 8077 --timeout-graceful-shutdown 5
