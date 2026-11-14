import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from downloader import download_content, get_video_info, is_retryable_error
from download_errors import diagnostic_error


class RotationTests(unittest.TestCase):
    def test_fallback_download_succeeds(self):
        for failure in ('not available in your country. Use --cookies',
                        'Private video', 'cookies are required',
                        diagnostic_error('not available in your country')):
            with self.subTest(failure=str(failure)), tempfile.TemporaryDirectory() as directory, \
                 patch('downloader.get_proxy_list', return_value=['http://first', 'http://second']), \
                 patch('downloader.get_cookie_file', return_value=None), \
                 patch('downloader.yt_dlp.YoutubeDL') as factory:
                path = Path(directory, 'video.mp4')
                path.write_bytes(b'media')
                ydl = factory.return_value.__enter__.return_value
                ydl.extract_info.side_effect = [Exception(failure) if isinstance(failure, str) else failure,
                                                {'id': 'video', 'title': 'video'}]
                ydl.prepare_filename.return_value = str(path)
                self.assertEqual(download_content('https://example.test/v')[0], str(path))
                self.assertEqual([c.args[0]['proxy'] for c in factory.call_args_list],
                                 ['http://first', 'http://second'])

    def test_metadata_fallback(self):
        with patch('downloader.get_proxy_list', return_value=['http://first', 'http://second']), \
             patch('downloader.get_cookie_file', return_value=None), \
             patch('downloader.yt_dlp.YoutubeDL') as factory:
            factory.return_value.__enter__.return_value.extract_info.side_effect = [
                Exception('not available in your country. Use --cookies'), {'id': 'ok'}]
            self.assertEqual(get_video_info('https://example.test/v')['id'], 'ok')
            self.assertEqual(factory.call_count, 2)

    def test_exhaustion_attempts_every_proxy(self):
        with patch('downloader.get_proxy_list', return_value=['http://first', 'http://second']), \
             patch('downloader.get_cookie_file', return_value=None), \
             patch('downloader.yt_dlp.YoutubeDL') as factory:
            factory.return_value.__enter__.return_value.extract_info.side_effect = Exception('not available in your country')
            with self.assertRaisesRegex(Exception, '全部 2 個連線'):
                download_content('https://example.test/v')
            self.assertEqual(factory.call_count, 2)

    def test_geo_with_cookie_advice_does_not_restart_warp(self):
        with patch('downloader.restart_warp_proxy') as restart:
            self.assertTrue(is_retryable_error('not available in your country. Use --cookies', 'http://warp-proxy:8080'))
            restart.assert_not_called()
