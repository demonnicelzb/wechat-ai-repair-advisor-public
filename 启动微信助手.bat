@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 没找到 python，请先装 Python 3.9+ 并勾选 Add to PATH
  pause
  exit /b 1
)

echo ============================================================
echo   WeChat AI Repair Advisor
echo.
echo   normal run  : double click this file
echo   list chats  : 启动微信助手.bat --list
echo   change who  : 启动微信助手.bat --targets "name"
echo   dry-run     : 启动微信助手.bat --dry-run
echo   selftest    : 启动微信助手.bat --selftest
echo ============================================================
python auto_reply_v2.py %*
echo.
echo (process exited, press any key to close)
pause >nul
