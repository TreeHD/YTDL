import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cookie_manager import (MAX_COOKIE_BYTES, cookie_snapshot, install_cookies,
                            is_cookie_admin, validate_cookies)
from download_errors import classify_error, error_hint
from downloader import download_content, get_video_info, get_playlist_info, cookie_youtube_dl, probe_live_state
from handlers import handle_cookie_document


def cookie(expiry='4102444800', value='secret', domain='.youtube.com'):
    return ('# Netscape HTTP Cookie File\n'
            f'{domain}\tTRUE\t/\tTRUE\t{expiry}\tSID\t{value}\n').encode()


class CookieTests(unittest.TestCase):
    def test_validation_and_expiry(self):
        for data in (cookie(), cookie('0'), cookie(''), b'\xef\xbb\xbf' + cookie(),
                     cookie(domain='#HttpOnly_.youtube.com').replace(b'\n', b'\r\n')):
            self.assertEqual(validate_cookies(data)[1:], (1, 0))
        for data in (b'', b'{}', b'# Netscape HTTP Cookie File\n', cookie('1'),
                     b'a' * (MAX_COOKIE_BYTES + 1), cookie() + b'bad\trow\n'):
            with self.subTest(data=data[:30]), self.assertRaises(ValueError):
                validate_cookies(data)

    def test_atomic_replace_and_snapshot_isolation(self):
        with tempfile.TemporaryDirectory() as directory, patch('config.COOKIE_FILE', directory + '/cookies.txt'):
            path = Path(directory, 'cookies.txt')
            install_cookies(cookie(value='old'))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with cookie_snapshot(str(path)) as snapshot:
                install_cookies(cookie(value='new'))
                Path(snapshot).write_bytes(cookie(value='stale'))
            self.assertFalse(Path(snapshot).exists())
            self.assertIn(b'new', path.read_bytes())
            with self.assertRaises(ValueError):
                install_cookies(b'invalid')
            self.assertIn(b'new', path.read_bytes())
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_admin_fails_closed(self):
        for value in ('', '123,broken', '-123'):
            with patch.dict(os.environ, {'COOKIE_ADMIN_IDS': value}):
                self.assertFalse(is_cookie_admin(123))
        with patch.dict(os.environ, {'COOKIE_ADMIN_IDS': '123, 456'}):
            self.assertTrue(is_cookie_admin(123))

    def test_diagnostic_categories(self):
        for error, kind in [
            ('The provided YouTube account cookies are no longer valid', 'cookie_expired'),
            ('Sign in to confirm your age', 'auth'),
            ("Sign in to confirm you’re not a bot. Use --cookies", 'blocked'),
            ('HTTP Error 403: Forbidden', 'blocked'),
            ('HTTP Error 429: Too Many Requests', 'blocked'),
            ('HTTP Error 407: Proxy Authentication Required', 'proxy'),
            ('Connection refused', 'network'),
            ('not available in your country', 'geo'),
        ]:
            self.assertEqual(classify_error(error), kind)
            self.assertTrue(error_hint(error))

    def test_auth_failures_never_rotate_warp(self):
        for function in (download_content, get_video_info, get_playlist_info):
            with patch('downloader.get_proxy_list', return_value=['http://warp-proxy:8080']), \
                 patch('downloader.get_cookie_file', return_value=None), \
                 patch('downloader.yt_dlp.YoutubeDL') as factory, \
                 patch('downloader.restart_warp_proxy') as restart:
                factory.return_value.__enter__.return_value.extract_info.side_effect = Exception('cookies are no longer valid')
                with self.assertRaisesRegex(Exception, '遭撤銷'):
                    function('https://example.test/video')
                restart.assert_not_called()

    def test_exhausted_retries_keep_diagnosis(self):
        for message, expected in [('HTTP Error 403: Forbidden', '無法只憑此判定'),
                                  ('Connection refused', '網路連線失敗')]:
            for function in (download_content, get_video_info, get_playlist_info):
                with patch('downloader.get_proxy_list', return_value=[None]), \
                     patch('downloader.get_cookie_file', return_value=None), \
                     patch('downloader.yt_dlp.YoutubeDL') as factory:
                    factory.return_value.__enter__.return_value.extract_info.side_effect = Exception(message)
                    with self.assertRaisesRegex(Exception, expected):
                        function('https://example.test/video')

    def test_live_probe_keeps_cookie_evidence(self):
        with patch('downloader.get_proxy_list', return_value=[None]), \
             patch('downloader.get_cookie_file', return_value=None), \
             patch('downloader.yt_dlp.YoutubeDL') as factory:
            def fail(*args, **kwargs):
                factory.call_args.args[0]['logger'].warning('cookies are no longer valid')
                raise Exception("Sign in to confirm you're not a bot")
            factory.return_value.__enter__.return_value.extract_info.side_effect = fail
            result = probe_live_state('https://example.test/live')
            self.assertEqual(result.state, 'UNKNOWN')
            self.assertEqual(classify_error(result.errors[0]), 'cookie_expired')

    def test_revocation_warning_is_not_lost_to_bot_error(self):
        with patch('downloader.yt_dlp.YoutubeDL') as factory:
            with self.assertRaisesRegex(Exception, '遭撤銷'):
                with cookie_youtube_dl({}):
                    factory.call_args.args[0]['logger'].warning('The provided YouTube account cookies are no longer valid')
                    raise Exception("Sign in to confirm you're not a bot")


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_permissions_size_validation_and_success(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('config.COOKIE_FILE', directory + '/cookies.txt'), \
             patch.dict(os.environ, {'COOKIE_ADMIN_IDS': '123'}):
            document = SimpleNamespace(file_name='cookie.txt', file_size=len(cookie()), get_file=AsyncMock())
            remote = document.get_file.return_value
            remote.download_as_bytearray = AsyncMock(return_value=bytearray(cookie()))
            status = SimpleNamespace(edit_text=AsyncMock())
            message = SimpleNamespace(document=document, reply_text=AsyncMock(return_value=status))
            update = SimpleNamespace(effective_message=message,
                                     effective_user=SimpleNamespace(id=999),
                                     effective_chat=SimpleNamespace(type='private'))
            await handle_cookie_document(update, None)
            document.get_file.assert_not_called()
            update.effective_user.id = 123
            update.effective_chat.type = 'group'
            await handle_cookie_document(update, None)
            document.get_file.assert_not_called()
            update.effective_chat.type = 'private'
            document.file_size = MAX_COOKIE_BYTES + 1
            await handle_cookie_document(update, None)
            document.get_file.assert_not_called()
            document.file_size = len(cookie())
            await handle_cookie_document(update, None)
            self.assertEqual(Path(directory, 'cookies.txt').read_bytes(), cookie())
            self.assertIn('已更新', status.edit_text.call_args.args[0])
            remote.download_as_bytearray.return_value = bytearray(b'invalid')
            await handle_cookie_document(update, None)
            self.assertEqual(Path(directory, 'cookies.txt').read_bytes(), cookie())
            self.assertIn('未變更', status.edit_text.call_args.args[0])
