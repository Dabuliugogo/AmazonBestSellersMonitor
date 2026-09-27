@echo off
cd /d "%~dp0"
echo ============================================
echo   Packaging BSR Monitor into exe
echo   Requires Python 3.10+ and pyinstaller
echo ============================================
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ first.
    pause
    exit /b 1
)
python -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
    echo Installing pyinstaller ...
    python -m pip install pyinstaller
)
echo Packaging, please wait 1-3 minutes ...
python -m PyInstaller --noconfirm --onefile --name BSRMonitor --clean bsr_monitor.py
if exist dist\BSRMonitor.exe (
    echo.
    echo SUCCESS: dist\BSRMonitor.exe
    echo Distribute dist\BSRMonitor.exe to end users.
    echo Note: the data\ folder is created next to the exe at runtime.
) else (
    echo PACKAGING FAILED. Check the log above.
)
pause
