@echo off
chcp 65001 >nul
title INO Accompany - Windows 一键部署向导
cd /d "%~dp0"

echo ================================================================
echo           🌸 INO Accompany 桌面智能守护伴侣 - Windows 一键部署向导
echo ================================================================
echo.

:: 1. 检查 Python 环境
echo [*] 正在检测 Python 环境...
python --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [!] 错误: 未检测到系统 Python。请先安装 Python 3.10 或 3.11 并勾选 "Add python.exe to PATH"。
    echo     官方下载: https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo [+] 检测到 Python 版本: %PY_VER%

:: 2. 创建或检查虚拟环境
if not exist ".venv" (
    echo [*] 正在创建虚拟环境 (.venv)...
    python -m venv .venv
    if %ERRORLEVEL% neq 0 (
        echo [!] 创建虚拟环境失败，请检查 Python 是否支持 venv 模块。
        pause
        exit /b 1
    )
    echo [+] 虚拟环境创建成功！
) else (
    echo [+] 虚拟环境 (.venv) 已存在，跳过创建。
)

:: 3. 升级 pip 并安装依赖
echo [*] 正在激活虚拟环境并安装项目依赖 (这可能需要几分钟)...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo [!] 依赖安装过程中出现警告或错误，请检查网络连接后重试。
    pause
    exit /b 1
)
echo [+] 核心依赖安装完毕！

:: 4. 初始化配置文件
if not exist "config.yaml" (
    echo [*] 正在从模板生成默认配置文件 config.yaml...
    copy config.example.yaml config.yaml >nul
    echo [+] 已自动生成 config.yaml。
) else (
    echo [+] 配置文件 config.yaml 已存在。
)

echo.
echo ================================================================
echo  🎉 恭喜！INO Accompany 一键部署完成！
echo.
echo  【启动方式】：
echo   1. 运行 start_ino.bat：前台启动，带控制台窗口查看交互与思考日志
echo   2. 运行 start_ino_silent.vbs：静默后台常驻启动（无黑框）
echo   3. 运行 powershell -File create_shortcut.ps1：在桌面生成快捷方式
echo.
echo  【模型准备】：
echo   请启动 LM Studio 并开启 Local Server (默认端口 1234)；
echo   或在系统环境变量中配置 DEEPSEEK_API_KEY 作为备用云端通道。
echo ================================================================
echo.
pause
