@echo off
chcp 65001 >nul
title super_candy - self evolution
cd /d "%~dp0"
setlocal enabledelayedexpansion
set "PYEXE="

rem ---------------------------------------------------------------------------
rem Same Python detection as the launcher: this machine PATH "python" is the
rem Microsoft Store stub, so each candidate is really executed once.
rem Keep this file pure ASCII. A single non-ASCII char here makes cmd eat the
rem next few characters of the line and every command breaks.
rem Double click = one full evolution round. No command line needed.
rem Default draft is a local deterministic template: no key, no network, no cost.
rem To let the model draft instead, append --llm when launching from a terminal.
rem ---------------------------------------------------------------------------

for %%C in (python python3) do (
    if not defined PYEXE call :trycmd %%C
)

if not defined PYEXE (
    py -3 -c "import unicodedata" >nul 2>nul
    if not errorlevel 1 set "PYEXE=py -3"
)

if not defined PYEXE (
    for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
        if not defined PYEXE if exist "%%D\python.exe" call :tryexe "%%D\python.exe"
    )
)

if not defined PYEXE (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "C:\Python313\python.exe"
        "C:\Python312\python.exe"
    ) do (
        if not defined PYEXE if exist %%P call :tryexe %%P
    )
)

if not defined PYEXE (
    echo.
    echo   [ERROR] No usable Python found on this machine.
    echo   Install one from https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo.
echo   Python: !PYEXE!
echo   Running scripts\auto_evolve.py ...
echo.

!PYEXE! scripts\auto_evolve.py %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo   Done. Read the raw output above, not a summary.
) else (
    echo   Stopped. Read the raw output above for the reason.
)
echo.
if "%NOPAUSE%"=="1" exit /b %RC%
pause
exit /b %RC%

:trycmd
%1 -c "import unicodedata" >nul 2>nul
if not errorlevel 1 set "PYEXE=%1"
exit /b

:tryexe
"%~1" -c "import unicodedata" >nul 2>nul
if not errorlevel 1 set "PYEXE=%~1"
exit /b
