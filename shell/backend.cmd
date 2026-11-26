@echo off
rem Shell starts the backend with: backend.cmd <port> <data dir>
rem Default: python\pythonw.exe next to this file, app.py next to this file.
rem Point PY / APP anywhere if your layout differs.
setlocal
set "PYTHONIOENCODING=utf-8"
set "PY=%~dp0python\pythonw.exe"
if not exist "%PY%" set "PY=python"
set "APP=%~dp0app.py"
"%PY%" "%APP%" --headless --port %1 --data-dir %2 1>"%~dp0backend.log" 2>&1