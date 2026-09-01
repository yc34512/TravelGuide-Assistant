@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   TravelGuide Assistant - One-click Install
echo ============================================

rem --- 已有虚拟环境则直接跳到依赖安装（可重复执行）---
if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment .venv ...
    rem 探测 Python：PATH 优先，其次 py 启动器（探测与创建在同一括号块内，避免延迟变量问题）
    where python >nul 2>nul && python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        where py >nul 2>nul && py -m venv .venv
    )
    if not exist ".venv\Scripts\python.exe" (
        echo [X] Python not found, or failed to create virtual environment.
        echo [X] Install Python 3.10+ first and check "Add to PATH": https://www.python.org/downloads/
        pause
        exit /b 1
    )
) else (
    echo [1/3] Virtual environment .venv exists, skipping creation.
)

echo [2/3] Installing dependencies, may take a few minutes on first run ...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [X] Dependency install failed. Check network or try a mirror:
    echo     ".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
)

echo [3/3] Self-check: importing key packages ...
".venv\Scripts\python.exe" -c "import DrissionPage, openai, fastapi, keyring, rich; print('[OK] all dependencies ready')"
if errorlevel 1 (
    echo [X] Self-check failed. Re-run this script or check the error above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Install finished.
echo   Next: double-click  运行.bat      (CLI mode)
echo   or:   double-click  运行服务.bat  (Web UI, recommended)
echo ============================================
pause
