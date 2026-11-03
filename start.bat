@echo off
chcp 65001 >nul
title 虚拟驾驶系统启动器

echo ========================================================
echo           沉浸式街景虚拟驾驶系统 (Virtual Driving)
echo ========================================================
echo.

cd /d "%~dp0"

:: 1. 默认端口参数
set APP_PORT=3000

:: 从 .env 读取配置端口
if exist .env (
    for /f "usebackq tokens=1,2 delims==" %%a in (".env") do (
        if /i "%%a"=="PORT" set APP_PORT=%%b
    )
)
if "%APP_PORT%"=="" set APP_PORT=3000
set APP_PORT=%APP_PORT: =%

:: 2. 检查并强制释放端口占用 (有残留进程先结束再继续)
echo [1/3] 检查端口 %APP_PORT% 占用状态...
python clean_port.py %APP_PORT%
echo.

:: 3. 检查依赖环境
echo [2/3] 正在检查依赖环境...
python -c "import streetlevel, aiohttp, PIL, fastapi, uvicorn, dotenv" >nul 2>nul
if errorlevel 1 (
    echo [*] 检测到依赖不完整，正在自动安装...
    python -m pip install streetlevel aiohttp pillow fastapi uvicorn python-dotenv
    if errorlevel 1 (
        echo.
        echo [!] 依赖安装失败，请检查网络或代理设置！
        echo.
        pause
        exit /b 1
    )
)

:: 4. 启动服务与浏览器
echo [3/3] 正在启动虚拟驾驶服务...
echo       服务地址: http://127.0.0.1:%APP_PORT%
echo       配置文件: .env
echo.

:: 延时 2 秒后自动打开浏览器
start "" cmd /c "ping 127.0.0.1 -n 3 >nul && start http://127.0.0.1:%APP_PORT%"

echo 服务运行中，按 Ctrl+C 可停止服务。
echo --------------------------------------------------------
python server.py

if errorlevel 1 (
    echo.
    echo [!] 服务异常退出，退出代码: %errorlevel%
)
pause
