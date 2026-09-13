import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import queue_processor as qp


class FakeProcess:
    def __init__(self, returncode=None, stdout=None, stdin=None):
        self.returncode = returncode
        self.pid = 123
        self.stdout = stdout
        self.stderr = SimpleNamespace(read=AsyncMock(return_value=b''))
        self.stdin = stdin

    def send_signal(self, signal):
        self.returncode = 0

    async def wait(self):
        return self.returncode


class TestLiveRecording(unittest.IsolatedAsyncioTestCase):
    async def run_recording(self, scenario):
        task_id = 'test_live'
        real_sleep = asyncio.sleep
        recorders = []
        statuses = []
        uploads = []
        retired = asyncio.Event()
        archive_upload_started = asyncio.Event()
        resumed = asyncio.Event()
        reads = 0
        ticks = 0
        archive_scenario = scenario in ('archive_eof', 'archive_stall')

        async def archive_read(size):
            nonlocal reads
            if not archive_scenario:
                return b''
            reads += 1
            if reads <= 2:
                return b'x' * 2048
            await retired.wait()
            if scenario == 'archive_stall':
                if reads == 3:
                    raise asyncio.TimeoutError
                await resumed.wait()
            archive.returncode = 0
            return b''

        archive = FakeProcess(
            returncode=None if archive_scenario else 1,
            stdout=SimpleNamespace(read=archive_read),
        )

        async def status(text, **kwargs):
            statuses.append(text)
            if 'Live-edge backup stopped' in text:
                retired.set()
            if 'Part 3)' in text:
                qp.stopped_tasks.add(task_id)

        async def sleep(delay):
            nonlocal ticks
            ticks += 1
            if ticks > 200:
                qp.cancelled_tasks.add(task_id)
                resumed.set()
            await real_sleep(0)

        async def upload(*args, **kwargs):
            uploads.append(args[3])
            if scenario == 'archive_eof' and 'From Start' in args[3]:
                archive_upload_started.set()
                await resumed.wait()
            return True

        with tempfile.TemporaryDirectory() as directory:
            class FakeStdin:
                def __init__(self, segment_path, list_path):
                    self.segment_path = segment_path
                    self.list_path = list_path
                    self.written = 0
                    self.closed = False

                def write(self, data):
                    self.written += len(data)
                    if self.written >= 1024 and not os.path.exists(self.segment_path):
                        with open(self.segment_path, 'wb') as output:
                            output.write(b'x' * 2048)
                        with open(self.list_path, 'w') as listing:
                            listing.write(f'{os.path.basename(self.segment_path)},0,60\n')

                async def drain(self):
                    return None

                def is_closing(self):
                    return self.closed

                def close(self):
                    self.closed = True

                async def wait_closed(self):
                    return None

            async def spawn(*cmd, **kwargs):
                if cmd[0] == 'yt-dlp':
                    return archive
                if cmd[0] == 'ffmpeg' and '-segment_list' in cmd:
                    pattern = cmd[-1]
                    segment_path = pattern.replace('%06d', '000001')
                    list_path = cmd[cmd.index('-segment_list') + 1]
                    return FakeProcess(
                        returncode=0,
                        stdin=FakeStdin(segment_path, list_path),
                    )
                if cmd[0] == 'streamlink':
                    proc = FakeProcess()
                    recorders.append(proc)
                    path = cmd[cmd.index('-o') + 1]
                    if archive_scenario:
                        size = 2048
                        if len(recorders) == 2:
                            # Recovery must occur while the archive upload is pending.
                            if scenario == 'archive_eof':
                                await archive_upload_started.wait()
                            resumed.set()
                            qp.stopped_tasks.add(task_id)
                    elif scenario == 'empty_handoff' and len(recorders) == 2:
                        size = 0
                    else:
                        if scenario == 'empty_handoff' and len(recorders) == 3:
                            self.assertIsNone(recorders[0].returncode)
                            self.assertEqual(recorders[1].returncode, 0)
                        size = 1900 * 1024 * 1024
                    with open(path, 'wb') as media:
                        # Sparse files exercise the real size threshold without disk I/O.
                        media.truncate(size)
                    return proc
                with open(cmd[-1], 'wb') as media:
                    media.write(b'm' * 2048)
                return FakeProcess(returncode=0)

            with ExitStack() as stack:
                for name, value in {
                    'DOWNLOAD_DIR': directory,
                    'LIVE_FROM_START_STABILITY_SECONDS': 0,
                    'LIVE_FROM_START_MAX_DATA_GAP_SECONDS': -1 if scenario == 'archive_stall' else 30,
                    'get_proxy_list': lambda: [None],
                    'get_cookie_file': lambda: None,
                    'get_ffmpeg_command': lambda: 'ffmpeg',
                    'handle_upload': upload,
                    '_free_memory': lambda: None,
                }.items():
                    stack.enter_context(patch.object(qp, name, value))
                stack.enter_context(patch.object(qp.asyncio, 'sleep', sleep))
                stack.enter_context(patch.object(qp.asyncio, 'create_subprocess_exec', spawn))
                message = SimpleNamespace(text='', edit_text=status, delete=AsyncMock())
                try:
                    await asyncio.wait_for(qp.process_live_stream(
                        SimpleNamespace(bot=SimpleNamespace()), 1, 'https://example.test/live',
                        2, message, task_id, status, 'Channel',
                    ), timeout=5)
                finally:
                    qp.stopped_tasks.discard(task_id)
                    qp.cancelled_tasks.discard(task_id)

        self.assertLess(ticks, 200, statuses)
        self.assertFalse(any('Live recording error' in text for text in statuses), statuses)
        return recorders, statuses, uploads, resumed.is_set()

    async def test_no_vod_continues_across_two_size_rollovers(self):
        recorders, statuses, uploads, _ = await self.run_recording('no_vod')
        self.assertEqual(len(recorders), 3)
        self.assertEqual(len(uploads), 3)
        self.assertTrue(any('Part 3)' in text for text in statuses))
        self.assertFalse(any('(End)' in title for title in uploads))

    async def test_empty_replacement_keeps_original_recorder_alive(self):
        recorders, statuses, uploads, _ = await self.run_recording('empty_handoff')
        self.assertEqual(len(recorders), 4)
        self.assertTrue(any('handoff delayed' in text for text in statuses))
        self.assertEqual(len(uploads), 3)

    async def test_archive_eof_restores_edge_before_upload_completes(self):
        recorders, statuses, uploads, resumed = await self.run_recording('archive_eof')
        self.assertTrue(resumed)
        self.assertEqual(len(recorders), 2)
        self.assertTrue(any('Live-edge backup stopped' in text for text in statuses))
        self.assertTrue(any('From Start' in title for title in uploads))

    async def test_stalled_archive_restores_edge_while_process_is_alive(self):
        recorders, statuses, uploads, resumed = await self.run_recording('archive_stall')
        self.assertTrue(resumed)
        self.assertEqual(len(recorders), 2)
        self.assertTrue(any('Live-edge backup stopped' in text for text in statuses))

    def test_archive_segmenter_outputs_container_aware_closed_chunks(self):
        command = qp._build_archive_segment_command(
            'ffmpeg', '/tmp/live_chunk_%06d.ts', '/tmp/live_segments.csv'
        )
        self.assertIn('pipe:0', command)
        self.assertIn('segment', command)
        self.assertIn('mpegts', command)
        self.assertIn('copy', command)
        self.assertEqual(command[command.index('-segment_time') + 1], '60')

    def test_only_closed_segments_from_ffmpeg_manifest_are_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            first = os.path.join(directory, 'chunk_000001.ts')
            with open(first, 'wb') as media:
                media.write(b'a' * 2048)
            listing = os.path.join(directory, 'segments.csv')
            with open(listing, 'w') as output:
                output.write('chunk_000001.ts,0,60\n')
                output.write('chunk_000002.ts,60')  # ffmpeg is still writing it
            self.assertEqual(qp._read_completed_archive_chunks(listing, directory), [first])


if __name__ == '__main__':
    unittest.main()
