#!/bin/sh
cd "$(dirname "$0")"
export HIA_HOST=${HIA_HOST:-0.0.0.0}
export HIA_PORT=${HIA_PORT:-8000}
export HIA_SECRET_KEY=${HIA_SECRET_KEY:-change-this-secret-key}
export HIA_MAX_EXPORT_ROWS=${HIA_MAX_EXPORT_ROWS:-1000}
[ -f hia.db ] || python3 seed.py
if python3 -c "import waitress" 2>/dev/null; then
  exec python3 -m waitress --host="$HIA_HOST" --port="$HIA_PORT" app:app
else
  exec python3 app.py
fi
