"""Tests for offline-safe Twitch subscription discovery."""

import unittest
from unittest.mock import patch

from downloader import get_channel_info, get_live_info, is_twitch_channel_id


class OfflineTwitchYDL:
    def __init__(self, _options):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, _url, download=False):
        raise Exception('ERROR: [twitch:stream] kspksp: The channel is not currently live')


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
             patch('downloader.yt_dlp.YoutubeDL', OfflineTwitchYDL):
            result = get_live_info('twitch:kspksp')

        self.assertEqual(result.state, 'ENDED')
