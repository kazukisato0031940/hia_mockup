@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo  ロールごとのサンプルアカウントを作成 / 更新
echo ============================================================
echo.
set PY=
where python >nul 2>nul
if %errorlevel%==0 set PY=python
if defined PY goto HAVEPY
where py >nul 2>nul
if %errorlevel%==0 set PY=py -3
if defined PY goto HAVEPY
where python3 >nul 2>nul
if %errorlevel%==0 set PY=python3
if defined PY goto HAVEPY
echo.
echo [エラー] Python が見つかりません。
pause
exit /b 1
:HAVEPY

if not exist hia.db (
  echo [エラー] hia.db がありません。先に run.bat を実行してください。
  pause
  exit /b 1
)
%PY% seed_samples.py
echo.
pause
endlocal
