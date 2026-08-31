import unittest
from unittest.mock import patch

from downloader import is_retryable_error, probe_live_state

class TestDownloaderUtils(unittest.TestCase):
    def test_retryable_error_detection(self):
        self.assertTrue(is_retryable_error("This video is not available in your country"))
        self.assertTrue(is_retryable_error("The uploader has not made this video available in your country"))
        self.assertTrue(is_retryable_error("Sign in to confirm your age"))
        self.assertTrue(is_retryable_error("Private video"))
        self.assertTrue(is_retryable_error("Sign in to confirm you're not a bot"))
        self.assertTrue(is_retryable_error("cookies are required"))
        self.assertTrue(is_retryable_error("Connection to www.youtube.com timed out"))
        self.assertTrue(is_retryable_error("Unable to download API page"))

        self.assertFalse(is_retryable_error("Could not resolve host"))
        self.assertFalse(is_retryable_error("HTTP Error 404: Not Found"))

    @patch('downloader.restart_warp_proxy')
    def test_geo_restriction_does_not_rotate_warp(self, restart_warp_proxy):
        self.assertTrue(is_retryable_error(
            "This video is not available in your country",
            "http://warp-proxy:8080",
        ))
        restart_warp_proxy.assert_not_called()

    @patch('downloader.restart_warp_proxy')
    def test_non_geo_retryable_error_rotates_warp(self, restart_warp_proxy):
        self.assertTrue(is_retryable_error(
            "Sign in to confirm you're not a bot",
            "http://warp-proxy:8080",
        ))
        restart_warp_proxy.assert_called_once()

    @patch('downloader.yt_dlp.YoutubeDL')
    def test_live_probe_requires_the_expected_video_id(self, youtube_dl):
        ydl = youtube_dl.return_value.__enter__.return_value
        ydl.extract_info.return_value = {'id': 'other-video', 'is_live': False}
        with patch.dict('os.environ', {'PROXY': '', 'PROXY_LIST': ''}):
            result = probe_live_state('https://example.test/live', 'expected-video')
        self.assertEqual(result.state, 'UNKNOWN')

    @patch('downloader.yt_dlp.YoutubeDL')
    def test_live_probe_reports_ended_only_after_successful_lookup(self, youtube_dl):
        ydl = youtube_dl.return_value.__enter__.return_value
        ydl.extract_info.return_value = {'id': 'expected-video', 'is_live': False}
        with patch.dict('os.environ', {'PROXY': '', 'PROXY_LIST': ''}):
            result = probe_live_state('https://example.test/live', 'expected-video')
        self.assertEqual(result.state, 'ENDED')

    @patch('downloader.yt_dlp.YoutubeDL')
    def test_live_probe_treats_extractor_failure_as_unknown(self, youtube_dl):
        ydl = youtube_dl.return_value.__enter__.return_value
        ydl.extract_info.side_effect = OSError('proxy connection refused')
        with patch.dict('os.environ', {'PROXY': '', 'PROXY_LIST': ''}):
            result = probe_live_state('https://example.test/live', 'expected-video')
        self.assertEqual(result.state, 'UNKNOWN')

if __name__ == '__main__':
    unittest.main()
