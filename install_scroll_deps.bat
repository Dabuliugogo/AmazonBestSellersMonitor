@echo off
rem ============================================================
rem  亚马逊 BSR TOP100 店铺监控 - 安装"浏览器滚动抓满 TOP100"依赖(playwright)
rem  复用本机已安装 Edge/Chrome，不会下载浏览器内核
rem  安装一次即可；不装也不影响 HTTP 直抓(自动模式会回退)
rem ============================================================
cd /d "%~dp0"

set PY=python
if exist "%~dp0python_local.txt" set /p PY=<"%~dp0python_local.txt"
if not exist "%PY%" goto :no_py

echo Installing playwright via: %PY%
"%PY%" -m pip install playwright
if errorlevel 1 goto :fail
echo.
echo [OK] 依赖安装完成。重启工具后，在 ⑤设置 →「抓取与代理」选择
echo      「自动（推荐）」或「浏览器滚动」，保存后即可抓满 100 款。
pause
exit /b 0

:no_py
echo [ERROR] 未找到 Python（%PY%）。请先安装 Python 或在 python_local.txt 配置路径。
pause
exit /b 1

:fail
echo [ERROR] pip 安装失败，请手动执行: python -m pip install playwright
pause
exit /b 1
