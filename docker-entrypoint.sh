#!/bin/sh

# Upgrade before importing yt_dlp in the bot process so this run uses the
# newly installed nightly version. Upgrade failures are logged but do not keep
# the Telegram bot offline.
python -m upgrader

exec python bot.py
