#!/bin/sh
cd "$(dirname "$0")"
export HIA_HOST=${HIA_HOST:-0.0.0.0}
export HIA_PORT=${HIA_PORT:-8000}
export HIA_SECRET_KEY=${HIA_SECRET_KEY:-change-this-secret-key}
export HIA_MAX_EXPORT_ROWS=${HIA_MAX_EXPORT_ROWS:-1000}
# 画面の表示倍率（ブラウザ100%表示で90%相当に見せる。等倍にするなら 1）
export HIA_UI_ZOOM=${HIA_UI_ZOOM:-0.9}
# hia.db が無ければ同梱の初期データ（hia_initial.db）から作られる（app.py）。
# どちらも無い場合だけ初期データを作る。
if [ ! -f hia.db ] && [ ! -f hia_initial.db ]; then python3 seed.py; fi
if python3 -c "import waitress" 2>/dev/null; then
  exec python3 -m waitress --host="$HIA_HOST" --port="$HIA_PORT" app:app
else
  exec python3 app.py
fi
