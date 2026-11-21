"""YouTube DVR capture and packet-level verification of closed media files."""

import asyncio
import csv
from contextlib import ExitStack
from dataclasses import dataclass
import json
import logging
import math
import os
import subprocess
import tempfile
import time

from config import get_ffmpeg_command
from cookie_manager import cookie_snapshot
from download_errors import error_hint

logger = logging.getLogger(__name__)
CHUNK_SECONDS = 60
MAX_DATA_GAP = 30


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    audio: str
    bitrate: int
    boundary: tuple = ()

    def describe(self):
        return f"{self.width}×{self.height} · {self.audio} · {self.bitrate / 1000000:.2f} Mbps"


def probe_media(path, *, chunk=False):
    """Measure actual packet timelines for minute chunks and count packets in final MP4s."""
    fields = 'stream=index,codec_type,codec_name,width,height,start_time,duration,nb_read_packets:format=duration,size'
    options = ['-count_packets']
    if chunk:
        fields += ':packet=stream_index,pts_time,data_hash'
        options = ['-show_packets', '-show_data_hash', 'sha256']
    ffprobe = os.path.join(os.path.dirname(get_ffmpeg_command()), 'ffprobe')
    try:
        result = subprocess.run(
            [ffprobe, '-v', 'error', *options, '-show_entries', fields, '-of', 'json', path],
            capture_output=True, check=True, timeout=30 if chunk else 180,
        )
        info = json.loads(result.stdout)
        streams = info.get('streams', [])
        spans = {}
        fingerprints = {}
        for packet in info.get('packets', []):
            if packet.get('pts_time') in (None, 'N/A'):
                continue
            index = packet['stream_index']
            pts = float(packet['pts_time'])
            digest = packet['data_hash']
            first = fingerprints.get(index, (digest,))[0]
            fingerprints[index] = (first, digest)
            low, high = spans.get(index, (pts, pts))
            spans[index] = min(low, pts), max(high, pts)
        tracks = {}
        for stream in streams:
            kind = stream['codec_type']
            if kind not in ('audio', 'video'):
                continue
            if chunk:
                low, high = spans.get(stream['index'], (0, 0))
            else:
                if int(stream.get('nb_read_packets', 0)) < 2:
                    continue
                low = float(stream.get('start_time', 0))
                high = low + float(stream['duration'])
            if high > low:
                tracks[kind] = stream, low, high
        if 'audio' not in tracks or 'video' not in tracks:
            raise ValueError('missing audio or video packets with advancing timestamps')
        video, vstart, vend = tracks['video']
        audio, astart, aend = tracks['audio']
        duration = float(info['format']['duration'])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('invalid media duration')
        if min(vend, aend) <= max(vstart, astart) or max(abs(vstart - astart), abs(vend - aend)) > 2:
            raise ValueError('audio and video timelines do not cover the same interval')
        return MediaInfo(duration, video['width'], video['height'], audio['codec_name'],
                         round(8 * int(info['format']['size']) / duration),
                         tuple(fingerprints[track['index']] for track in (video, audio)) if chunk else ())
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f'ffprobe verification failed: {exc}') from exc


def closed_chunks(list_path):
    try:
        with open(list_path, newline='', encoding='utf-8') as listing:
            return [os.path.join(os.path.dirname(list_path), os.path.basename(row[0]))
                    for row in csv.reader(listing) if len(row) == 3 and row[2].strip()]
    except FileNotFoundError:
        return []


async def drain_tail(stream, tail, max_bytes=32768):
    if stream is None:
        return
    while data := await stream.read(4096):
        tail.extend(data)
        del tail[:-max_bytes]


async def stop_process(proc):
    if proc.returncode is None:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


async def record_dvr(url, proxy, cookies, prefix, on_chunk, should_stop, is_cancelled):
    """One proxy attempt. Feed audio and video concurrently through separate OS pipes."""
    ffmpeg = get_ffmpeg_command()
    listing = prefix + '.csv'
    pattern = prefix + '_%06d.mkv'
    sources = []
    processes = []
    drains = []
    tails = []
    mux = None
    consumed = 0

    async def collect():
        nonlocal consumed
        for path in closed_chunks(listing)[consumed:]:
            await on_chunk(path)
            consumed += 1

    async def spawn(cmd, **kwargs):
        proc = await asyncio.create_subprocess_exec(*cmd, stderr=asyncio.subprocess.PIPE, **kwargs)
        processes.append(proc)
        tail = bytearray()
        tails.append(tail)
        drains.append(asyncio.create_task(drain_tail(proc.stderr, tail)))
        return proc

    with ExitStack() as files:
        try:
            with ExitStack() as pipes:
                read_fds = []
                for fmt in ('bv*/b', 'ba/b'):
                    snapshot = files.enter_context(cookie_snapshot(cookies))
                    read_fd, write_fd = os.pipe()
                    pipes.callback(os.close, read_fd)
                    with os.fdopen(write_fd, 'wb') as output:
                        cmd = ['yt-dlp', '--no-playlist', '--no-part', '--no-progress',
                               '--live-from-start', '--match-filter', 'is_live', '--abort-on-unavailable-fragments',
                               '-S', 'res,fps,br', '--socket-timeout', '30',
                               '--ffmpeg-location', ffmpeg, '-f', fmt, '-o', '-']
                        if proxy:
                            cmd.extend(['--proxy', proxy])
                        if snapshot:
                            cmd.extend(['--cookies', snapshot])
                        # yt-dlp writes temporary DASH fragments even with stdout output.
                        work = files.enter_context(tempfile.TemporaryDirectory(dir=os.path.dirname(prefix)))
                        sources.append(await spawn([*cmd, '--', url], stdout=output, cwd=work))
                    read_fds.append(read_fd)
                cmd = [ffmpeg, '-nostdin', '-y', '-hide_banner', '-loglevel', 'warning']
                for fd in read_fds:
                    cmd.extend(['-thread_queue_size', '512', '-i', f'pipe:{fd}'])
                cmd.extend(['-map', '0:v:0', '-map', '1:a:0', '-c', 'copy',
                            '-f', 'segment', '-segment_format', 'matroska',
                            '-segment_time', str(CHUNK_SECONDS), '-reset_timestamps', '1',
                            '-segment_list', listing, '-segment_list_type', 'csv', pattern])
                mux = await spawn(cmd, pass_fds=tuple(read_fds), stdout=asyncio.subprocess.DEVNULL)

            last_segment = time.monotonic()
            while mux.returncode is None and not should_stop():
                before = consumed
                await collect()
                if consumed > before:
                    last_segment = time.monotonic()
                if any(p.returncode not in (None, 0) for p in sources):
                    break
                if time.monotonic() - last_segment > CHUNK_SECONDS + MAX_DATA_GAP + (0 if consumed else 60):
                    raise ValueError('timed out waiting for a closed DVR segment')
                await asyncio.sleep(1)
        finally:
            # Closing both writers gives FFmpeg EOF so that it finalizes its last segment.
            await asyncio.gather(*(stop_process(p) for p in sources))
            if mux and mux.returncode is None:
                try:
                    await asyncio.wait_for(mux.wait(), 15)
                except asyncio.TimeoutError:
                    await stop_process(mux)
            await asyncio.gather(*drains)
        if not is_cancelled():
            await collect()
        if not should_stop() and (not consumed or any(p.returncode != 0 for p in processes)):
            hints = [error_hint(t.decode(errors='replace')) for t in tails]
            raise ValueError(next((h for h in hints if h), 'DVR stream unavailable or interrupted'))


async def fetch_replay(video_id, proxies, cookies, target, stopping, archive_duration=0, archive_height=0,
                       archive_bitrate=0):
    """Fetch a processed replay of this exact broadcast, using isolated attempts."""
    url = f'https://www.youtube.com/watch?v={video_id}'
    for attempt, proxy in enumerate(proxies, 1):
        if stopping():
            break
        with tempfile.TemporaryDirectory(dir=os.path.dirname(target)) as work, cookie_snapshot(cookies) as snapshot:
            duration_file = os.path.join(work, 'duration')
            cmd = ['yt-dlp', '--no-playlist', '--no-progress', '--socket-timeout', '30',
                   '--retries', '2', '--fragment-retries', '2', '--abort-on-unavailable-fragments',
                   '--no-simulate',
                   '--print-to-file', 'before_dl:%(id)s %(duration)s', duration_file,
                   '--ffmpeg-location', get_ffmpeg_command(), '-S', 'res,fps,br', '-f', 'bv*+ba/b',
                   '--merge-output-format', 'mp4', '--remux-video', 'mp4',
                   '-o', os.path.join(work, 'replay.%(ext)s')]
            filters = ['live_status=was_live']
            if archive_duration and archive_height:
                filters = [f'live_status=was_live & duration>{archive_duration + 3:.2f}',
                           f'live_status=was_live & height>{archive_height}']
                if archive_bitrate:
                    filters.append(f'live_status=was_live & height>={archive_height} & tbr>{archive_bitrate / 1000:.2f}')
            for condition in filters:
                cmd.extend(['--match-filter', condition])
            if snapshot:
                cmd.extend(['--cookies', snapshot])
            if proxy:
                cmd.extend(['--proxy', proxy])
            try:
                proc = await asyncio.create_subprocess_exec(*cmd, '--', url, stdout=asyncio.subprocess.DEVNULL,
                                                            stderr=asyncio.subprocess.PIPE)
            except OSError as exc:
                logger.warning('Could not start replay download: %s', exc)
                return None
            tail = bytearray()
            drain = asyncio.create_task(drain_tail(proc.stderr, tail))
            try:
                while proc.returncode is None and not stopping():
                    try:
                        await asyncio.wait_for(proc.wait(), 2)
                    except asyncio.TimeoutError:
                        pass
            finally:
                await stop_process(proc)
                await drain
            if stopping():
                return None
            if proc.returncode != 0:
                logger.warning('Replay attempt %d failed: %s', attempt,
                               error_hint(tail.decode(errors='replace')) or 'source unavailable')
                continue
            path = os.path.join(work, 'replay.mp4')
            if not os.path.isfile(path):
                continue
            try:
                with open(duration_file, encoding='utf-8') as meta:
                    actual_id, length = meta.read().strip().split()
                info = await asyncio.to_thread(probe_media, path)
                expected = float(length)
                if actual_id != video_id or not math.isfinite(expected) or abs(info.duration - expected) > 3:
                    raise ValueError('replay ID or duration mismatch')
            except (ValueError, OSError) as exc:
                logger.warning('Replay attempt %d failed verification: %s', attempt, exc)
                continue
            if (archive_duration and archive_height and info.duration <= archive_duration + 3
                    and info.height <= archive_height
                    and (info.height < archive_height or info.bitrate <= archive_bitrate)):
                return None
            os.replace(path, target)
            return info
    return None
