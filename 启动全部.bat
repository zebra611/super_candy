@echo off
chcp 65001 >nul
title super_candy - all services
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem Start two processes: the mock business service (:8100) and the call service
rem (:8000). Python detection is shared with the single-service launcher through
rem _pycheck.bat, so "can this machine run it" has one definition.
rem This file stays pure ASCII on purpose: cmd parses .bat with the console code
rem page, and a multibyte char can truncate the line mid-command.
rem ---------------------------------------------------------------------------

set "PYEXE="
for /f "tokens=1,* delims==" %%A in ('call "%~dp0_pycheck.bat"') do (
    if /i "%%A"=="FOUND" set "PYEXE=%%B"
)
if /i "%PYEXE%"=="NONE" set "PYEXE="
if not defined PYEXE goto :nopython

echo.
echo   Python : %PYEXE%
echo   Starting:
echo     [1] Mock business service  http://127.0.0.1:8100/mock/health
echo     [2] Call service           http://127.0.0.1:8000
echo                                http://127.0.0.1:8000/dashboard   ^(metrics^)
echo.
echo   Mock runs in its own window; close that window to stop Mock.
echo   Ctrl+C in this window stops the call service.
echo.

rem Run mock in a separate window so its log does not mix with the main service.
start "super_candy mock :8100" cmd /k ""%PYEXE%" mock_business\service.py"

rem Give mock a moment, otherwise the call service may report "business API down".
timeout /t 2 >nul

%PYEXE% app.py

echo.
echo   Call service stopped.
pause
exit /b 0

:nopython
echo.
echo   [ERROR] No usable Python found on this machine.
echo.
echo   Install one from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" during setup.
echo   Or double-click the single-service launcher and read what it says.
echo.
pause
exit /b 1
