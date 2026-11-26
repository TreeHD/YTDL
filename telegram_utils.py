"""Shared Telegram API retry helpers."""

import asyncio
import collections
import logging

from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

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

    async def edit(self, message, text, reply_markup=None, *, wait_on_flood=True):
        async with self._lock:
            payload = (text, reply_markup)
            if self._message is not message:
                self._message = message
                self._payload = (message.text, getattr(message, 'reply_markup', None))
            if self._payload == payload:
                return False
            if wait_on_flood:
                await tg_retry(message.edit_text, text, reply_markup=reply_markup)
            else:
                # The caller handles RetryAfter itself, so that it can send a
                # newer text after the wait instead of this stale one.
                try:
                    await message.edit_text(text, reply_markup=reply_markup)
                except BadRequest as exc:
                    if 'message is not modified' not in str(exc).lower():
                        raise
            # Telegram's harmless "not modified" response also acknowledges it.
            # Do not advance this cache on any other failure.
            self._payload = payload
            return True


def _retry_seconds(exc):
    value = exc.retry_after
    return value.total_seconds() if hasattr(value, 'total_seconds') else float(value)


class BackgroundStatusPublisher:
    """Deliver status edits in order in the background; callers never wait.

    A recording loop must keep handling Stop, Cancel and segment rollover
    while Telegram applies flood control. Edits normally go out in order,
    exactly as if they were awaited. During a flood-control wait, the
    backlog collapses to the newest text so only that one is sent next.
    """

    MAX_BACKLOG = 20
    NETWORK_RETRIES = 3
    NETWORK_RETRY_SECONDS = 2

    def __init__(self):
        self._editor = StatusMessageEditor()
        self._backlog = collections.deque()
        self._task = None

    def publish(self, message, text, reply_markup=None):
        self._backlog.append((message, text, reply_markup))
        while len(self._backlog) > self.MAX_BACKLOG:
            self._backlog.popleft()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._deliver())

    async def flush(self):
        """Wait until every published status was delivered or given up."""
        while self._task is not None and not self._task.done():
            # Shield delivery so a cancelled waiter does not drop the update.
            await asyncio.shield(self._task)

    def _retry_later(self, item):
        # A newer text already supersedes a failed one; resend only the newest.
        if not self._backlog:
            self._backlog.append(item)

    def _collapse_to_newest(self):
        if len(self._backlog) > 1:
            newest = self._backlog[-1]
            self._backlog.clear()
            self._backlog.append(newest)

    async def _deliver(self):
        network_failures = 0
        while self._backlog:
            item = self._backlog.popleft()
            message, text, reply_markup = item
            try:
                await self._editor.edit(message, text, reply_markup, wait_on_flood=False)
                network_failures = 0
            except RetryAfter as exc:
                delay = _retry_seconds(exc)
                logger.warning("Flood control: status update deferred %ss; only the newest text will be sent", delay)
                self._retry_later(item)
                await asyncio.sleep(delay)
                self._collapse_to_newest()
            except BadRequest as exc:
                logger.warning("Status update rejected: %s", exc)
            except NetworkError as exc:
                network_failures += 1
                if network_failures > self.NETWORK_RETRIES:
                    logger.warning("Status update dropped after network errors: %s", exc)
                    network_failures = 0
                    continue
                self._retry_later(item)
                await asyncio.sleep(self.NETWORK_RETRY_SECONDS)
            except Exception as exc:
                logger.warning("Status update failed: %s", exc, exc_info=True)
