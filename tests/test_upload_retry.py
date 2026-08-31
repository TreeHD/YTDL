import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import queue_processor
import uploader


class TestRawUploadRetries(unittest.IsolatedAsyncioTestCase):
    async def test_retryable_raw_failure_uses_telegram_delay_then_succeeds(self):
        request = AsyncMock(side_effect=[
            uploader.UploadRetryableError("Too Many Requests", retry_after=60),
            {"message_id": 1},
        ])

        with patch('uploader.asyncio.sleep', new_callable=AsyncMock) as sleep:
            result = await uploader._retry_raw_upload(request, "video", max_retries=2)

        self.assertEqual(result, {"message_id": 1})
        self.assertEqual(request.await_count, 2)
        sleep.assert_awaited_once_with(60)

    async def test_retryable_disconnect_is_retried_with_backoff(self):
        request = AsyncMock(side_effect=[
            uploader.UploadRetryableError("Telegram transport error: Server disconnected"),
            {"message_id": 1},
        ])

        with patch('uploader.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await uploader._retry_raw_upload(request, "video", max_retries=2)

        sleep.assert_awaited_once_with(2)


class TestResumableUploadJobs(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_queue = queue_processor._upload_retry_queue
        self.original_wakeup = queue_processor._upload_retry_wakeup
        self.original_jobs = queue_processor._upload_retry_jobs
        queue_processor._upload_retry_queue = asyncio.PriorityQueue()
        queue_processor._upload_retry_wakeup = asyncio.Event()
        queue_processor._upload_retry_jobs = {}
        self.temp_dir = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        queue_processor._upload_retry_queue = self.original_queue
        queue_processor._upload_retry_wakeup = self.original_wakeup
        queue_processor._upload_retry_jobs = self.original_jobs
        self.temp_dir.cleanup()

    async def test_failed_second_part_is_retained_and_resumes_from_that_part(self):
        source = os.path.join(self.temp_dir.name, 'video.mp4')
        part_one = os.path.join(self.temp_dir.name, 'video_part1.mp4')
        part_two = os.path.join(self.temp_dir.name, 'video_part2.mp4')
        for path in (source, part_one, part_two):
            with open(path, 'wb') as media:
                media.write(b'media')

        job = queue_processor.UploadJob(
            application=object(),
            chat_id=1,
            source_file_path=source,
            files_to_upload=[part_one, part_two],
            title='title',
            url='https://example.test/video',
        )
        queue_processor._upload_retry_jobs[job.job_id] = job

        with patch(
            'queue_processor._upload_one_part',
            new=AsyncMock(side_effect=[None, uploader.UploadRetryableError('Server disconnected')]),
        ) as upload_part:
            completed = await queue_processor._run_upload_job(job)

        self.assertFalse(completed)
        self.assertEqual(job.next_part_index, 1)
        self.assertTrue(os.path.exists(source))
        self.assertTrue(os.path.exists(part_one))
        self.assertTrue(os.path.exists(part_two))
        self.assertIn(job.job_id, queue_processor._upload_retry_jobs)
        self.assertEqual(upload_part.await_count, 2)

        with patch('queue_processor._upload_one_part', new=AsyncMock(return_value=None)) as upload_part:
            completed = await queue_processor._run_upload_job(job)

        self.assertTrue(completed)
        self.assertEqual(upload_part.await_count, 1)
        self.assertFalse(os.path.exists(source))
        self.assertFalse(os.path.exists(part_one))
        self.assertFalse(os.path.exists(part_two))
        self.assertNotIn(job.job_id, queue_processor._upload_retry_jobs)


class TestLiveUploadControls(unittest.TestCase):
    def test_live_upload_does_not_build_audio_download_button(self):
        raw_markup, telegram_markup = queue_processor._upload_reply_markups(
            'https://example.test/live', allow_audio_download=False
        )

        self.assertIsNone(raw_markup)
        self.assertIsNone(telegram_markup)


if __name__ == '__main__':
    unittest.main()
