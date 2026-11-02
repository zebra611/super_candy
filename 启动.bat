@echo off
chcp 65001 >nul
title super_candy - dummy pipeline
cd /d "%~dp0"
setlocal enabledelayedexpansion
set "PYEXE="

rem ---------------------------------------------------------------------------
rem Step 1: find a working Python.
rem Note: this machine's PATH "python" is the Microsoft Store stub, which only
rem prints "Python was not found". The only reliable test is really importing
rem something, so every candidate is executed once with `import unicodedata`.
rem ---------------------------------------------------------------------------

for %%C in (python python3) do (
    if not defined PYEXE call :trycmd %%C
)

if not defined PYEXE (
    py -3 -c "import unicodedata" >nul 2>nul
    if not errorlevel 1 set "PYEXE=py -3"
)

rem WorkBuddy ships its own Python; scan all versions and let the import test
rem reject the broken ".old" copy automatically.
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

if /i "%~1"=="--check" (
    if defined PYEXE (echo FOUND=!PYEXE!) else (echo FOUND=NONE)
    exit /b 0
)

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
rem Step 2: start the server. app.py opens the browser itself after the port is
rem already listening, so we do not need any delay logic here.
rem ---------------------------------------------------------------------------

echo.
echo   Using Python: !PYEXE!
echo   Starting server on http://127.0.0.1:8000
echo   Browser opens automatically. Close this window to stop the server.
echo.

!PYEXE! app.py

echo.
echo   Server stopped.
pause
exit /b 0

rem ---------------------------------------------------------------------------
rem Subroutines
rem ---------------------------------------------------------------------------
:trycmd
%1 -c "import unicodedata" >nul 2>nul
if not errorlevel 1 set "PYEXE=%1"
exit /b

:tryexe
"%~1" -c "import unicodedata" >nul 2>nul
if not errorlevel 1 set "PYEXE=%~1"
exit /b
