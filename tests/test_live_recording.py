import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import downloader
from live_archive import MediaInfo, closed_chunks
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
        ticks = 0
        archive_scenario = scenario in ('archive_eof', 'archive_stall', 'proxy_retry', 'missing_audio', 'resume', 'mismatch', 'bad_mp4', 'busy_upload')
        all_chunks_verified = asyncio.Event()
        attempts = []
        check_replay = scenario in ('public_replay', 'no_replay')
        reconnect = scenario == 'reconnect'
        replay_ids = []
        self.remux_inputs = []
        self.parts_left_at_upload = []

        async def replay(video_id, proxies, cookies, target, stopping, *coverage):
            replay_ids.append(video_id)
            if scenario == 'public_replay':
                Path(target).write_bytes(b'v' * 2048)
                return MediaInfo(60, 1920, 1080, 'aac', 2000000)

        async def record(url, proxy, cookies, prefix, accept, stopping, cancelled):
            attempts.append(proxy)
            if scenario == 'proxy_retry' and len(attempts) == 1:
                raise ValueError('no media from proxy')
            if not archive_scenario:
                raise ValueError('no DVR')
            path = Path(prefix + '.mkv')
            path.write_bytes(b'x' * 2048)
            await accept(str(path))
            if scenario == 'busy_upload':
                # Every chunk closes a batch, so batch 1 is uploading while
                # the DVR keeps producing; verification must not wait for it.
                for index in range(2, 8):
                    chunk = Path(f'{prefix}_{index}.mkv')
                    chunk.write_bytes(b'x' * 2048)
                    await accept(str(chunk))
                all_chunks_verified.set()
            if scenario in ('resume', 'mismatch'):
                if len(attempts) == 1:
                    raise ValueError('proxy disconnected')
                next_path = Path(prefix + '_next.mkv')
                next_path.write_bytes(b'y' * 2048)
                await accept(str(next_path))
            await retired.wait()
            if scenario == 'archive_stall':
                raise ValueError('DVR stopped producing segments')

        def probe(path, *, chunk=False):
            if scenario == 'missing_audio' and chunk:
                raise ValueError('missing audio packets')
            if scenario == 'bad_mp4' and not chunk and 'fromstart' in str(path):
                raise ValueError('final MP4 has no audio')
            boundary = ('changed' if scenario == 'mismatch' and len(attempts) > 1 else 'same',)
            return MediaInfo(60, 1920, 1080, 'aac', 2000000, boundary)

        async def status(text, **kwargs):
            statuses.append(text)
            if (check_replay or reconnect) and 'Recording live stream' in text:
                recorders[0].returncode = 1
            if 'Live-edge backup stopped' in text:
                retired.set()
            if 'missing audio packets' in text or 'timelines differ' in text:
                qp.stopped_tasks.add(task_id)

        async def sleep(delay):
            nonlocal ticks
            ticks += 1
            if ticks > 10000:
                qp.cancelled_tasks.add(task_id)
                resumed.set()
            await real_sleep(0.001)

        async def upload(*args, **kwargs):
            uploads.append(args[3])
            self.parts_left_at_upload.extend(
                name for name in os.listdir(os.path.dirname(args[2])) if name.endswith('.ts')
            )
            if scenario == 'archive_eof' and 'From Start' in args[3]:
                archive_upload_started.set()
                await resumed.wait()
            if scenario == 'busy_upload' and 'From Start Part 1' in args[3]:
                await asyncio.wait_for(all_chunks_verified.wait(), 2)
            return True

        with tempfile.TemporaryDirectory() as directory:
            async def spawn(*cmd, **kwargs):
                if scenario == 'resume' and '-f' in cmd and 'concat' in cmd:
                    manifest = Path(cmd[cmd.index('-i') + 1]).read_text()
                    self.assertEqual(manifest.count('file '), 2)
                    self.assertIn('_1.mkv', manifest)
                    self.assertNotIn('_2.mkv', manifest)
                if cmd[0] == 'streamlink':
                    proc = FakeProcess()
                    recorders.append(proc)
                    path = cmd[cmd.index('-o') + 1]
                    if check_replay:
                        size = 2048 if len(recorders) == 1 else 0
                        if not size:
                            proc.returncode = 1
                    elif reconnect:
                        # The first recorder drops mid-segment; the second one
                        # continues the same segment, then the user stops.
                        size = 2048
                        if len(recorders) == 2:
                            qp.stopped_tasks.add(task_id)
                    elif archive_scenario:
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
                        # Stop once the Part 3 recorder exists. Status edits are
                        # delivered in the background, so a status text cannot
                        # serve as a synchronous hook for this.
                        if len(recorders) == (4 if scenario == 'empty_handoff' else 3):
                            qp.stopped_tasks.add(task_id)
                        size = 1900 * 1024 * 1024
                    with open(path, 'wb') as media:
                        # Sparse files exercise the real size threshold without disk I/O.
                        media.truncate(size)
                    return proc
                if '-i' in cmd and '-f' not in cmd:
                    self.remux_inputs.append(cmd[cmd.index('-i') + 1])
                with open(cmd[-1], 'wb') as media:
                    media.write(b'm' * 2048)
                return FakeProcess(returncode=0)

            with ExitStack() as stack:
                for name, value in {
                    'DOWNLOAD_DIR': directory,
                    'LIVE_FROM_START_STABILITY_SECONDS': 0,
                    'ARCHIVE_BATCH_BYTES': 1 if scenario == 'busy_upload' else qp.ARCHIVE_BATCH_BYTES,
                    'record_dvr': record,
                    'probe_media': probe,
                    'fetch_replay': replay,
                    'probe_live_state': lambda *args: SimpleNamespace(state='ENDED', errors=[]),
                    'get_proxy_list': lambda: ['first', 'second'] if scenario in ('proxy_retry', 'resume', 'mismatch') else [None],
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
                        SimpleNamespace(bot=SimpleNamespace()), 1, 'https://www.youtube.com/watch?v=sample',
                        2, message, task_id, status, 'Channel', expected_live_video_id='sample',
                    ), timeout=5)
                finally:
                    qp.stopped_tasks.discard(task_id)
                    qp.cancelled_tasks.discard(task_id)

        self.assertLess(ticks, 10000, statuses)
        self.assertFalse(any('Live recording error' in text for text in statuses), statuses)
        self.assertEqual(replay_ids, ['sample'] if check_replay else [])
        if scenario == 'proxy_retry':
            self.assertEqual(attempts, ['first', 'second'])
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

    async def test_proxy_with_no_media_rotates_before_handoff(self):
        _, statuses, uploads, _ = await self.run_recording('proxy_retry')
        self.assertTrue(any('Live-edge backup stopped' in text for text in statuses))
        self.assertTrue(any('From Start' in title for title in uploads))

    async def test_unverified_archive_keeps_edge_and_skips_archive_upload(self):
        _, statuses, uploads, _ = await self.run_recording('missing_audio')
        self.assertFalse(any('Live-edge backup stopped' in text for text in statuses))
        self.assertFalse(any('From Start' in title for title in uploads))
        self.assertTrue(any('LIVE Part' in title for title in uploads))

    async def test_resume_deduplicates_verified_overlap(self):
        _, _, uploads, _ = await self.run_recording('resume')
        self.assertTrue(any('From Start' in title for title in uploads))

    async def test_mismatched_checkpoint_keeps_edge(self):
        _, statuses, uploads, _ = await self.run_recording('mismatch')
        self.assertTrue(any('timelines differ' in text for text in statuses))
        self.assertTrue(any('LIVE Part' in title for title in uploads))

    async def test_final_verification_failure_recovers_retired_backup(self):
        _, statuses, uploads, _ = await self.run_recording('bad_mp4')
        self.assertTrue(any('backup stopped and retained' in text for text in statuses))
        self.assertFalse(any('From Start' in title for title in uploads))
        self.assertEqual(sum('LIVE Part' in title for title in uploads), 2)

    async def test_public_replay_replaces_remaining_edge_after_confirmed_end(self):
        _, _, uploads, _ = await self.run_recording('public_replay')
        self.assertEqual(uploads, ['Channel - Full replay'])

    async def test_no_replay_delivers_recorded_edge(self):
        _, _, uploads, _ = await self.run_recording('no_replay')
        self.assertEqual(len(uploads), 1)
        self.assertIn('LIVE Part', uploads[0])

    async def test_archive_keeps_verifying_while_a_batch_uploads(self):
        _, statuses, uploads, _ = await self.run_recording('busy_upload')
        self.assertFalse(any('From-start segment' in text for text in statuses), statuses)
        self.assertEqual(sum('From Start' in title for title in uploads), 7)

    async def test_reconnected_segment_is_remuxed_from_parts_without_copying(self):
        _, _, uploads, _ = await self.run_recording('reconnect')
        self.assertEqual(uploads, ['\U0001f534 Channel - LIVE Part 1 (Stopped by user)'])
        self.assertEqual(len(self.remux_inputs), 1)
        source = self.remux_inputs[0]
        self.assertTrue(source.startswith('concat:'), source)
        self.assertEqual([os.path.basename(p) for p in source[len('concat:'):].split('|')],
                         ['live_test_live_p001.ts', 'live_test_live_p002.ts'])
        # Parts are released once the MP4 is verified, before the upload.
        self.assertEqual(self.parts_left_at_upload, [])

    async def test_tracked_recording_reports_outcome_and_blocks_warp_rotation(self):
        self.assertIs(qp.active_live_tasks, downloader.active_live_tasks)
        args = (None, 1, 'url', 2, None, 'tracked-task', None, 'Channel', 'vid')
        seen, outcomes = [], []

        async def finished(*_):
            seen.append(set(downloader.active_live_tasks))
            return 'proxy_exhausted'

        with patch.object(qp, 'process_live_stream', finished):
            await qp.process_live_stream_tracked(*args, on_finished=outcomes.append)
        self.assertEqual(seen, [{'tracked-task'}])
        self.assertEqual(outcomes, ['proxy_exhausted'])
        self.assertNotIn('tracked-task', downloader.active_live_tasks)

        async def crashed(*_):
            raise RuntimeError('boom')

        with patch.object(qp, 'process_live_stream', crashed), self.assertRaises(RuntimeError):
            await qp.process_live_stream_tracked(*args, on_finished=outcomes.append)
        self.assertEqual(outcomes, ['proxy_exhausted', 'error'])

    def test_only_closed_segments_from_ffmpeg_manifest_are_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            first = os.path.join(directory, 'chunk_000001.ts')
            with open(first, 'wb') as media:
                media.write(b'a' * 2048)
            listing = os.path.join(directory, 'segments.csv')
            with open(listing, 'w') as output:
                output.write('chunk_000001.ts,0,60\n')
                output.write('chunk_000002.ts,60')  # ffmpeg is still writing it
            self.assertEqual(closed_chunks(listing), [first])


if __name__ == '__main__':
    unittest.main()
