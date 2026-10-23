@echo off
chcp 65001 >nul
title Stop QQ-DeepSeek
echo Stopping QQ-DeepSeek processes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*qq-deepseek*' -and $_.Name -ne 'powershell.exe' -and $_.Name -ne 'cmd.exe' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Host ('stopped ' + $_.Name + ' (' + $_.ProcessId + ')') } catch {} }"
echo Done.
timeout /t 2 >nul
