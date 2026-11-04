@echo off
chcp 65001 >nul
title super_candy - all services
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem 一键起两个进程：Mock 业务系统（:8100）+ 通话服务（:8000）。
rem Python 的探测逻辑不重复实现，直接复用 启动.bat --check 的结果，
rem 这样"能跑起来"的判断标准永远只有一份（它真的会 import 一次模块）。
rem ---------------------------------------------------------------------------

set "PYEXE="
for /f "tokens=2 delims==" %%P in ('call "%~dp0启动.bat" --check') do set "PYEXE=%%P"

if not defined PYEXE goto :nopython
if /i "%PYEXE%"=="NONE" goto :nopython

echo.
echo   Python : %PYEXE%
echo   即将启动：
echo     [1] Mock 业务系统  http://127.0.0.1:8100/mock/health
echo     [2] 通话服务       http://127.0.0.1:8000
echo                        http://127.0.0.1:8000/dashboard   ^(指标看板^)
echo.
echo   Mock 在单独窗口里跑，关掉那个窗口就是停 Mock；本窗口 Ctrl+C 停通话服务。
echo.

rem 新窗口跑 Mock，日志与主服务分开看，排障时不用在长日志里翻
start "super_candy mock :8100" cmd /k ""%PYEXE%" mock_business\service.py"

rem 给 Mock 一点启动时间，避免通话服务一起来就发现"业务接口连不上"
timeout /t 2 >nul

"%PYEXE%" app.py

echo.
echo   通话服务已停止。
pause
exit /b 0

:nopython
echo.
echo   [ERROR] 没找到可用的 Python。请先安装 Python 3.9+ 并勾选 "Add python.exe to PATH"。
echo           或者直接双击 启动.bat 看它报什么错。
echo.
pause
exit /b 1
