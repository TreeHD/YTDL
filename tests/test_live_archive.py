import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from live_archive import closed_chunks, fetch_replay, probe_media, record_dvr


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools required')
class ArchiveMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

        def ffmpeg(*args):
            subprocess.run(['ffmpeg', '-v', 'error', *map(str, args)], check=True)

        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=size=96x64:rate=12:duration=5',
               '-c:v', 'mpeg4', '-g', '12', self.work / 'video.mkv')
        ffmpeg('-f', 'lavfi', '-i', 'sine=duration=5', '-c:a', 'aac', self.work / 'audio.mka')
        ffmpeg('-i', self.work / 'video.mkv', '-i', self.work / 'audio.mka',
               '-c', 'copy', self.work / 'complete.mp4')
        source = self.work / 'yt-dlp'
        source.write_text("""#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path
work = Path(__file__).parent
args = sys.argv[1:]
if '--live-from-start' in args:
    audio = args[args.index('-f') + 1] == 'ba/b'
    source = 'audio.mka' if audio else 'video.mkv'
    with (work / source).open('rb') as media:
        shutil.copyfileobj(media, sys.stdout.buffer)
    sys.stdout.flush()
    if os.getenv('DVR_SOURCE_FAILS'):
        # Keep the pipes open so the final segment is still open when the
        # video source dies, exactly like a proxy dropping mid-segment.
        import time
        time.sleep(60 if audio else 2)
        if not audio:
            sys.exit(1)
else:
    output = Path(args[args.index('-o') + 1].replace('%(ext)s', 'mp4'))
    shutil.copy(work / 'complete.mp4', output)
    Path(args[args.index('--print-to-file') + 2]).write_text('same ' + os.getenv('REPLAY_LENGTH', '5'))
""")
        source.chmod(0o755)
        self.enterContext(patch.dict(os.environ, PATH=str(self.work) + os.pathsep + os.environ['PATH']))
        self.enterContext(patch('live_archive.get_ffmpeg_command', return_value=shutil.which('ffmpeg')))

    async def test_parallel_pipes_produce_independently_verified_segments(self):
        verified = []

        async def accept(path):
            verified.append(probe_media(path, chunk=True))

        with patch('live_archive.CHUNK_SECONDS', 2):
            await asyncio.wait_for(record_dvr('local', None, None, str(self.work / 'part'),
                                              accept, lambda: False, lambda: False), 15)
        self.assertGreaterEqual(len(verified), 2)
        self.assertTrue(all(m.width == 96 and m.audio == 'aac' and m.bitrate > 0 for m in verified))

    async def test_failed_source_does_not_accept_truncated_final_segment(self):
        accepted = []

        async def accept(path):
            accepted.append(path)

        prefix = str(self.work / 'part')
        with patch('live_archive.CHUNK_SECONDS', 2), patch.dict(os.environ, DVR_SOURCE_FAILS='1'):
            with self.assertRaises(ValueError):
                await asyncio.wait_for(record_dvr('local', None, None, prefix,
                                                  accept, lambda: False, lambda: False), 30)
        listed = closed_chunks(prefix + '.csv')
        self.assertGreaterEqual(len(accepted), 1)
        self.assertLess(len(accepted), len(listed))
        self.assertNotIn(listed[-1], accepted)

    def test_video_only_and_short_audio_fail_verification(self):
        with self.assertRaisesRegex(ValueError, 'missing audio'):
            probe_media(self.work / 'video.mkv', chunk=True)
        short = self.work / 'short.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.work / 'video.mkv'), '-t', '1',
                        '-i', str(self.work / 'audio.mka'), '-c', 'copy', str(short)], check=True)
        with self.assertRaisesRegex(ValueError, 'timelines'):
            probe_media(short)

    async def test_replay_requires_matching_id_duration_and_both_tracks(self):
        target = str(self.work / 'result.mp4')
        args = ([None], None, target, lambda: False)
        self.assertIsNone(await fetch_replay('different', *args))
        with patch.dict(os.environ, REPLAY_LENGTH='90'):
            self.assertIsNone(await fetch_replay('same', *args))
        self.assertFalse(os.path.exists(target))
        info = await fetch_replay('same', *args)
        self.assertEqual((info.height, info.audio), (64, 'aac'))
        self.assertTrue(os.path.exists(target))
