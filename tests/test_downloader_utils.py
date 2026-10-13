import unittest
import os
import tempfile
from unittest.mock import patch

from downloader import (
    DOWNLOAD_OUTTMPL,
    download_content,
    is_retryable_error,
    probe_live_state,
)

class TestDownloaderUtils(unittest.TestCase):
    def test_postprocessed_output_preserves_title_and_rejects_missing_media(self):
        title = '繁體中文 日本語 🎵' * 40
        with tempfile.TemporaryDirectory() as directory, \
             patch('downloader.get_proxy_list', return_value=[None]), \
             patch('downloader.yt_dlp.YoutubeDL') as youtube_dl:
            source = os.path.join(directory, 'f_78K8u7QIU.webm')
            output = os.path.join(directory, 'f_78K8u7QIU.mp4')
            ydl = youtube_dl.return_value.__enter__.return_value
            ydl.extract_info.return_value = {
                'id': 'f_78K8u7QIU', 'title': title, 'ext': 'webm',
            }
            ydl.prepare_filename.return_value = source
            with open(output, 'wb') as media:
                media.write(b'media')
            result = download_content('https://example.test/video')
            self.assertEqual(result[:3], (output, title, 'f_78K8u7QIU'))
            os.remove(output)
            with self.assertRaisesRegex(FileNotFoundError, 'missing or empty'):
                download_content('https://example.test/video')
            with open(output, 'wb'):
                pass
            with self.assertRaisesRegex(FileNotFoundError, 'missing or empty'):
                download_content('https://example.test/video')

    def test_download_template_uses_only_the_stable_video_id(self):
        """Long titles must not affect media or temporary .part filenames."""
        self.assertTrue(DOWNLOAD_OUTTMPL.endswith('/%(id)s.%(ext)s'))
        self.assertNotIn('%(title)', DOWNLOAD_OUTTMPL)

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
