import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter, TimedOut
from telegram_utils import BackgroundStatusPublisher, StatusMessageEditor, tg_retry


class StatusTests(unittest.IsolatedAsyncioTestCase):
    def message(self):
        return SimpleNamespace(text='initial', reply_markup=None, edit_text=AsyncMock())

    async def test_repeated_and_concurrent_updates_are_deduplicated(self):
        message = self.message()
        editor = StatusMessageEditor()
        await asyncio.gather(*(editor.edit(message, 'progress') for _ in range(20)))
        message.edit_text.assert_awaited_once_with('progress', reply_markup=None)
        # Message.text remains stale; the editor remembers the acknowledged edit.
        self.assertEqual(message.text, 'initial')
        await editor.edit(message, 'initial')
        self.assertEqual(message.edit_text.await_count, 2)

    async def test_keyboard_only_change_and_removal(self):
        message = self.message()
        editor = StatusMessageEditor()
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Cancel', callback_data='cancel')]])
        await editor.edit(message, 'initial', keyboard)
        await editor.edit(message, 'initial', keyboard)
        await editor.edit(message, 'initial', None)
        self.assertEqual(message.edit_text.await_count, 2)

    async def test_not_modified_is_acknowledged_without_warning_or_retry(self):
        message = self.message()
        message.edit_text.side_effect = BadRequest('Message is not modified: same content')
        editor = StatusMessageEditor()
        with patch('telegram_utils.logger.warning') as warning, patch('telegram_utils.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await editor.edit(message, 'progress')
            await editor.edit(message, 'progress')
            message.edit_text.assert_awaited_once()
            warning.assert_not_called()
            sleep.assert_not_awaited()

    async def test_real_errors_propagate_and_do_not_poison_cache(self):
        message = self.message()
        message.edit_text.side_effect = [BadRequest('Message to edit not found'), True]
        editor = StatusMessageEditor()
        with self.assertRaises(BadRequest):
            await editor.edit(message, 'progress')
        await editor.edit(message, 'progress')
        self.assertEqual(message.edit_text.await_count, 2)

    async def test_new_message_resets_cache(self):
        editor = StatusMessageEditor()
        first, second = self.message(), self.message()
        await editor.edit(first, 'progress')
        await editor.edit(second, 'progress')
        second.edit_text.assert_awaited_once()


class BackgroundPublisherTests(unittest.IsolatedAsyncioTestCase):
    def message(self):
        return SimpleNamespace(text='initial', reply_markup=None, edit_text=AsyncMock())

    async def test_publish_never_waits_and_delivers_in_order(self):
        message = self.message()
        publisher = BackgroundStatusPublisher()
        for text in ('a', 'b', 'c'):
            publisher.publish(message, text)
        message.edit_text.assert_not_awaited()
        await publisher.flush()
        self.assertEqual([call.args[0] for call in message.edit_text.await_args_list], ['a', 'b', 'c'])

    async def test_flood_wait_does_not_block_callers_and_sends_only_newest(self):
        message = self.message()
        sent = []

        async def edit(text, reply_markup=None):
            if not sent:
                sent.append('flood')
                raise RetryAfter(30)
            sent.append(text)

        message.edit_text = edit
        waiting, release = asyncio.Event(), asyncio.Event()

        async def flood_wait(delay):
            self.assertEqual(delay, 30)
            waiting.set()
            await release.wait()

        publisher = BackgroundStatusPublisher()
        with patch('telegram_utils.asyncio.sleep', flood_wait):
            publisher.publish(message, 'first')
            await waiting.wait()
            # The recording loop keeps running while Telegram makes us wait.
            for text in ('second', 'third', 'newest'):
                publisher.publish(message, text)
            release.set()
            await publisher.flush()
        self.assertEqual(sent, ['flood', 'newest'])

    async def test_network_error_is_retried(self):
        message = self.message()
        message.edit_text.side_effect = [TimedOut(), True]
        publisher = BackgroundStatusPublisher()
        with patch('telegram_utils.asyncio.sleep', new_callable=AsyncMock):
            publisher.publish(message, 'progress')
            await publisher.flush()
        self.assertEqual(message.edit_text.await_count, 2)
