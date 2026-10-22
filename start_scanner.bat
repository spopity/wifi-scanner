@echo off
title Wi-Fi House Walk-Test & Coverage Scanner
cd /d "%~dp0"
echo Starting local Wi-Fi Scanner server...
"C:\Program Files\Python313\python.exe" app.py
pause
