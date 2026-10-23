@echo off
title Wi-Fi House Walk-Test ^& Coverage Scanner
cd /d "%~dp0"
echo Starting local Wi-Fi Scanner server...

where python >nul 2>nul
if not errorlevel 1 (
    python app.py
    goto :end
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 app.py
    goto :end
)
echo Python was not found. Install Python 3 and make sure it is on PATH.

:end
pause
