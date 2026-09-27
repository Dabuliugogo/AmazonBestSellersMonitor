@echo off
setlocal
cd /d "%~dp0"

rem --- 1) machine-specific override: python_local.txt (optional, do NOT share) ---
if not exist "%~dp0python_local.txt" goto :no_local
set /p PYPATH=<"%~dp0python_local.txt"
if not exist "%PYPATH%" goto :no_local
echo ============================================
echo   Amazon BSR TOP100 Store Monitor
echo   Starting with local python: %PYPATH%
echo ============================================
"%PYPATH%" bsr_monitor.py
goto :after_run

:no_local
where python >nul 2>nul
if errorlevel 1 goto :try_py
python -c "import sys" >nul 2>nul
if errorlevel 1 goto :try_py
echo ============================================
echo   Amazon BSR TOP100 Store Monitor
echo   Starting with python from PATH
echo ============================================
python bsr_monitor.py
goto :after_run

:try_py
where py >nul 2>nul
if errorlevel 1 goto :no_python
py -3 -c "import sys" >nul 2>nul
if errorlevel 1 goto :no_python
echo ============================================
echo   Amazon BSR TOP100 Store Monitor
echo   Starting with py launcher
echo ============================================
py -3 bsr_monitor.py
goto :after_run

:no_python
echo.
echo [ERROR] A real Python 3 was not found.
echo The Microsoft Store python alias does not count as Python.
echo Please install Python 3.10+ from https://www.python.org/downloads/
echo and check "Add python.exe to PATH" during installation.
echo Or ask the maintainer to build BSRMonitor.exe, then double-click that exe.
pause
exit /b 1

:after_run
echo.
echo The tool has exited. Press any key to close.
pause
