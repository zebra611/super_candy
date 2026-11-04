@echo off
chcp 65001 >nul
title super_candy - dummy pipeline
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem Step 1: find a working Python.
rem The detection itself lives in _pycheck.bat so that this file and the
rem all-in-one launcher share exactly one definition of "can this machine run".
rem ---------------------------------------------------------------------------

set "PYEXE="
for /f "tokens=1,* delims==" %%A in ('call "%~dp0_pycheck.bat"') do (
    if /i "%%A"=="FOUND" set "PYEXE=%%B"
)

rem Called by the all-in-one launcher: report the result and leave.
if /i "%~1"=="--check" (
    echo FOUND=%PYEXE%
    exit /b 0
)

if /i "%PYEXE%"=="NONE" set "PYEXE="

if not defined PYEXE (
    echo.
    echo   [ERROR] No usable Python found on this machine.
    echo.
    echo   Install one from https://www.python.org/downloads/
    echo   and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

rem ---------------------------------------------------------------------------
rem Step 2: start the server. app.py opens the browser itself once the port is
rem already listening, so no delay logic is needed here.
rem ---------------------------------------------------------------------------

echo.
echo   Using Python: %PYEXE%
echo   Starting server on http://127.0.0.1:8000
echo   Browser opens automatically. Close this window to stop the server.
echo.

%PYEXE% app.py

echo.
echo   Server stopped.
pause
exit /b 0
