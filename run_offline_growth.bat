@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if exist %~dp0FROZEN exit /b 0
cd /d %~dp0
python "%~dp0offline_growth.py" >> "%~dp0logs\growth_offline.log" 2>&1
