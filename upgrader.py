"""yt-dlp nightly upgrade support for startup, schedules, and commands."""

import asyncio
import logging
import subprocess
import sys
import threading
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import load_config

logger = logging.getLogger(__name__)

UPDATE_COMMAND = ('yt-dlp', '--update-to', 'nightly')
PIP_FALLBACK_COMMAND = (
    sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
    '--no-cache-dir', '--upgrade', '--pre', 'yt-dlp[default]'
)
_upgrade_lock = threading.Lock()


def _run_command(command, timeout=300):
    """Run an upgrade command and return its exit code and combined output."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = '\n'.join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        return result.returncode, output
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def upgrade_yt_dlp():
    """Upgrade yt-dlp to nightly, with the official pip-install fallback."""
    if not _upgrade_lock.acquire(blocking=False):
        return False, "Another yt-dlp upgrade is already running."

    try:
        returncode, output = _run_command(UPDATE_COMMAND)
        if returncode == 0:
            return True, output or "yt-dlp nightly is already up to date."

        logger.warning(
            "yt-dlp self-update failed; trying the pip nightly update: %s", output
        )
        pip_returncode, pip_output = _run_command(PIP_FALLBACK_COMMAND)
        combined_output = '\n'.join(part for part in (output, pip_output) if part)
        if pip_returncode == 0:
            return True, combined_output or "yt-dlp nightly upgrade completed."
        return False, combined_output or "yt-dlp nightly upgrade failed."
    finally:
        _upgrade_lock.release()


def startup_upgrade():
    """Run the configured startup upgrade without preventing bot startup on failure."""
    if not load_config().get('ytdlp_auto_update', True):
        logger.info("Startup yt-dlp nightly upgrade is disabled.")
        return True

    logger.info("Running startup command: yt-dlp --update-to nightly")
    success, output = upgrade_yt_dlp()
    log = logger.info if success else logger.error
    log("Startup yt-dlp upgrade result:\n%s", output)
    return success


def get_next_daily_update(now, update_time, timezone_name):
    """Return the next configured daily run as a timezone-aware datetime."""
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.error(
            "Invalid YTDLP_UPDATE_TIMEZONE %r; using Asia/Taipei.", timezone_name
        )
        timezone = ZoneInfo('Asia/Taipei')

    try:
        hour_text, minute_text = update_time.split(':', 1)
        scheduled_time = time(hour=int(hour_text), minute=int(minute_text))
    except (AttributeError, TypeError, ValueError):
        logger.error("Invalid YTDLP_UPDATE_TIME %r; using 04:00.", update_time)
        scheduled_time = time(hour=4)

    local_now = now.astimezone(timezone)
    next_run = datetime.combine(local_now.date(), scheduled_time, tzinfo=timezone)
    if next_run <= local_now:
        next_run += timedelta(days=1)
    return next_run


async def daily_upgrade_loop(is_download_busy):
    """Upgrade once daily, skipping the entire day when downloads are active."""
    config = load_config()
    update_time = config.get('ytdlp_update_time', '04:00')
    timezone_name = config.get('ytdlp_update_timezone', 'Asia/Taipei')

    while True:
        now = datetime.now(tz=ZoneInfo('UTC'))
        next_run = get_next_daily_update(now, update_time, timezone_name)
        delay = max(0, (next_run - now).total_seconds())
        logger.info("Next daily yt-dlp update scheduled for %s", next_run.isoformat())
        await asyncio.sleep(delay)

        if is_download_busy():
            logger.info(
                "Skipping today's yt-dlp update because downloads are active; "
                "the next attempt will be tomorrow."
            )
            continue

        loop = asyncio.get_running_loop()
        success, output = await loop.run_in_executor(None, upgrade_yt_dlp)
        log = logger.info if success else logger.error
        log("Daily yt-dlp upgrade result:\n%s", output)


if __name__ == '__main__':
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO,
    )
    startup_upgrade()
