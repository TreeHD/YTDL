"""Subscription notifications and live-stream deduplication."""

import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest

from subscription import MAX_LIVE_RECORDING_ATTEMPTS, SubscriptionMonitor


class LiveSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    SUBSCRIBERS = [{'chat_id': 7, 'max_quality': 1080}]

    def setUp(self):
        self.processed = set()
        self.send_message = AsyncMock(return_value='status')
        self.queue = asyncio.Queue()
        self.monitor = SubscriptionMonitor(SimpleNamespace(bot=SimpleNamespace(send_message=self.send_message)),
                                           self.queue)
        live = SimpleNamespace(state='LIVE', info={
            'id': 'abc', 'title': 'Karaoke <b> & _*[', 'url': 'https://youtu.be/abc',
        })
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('subscription.get_live_info', return_value=live))
        stack.enter_context(patch('subscription.is_video_processed', side_effect=self.processed.__contains__))
        stack.enter_context(patch('subscription.mark_video_processed',
                                  side_effect=lambda key, channel, title: self.processed.add(key)))

    async def poll(self):
        await self.monitor._check_live('UC1', 'Chan_*', self.SUBSCRIBERS)

    async def test_live_is_marked_done_only_when_the_recording_really_finished(self):
        await self.poll()
        task = self.queue.get_nowait()
        self.assertEqual(len(task), 9)
        self.assertEqual(task[6:8], (True, 'abc'))
        # Detection alone must not mark it: a crash or restart would lose the stream.
        self.assertEqual(self.processed, set())

        await self.poll()  # Still recording: no duplicate.
        self.assertTrue(self.queue.empty())

        task[8]('proxy_exhausted')  # Ended early while the stream is still live.
        self.assertEqual(self.processed, set())
        await self.poll()
        restarted = self.queue.get_nowait()
        self.assertIn('restarting (2/3)', self.send_message.await_args.kwargs['text'])

        restarted[8]('stopped')  # The user chose to stop; never restart it.
        self.assertEqual(self.processed, {'live_abc_7'})
        await self.poll()
        self.assertTrue(self.queue.empty())

    async def test_repeated_abnormal_endings_stop_after_the_limit(self):
        for _ in range(MAX_LIVE_RECORDING_ATTEMPTS):
            await self.poll()
            self.queue.get_nowait()[8]('error')
        self.assertEqual(self.processed, {'live_abc_7'})
        await self.poll()
        self.assertTrue(self.queue.empty())

    async def test_titles_are_html_escaped_and_a_failed_notification_still_queues(self):
        self.send_message.side_effect = BadRequest("Can't parse entities")
        await self.poll()
        task = self.queue.get_nowait()
        self.assertIsNone(task[4])  # The recorder sends its own status message.
        kwargs = self.send_message.await_args.kwargs
        self.assertEqual(kwargs['parse_mode'], 'HTML')
        self.assertIn('Karaoke &lt;b&gt; &amp; _*[', kwargs['text'])

    async def test_new_video_is_queued_even_if_notification_fails(self):
        self.send_message.side_effect = BadRequest("Can't parse entities")
        video = {'id': 'v1', 'title': 'Clip_*', 'url': 'https://youtu.be/v1'}
        with patch('subscription.get_latest_videos', return_value=[video]):
            await self.monitor._check_videos('UC1', 'Chan', self.SUBSCRIBERS)
        task = self.queue.get_nowait()
        self.assertEqual(task[1], 'https://youtu.be/v1')
        self.assertIsNone(task[4])
        self.assertEqual(self.processed, {'v1'})


if __name__ == '__main__':
    unittest.main()
