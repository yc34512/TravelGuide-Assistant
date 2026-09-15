@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 优先使用 install.bat 创建的虚拟环境，其次 PATH 与 py 启动器
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run_cli.py %*
    goto :end
)
where python >nul 2>nul
if %errorlevel%==0 (
    python run_cli.py %*
    goto :end
)
where py >nul 2>nul
if %errorlevel%==0 (
    py run_cli.py %*
    goto :end
)
echo [X] 未找到 Python：请先双击 install.bat 一键安装；或安装 Python 3.10+ 并勾选 "Add to PATH"
:end
pause