import unittest
from unittest.mock import patch
from downloader import CookieDiagnosticLogger, cookie_youtube_dl


class FFmpegDiagnosticsTests(unittest.TestCase):
    def test_ffmpeg_failure_is_visible_and_urls_are_redacted(self):
        diagnostic = CookieDiagnosticLogger()
        with patch('downloader.logger') as log:
            diagnostic.debug('[debug] ffmpeg version 7.1\nInput https://user:password@host/video?token=secret\nNo space left on device\nConversion failed!')
            log.error.assert_called_once()
            detail = log.error.call_args.args[1]
            self.assertIn('No space left on device', detail)
            self.assertNotIn('password', detail)
            self.assertNotIn('token=secret', detail)

    def test_diagnostic_output_is_bounded(self):
        with patch('downloader.logger') as log:
            CookieDiagnosticLogger().debug('[debug] ffmpeg version 7\n' + 'x' * 12000 + '\nConversion failed!')
            self.assertLessEqual(len(log.error.call_args.args[1]), 8000)

    def test_debug_configuration_and_commands_are_not_logged(self):
        with patch('downloader.logger') as log:
            diagnostic = CookieDiagnosticLogger()
            diagnostic.debug('[debug] params: secret')
            diagnostic.debug('[debug] ffmpeg command line: ffmpeg secret')
            log.error.assert_not_called()
            log.debug.assert_not_called()

    def test_yt_dlp_verbose_enabled_with_custom_logger(self):
        with patch('downloader.yt_dlp.YoutubeDL') as factory:
            with cookie_youtube_dl({}):
                pass
            self.assertTrue(factory.call_args.args[0]['verbose'])
            self.assertIsInstance(factory.call_args.args[0]['logger'], CookieDiagnosticLogger)
