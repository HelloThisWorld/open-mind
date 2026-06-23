@echo off
cd /d "%~dp0"
echo Starting Open Mind on http://127.0.0.1:8077  (Ctrl+C to stop)
python -m uvicorn openmind.main:app --host 127.0.0.1 --port 8077
