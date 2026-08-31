import os
import gc
import ctypes
import asyncio
import time
import logging
import glob
import itertools
import secrets
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import load_config, check_disk_space, check_ffmpeg, DOWNLOAD_DIR, get_ffmpeg_command, get_proxy_list, get_cookie_file
from downloader import download_content, get_video_info, get_playlist_info
from uploader import (
    upload_video_streaming,
    upload_audio_streaming,
    split_video,
    crop_to_square,
    UploadRetryableError,
    UploadPermanentError,
)
from handlers import cancelled_tasks, stopped_tasks
from telegram_utils import tg_retry

logger = logging.getLogger(__name__)

# Live recordings run outside the request queue processor and need explicit
# tracking so maintenance never starts while they are active.
active_live_tasks = set()

# The live-edge recorder is only a temporary safety net while the archive from
# the beginning proves it can keep recording.  After this interval, keeping
# both recorders would only create duplicate uploads.
LIVE_FROM_START_STABILITY_SECONDS = 10 * 60
LIVE_FROM_START_MAX_DATA_GAP_SECONDS = 30

# Upload retries are deliberately separate from the download queue.  A Telegram
# flood-control delay must never keep every later download waiting behind it.
UPLOAD_RETRY_WINDOW_SECONDS = 24 * 60 * 60
UPLOAD_RETRY_MAX_DELAY_SECONDS = 15 * 60
_upload_retry_queue = None
_upload_retry_wakeup = None
_upload_retry_jobs = {}
_upload_retry_counter = itertools.count()
_upload_retry_tasks = set()


@dataclass
class UploadJob:
    """An in-memory, resumable upload of one media item or its split parts."""

    application: object
    chat_id: int
    source_file_path: str
    files_to_upload: list
    title: str
    url: str
    audio_only: bool = False
    update_status_func: object = None
    channel_name: str = None
    reply_to_message_id: int = None
    thumb_path: str = None
    allow_audio_download: bool = True
    job_id: str = field(default_factory=lambda: secrets.token_urlsafe(8))
    next_part_index: int = 0
    retry_deadline: float = None
    retry_count: int = 0
    retry_generation: int = 0
    running: bool = False
    completed: bool = False
    auto_retry_expired: bool = False


def _safe_remove(path):
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as exc:
        logger.warning("Failed to remove temporary upload file %s: %s", path, exc)


async def _update_upload_job_status(job, text, show_retry=False):
    """Update the request's one status message, where one is available."""
    if not job.update_status_func:
        return
    try:
        if show_retry:
            await job.update_status_func(text, force=True, retry_job_id=job.job_id)
        else:
            await job.update_status_func(text, force=True)
    except TypeError:
        # Live and older call sites do not expose inline keyboard support.
        await job.update_status_func(text, force=True)
    except Exception as exc:
        logger.warning("Failed to update upload status for %s: %s", job.job_id, exc)


def _retry_delay(error, retry_count):
    if isinstance(error, UploadRetryableError) and error.retry_after is not None:
        return max(0, error.retry_after)
    return min(60 * (2 ** min(retry_count, 4)), UPLOAD_RETRY_MAX_DELAY_SECONDS)


async def _schedule_upload_retry(job, error):
    """Keep failed media on disk and schedule its next background attempt."""
    global _upload_retry_queue, _upload_retry_wakeup

    if isinstance(error, UploadPermanentError):
        job.auto_retry_expired = True
        await _update_upload_job_status(
            job,
            (
                f"❌ Upload rejected at part {job.next_part_index + 1}/{len(job.files_to_upload)}: {error}\n"
                "Files are kept; correct the problem, then use Retry upload now."
            ),
            show_retry=True,
        )
        return

    now = time.monotonic()
    if job.retry_deadline is None:
        job.retry_deadline = now + UPLOAD_RETRY_WINDOW_SECONDS

    if now >= job.retry_deadline:
        job.auto_retry_expired = True
        await _update_upload_job_status(
            job,
            (
                f"❌ Upload paused at part {job.next_part_index + 1}/{len(job.files_to_upload)}.\n"
                "Automatic retries stopped after 24 hours; files are kept."
            ),
            show_retry=True,
        )
        return

    job.retry_count += 1
    job.auto_retry_expired = False
    delay = _retry_delay(error, job.retry_count - 1)
    due_at = min(now + delay, job.retry_deadline)
    job.retry_generation += 1

    if _upload_retry_queue is None:
        logger.error("Upload retry worker is unavailable; keeping job %s for manual retry", job.job_id)
        await _update_upload_job_status(
            job,
            f"❌ Upload failed for part {job.next_part_index + 1}/{len(job.files_to_upload)}: {error}\nFiles are kept.",
            show_retry=True,
        )
        return

    await _upload_retry_queue.put((due_at, next(_upload_retry_counter), job.job_id, job.retry_generation))
    if _upload_retry_wakeup:
        _upload_retry_wakeup.set()
    await _update_upload_job_status(
        job,
        (
            f"⚠️ Upload failed for part {job.next_part_index + 1}/{len(job.files_to_upload)}: {error}\n"
            f"⏳ Retrying automatically in {int(max(0, due_at - now))} seconds; files are kept."
        ),
        show_retry=True,
    )


def _caption_for_upload_part(job, part_index):
    if job.channel_name:
        caption = f"{job.channel_name}\n{job.title}\n{job.url}"
    else:
        caption = f"{job.title}\n{job.url}"

    if len(job.files_to_upload) > 1:
        if job.channel_name:
            caption = f"{job.channel_name}\n{job.title} (Part {part_index + 1}/{len(job.files_to_upload)})\n{job.url}"
        else:
            caption = f"{job.title} (Part {part_index + 1}/{len(job.files_to_upload)})\n{job.url}"
    return caption


def _upload_reply_markups(url, allow_audio_download=True):
    """Build the optional audio-download button for non-live uploads only."""
    if not allow_audio_download:
        return None, None
    audio_cb_data = f"audio:{url}"
    if len(audio_cb_data.encode('utf-8')) > 64:
        return None, None
    return (
        {"inline_keyboard": [[{"text": "🎵 Download Audio", "callback_data": audio_cb_data}]]},
        InlineKeyboardMarkup([[InlineKeyboardButton("🎵 Download Audio", callback_data=audio_cb_data)]]),
    )


async def _upload_one_part(job, part_index):
    """Send exactly one part; raw uploads perform their own bounded retries."""
    file_path = job.files_to_upload[part_index]
    if not os.path.exists(file_path):
        raise UploadPermanentError(f"Upload file is missing: {file_path}")

    config = load_config()
    api_url = config.get('api_url', '')
    bot_token = config.get('bot_token', '')
    is_local_api = api_url and 'api.telegram.org' not in api_url
    caption = _caption_for_upload_part(job, part_index)

    if job.audio_only:
        if is_local_api:
            await upload_audio_streaming(
                bot_token, api_url, job.chat_id, file_path, job.title, caption,
                reply_to_message_id=job.reply_to_message_id, thumb_path=job.thumb_path,
                max_retries=1,
            )
            return

        with open(file_path, 'rb') as media_fh:
            thumb_fh = None
            try:
                if job.thumb_path and os.path.exists(job.thumb_path):
                    thumb_fh = open(crop_to_square(job.thumb_path), 'rb')
                await tg_retry(
                    job.application.bot.send_audio,
                    chat_id=job.chat_id,
                    audio=media_fh,
                    title=job.title,
                    caption=caption,
                    reply_to_message_id=job.reply_to_message_id,
                    thumbnail=thumb_fh,
                )
            finally:
                if thumb_fh:
                    thumb_fh.close()
        return

    raw_markup, telegram_markup = _upload_reply_markups(
        job.url, allow_audio_download=job.allow_audio_download
    )
    if is_local_api:
        await upload_video_streaming(
            bot_token, api_url, job.chat_id, file_path, caption, raw_markup,
            reply_to_message_id=job.reply_to_message_id, thumb_path=job.thumb_path,
            max_retries=1,
        )
        return

    with open(file_path, 'rb') as media_fh:
        thumb_fh = None
        try:
            if job.thumb_path and os.path.exists(job.thumb_path):
                thumb_fh = open(crop_to_square(job.thumb_path), 'rb')
            await tg_retry(
                job.application.bot.send_video,
                chat_id=job.chat_id,
                video=media_fh,
                caption=caption,
                supports_streaming=True,
                reply_markup=telegram_markup,
                reply_to_message_id=job.reply_to_message_id,
                thumbnail=thumb_fh,
            )
        finally:
            if thumb_fh:
                thumb_fh.close()


def _cleanup_completed_upload(job):
    """Delete artifacts only after every part has reached Telegram."""
    paths = set(job.files_to_upload)
    paths.add(job.source_file_path)
    if job.thumb_path:
        paths.add(job.thumb_path)
    for path in paths:
        _safe_remove(path)


async def _execute_upload_job(job):
    """Run a job from its first unfinished part through completion or deferral."""
    try:
        while job.next_part_index < len(job.files_to_upload):
            part_number = job.next_part_index + 1
            await _update_upload_job_status(
                job,
                f"⬆️ Uploading {'audio' if job.audio_only else f'part {part_number}/{len(job.files_to_upload)}'}...",
            )
            try:
                await _upload_one_part(job, job.next_part_index)
            except Exception as exc:
                logger.error("Upload failed for job %s part %s: %s", job.job_id, part_number, exc)
                await _schedule_upload_retry(job, exc)
                return False
            job.next_part_index += 1

        await _update_upload_job_status(job, "🧹 Cleaning up...", show_retry=False)
        _cleanup_completed_upload(job)
        job.completed = True
        _upload_retry_jobs.pop(job.job_id, None)
        await _update_upload_job_status(job, "✅ Upload complete.", show_retry=False)
        return True
    finally:
        job.running = False


async def _run_upload_job(job):
    if job.running or job.completed:
        return False
    job.running = True
    return await _execute_upload_job(job)


async def process_upload_retry_queue():
    """Run one deferred upload at a time, leaving the download queue unblocked."""
    logger.info("Upload retry worker started.")
    while True:
        due_at, _, job_id, generation = await _upload_retry_queue.get()
        try:
            wait_seconds = due_at - time.monotonic()
            requeued_for_earlier_job = False
            if wait_seconds > 0:
                try:
                    # A newly scheduled job may have an earlier due time than
                    # this one.  Wake and put this entry back so PriorityQueue
                    # can choose the right job instead of sleeping past it.
                    await asyncio.wait_for(_upload_retry_wakeup.wait(), timeout=wait_seconds)
                    _upload_retry_wakeup.clear()
                    await _upload_retry_queue.put((due_at, next(_upload_retry_counter), job_id, generation))
                    requeued_for_earlier_job = True
                except asyncio.TimeoutError:
                    pass

            if requeued_for_earlier_job:
                continue

            job = _upload_retry_jobs.get(job_id)
            if not job or job.completed or job.running or job.retry_generation != generation:
                continue
            await _run_upload_job(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Upload retry worker error for %s: %s", job_id, exc)
        finally:
            _upload_retry_queue.task_done()


def start_upload_retry_worker():
    """Create the in-memory retry queue and return its single worker task."""
    global _upload_retry_queue, _upload_retry_wakeup
    _upload_retry_queue = asyncio.PriorityQueue()
    _upload_retry_wakeup = asyncio.Event()
    return asyncio.create_task(process_upload_retry_queue())


async def stop_upload_retry_tasks():
    """Stop user-triggered immediate retry tasks during bot shutdown."""
    tasks = list(_upload_retry_tasks)
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def retry_upload_job(job_id):
    """Request an immediate retry from the status-message callback."""
    job = _upload_retry_jobs.get(job_id)
    if not job or job.completed:
        return False, "This upload is no longer available."
    if job.running:
        return False, "Upload retry is already in progress."

    # Invalidate a delayed queue entry.  The direct task starts now rather
    # than waiting for the retry worker to wake for its old due time.
    job.retry_generation += 1
    job.auto_retry_expired = False
    # Mark it running before yielding so double-taps cannot start two uploads
    # of the same Telegram part.
    job.running = True
    task = asyncio.create_task(_execute_upload_job(job))
    _upload_retry_tasks.add(task)
    task.add_done_callback(_upload_retry_tasks.discard)
    return True, "Retrying upload now."


def has_active_downloads(request_queue, playlist_queue):
    """Return whether a queued, processing, uploading, or live task exists."""
    request_active = getattr(request_queue, '_unfinished_tasks', 0)
    playlist_active = getattr(playlist_queue, '_unfinished_tasks', 0)
    retry_upload_active = any(not job.completed for job in _upload_retry_jobs.values())
    return bool(request_active or playlist_active or retry_upload_active or active_live_tasks)


async def process_live_stream_tracked(*args):
    """Track a detached live recording for daily maintenance decisions."""
    task_id = args[5]
    active_live_tasks.add(task_id)
    try:
        await process_live_stream(*args)
    finally:
        active_live_tasks.discard(task_id)

def _free_memory():
    """Force garbage collection and release memory back to OS via glibc malloc_trim."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass

def _cleanup_partial_downloads():
    """Remove .part files and orphaned thumbnails from downloads directory."""
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, "*.part")):
        try: os.remove(f)
        except: pass
    for ext in ('*.jpg', '*.webp', '*.jpeg'):
        for f in glob.glob(os.path.join(DOWNLOAD_DIR, ext)):
            base = os.path.splitext(f)[0]
            has_video = any(os.path.exists(base + v) for v in ('.mp4', '.mkv', '.webm', '.m4a', '.mp3'))
            if not has_video:
                try: os.remove(f)
                except: pass

async def handle_upload(application, chat_id, file_path, title, url, audio_only=False, update_status_func=None, channel_name=None, reply_to_message_id=None, thumb_path=None, allow_audio_download=True):
    """Upload media now, or retain it and defer retries after a failure.

    Returns ``True`` only when all parts are delivered and cleanup is complete.
    A ``False`` result means the retry worker owns the retained files.
    """
    try:
        if audio_only:
            files_to_upload = [file_path]
        else:
            if update_status_func:
                await update_status_func("✂️ Checking file size...", force=True)
            loop = asyncio.get_running_loop()
            if not check_ffmpeg():
                files_to_upload = [file_path]
            else:
                files_to_upload = await loop.run_in_executor(None, split_video, file_path)

        job = UploadJob(
            application=application,
            chat_id=chat_id,
            source_file_path=file_path,
            files_to_upload=files_to_upload,
            title=title,
            url=url,
            audio_only=audio_only,
            update_status_func=update_status_func,
            channel_name=channel_name,
            reply_to_message_id=reply_to_message_id,
            thumb_path=thumb_path,
            allow_audio_download=allow_audio_download,
        )
        _upload_retry_jobs[job.job_id] = job
        completed = await _run_upload_job(job)
        return completed
    except Exception as e:
        logger.error(f"Error in handle_upload: {e}")
        error_text = f"🔥 Upload error: {e}"
        if update_status_func:
            await update_status_func(error_text, force=True)
        else:
            await tg_retry(application.bot.send_message, chat_id=chat_id, text=error_text)
        # Never remove media on an upload failure.  If construction failed
        # before a job was registered, this is still safer than data loss.
        return False
    finally:
        _free_memory()

async def process_queue(application, request_queue):
    """Main queue processor for single video downloads."""
    logger.info("Queue processor started.")
    
    while True:
        task = await request_queue.get()
        try:
            status_msg_passed = None
            is_live = False
            channel_name = None
            if len(task) == 7:
                chat_id, url, message_id, max_height, status_msg_passed, channel_name, is_live = task
            elif len(task) == 6:
                chat_id, url, message_id, max_height, status_msg_passed, channel_name = task
            elif len(task) == 5:
                chat_id, url, message_id, max_height, status_msg_passed = task
            elif len(task) == 4:
                chat_id, url, message_id, max_height = task
            else:
                chat_id, url, message_id = task
                max_height = 1080
            
            audio_only = (max_height in (-1, -2))
            audio_format = 'mp3' if max_height == -2 else 'm4a'
            if audio_only:
                max_height = 1080
            
            task_id = f"{chat_id}_{message_id}_{int(time.time())}"
            status_msg = status_msg_passed
            last_edit_time = 0
            
            async def update_status_msg(text, force=False, show_cancel=False, retry_job_id=None):
                nonlocal status_msg, last_edit_time
                now = time.time()
                if not force and (now - last_edit_time < 20):
                    return
                try:
                    keyboard = []
                    if show_cancel:
                        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{task_id}")])
                    if retry_job_id:
                        keyboard.append([
                            InlineKeyboardButton("🔁 Retry upload now", callback_data=f"retryupload:{retry_job_id}")
                        ])
                    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
                    if status_msg:
                        if status_msg.text != text:
                            await tg_retry(status_msg.edit_text, text, reply_markup=reply_markup)
                            last_edit_time = now
                    else:
                        status_msg = await tg_retry(application.bot.send_message,
                            chat_id=chat_id, text=text, reply_to_message_id=message_id, reply_markup=reply_markup
                        )
                        last_edit_time = now
                except Exception as e:
                    logger.warning(f"Failed to update status: {e}")

            # Initial Live Detection (from queue flag)
            if is_live:
                asyncio.create_task(process_live_stream_tracked(application, chat_id, url, message_id, status_msg, task_id, update_status_msg, channel_name))
                continue
                
            await update_status_msg(f"🚀 Processing: {url}", force=True, show_cancel=True)
            
            # Info extraction and secondary Live Detection
            await update_status_msg("📊 Checking video info...", force=True, show_cancel=True)
            video_info = {}
            try:
                loop = asyncio.get_running_loop()
                # 45s timeout for extraction to avoid blocking the queue permanently
                video_info = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: get_video_info(url)),
                    timeout=45
                )
                
                # If info extraction reveals it IS a live stream, handle it
                if video_info.get('is_live'):
                    logger.info(f"URL detected as LIVE during info check: {url}")
                    channel_name = channel_name or video_info.get('uploader') or video_info.get('title', 'Live')
                    asyncio.create_task(process_live_stream_tracked(application, chat_id, url, message_id, status_msg, task_id, update_status_msg, channel_name))
                    continue
            except asyncio.TimeoutError:
                logger.warning(f"Timeout checking info for {url}, proceeding with defaults")
            except Exception as e:
                logger.error(f"Error checking video info: {e}")
            
            # Disk space check
            config = load_config()
            max_disk_gb = config.get('max_disk_gb', 0)
            if max_disk_gb > 0:
                estimated_mb = video_info.get('filesize_mb', 0)
                if estimated_mb > 0:
                    can_download, remaining_gb = check_disk_space(estimated_mb)
                    if not can_download:
                        await update_status_msg(f"❌ Low disk space! Need {estimated_mb/1024:.1f}GB, have {remaining_gb:.1f}GB.", force=True)
                        continue

            if task_id in cancelled_tasks:
                if status_msg:
                    try: await tg_retry(status_msg.delete)
                    except: pass
                cancelled_tasks.discard(task_id)
                continue

            loop = asyncio.get_running_loop()
            def progress_cb(d):
                if task_id in cancelled_tasks: raise Exception("Download cancelled")
                if d['status'] == 'downloading':
                    p = d.get('_percent_str', '0%')
                    eta = d.get('_eta_str', '?')
                    mode = f"🎵 Audio {audio_format.upper()}" if audio_only else f"{max_height}p"
                    asyncio.run_coroutine_threadsafe(update_status_msg(f"⬇️ Downloading ({mode}): {p}\nETA: {eta}", show_cancel=True), loop)

            # Download
            try:
                file_path, title, video_id, thumb_path = await loop.run_in_executor(
                    None, 
                    lambda: download_content(url, progress_cb, audio_only=audio_only, audio_format=audio_format, max_height=max_height, task_id=task_id, cancelled_tasks=cancelled_tasks)
                )
                # Upload using helper
                upload_completed = await handle_upload(
                    application, chat_id, file_path, title, url, audio_only,
                    update_status_msg, channel_name, message_id, thumb_path,
                )
                if not upload_completed:
                    # The retry worker owns the files and the status message.
                    # Do not delete either while it is attempting recovery.
                    continue
            except Exception as e:
                # Cleanup potential partial files on failure
                logger.error(f"Download failed for {url}: {e}")
                _cleanup_partial_downloads()

                await update_status_msg(f"❌ Download failed: {e}", force=True)
                continue
            
            # Delete the progress/status message upon completion
            if status_msg:
                try:
                    await tg_retry(status_msg.delete)
                except Exception as e:
                    logger.warning(f"Failed to delete status message: {e}")

        except Exception as e:
            logger.error(f"Error in process_queue: {e}")
            await update_status_msg(f"🔥 Error: {e}", force=True)
        finally:
            request_queue.task_done()
            _free_memory()

async def _kill_process(process, task_id):
    """Gracefully stop process: SIGINT → SIGTERM → SIGKILL."""
    import signal
    try:
        process.send_signal(signal.SIGINT)
        await asyncio.wait_for(process.wait(), timeout=20)
        logger.info(f"[LIVE:{task_id}] Process stopped gracefully via SIGINT")
    except asyncio.TimeoutError:
        logger.warning(f"[LIVE:{task_id}] SIGINT timeout, sending SIGTERM")
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=15)
            logger.info(f"[LIVE:{task_id}] Process terminated via SIGTERM")
        except asyncio.TimeoutError:
            logger.warning(f"[LIVE:{task_id}] SIGTERM timeout, sending SIGKILL")
            try:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=10)
            except Exception:
                logger.error(f"[LIVE:{task_id}] SIGKILL also failed, process may be orphaned")
    except Exception as e:
        logger.error(f"[LIVE:{task_id}] _kill_process error: {e}", exc_info=True)

async def process_live_stream(application, chat_id, url, message_id, status_msg, task_id, update_status_msg, channel_name):
    """Use the live edge as a temporary backup while archiving from the start.

    streamlink protects the live edge when YouTube has no VOD/DVR available,
    while yt-dlp's independent --live-from-start process captures the archive.
    Once the archive is healthy for ten minutes, stop and discard the duplicate
    live-edge recording.
    """
    SEGMENT_SIZE_BYTES = 1900 * 1024 * 1024  # 1.9GB per segment
    logger.info(f"[LIVE:{task_id}] START url={url}, chat_id={chat_id}, channel={channel_name}")
    fromstart_upload_tasks = []
    fromstart_stable = asyncio.Event()

    def _make_keyboard():
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("⏹ Stop & Upload", callback_data=f"stoplive:{task_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{task_id}"),
        ]])

    async def live_status(text):
        nonlocal status_msg
        try:
            keyboard = _make_keyboard()
            logger.info(f"[LIVE:{task_id}] live_status: '{text}'")
            if status_msg:
                if status_msg.text != text:
                    await tg_retry(status_msg.edit_text, text, reply_markup=keyboard)
            else:
                status_msg = await tg_retry(
                    application.bot.send_message,
                    chat_id=chat_id, text=text, reply_to_message_id=message_id, reply_markup=keyboard
                )
        except Exception as e:
            logger.error(f"[LIVE:{task_id}] live_status failed: {e}", exc_info=True)

    def _build_record_cmd(output_path, proxy=None):
        """streamlink command for live recording — writes continuously to file."""
        cmd = [
            'streamlink',
            '--force',
            '--loglevel', 'warning',
            '--ffmpeg-ffmpeg', get_ffmpeg_command(),
            '-o', output_path,
        ]
        if proxy:
            cmd += ['--http-proxy', proxy]
        cmd += [url, 'best']
        return cmd

    def _build_fromstart_cmd(proxy=None):
        """yt-dlp command for from-start download, outputs to stdout."""
        cmd = [
            'yt-dlp',
            '--no-part',
            '--no-check-certificates',
            '--no-playlist',
            '--hls-use-mpegts',
            '--live-from-start',
            '--ffmpeg-location', get_ffmpeg_command(),
            '--socket-timeout', '30',
            '--retries', '10',
            '--fragment-retries', '10',
            '-o', '-',
        ]
        cookie_file = get_cookie_file()
        if cookie_file:
            cmd += ['--cookies', cookie_file]
        if proxy:
            cmd += ['--proxy', proxy]
        cmd.append(url)
        return cmd

    async def _download_from_start():
        """Archive from the beginning without interrupting the live-edge recorder.

        A missing YouTube DVR/VOD is expected for some streams.  In that case
        this worker exits quietly and leaves the streamlink recording untouched.
        """
        bg_id = f"{task_id}_fromstart"
        logger.info(f"[LIVE:{bg_id}] Auto from-start archive starting (pipe mode)")
        proxy_list = get_proxy_list()

        proc = None
        for proxy in proxy_list:
            cmd = _build_fromstart_cmd(proxy)
            logger.info(f"[LIVE:{bg_id}] cmd: {' '.join(cmd[:8])}...")
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                logger.info(f"[LIVE:{bg_id}] pid={proc.pid}")
                break
            except Exception as e:
                logger.error(f"[LIVE:{bg_id}] Spawn failed: {e}", exc_info=True)
                continue

        if proc is None:
            logger.error(f"[LIVE:{bg_id}] All proxies failed to spawn")
            await live_status(
                "⚠️ From-start archive could not be started.\n"
                "🔴 Continuing to record from the current live position."
            )
            return False

        seg_num = 0
        seg_file = None
        seg_path = None
        seg_bytes = 0
        total_bytes = 0
        start_time = time.time()
        first_data_time = None
        last_data_time = None
        got_data = False
        cancelled = False

        try:
            while True:
                # Check signals
                if task_id in cancelled_tasks:
                    cancelled = True
                    await _kill_process(proc, bg_id)
                    logger.info(f"[LIVE:{bg_id}] Cancelled")
                    return False
                if task_id in stopped_tasks:
                    logger.info(f"[LIVE:{bg_id}] Stop signal received")
                    await _kill_process(proc, bg_id)
                    break

                # Read chunk from pipe (non-blocking with timeout)
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(1024 * 1024), timeout=5)
                except asyncio.TimeoutError:
                    # No data yet — check if process died
                    if proc.returncode is not None:
                        break
                    # VOD unavailable check
                    elapsed = time.time() - start_time
                    if elapsed > 90 and not got_data:
                        logger.warning(f"[LIVE:{bg_id}] No data after {elapsed:.0f}s, VOD likely unavailable")
                        await _kill_process(proc, bg_id)
                        return False
                    if (
                        first_data_time is not None
                        and last_data_time is not None
                        and time.monotonic() - last_data_time > LIVE_FROM_START_MAX_DATA_GAP_SECONDS
                    ):
                        logger.warning(
                            f"[LIVE:{bg_id}] No archive data for over "
                            f"{LIVE_FROM_START_MAX_DATA_GAP_SECONDS}s; resetting stability timer"
                        )
                        first_data_time = None
                    continue

                if not chunk:
                    # EOF — yt-dlp finished
                    break

                got_data = True
                data_time = time.monotonic()
                if first_data_time is None:
                    first_data_time = data_time
                elif (
                    not fromstart_stable.is_set()
                    and data_time - first_data_time >= LIVE_FROM_START_STABILITY_SECONDS
                ):
                    fromstart_stable.set()
                    logger.info(
                        f"[LIVE:{bg_id}] From-start archive stable for "
                        f"{LIVE_FROM_START_STABILITY_SECONDS}s; retiring live-edge backup"
                    )
                last_data_time = data_time
                total_bytes += len(chunk)

                # Open new segment file if needed
                if seg_file is None:
                    seg_num += 1
                    seg_path = os.path.join(DOWNLOAD_DIR, f"live_{bg_id}_seg{seg_num:03d}.ts")
                    seg_file = open(seg_path, 'wb')
                    seg_bytes = 0

                seg_file.write(chunk)
                seg_bytes += len(chunk)

                # Segment full — close, remux+upload, start next
                if seg_bytes >= SEGMENT_SIZE_BYTES:
                    seg_file.close()
                    seg_file = None
                    seg_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{bg_id}_seg{seg_num:03d}.mp4")
                    logger.info(f"[LIVE:{bg_id}] Segment {seg_num} complete: {seg_bytes/(1024*1024):.1f}MB (total: {total_bytes/(1024*1024):.0f}MB)")
                    fromstart_upload_tasks.append(
                        asyncio.create_task(_remux_and_upload_bg(bg_id, seg_path, seg_mp4, seg_num))
                    )

        except Exception as e:
            logger.error(f"[LIVE:{bg_id}] Pipe read error: {e}", exc_info=True)
        finally:
            # Close last segment and upload if it has data
            if seg_file:
                seg_file.close()
                if not cancelled and seg_bytes > 1024:
                    seg_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{bg_id}_seg{seg_num:03d}.mp4")
                    logger.info(f"[LIVE:{bg_id}] Final segment {seg_num}: {seg_bytes/(1024*1024):.1f}MB (total: {total_bytes/(1024*1024):.0f}MB)")
                    fromstart_upload_tasks.append(
                        asyncio.create_task(
                            _remux_and_upload_bg(bg_id, seg_path, seg_mp4, seg_num, is_final=True)
                        )
                    )
                else:
                    try: os.remove(seg_path)
                    except: pass
            elif seg_path and os.path.exists(seg_path) and os.path.getsize(seg_path) > 1024:
                # Edge case: segment was closed by size limit but we need to mark last uploaded as final
                pass

            # Wait for proc to finish if still running
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    await _kill_process(proc, bg_id)

            if proc.returncode not in (None, 0) and not cancelled and not got_data:
                logger.warning(
                    f"[LIVE:{bg_id}] From-start process exited rc={proc.returncode} before producing data"
                )
                await live_status(
                    "⚠️ From-start archive is unavailable (DVR/VOD is not enabled).\n"
                    "🔴 Continuing to record from the current live position."
                )

            if fromstart_upload_tasks:
                if cancelled:
                    for upload_task in fromstart_upload_tasks:
                        upload_task.cancel()
                await asyncio.gather(*fromstart_upload_tasks, return_exceptions=True)

            _cleanup_live_files(bg_id)
            logger.info(f"[LIVE:{bg_id}] Complete. Segments: {seg_num}, Total: {total_bytes/(1024*1024):.1f}MB")

        return got_data

    async def _remux_and_upload_bg(bg_id, ts_path, mp4_path, seg_num, is_final=False):
        """Remux a from-start segment and upload."""
        try:
            ts_size = os.path.getsize(ts_path) if os.path.exists(ts_path) else 0
            if ts_size == 0:
                try: os.remove(ts_path)
                except: pass
                return
            logger.info(f"[LIVE:{bg_id}] Remuxing seg {seg_num}: {ts_size/(1024*1024):.1f}MB")
            remux = await asyncio.create_subprocess_exec(
                get_ffmpeg_command(), '-y',
                '-err_detect', 'ignore_err',
                '-fflags', '+genpts+discardcorrupt',
                '-i', ts_path,
                '-c', 'copy', '-movflags', '+faststart', mp4_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await remux.stderr.read()
            await remux.wait()
            if remux.returncode != 0:
                remux2 = await asyncio.create_subprocess_exec(
                    get_ffmpeg_command(), '-y',
                    '-err_detect', 'ignore_err',
                    '-fflags', '+genpts+discardcorrupt',
                    '-i', ts_path,
                    '-c', 'copy', mp4_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await remux2.wait()
            try: os.remove(ts_path)
            except: pass

            if os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
                title = f"⏪ {channel_name} - From Start Part {seg_num}"
                if is_final:
                    title += " (End)"
                logger.info(f"[LIVE:{bg_id}] Uploading seg {seg_num}: {os.path.getsize(mp4_path)/(1024*1024):.1f}MB")
                await handle_upload(
                    application, chat_id, mp4_path, title, url, False, None,
                    channel_name, message_id, allow_audio_download=False,
                )
                logger.info(f"[LIVE:{bg_id}] Upload done seg {seg_num}")
            else:
                logger.error(f"[LIVE:{bg_id}] Remux produced no output for seg {seg_num}")
        except Exception as e:
            logger.error(f"[LIVE:{bg_id}] Remux/upload seg {seg_num} error: {e}", exc_info=True)

    async def _concat_parts(part_files, output_ts):
        """Concatenate multiple .ts part files using binary concat (TS is designed for this)."""
        if len(part_files) == 1:
            os.rename(part_files[0], output_ts)
            return True
        try:
            with open(output_ts, 'wb') as out:
                for p in part_files:
                    if os.path.exists(p) and os.path.getsize(p) > 0:
                        with open(p, 'rb') as inp:
                            while True:
                                chunk = inp.read(8 * 1024 * 1024)
                                if not chunk:
                                    break
                                out.write(chunk)
            if os.path.exists(output_ts) and os.path.getsize(output_ts) > 0:
                for p in part_files:
                    try: os.remove(p)
                    except: pass
                return True
            else:
                logger.error(f"[LIVE:{task_id}] binary concat produced empty file")
                try: os.remove(output_ts)
                except: pass
                return False
        except Exception as e:
            logger.error(f"[LIVE:{task_id}] binary concat error: {e}")
            try: os.remove(output_ts)
            except: pass
            return False

    async def _remux_and_upload(ts_path, mp4_path, seg_num, is_final=False):
        """Background task: remux .ts to .mp4 and upload."""
        try:
            ts_size = os.path.getsize(ts_path) if os.path.exists(ts_path) else 0
            if ts_size == 0:
                try: os.remove(ts_path)
                except: pass
                return

            logger.info(f"[LIVE:{task_id}] BG remux seg {seg_num}: {ts_size/(1024*1024):.1f}MB")
            remux = await asyncio.create_subprocess_exec(
                get_ffmpeg_command(), '-y',
                '-err_detect', 'ignore_err',
                '-fflags', '+genpts+discardcorrupt',
                '-i', ts_path,
                '-c', 'copy', '-movflags', '+faststart', mp4_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await remux.stderr.read()
            await remux.wait()
            if remux.returncode != 0:
                remux2 = await asyncio.create_subprocess_exec(
                    get_ffmpeg_command(), '-y',
                    '-err_detect', 'ignore_err',
                    '-fflags', '+genpts+discardcorrupt',
                    '-i', ts_path,
                    '-c', 'copy', mp4_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await remux2.stderr.read()
                await remux2.wait()
                if remux2.returncode != 0:
                    logger.error(f"[LIVE:{task_id}] BG remux seg {seg_num} failed completely")
                    try: os.remove(ts_path)
                    except: pass
                    return
            try: os.remove(ts_path)
            except: pass

            upload_size = os.path.getsize(mp4_path) if os.path.exists(mp4_path) else 0
            if upload_size > 0:
                title = f"\U0001f534 {channel_name} - LIVE Part {seg_num}"
                if is_final:
                    title += " (End)"
                logger.info(f"[LIVE:{task_id}] BG uploading seg {seg_num}: {upload_size/(1024*1024):.1f}MB")
                await handle_upload(
                    application, chat_id, mp4_path, title, url, False, None,
                    channel_name, message_id, allow_audio_download=False,
                )
                logger.info(f"[LIVE:{task_id}] BG upload done seg {seg_num}")
            else:
                logger.warning(f"[LIVE:{task_id}] BG remux produced empty file seg {seg_num}")
        except Exception as e:
            logger.error(f"[LIVE:{task_id}] BG remux/upload seg {seg_num} error: {e}", exc_info=True)

    async def _start_recording(part_path, proxy_list):
        """Start a streamlink recording process, trying each proxy.
        Waits a few seconds to verify the process doesn't die immediately.
        Returns (process, proxy) or (None, None)."""
        for proxy in proxy_list:
            cmd = _build_record_cmd(part_path, proxy)
            logger.info(f"[LIVE:{task_id}] streamlink cmd (proxy={proxy}): {' '.join(cmd[:10])}...")
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                logger.info(f"[LIVE:{task_id}] streamlink pid={proc.pid} proxy={proxy}")
            except Exception as e:
                logger.error(f"[LIVE:{task_id}] Spawn failed proxy={proxy}: {e}")
                continue

            # Wait up to 5s to verify process doesn't die immediately
            for _ in range(5):
                await asyncio.sleep(1)
                if proc.returncode is not None:
                    break
                file_size = os.path.getsize(part_path) if os.path.exists(part_path) else 0
                if file_size > 0:
                    break

            if proc.returncode is not None:
                # Process died — read stderr for diagnosis, try next proxy
                stderr_out = b''
                try:
                    stderr_out = await asyncio.wait_for(proc.stderr.read(), timeout=3)
                except Exception:
                    pass
                logger.warning(f"[LIVE:{task_id}] streamlink died immediately with proxy={proxy} rc={proc.returncode}: {stderr_out.decode(errors='replace')[:200]}")
                if os.path.exists(part_path) and os.path.getsize(part_path) == 0:
                    try: os.remove(part_path)
                    except: pass
                continue

            # Process survived — drain stderr pipe in background to prevent deadlock
            asyncio.create_task(_drain_stderr(proc, proxy))
            return proc, proxy

        return None, None

    async def _drain_stderr(proc, proxy):
        """Continuously drain stderr to prevent pipe buffer deadlock."""
        try:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
        except Exception:
            pass

    def _get_total_parts_size(part_files):
        """Get total size of all part files."""
        total = 0
        for p in part_files:
            try:
                total += os.path.getsize(p) if os.path.exists(p) else 0
            except OSError:
                pass
        return total

    try:
        await live_status(
            f"\U0001f534 Starting live recording: {channel_name}\n"
            "⏪ Starting a parallel archive from the beginning..."
        )
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

        proxy_list = get_proxy_list()
        logger.info(f"[LIVE:{task_id}] Proxies: {proxy_list}")
        segment_num = 0
        uploaded_segments = []
        consecutive_failures = 0
        bg_tasks = []
        live_edge_retired = False
        # Start this before streamlink.  It must never replace or interrupt the
        # live-edge recorder until the DVR/VOD stability window has passed.
        fromstart_task = asyncio.create_task(_download_from_start())
        bg_tasks.append(fromstart_task)

        # Parts accumulate until size limit, then get concat'd into a segment
        part_num = 0
        part_files = []

        # Start first part
        part_num = 1
        current_part = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_p{part_num:03d}.ts")
        part_files.append(current_part)
        process, used_proxy = await _start_recording(current_part, proxy_list)

        if process is None:
            await live_status(
                "⚠️ Could not start recording from the current position.\n"
                "⏪ The from-start archive is still running."
            )
            fromstart_result = await asyncio.gather(fromstart_task, return_exceptions=True)
            fromstart_succeeded = fromstart_result[0] is True
            bg_tasks.clear()
            if fromstart_succeeded:
                if status_msg:
                    try:
                        await tg_retry(status_msg.delete)
                    except Exception as e:
                        logger.warning(f"[LIVE:{task_id}] Could not delete status msg: {e}")
            else:
                await live_status("❌ Live recording could not be started.")
            return

        await live_status(
            f"\U0001f534 Recording live stream: {channel_name}\n"
            "⏪ Archiving from the beginning in parallel"
        )
        poll_count = 0

        while True:
            if fromstart_stable.is_set():
                logger.info(
                    f"[LIVE:{task_id}] From-start archive is stable; "
                    "stopping and discarding the duplicate live-edge backup"
                )
                await _kill_process(process, task_id)
                for part_path in part_files:
                    try:
                        os.remove(part_path)
                    except Exception:
                        pass
                part_files = []
                live_edge_retired = True
                await live_status(
                    f"✅ From-start archive is stable: {channel_name}\n"
                    "⏹ Live-edge backup stopped; continuing from the beginning only."
                )
                break

            if process.returncode is not None:
                logger.info(f"[LIVE:{task_id}] streamlink exited rc={process.returncode}")

                if process.returncode != 0:
                    current_size = os.path.getsize(current_part) if os.path.exists(current_part) else 0
                    logger.warning(f"[LIVE:{task_id}] streamlink error rc={process.returncode}, part_size={current_size}")

                    if current_size == 0 and not uploaded_segments and _get_total_parts_size(part_files) == 0:
                        # Nothing recorded at all — might not be live
                        pass

                    if current_size == 0:
                        # Remove empty part from list
                        part_files = [p for p in part_files if p != current_part]
                        try: os.remove(current_part)
                        except: pass

                        consecutive_failures += 1
                        if consecutive_failures >= 3:
                            # Stream probably dead — upload what we have
                            if part_files:
                                segment_num += 1
                                seg_ts = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.ts")
                                seg_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.mp4")
                                if await _concat_parts(part_files, seg_ts):
                                    bg_tasks.append(asyncio.create_task(_remux_and_upload(seg_ts, seg_mp4, segment_num, is_final=True)))
                                    uploaded_segments.append(segment_num)
                                else:
                                    for pi, p in enumerate(part_files, 1):
                                        if os.path.exists(p) and os.path.getsize(p) > 1024:
                                            p_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_fallback{pi:03d}.mp4")
                                            bg_tasks.append(asyncio.create_task(_remux_and_upload(p, p_mp4, pi, is_final=(pi == len(part_files)))))
                                    uploaded_segments.append(segment_num)
                                part_files = []
                            elif not uploaded_segments:
                                await live_status(
                                    "⚠️ Live-edge recording stopped after "
                                    f"{consecutive_failures} attempts.\n"
                                    "⏪ The from-start archive is still running."
                                )
                            logger.error(f"[LIVE:{task_id}] {consecutive_failures} consecutive failures, giving up")
                            break
                        logger.info(f"[LIVE:{task_id}] No data, retry {consecutive_failures}/3 in 5s")
                        await asyncio.sleep(5)
                        # Reuse same part path
                        current_part = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_p{part_num:03d}.ts")
                        part_files.append(current_part)
                        process, used_proxy = await _start_recording(current_part, proxy_list)
                        if process is None:
                            break
                        continue
                    else:
                        consecutive_failures = 0
                        # Had data but crashed — check if we need to segment first
                        total_size = _get_total_parts_size(part_files)
                        if total_size >= SEGMENT_SIZE_BYTES:
                            logger.info(f"[LIVE:{task_id}] Crash recovery: size {total_size/(1024*1024):.1f}MB >= limit, segmenting")
                            segment_num += 1
                            seg_ts = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.ts")
                            seg_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.mp4")
                            if await _concat_parts(part_files, seg_ts):
                                bg_tasks.append(asyncio.create_task(_remux_and_upload(seg_ts, seg_mp4, segment_num)))
                                uploaded_segments.append(segment_num)
                            else:
                                for pi, p in enumerate(part_files, 1):
                                    if os.path.exists(p) and os.path.getsize(p) > 1024:
                                        p_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_fallback{pi:03d}.mp4")
                                        bg_tasks.append(asyncio.create_task(_remux_and_upload(p, p_mp4, pi)))
                                uploaded_segments.append(segment_num)
                            part_files = []
                        # Start new part
                        part_num += 1
                        current_part = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_p{part_num:03d}.ts")
                        part_files.append(current_part)
                        await live_status(f"\U0001f534 Reconnecting: {channel_name}")
                        await asyncio.sleep(3)
                        process, used_proxy = await _start_recording(current_part, proxy_list)
                        if process is None:
                            break
                        await live_status(f"\U0001f534 Recording live stream: {channel_name}")
                        continue
                else:
                    # rc=0: streamlink exited cleanly — check size before restarting
                    consecutive_failures = 0
                    logger.info(f"[LIVE:{task_id}] streamlink exited rc=0, trying to continue...")
                    total_size = _get_total_parts_size(part_files)
                    if total_size >= SEGMENT_SIZE_BYTES:
                        logger.info(f"[LIVE:{task_id}] Clean exit: size {total_size/(1024*1024):.1f}MB >= limit, segmenting")
                        segment_num += 1
                        seg_ts = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.ts")
                        seg_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.mp4")
                        if await _concat_parts(part_files, seg_ts):
                            bg_tasks.append(asyncio.create_task(_remux_and_upload(seg_ts, seg_mp4, segment_num)))
                            uploaded_segments.append(segment_num)
                        else:
                            for pi, p in enumerate(part_files, 1):
                                if os.path.exists(p) and os.path.getsize(p) > 1024:
                                    p_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_fallback{pi:03d}.mp4")
                                    bg_tasks.append(asyncio.create_task(_remux_and_upload(p, p_mp4, pi)))
                            uploaded_segments.append(segment_num)
                        part_files = []
                    await asyncio.sleep(3)
                    part_num += 1
                    current_part = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_p{part_num:03d}.ts")
                    part_files.append(current_part)
                    process, used_proxy = await _start_recording(current_part, proxy_list)
                    if process is None:
                        logger.info(f"[LIVE:{task_id}] Cannot restart, stream ended")
                        break
                    # Wait up to 15s to see if it produces data or exits
                    for _ in range(5):
                        await asyncio.sleep(3)
                        if process.returncode is not None:
                            break
                        check_size = os.path.getsize(current_part) if os.path.exists(current_part) else 0
                        if check_size > 0:
                            break
                    if process.returncode is not None:
                        check_size = os.path.getsize(current_part) if os.path.exists(current_part) else 0
                        if check_size == 0:
                            logger.info(f"[LIVE:{task_id}] Restart produced no data, stream truly ended")
                            part_files = [p for p in part_files if p != current_part]
                            try: os.remove(current_part)
                            except: pass
                            break
                    # Stream still live — continue accumulating
                    logger.info(f"[LIVE:{task_id}] Stream still live, continuing (part {part_num})")
                    total_mb = _get_total_parts_size(part_files) / (1024*1024)
                    await live_status(f"\U0001f534 Recording: {channel_name} ({total_mb:.0f}MB)")
                    poll_count = 0
                    continue

            # Check cancel
            if task_id in cancelled_tasks:
                logger.info(f"[LIVE:{task_id}] Cancel signal")
                await _kill_process(process, task_id)
                # Leave the signal in place so the from-start worker cancels too.
                await update_status_msg("❌ Live recording cancelled.", force=True)
                return

            # Check stop & upload
            if task_id in stopped_tasks:
                logger.info(f"[LIVE:{task_id}] Stop & Upload signal")
                await _kill_process(process, task_id)
                # Leave the signal in place so the from-start worker finalizes too.
                # Filter out empty/tiny parts before concat
                valid_parts = [p for p in part_files if os.path.exists(p) and os.path.getsize(p) > 1024]
                total_size = sum(os.path.getsize(p) for p in valid_parts)
                if total_size > 0 and valid_parts:
                    segment_num += 1
                    seg_ts = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.ts")
                    seg_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.mp4")
                    if await _concat_parts(valid_parts, seg_ts):
                        bg_tasks.append(asyncio.create_task(_remux_and_upload(seg_ts, seg_mp4, segment_num, is_final=True)))
                        uploaded_segments.append(segment_num)
                    elif len(valid_parts) == 1:
                        # Concat not needed for single file — just rename
                        os.rename(valid_parts[0], seg_ts)
                        bg_tasks.append(asyncio.create_task(_remux_and_upload(seg_ts, seg_mp4, segment_num, is_final=True)))
                        uploaded_segments.append(segment_num)
                    else:
                        # Concat failed — try uploading each part individually
                        logger.warning(f"[LIVE:{task_id}] Concat failed, uploading parts individually")
                        for pi, p in enumerate(valid_parts, 1):
                            p_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_part{pi:03d}.mp4")
                            bg_tasks.append(asyncio.create_task(_remux_and_upload(p, p_mp4, pi, is_final=(pi == len(valid_parts)))))
                        uploaded_segments.append(segment_num)
                # Clean up empty parts
                for p in part_files:
                    if p not in valid_parts and os.path.exists(p):
                        try: os.remove(p)
                        except: pass
                part_files = []
                break

            # Check total accumulated size
            total_size = _get_total_parts_size(part_files)

            if total_size >= SEGMENT_SIZE_BYTES:
                logger.info(f"[LIVE:{task_id}] Size limit {total_size/(1024*1024):.1f}MB — segmenting")

                # 1. Start new part BEFORE killing old one (zero gap)
                part_num += 1
                next_part = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_p{part_num:03d}.ts")
                new_process, new_proxy = await _start_recording(next_part, proxy_list)

                # 2. Kill old process
                await _kill_process(process, task_id)

                # 3. Concat current parts into segment, send to background upload
                segment_num += 1
                seg_ts = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.ts")
                seg_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.mp4")
                if await _concat_parts(part_files, seg_ts):
                    bg_tasks.append(asyncio.create_task(_remux_and_upload(seg_ts, seg_mp4, segment_num)))
                    uploaded_segments.append(segment_num)
                else:
                    for pi, p in enumerate(part_files, 1):
                        if os.path.exists(p) and os.path.getsize(p) > 1024:
                            p_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_fallback{pi:03d}.mp4")
                            bg_tasks.append(asyncio.create_task(_remux_and_upload(p, p_mp4, pi)))
                    uploaded_segments.append(segment_num)

                # 4. Reset parts for next segment
                part_files = [next_part]
                current_part = next_part

                if new_process is None:
                    logger.error(f"[LIVE:{task_id}] Failed to start next segment, ending")
                    break
                process = new_process
                used_proxy = new_proxy
                await live_status(f"\U0001f534 Recording: {channel_name} (Part {segment_num + 1})")
                poll_count = 0
                continue

            poll_count += 1
            if poll_count % 10 == 0:
                total_mb = total_size / (1024*1024)
                logger.info(f"[LIVE:{task_id}] Recording total={total_mb:.1f}MB parts={len(part_files)} polls={poll_count}")
                await live_status(f"\U0001f534 Recording: {channel_name} ({total_mb:.0f}MB)")

            await asyncio.sleep(3)

        # Upload remaining parts if any
        if part_files:
            valid_parts = [p for p in part_files if os.path.exists(p) and os.path.getsize(p) > 1024]
            total_size = sum(os.path.getsize(p) for p in valid_parts) if valid_parts else 0
            if total_size > 0 and valid_parts:
                segment_num += 1
                seg_ts = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.ts")
                seg_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.mp4")
                if await _concat_parts(valid_parts, seg_ts):
                    bg_tasks.append(asyncio.create_task(_remux_and_upload(seg_ts, seg_mp4, segment_num, is_final=True)))
                    uploaded_segments.append(segment_num)
                elif len(valid_parts) == 1:
                    os.rename(valid_parts[0], seg_ts)
                    bg_tasks.append(asyncio.create_task(_remux_and_upload(seg_ts, seg_mp4, segment_num, is_final=True)))
                    uploaded_segments.append(segment_num)
                else:
                    logger.warning(f"[LIVE:{task_id}] Final concat failed, uploading parts individually")
                    for pi, p in enumerate(valid_parts, 1):
                        p_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_part{pi:03d}.mp4")
                        bg_tasks.append(asyncio.create_task(_remux_and_upload(p, p_mp4, pi, is_final=(pi == len(valid_parts)))))
                    uploaded_segments.append(segment_num)
            # Clean up empty parts
            for p in part_files:
                if p not in valid_parts and os.path.exists(p):
                    try: os.remove(p)
                    except: pass

        # Wait for all background tasks
        if bg_tasks:
            logger.info(f"[LIVE:{task_id}] Waiting for {len(bg_tasks)} background tasks...")
            if not fromstart_task.done():
                if live_edge_retired:
                    await live_status(
                        f"⏪ Archiving from the beginning: {channel_name}\n"
                        "✅ Live-edge backup was stopped after the 10-minute safety check."
                    )
                else:
                    await update_status_msg(
                        "⬆️ Uploading live segment(s) and completing the from-start archive...",
                        force=True,
                    )
            else:
                await update_status_msg(f"⬆️ Uploading {len(bg_tasks)} segment(s)...", force=True)
            await asyncio.gather(*bg_tasks, return_exceptions=True)
            bg_tasks.clear()

        logger.info(f"[LIVE:{task_id}] COMPLETE. Segments: {len(uploaded_segments)}")
        if status_msg:
            try: await tg_retry(status_msg.delete)
            except Exception as e:
                logger.warning(f"[LIVE:{task_id}] Could not delete status msg: {e}")

    except Exception as e:
        logger.error(f"[LIVE:{task_id}] UNHANDLED EXCEPTION: {e}", exc_info=True)
        try:
            await update_status_msg(f"\U0001f525 Live recording error: {e}", force=True)
        except Exception as e2:
            logger.error(f"[LIVE:{task_id}] Could not send error msg: {e2}", exc_info=True)
    finally:
        # Wait for any pending background tasks before cleanup
        try:
            if bg_tasks:
                logger.info(f"[LIVE:{task_id}] Waiting for {len(bg_tasks)} bg tasks before cleanup...")
                await asyncio.gather(*bg_tasks, return_exceptions=True)
        except NameError:
            pass
        stopped_tasks.discard(task_id)
        cancelled_tasks.discard(task_id)
        _cleanup_live_files(task_id)
        _cleanup_partial_downloads()
        _free_memory()


def _cleanup_live_files(task_id):
    """Remove any leftover live recording segments and temp files.
    When called with main task_id, skips fromstart files (they manage their own cleanup)."""
    is_fromstart = 'fromstart' in task_id
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"live_{task_id}*")):
        if not is_fromstart and 'fromstart' in f:
            continue
        try: os.remove(f)
        except: pass

async def process_playlist_queue(application, playlist_queue):
    """Process playlist download queue SEQUENTIALLY to save space."""
    logger.info("Playlist queue processor started.")
    
    while True:
        task = await playlist_queue.get()
        try:
            status_msg_passed = None
            if len(task) == 5:
                chat_id, url, message_id, max_height, status_msg_passed = task
            else:
                chat_id, url, message_id, max_height = task
            
            task_id = f"pl_{chat_id}_{int(time.time())}"
            status_msg = status_msg_passed
            
            audio_only = (max_height in (-1, -2))
            audio_format = 'mp3' if max_height == -2 else 'm4a'
            
            last_edit_time = 0
            async def update_status_msg(text, force=True, show_cancel=True, send_new=False):
                nonlocal status_msg, last_edit_time
                now = time.time()
                if not force and (now - last_edit_time < 20):
                    return
                try:
                    reply_markup = None
                    if show_cancel:
                        keyboard = [[InlineKeyboardButton("❌ Cancel Playlist", callback_data=f"cancel:{task_id}")]]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        
                    if send_new and status_msg:
                        try:
                            await tg_retry(status_msg.delete)
                        except Exception as e:
                            logger.warning(f"Failed to delete old status msg: {e}")
                        status_msg = None

                    if status_msg:
                        if status_msg.text != text:
                            await tg_retry(status_msg.edit_text, text, reply_markup=reply_markup)
                            last_edit_time = now
                    else:
                        status_msg = await tg_retry(application.bot.send_message, chat_id=chat_id, text=text, reply_to_message_id=message_id, reply_markup=reply_markup)
                        last_edit_time = now
                except Exception as e:
                    logger.warning(f"Failed to update status: {e}")

            await update_status_msg("📋 Getting playlist info...")
            loop = asyncio.get_running_loop()

            try:
                cancel_event = asyncio.Event()

                async def _watch_cancel():
                    while not cancel_event.is_set():
                        if task_id in cancelled_tasks:
                            cancel_event.set()
                            return
                        await asyncio.sleep(1)

                watcher = asyncio.create_task(_watch_cancel())
                info_future = loop.run_in_executor(None, lambda: get_playlist_info(url))

                done, _ = await asyncio.wait(
                    [info_future, watcher],
                    timeout=60,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                cancel_event.set()
                watcher.cancel()

                if task_id in cancelled_tasks:
                    await update_status_msg("❌ Playlist cancelled.")
                    cancelled_tasks.discard(task_id)
                    continue

                if info_future in done:
                    info = info_future.result()
                else:
                    await update_status_msg("❌ Timeout getting playlist info.")
                    continue

                entries = info.get('entries', [])
                playlist_title = info.get('title', 'Playlist')
                total_videos = len(entries)
                
                if total_videos == 0:
                    await update_status_msg("❌ No videos found in playlist.")
                    continue
                
                mode_str = f"Audio {audio_format.upper()}" if audio_only else f"{max_height}p"
                await update_status_msg(f"📋 Playlist: {playlist_title}\n🎬 Found {total_videos} videos.\n🚀 Starting sequential process ({mode_str})...")
                
                for i, entry in enumerate(entries):
                    if task_id in cancelled_tasks:
                        await update_status_msg("❌ Playlist cancelled.")
                        cancelled_tasks.discard(task_id)
                        _cleanup_partial_downloads()
                        break

                    v_url = entry['url']
                    v_title = entry['title']
                    v_id = entry.get('id') or v_url.split('v=')[-1].split('&')[0] if 'v=' in v_url else ''

                    await update_status_msg(f"🔄 Processing {i+1}/{total_videos}: {v_title[:30]}...", send_new=True)

                    def progress_cb(d):
                        if task_id in cancelled_tasks: raise Exception("Cancelled")
                        if d['status'] == 'downloading':
                            p = d.get('_percent_str', '0%')
                            asyncio.run_coroutine_threadsafe(update_status_msg(f"📋 Playlist: {i+1}/{total_videos}\n⬇️ {mode_str}: {p}", force=False), loop)

                    try:
                        file_path, title, video_id, thumb_path = await loop.run_in_executor(
                            None,
                            lambda: download_content(v_url, progress_cb, audio_only=audio_only, audio_format=audio_format, max_height=max_height, task_id=task_id, cancelled_tasks=cancelled_tasks)
                        )
                        await handle_upload(application, chat_id, file_path, title, v_url, audio_only, update_status_func=update_status_msg, reply_to_message_id=message_id, thumb_path=thumb_path)
                    except Exception as e:
                        logger.error(f"Failed for video {i+1}: {e}")
                        await application.bot.send_message(chat_id=chat_id, text=f"⚠️ Skipped {v_title[:30]}: {e}")
                        if v_id:
                            for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"*{v_id}*")):
                                try: os.remove(f)
                                except: pass
                        continue

                
                await update_status_msg(f"✨ Playlist complete! Finished {total_videos} videos.", send_new=True)

            except Exception as e:
                await update_status_msg(f"❌ Failed to get playlist info: {e}")

        except Exception as e:
            logger.error(f"Playlist error: {e}")
            await update_status_msg(f"🔥 Error: {e}")
        finally:
            _cleanup_partial_downloads()
            playlist_queue.task_done()
            _free_memory()
