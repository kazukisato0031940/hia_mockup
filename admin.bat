@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo  管理者アカウントの確認 / パスワードの再設定
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
echo   python.org から Python 3 をインストールし、
echo   インストール時に "Add python.exe to PATH" にチェックを入れてください。
echo.
pause
exit /b 1
:HAVEPY

if not exist hia.db (
  echo [エラー] hia.db がありません。先に run.bat を実行してください。
  pause
  exit /b 1
)
%PY% reset_admin.py
echo.
set EMAIL=
set /p EMAIL="パスワードを再設定するログインIDを入力（そのままEnterで終了）: "
if not defined EMAIL goto END
%PY% reset_admin.py %EMAIL%
:END
echo.
pause
endlocal
