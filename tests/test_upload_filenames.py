import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

import queue_processor
import uploader


class TestUploadFilenames(unittest.IsolatedAsyncioTestCase):
    def test_long_unicode_title_and_unsafe_characters(self):
        name = uploader.build_upload_filename('id.mp4', '中文🎵' * 100, 2)
        self.assertLessEqual(len(os.path.splitext(name)[0]), 70)
        self.assertLessEqual(len(name.encode('utf-8')), 255)
        self.assertTrue(name.endswith(' (Part 2).mp4'))
        self.assertEqual(uploader.build_upload_filename('id.m4a', '中文/測試\n'), '中文_測試_.m4a')
        self.assertEqual(uploader.build_upload_filename('id.mp4', '...'), 'video.mp4')

    async def test_title_filename_reaches_both_apis_without_renaming_disk_file(self):
        with tempfile.TemporaryDirectory() as directory:
            for audio in (False, True):
                extension = '.m4a' if audio else '.mp4'
                path = os.path.join(directory, 'f_78K8u7QIU' + extension)
                with open(path, 'wb') as media:
                    media.write(b'media')
                for local in (False, True):
                    with self.subTest(audio=audio, local=local):
                        bot = SimpleNamespace(send_video=AsyncMock(), send_audio=AsyncMock())
                        job = queue_processor.UploadJob(
                            application=SimpleNamespace(bot=bot), chat_id=1,
                            source_file_path=path, files_to_upload=[path, path],
                            title='繁體中文🎵', url='https://example.test/video',
                            audio_only=audio,
                        )
                        expected = '繁體中文🎵 (Part 2)' + extension

                        async def capture_request(endpoint, writer, timeout):
                            field = 'audio' if audio else 'video'
                            headers = [payload.headers['Content-Disposition']
                                       for payload, _, _ in writer._parts]
                            header = next(h for h in headers if f'name="{field}"' in h)
                            self.assertIn(f'filename="{expected}"', unquote(header))
                            return {'message_id': 1}

                        api = 'http://local/bot' if local else 'https://api.telegram.org/bot'
                        with patch('queue_processor.load_config', return_value={'api_url': api}), \
                             patch('uploader._post_raw_telegram', side_effect=capture_request):
                            await queue_processor._upload_one_part(job, 1)
                        if not local:
                            method = bot.send_audio if audio else bot.send_video
                            self.assertEqual(method.await_args.kwargs['filename'], expected)
                        self.assertTrue(os.path.isfile(path))
                        self.assertFalse(os.path.exists(os.path.join(directory, expected)))


if __name__ == '__main__':
    unittest.main()
