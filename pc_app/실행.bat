@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === Floor Impact Sound Meter ===
echo.

where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)

%PY% -c "import PySide6, numpy, scipy" >nul 2>nul
if %errorlevel% neq 0 (
    echo Installing required packages. This only happens once...
    %PY% -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo.
        echo Installation failed. Check that Python is installed.
        pause
        exit /b 1
    )
)

if "%~1"=="" (
    echo Running against real hardware.
    echo   To try it without hardware:  run.bat --demo
    echo.
)

%PY% run_gui.py %*
if %errorlevel% neq 0 pause
