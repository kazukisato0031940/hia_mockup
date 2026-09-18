@echo off
setlocal
cd /d "%~dp0"

rem ---- HIA アカウント管理システム 起動 ----
set HIA_HOST=0.0.0.0
rem 待受ポート。他のソフトと重なる場合はここを変更してください
set HIA_PORT=8000

rem セッション用の秘密鍵（任意の文字列へ変更してください）
if "%HIA_SECRET_KEY%"=="" set HIA_SECRET_KEY=change-this-secret-key

rem 1回のCSV出力で許可する最大件数
set HIA_MAX_EXPORT_ROWS=1000

rem 画面の表示倍率（画面はブラウザ90%表示に合わせて作ってあるため、100%表示でも同じ見え方になるよう 0.9 を掛けています。等倍にするなら 1）
set HIA_UI_ZOOM=0.9

rem 接続を許可するIPの前方一致（社内LANのみに限定する場合は行頭の rem を外す）
rem set HIA_ALLOW_IPS=10.0.105.,127.0.0.1

rem ---- 案内メールの自動送信（社内SMTPサーバーを指定してください）----
rem set HIA_SMTP_HOST=smtp.example.local
rem set HIA_SMTP_PORT=25
rem set HIA_SMTP_FROM=no-reply@example.local
rem set HIA_SMTP_USER=
rem set HIA_SMTP_PASS=

rem ---- 疾患予測の設定（画面からは変更できません。ここで設定します）----
rem 日立 Risk Simulator for Insurance の接続先と認証キー。
rem 未設定の場合は社内の簡易ロジックで試算します。
rem set HIA_HITACHI_ENDPOINT=https://api.example.co.jp/risk/v1
rem set HIA_HITACHI_KEY=

rem 健診結果の連携（当社の予約管理システム）
rem set HIA_KENSHIN_ENDPOINT=https://yoyaku.example.local/kenshin/v1
set HIA_KENSHIN_TIME=02:00

rem NSIPS連携（自社の調剤システム）。batch または realtime
rem set HIA_NSIPS_ENDPOINT=https://chozai.example.local/nsips/v1
set HIA_NSIPS_MODE=batch

rem 健診結果と加入者の突合方法。cert（被保険者証の記号・番号・枝番）または anonid
set HIA_RISK_MATCH_RULE=cert
rem 既定の予測年数
set HIA_RISK_HORIZON=3

rem メールに載せるURLの基準。空にしておくと、このパソコンのIPを自動で使います
rem 固定したい場合は次の行の rem を外して書き換えてください
rem set HIA_BASE_URL=http://10.0.105.122:8000

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
  echo [エラー] Python の実行に失敗しました。
  pause
  exit /b 1
)
echo.

rem ---- 依存ライブラリの確認 ----
%PY% -c "import flask, werkzeug" 2>nul
if not errorlevel 1 goto DEPSOK
echo 必要なライブラリをインストールします...
%PY% -m pip install -r requirements.txt
%PY% -c "import flask, werkzeug" 2>nul
if not errorlevel 1 goto DEPSOK
echo.
echo [エラー] Flask のインストールに失敗しました。
echo   ネットワークに接続できない場合は、次のコマンドを手動で実行してください。
echo     %PY% -m pip install Flask Werkzeug waitress
echo.
pause
exit /b 1
:DEPSOK
echo.

if exist hia.db (
  echo [1/3] データベースのスキーマを確認します...
  %PY% migrate.py
  echo.
)

if not exist hia.db (
  echo [1/3] データベースを作成し、初期データを投入します...
  echo.
  %PY% seed.py
  if errorlevel 1 (
    echo [エラー] 初期データの投入に失敗しました。
    pause
    exit /b 1
  )
  echo.
  echo ************************************************************
  echo  上に表示された「ログインID」と「パスワード」を控えてください
  echo  パスワードは再表示できません
  echo  分からなくなった場合は force_reset.bat で再設定できます
  echo ************************************************************
  echo.
  pause
)

rem ---- このパソコンのIPアドレスを調べる ----
set HIA_IP=
for /f "usebackq delims=" %%I in (`%PY% -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0]);s.close()" 2^>nul`) do set HIA_IP=%%I
if "%HIA_IP%"=="" set HIA_IP=127.0.0.1
if "%HIA_BASE_URL%"=="" set HIA_BASE_URL=http://%HIA_IP%:%HIA_PORT%

rem ---- ポートが空いているか確認する ----
netstat -ano | findstr /r /c:":%HIA_PORT% .*LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo.
  echo ************************************************************
  echo  [注意] ポート %HIA_PORT% は既に使われています
  echo.
  echo  次のどちらかで対応してください。
  echo   1) 使用中のプログラムを終了する
  echo      調べ方: netstat -ano ^| findstr :%HIA_PORT%
  echo   2) このファイルの set HIA_PORT= を別の番号に変える
  echo.
  echo  このまま進めると、空いている番号を自動で探して起動します。
  echo ************************************************************
  echo.
  pause
)

echo [3/3] サーバーを起動します
echo   このパソコンから      : http://127.0.0.1:%HIA_PORT%/login
echo   同じネットワークから  : http://%HIA_IP%:%HIA_PORT%/login
echo   HIA総合管理（当社）   : http://%HIA_IP%:%HIA_PORT%/km
echo   HIA健保管理（健保）   : http://%HIA_IP%:%HIA_PORT%/app
echo.
echo   ほかの端末からつながらない場合は、管理者権限のコマンドプロンプトで
echo   次の1行を実行してファイアウォールを開けてください。
echo   netsh advfirewall firewall add rule name="HIA %HIA_PORT%" dir=in action=allow protocol=TCP localport=%HIA_PORT%
echo.
echo   終了する場合は Ctrl+C を押してください
echo.
%PY% -m waitress --host=%HIA_HOST% --port=%HIA_PORT% app:app
if errorlevel 1 (
  echo.
  echo waitress で起動できなかったため、標準サーバーで起動します。
  echo （ポートが使用中の場合は、空いている番号を自動で探します）
  %PY% app.py
)
echo.
pause
endlocal
