@echo off
cd /d "%~dp0"
rem 自动探测 Python：PATH 优先，其次 py 启动器；找不到时提示安装
where python >nul 2>nul
if %errorlevel%==0 (
    python run_server.py
    goto :end
)
where py >nul 2>nul
if %errorlevel%==0 (
    py run_server.py
    goto :end
)
echo [X] 未找到 Python：请先安装 Python 3.10+（安装时勾选 Add to PATH）
:end
pause
