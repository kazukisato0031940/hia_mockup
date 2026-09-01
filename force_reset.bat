@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo  管理者パスワードの強制リセット
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

echo 使用する Python: %PY%
%PY% -V
if errorlevel 1 (
  echo.
  echo [エラー] Python の実行に失敗しました。
  pause
  exit /b 1
)
echo.
if not exist hia.db (
  echo [エラー] hia.db がありません。先に run.bat を実行してください。
  pause
  exit /b 1
)
echo システム管理者のパスワードを、いま入力する値で上書きします。
echo 入力した文字は画面に表示されません。
echo.
%PY% reset_admin.py --admin
if errorlevel 1 (
  echo.
  echo [エラー] リセットに失敗しました。上のメッセージを確認してください。
) else (
  echo.
  echo リセットが完了しました。
  echo http://10.0.105.122:8000/login からログインしてください。
)
echo.
pause
endlocal
