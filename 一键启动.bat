@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title QQ-DeepSeek Bridge (XiaoSha)
cd /d "%~dp0"

echo ============================================
echo   QQ ?????QQ  ^<-^>  DeepSeek
echo ============================================
echo.
echo   Keep this window OPEN. Closing it stops the bot.
echo.
echo   If QQ is not logged in yet, a QR image pops up.
echo   WebUI: http://127.0.0.1:6099/webui?token=a892c3b8e942
echo.

python run.py

echo.
echo Program exited. Press any key to close.
pause >nul
