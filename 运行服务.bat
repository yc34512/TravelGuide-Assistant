@echo off
cd /d "%~dp0"
rem 优先使用 install.bat 创建的虚拟环境，其次 PATH 与 py 启动器
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run_server.py
    goto :end
)
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
echo [X] Python not found: run install.bat first, or install Python 3.10+ (check Add to PATH)
:end
pause
