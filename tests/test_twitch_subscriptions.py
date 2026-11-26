"""Tests for offline-safe Twitch subscription discovery."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from yt_dlp.extractor.twitch import TwitchStreamIE
from yt_dlp.utils import ExtractorError, UserNotLive

from downloader import (
    get_channel_info, get_latest_videos, get_live_info,
    is_legacy_numeric_channel_id, is_twitch_channel_id, probe_live_state,
)
from subscription import SubscriptionMonitor


class TestTwitchSubscriptions(unittest.TestCase):
    def test_channel_info_does_not_probe_live_stream(self):
        with patch('downloader.yt_dlp.YoutubeDL') as youtube_dl:
            info = get_channel_info('https://www.twitch.tv/KSPKSP/')

        self.assertEqual(info, {
            'channel_id': 'twitch:kspksp',
            'channel_name': 'kspksp',
            'url': 'https://www.twitch.tv/kspksp',
        })
        self.assertTrue(is_twitch_channel_id(info['channel_id']))
        youtube_dl.assert_not_called()

    def test_offline_channel_is_not_a_subscription_error(self):
        with patch('downloader.get_proxy_list', return_value=[None]), \
             patch.object(TwitchStreamIE, '_real_extract', side_effect=UserNotLive(video_id='kspksp')), \
             self.assertNoLogs('downloader', level='WARNING'):
            result = get_live_info('twitch:kspksp')
        self.assertEqual(result.state, 'ENDED')

    def test_twitch_probe_failure_remains_visible(self):
        with patch('downloader.get_proxy_list', return_value=[None]), \
             patch.object(TwitchStreamIE, '_real_extract', side_effect=ExtractorError('proxy timed out')), \
             self.assertLogs('downloader', level='ERROR') as logs:
            result = get_live_info('twitch:kspksp')
        self.assertEqual(result.state, 'UNKNOWN')
        self.assertTrue(any('proxy timed out' in entry for entry in logs.output))

    def test_recorder_probe_confirms_end_when_twitch_channel_goes_offline(self):
        # Without this, every Twitch recording waited out the whole recovery
        # window and was reported as a proxy interruption.
        with patch('downloader.get_proxy_list', return_value=[None, 'http://second:8080']), \
             patch.object(TwitchStreamIE, '_real_extract', side_effect=UserNotLive(video_id='kspksp')) as extract:
            result = probe_live_state('https://www.twitch.tv/kspksp', '123456')
        self.assertEqual(result.state, 'ENDED')
        self.assertEqual(extract.call_count, 1)

    def test_recorder_probe_twitch_transport_failure_stays_unknown(self):
        with patch('downloader.get_proxy_list', return_value=[None]), \
             patch.object(TwitchStreamIE, '_real_extract', side_effect=ExtractorError('proxy timed out')):
            result = probe_live_state('https://www.twitch.tv/kspksp', '123456')
        self.assertEqual(result.state, 'UNKNOWN')

    def test_numeric_legacy_id_is_never_queried_as_youtube(self):
        self.assertTrue(is_legacy_numeric_channel_id('315460461806'))
        with patch('downloader.yt_dlp.YoutubeDL') as youtube_dl:
            self.assertEqual(get_latest_videos('315460461806'), [])
        youtube_dl.assert_not_called()

        with patch('downloader.get_proxy_list', return_value=[None]), \
             patch('downloader.yt_dlp.YoutubeDL') as youtube_dl:
            result = get_live_info('315460461806')
        self.assertEqual(result.state, 'UNKNOWN')
        youtube_dl.assert_not_called()


class TestSubscriptionRouting(unittest.IsolatedAsyncioTestCase):
    async def test_twitch_video_legacy_type_routes_to_live_and_numeric_id_is_skipped(self):
        class InlineLoop:
            def run_in_executor(self, _executor, callback):
                future = asyncio.get_running_loop().create_future()
                future.set_result(callback())
                return future

        monitor = SubscriptionMonitor(
            SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock())), AsyncMock()
        )
        rows = [
            ('twitch:kspksp', 'kspksp', 1, 720, 'video'),
            ('315460461806', 'Old Twitch entry', 1, 720, 'live'),
        ]
        with patch('subscription.get_all_subscriptions', return_value=rows), \
             patch('subscription.get_latest_videos') as latest, \
             patch('subscription.get_live_info', return_value=SimpleNamespace(state='UNKNOWN')) as live, \
             patch('subscription.asyncio', SimpleNamespace(
                 get_running_loop=lambda: InlineLoop(), sleep=AsyncMock()
             )):
            await monitor._check_subscriptions()

        latest.assert_not_called()
        live.assert_called_once_with('twitch:kspksp')
