@echo off
chcp 65001 >nul
title INO Companion Agent
cd /d "%~dp0"

echo ===================================================
echo           🌸 INO Companion Agent 启动中...
echo ===================================================

if not exist ".venv\Scripts\python.exe" (
    echo [!] 未检测到虚拟环境，正在自动启动一键部署向导...
    call setup_windows.bat
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" app.py
) else (
    python app.py
)

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Agent 异常退出，错误码: %ERRORLEVEL%。
    pause
)
