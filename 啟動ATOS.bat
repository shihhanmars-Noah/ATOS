@echo off
chcp 65001 > nul
title ATOS Commander
cd /d "%~dp0"
set PYTHONUTF8=1

echo.
echo  ============================================
echo     ATOS Commander
echo  ============================================
echo.

python --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    pause
    exit /b 1
)

if not exist "main_commander.py" (
    echo [ERROR] main_commander.py not found.
    pause
    exit /b 1
)

:LOOP
echo [START] python main_commander.py
python main_commander.py
set ERR=%ERRORLEVEL%

if %ERR% == 0 (
    echo [DONE] Exited normally.
    pause
    exit /b 0
)

echo [WARN] Crashed with code %ERR%. Retrying in 10s...
timeout /t 10 /nobreak > nul
goto LOOP
