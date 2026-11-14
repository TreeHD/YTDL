"""Shared Telegram API retry helpers."""

import asyncio
import logging

from telegram.error import BadRequest, RetryAfter, TelegramError

logger = logging.getLogger(__name__)


async def tg_retry(func, *args, **kwargs):
    """Retry Telegram API calls up to 10 times on flood control/errors."""
    max_retries = 10
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except RetryAfter as exc:
            wait_time = exc.retry_after
            logger.warning(
                "Flood control: Waiting %ss (Attempt %s/10)",
                wait_time,
                attempt + 1,
            )
            await asyncio.sleep(wait_time)
        except BadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                return None
            raise
        except TelegramError as exc:
            if "Flood control" in str(exc):
                logger.warning(
                    "Flood caught via error msg: %s (Attempt %s/10)",
                    exc,
                    attempt + 1,
                )
                await asyncio.sleep(5)
                continue
            raise
        except Exception:
            if attempt == max_retries - 1:
                raise
            logger.warning("Unexpected Telegram error. Retrying...", exc_info=True)
            await asyncio.sleep(2)
    raise RuntimeError("Max retries exceeded for Telegram API call")


class StatusMessageEditor:
    """Remember acknowledged text AND buttons, and serialize concurrent edits."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._message = None
        self._payload = None

    async def edit(self, message, text, reply_markup=None):
        async with self._lock:
            payload = (text, reply_markup)
            if self._message is not message:
                self._message = message
                self._payload = (message.text, getattr(message, 'reply_markup', None))
            if self._payload == payload:
                return False
            await tg_retry(message.edit_text, text, reply_markup=reply_markup)
            # Telegram's harmless "not modified" response also acknowledges it.
            # Do not advance this cache on any other failure.
            self._payload = payload
            return True
