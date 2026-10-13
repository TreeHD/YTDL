#!/bin/sh

# Normalize even when env_file or the host overrides the image defaults.
# Debian supplies C.UTF-8 without installing additional language packs.
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

python - <<'PY'
import locale
import sys

encodings = (locale.getencoding(), sys.getfilesystemencoding(), sys.stdout.encoding)
if any(encoding.lower().replace('-', '') != 'utf8' for encoding in encodings):
    raise SystemExit(f"UTF-8 runtime initialization failed: {encodings}")
PY
if [ "$?" -ne 0 ]; then
    exit 1
fi

# Upgrade before importing yt_dlp in the bot process so this run uses the
# newly installed nightly version. Upgrade failures are logged but do not keep
# the Telegram bot offline.
python -m upgrader

exec python bot.py
