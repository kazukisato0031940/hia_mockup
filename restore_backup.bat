@echo off
setlocal
cd /d "%~dp0"
rem ---- backup フォルダーの控えから hia.db を復元します ----
python restore_backup.py
pause
