@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo  当社スタッフ（管理者）のアカウントを用意します
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
echo [エラー] Python が見つかりません。
pause
exit /b 1
:HAVEPY

rem ---- ここを書き換えると、別のIDやパスワードにできます ----
set HIA_ADMIN_EMAIL=Jun.Ito@kusurinomadoguchi.co.jp
set HIA_ADMIN_NAME=伊藤 惇
set HIA_ADMIN_PASSWORD=EPARK1234567890-

echo 対象のログインID : %HIA_ADMIN_EMAIL%
echo.

if not exist hia.db (
  echo データベースがないため、初期データから作成します。
  %PY% seed.py
  goto DONE
)

rem 既にデータがある場合は、アカウントの追加またはパスワードの再設定を行う
%PY% seed.py
%PY% reset_admin.py %HIA_ADMIN_EMAIL%

:DONE
echo.
echo ------------------------------------------------------------
echo  上のログインIDとパスワードでログインしてください。
echo  ログイン後に「設定・サポート」→「パスワード変更」で
echo  必ずパスワードを変更してください。
echo ------------------------------------------------------------
echo.
pause
endlocal
