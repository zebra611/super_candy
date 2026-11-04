@echo off
rem ---------------------------------------------------------------------------
rem Locate a Python that can actually import something.
rem
rem Why this file exists in its own ASCII-named batch:
rem   1. Two launchers need the same detection. Reusing it from one place means
rem      "can this machine run the project" has exactly one definition.
rem   2. The launchers must stay pure ASCII (cmd parses .bat with the console
rem      code page; a stray multibyte char truncates the line). One launcher is
rem      the Chinese-named entry point, so it cannot be spelled from the other
rem      one without putting Chinese back into a .bat. Calling this file solves
rem      both problems at once.
rem
rem Contract: prints exactly one line on stdout, either
rem     FOUND=<command or quoted path>
rem     FOUND=NONE
rem Note: the PATH "python" on some machines is the Microsoft Store stub, which
rem only prints "Python was not found". The only reliable test is to really
rem import a module, so every candidate is executed once.
rem ---------------------------------------------------------------------------

setlocal enabledelayedexpansion
set "PYEXE="

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

if defined PYEXE (echo FOUND=!PYEXE!) else (echo FOUND=NONE)
exit /b 0

:trycmd
%1 -c "import unicodedata" >nul 2>nul
if not errorlevel 1 set "PYEXE=%1"
exit /b

:tryexe
"%~1" -c "import unicodedata" >nul 2>nul
if not errorlevel 1 set "PYEXE=%~1"
exit /b
