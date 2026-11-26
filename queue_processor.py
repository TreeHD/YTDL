import os
import gc
import ctypes
import asyncio
import time
import logging
import glob
import itertools
import secrets
import signal
from datetime import datetime, timezone
from urllib.parse import urlsplit
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from live_archive import (
    CHUNK_SECONDS, MAX_DATA_GAP, drain_tail as _drain_process_stderr,
    fetch_replay, probe_media, record_dvr, stop_process,
)
from download_errors import classify_error, error_hint
from config import load_config, check_disk_space, check_ffmpeg, DOWNLOAD_DIR, get_ffmpeg_command, get_proxy_list, get_cookie_file
from downloader import (
    active_live_tasks, download_content, get_video_info, get_playlist_info,
    probe_live_state, restart_warp_proxy, is_twitch_url,
)
from uploader import (
    build_upload_filename,
    upload_video_streaming,
    upload_audio_streaming,
    split_video,
    crop_to_square,
    UploadRetryableError,
    UploadPermanentError,
)
from handlers import cancelled_tasks, stopped_tasks
from telegram_utils import tg_retry, BackgroundStatusPublisher, StatusMessageEditor

logger = logging.getLogger(__name__)

# Live recordings run outside the request queue processor and are tracked in
# downloader.active_live_tasks, so maintenance never starts while they are
# active and ordinary downloads never rotate WARP underneath them.

# The live-edge recorder is only a temporary safety net while the archive from
# the beginning proves it can keep recording.  After this interval, keeping
# both recorders would only create duplicate uploads.
LIVE_FROM_START_STABILITY_SECONDS = 10 * 60

# Remuxing the archive's MKV chunks into MP4 adds a little container overhead.
# Batches sized right at the 1900MiB upload target came out a few MB over it,
# so split_video re-copied the whole 1.9GB file only to produce one identical
# part. Leave headroom so a batch always fits as a single upload.
ARCHIVE_BATCH_BYTES = 1850 * 1024 * 1024

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
    upload_filename = build_upload_filename(
        file_path, job.title,
        part_number=part_index + 1 if len(job.files_to_upload) > 1 else None,
    )

    if job.audio_only:
        if is_local_api:
            await upload_audio_streaming(
                bot_token, api_url, job.chat_id, file_path, job.title, caption,
                reply_to_message_id=job.reply_to_message_id, thumb_path=job.thumb_path,
                max_retries=1,
                upload_filename=upload_filename,
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
                    filename=upload_filename,
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
            upload_filename=upload_filename,
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
                filename=upload_filename,
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


async def process_live_stream_tracked(*args, on_finished=None):
    """Track a detached live recording for maintenance and WARP decisions.

    ``on_finished`` receives how the recording ended: ``confirmed_end``,
    ``stopped``, ``cancelled``, ``proxy_exhausted`` or ``error``. Only the
    first three mean the broadcast must not be recorded again.
    """
    task_id = args[5]
    active_live_tasks.add(task_id)
    outcome = 'error'
    try:
        outcome = await process_live_stream(*args) or 'error'
    finally:
        active_live_tasks.discard(task_id)
        if on_finished:
            try:
                on_finished(outcome)
            except Exception:
                logger.exception("[LIVE:%s] Live completion callback failed", task_id)

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
            expected_live_video_id = None
            on_live_finished = None
            if len(task) == 9:
                (chat_id, url, message_id, max_height, status_msg_passed, channel_name,
                 is_live, expected_live_video_id, on_live_finished) = task
            elif len(task) == 8:
                chat_id, url, message_id, max_height, status_msg_passed, channel_name, is_live, expected_live_video_id = task
            elif len(task) == 7:
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
            status_editor = StatusMessageEditor()
            
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
                        await status_editor.edit(status_msg, text, reply_markup=reply_markup)
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
                asyncio.create_task(process_live_stream_tracked(
                    application, chat_id, url, message_id, status_msg, task_id, update_status_msg,
                    channel_name, expected_live_video_id, on_finished=on_live_finished,
                ))
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
                    asyncio.create_task(process_live_stream_tracked(application, chat_id, url, message_id, status_msg, task_id, update_status_msg, channel_name, video_info.get('id')))
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

async def process_live_stream(application, chat_id, url, message_id, status_msg, task_id, update_status_msg, channel_name, expected_live_video_id=None):
    """Use the live edge as a temporary backup while archiving from the start.

    streamlink protects the live edge when YouTube has no VOD/DVR available,
    while yt-dlp's independent --live-from-start process captures the archive.
    Keep the live-edge recording until the verified archive is delivered.
    """
    SEGMENT_SIZE_BYTES = 1900 * 1024 * 1024  # 1.9GB per segment
    logger.info(f"[LIVE:{task_id}] START url={url}, chat_id={chat_id}, channel={channel_name}")
    fromstart_stable = asyncio.Event()
    last_verified_at = 0.0
    fromstart_recording_done = asyncio.Event()
    archive_delivered = False
    archive_info = None
    reserve_parts = []
    archive_finished_cleanly = False
    archive_duration = 0.0
    archive_height = None
    archive_bits = 0
    dvr_proxy = None
    recovery_gaps = []
    fromstart_failed = asyncio.Event()
    pending_live_upload_retries = set()
    live_failed_paths = set()

    def _make_keyboard(retry_job_id=None):
        rows = [[
            InlineKeyboardButton("⏹ Stop & Upload", callback_data=f"stoplive:{task_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{task_id}"),
        ]]
        if retry_job_id:
            rows.append([InlineKeyboardButton(
                "🔁 Retry upload now", callback_data=f"retryupload:{retry_job_id}"
            )])
        return InlineKeyboardMarkup(rows)

    # Status edits are delivered in the background. Awaiting Telegram here
    # would freeze the recording loop (Stop, Cancel, rollover, archive
    # handoff) for the whole duration of a flood-control wait.
    live_status_publisher = BackgroundStatusPublisher()

    async def live_status(text, force=False, retry_job_id=None):
        nonlocal status_msg
        try:
            if retry_job_id:
                pending_live_upload_retries.add(retry_job_id)
            if pending_live_upload_retries and retry_job_id is None:
                retry_job_id = next(iter(pending_live_upload_retries))
            if text.startswith("🔴 Recording") and pending_live_upload_retries:
                text += "\n⏳ A previous segment is queued for upload retry."
            if text.startswith("🔴 Recording") and fromstart_failed.is_set():
                text += "\n⚠️ From-start archive failed; continuing with live-edge recording."
            keyboard = _make_keyboard(retry_job_id)
            logger.info(f"[LIVE:{task_id}] live_status: '{text}'")
            if status_msg:
                live_status_publisher.publish(status_msg, text, reply_markup=keyboard)
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
        cmd += [record_url, 'best']
        return cmd

    async def _download_from_start():
        nonlocal archive_info, archive_delivered, archive_finished_cleanly, dvr_proxy
        bg_id = f"{task_id}_fromstart"
        # Unbounded on purpose: FFmpeg keeps writing chunks to disk regardless,
        # the queue only holds their paths. A bound would stop verification
        # while a 1.9GB batch uploads, and the stale verification time would
        # then be mistaken for a stalled archive, restarting the live edge.
        segment_queue = asyncio.Queue()
        failed_paths = set()
        verified_count = 0
        first_verified = None
        checkpoint = ()
        replayed_count = 0
        delivered_batches = True

        async def _remux_and_upload_batch(chunk_paths, batch_num, is_final=False):
            concat_path = os.path.join(DOWNLOAD_DIR, f"live_{bg_id}_batch_{batch_num:04d}.ffconcat")
            mp4_path = os.path.join(DOWNLOAD_DIR, f"live_{bg_id}_batch_{batch_num:04d}.mp4")
            try:
                present = [path for path in chunk_paths if os.path.exists(path) and os.path.getsize(path) > 1024]
                if len(present) != len(chunk_paths):
                    raise ValueError('one or more verified source segments are missing')
                with open(concat_path, 'w', encoding='utf-8') as concat_file:
                    concat_file.write("ffconcat version 1.0\n")
                    for path in present:
                        # Generated names contain no quote or newline characters.
                        concat_file.write(f"file '{os.path.abspath(path)}'\n")

                remux = await asyncio.create_subprocess_exec(
                    get_ffmpeg_command(), '-y', '-hide_banner', '-loglevel', 'warning',
                    '-fflags', '+genpts+discardcorrupt',
                    '-f', 'concat', '-safe', '0', '-i', concat_path,
                    '-map', '0', '-c', 'copy', '-movflags', '+faststart', mp4_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                remux_stderr = bytearray()
                remux_stderr_task = asyncio.create_task(
                    _drain_process_stderr(remux.stderr, remux_stderr)
                )
                try:
                    await asyncio.wait_for(remux.wait(), timeout=30 * 60)
                except asyncio.CancelledError:
                    await stop_process(remux)
                    await remux_stderr_task
                    raise
                except asyncio.TimeoutError:
                    logger.error("[LIVE:%s] Remux batch %s timed out", bg_id, batch_num)
                    remux.kill()
                    await remux.wait()
                await remux_stderr_task

                output_size = os.path.getsize(mp4_path) if os.path.exists(mp4_path) else 0
                if remux.returncode != 0 or output_size <= 1024:
                    details = remux_stderr.decode(errors='replace').strip()[-1200:]
                    reason = details or f"ffmpeg exited with code {remux.returncode}"
                    failed_paths.update(present)
                    fromstart_failed.set()
                    fromstart_stable.clear()
                    logger.error("[LIVE:%s] Archive batch %s could not be packaged: %s", bg_id, batch_num, reason)
                    await live_status(
                        f"❌ From-start segment {batch_num} could not be packaged.\n"
                        f"Source chunks are retained in downloads/.\n{reason}"
                    )
                    _safe_remove(mp4_path)
                    return False

                await asyncio.to_thread(probe_media, mp4_path)
                for path in present:
                    _safe_remove(path)
                _safe_remove(concat_path)

                title = f"⏪ {channel_name} - From Start Part {batch_num}"
                if is_final:
                    if task_id in stopped_tasks:
                        title += " (Stopped by user)"
                    elif archive_finished_cleanly:
                        title += " (Archive final segment)"
                    else:
                        title += " (Partial DVR recording)"

                retry_job_ids = set()

                async def _batch_upload_status(text, force=False, retry_job_id=None):
                    if retry_job_id:
                        retry_job_ids.add(retry_job_id)
                        pending_live_upload_retries.add(retry_job_id)
                    elif text.startswith("✅ Upload complete"):
                        pending_live_upload_retries.difference_update(retry_job_ids)
                    current_retry_id = retry_job_id
                    if current_retry_id is None and retry_job_ids and any(
                        retry_id in pending_live_upload_retries for retry_id in retry_job_ids
                    ):
                        current_retry_id = next(iter(retry_job_ids))
                    await live_status(text, retry_job_id=current_retry_id)

                logger.info(
                    "[LIVE:%s] Uploading archive batch %s: %.1fMB",
                    bg_id, batch_num, output_size / (1024 * 1024),
                )
                completed = await handle_upload(
                    application, chat_id, mp4_path, title, url, False,
                    _batch_upload_status, channel_name, message_id,
                    allow_audio_download=False,
                )
                if completed:
                    pending_live_upload_retries.difference_update(retry_job_ids)
                else:
                    if os.path.exists(mp4_path):
                        failed_paths.add(mp4_path)
                    for retry_id in retry_job_ids:
                        job = _upload_retry_jobs.get(retry_id)
                        if job:
                            failed_paths.update(job.files_to_upload)
                            failed_paths.add(job.source_file_path)
                logger.info("[LIVE:%s] Archive batch %s upload returned completed=%s", bg_id, batch_num, completed)
                return completed
            except Exception as exc:
                failed_paths.update(path for path in chunk_paths if os.path.exists(path))
                fromstart_failed.set()
                fromstart_stable.clear()
                logger.error("[LIVE:%s] Remux/upload batch %s failed: %s", bg_id, batch_num, exc, exc_info=True)
                await live_status(
                    f"❌ From-start segment {batch_num} failed: {exc}\n"
                    "Source chunks are retained in downloads/."
                )
                return False
            finally:
                _safe_remove(concat_path)

        async def _consume_archive_chunks():
            nonlocal delivered_batches
            batch_paths = []
            batch_bytes = 0
            batch_num = 0
            while True:
                chunk_path = await segment_queue.get()
                if chunk_path is None:
                    break
                try:
                    chunk_size = os.path.getsize(chunk_path)
                except OSError:
                    chunk_size = 0
                if chunk_size <= 1024:
                    delivered_batches = False
                    fromstart_failed.set()
                    fromstart_stable.clear()
                    continue
                if batch_paths and batch_bytes + chunk_size > ARCHIVE_BATCH_BYTES:
                    batch_num += 1
                    if not await _remux_and_upload_batch(batch_paths, batch_num):
                        delivered_batches = False
                    batch_paths = []
                    batch_bytes = 0
                batch_paths.append(chunk_path)
                batch_bytes += chunk_size

            if batch_paths and task_id not in cancelled_tasks:
                batch_num += 1
                if not await _remux_and_upload_batch(batch_paths, batch_num, is_final=True):
                    delivered_batches = False

        async def accept_chunk(path):
            nonlocal verified_count, first_verified, archive_info, archive_duration, archive_height
            nonlocal replayed_count, checkpoint, last_verified_at, archive_bits
            replayed_count += 1
            if replayed_count < verified_count:
                _safe_remove(path)
                return
            info = await asyncio.to_thread(probe_media, path, chunk=True)
            if replayed_count == verified_count:
                # A new exit IP can expose a different DVR window or rendition.
                # Rejoin only at the same ordinal and matching compressed A/V packets.
                if not checkpoint or info.boundary != checkpoint:
                    raise ValueError('DVR timelines differ at the last verified segment')
                _safe_remove(path)
                return
            last_verified_at = time.monotonic()
            archive_info = info
            checkpoint = info.boundary
            archive_duration += info.duration
            archive_bits += info.bitrate * info.duration
            archive_height = min(archive_height or info.height, info.height)
            verified_count += 1
            if first_verified is None:
                first_verified = time.monotonic()
                await live_status(f"⏪ DVR verified: {info.describe()}")
            await segment_queue.put(path)
            if time.monotonic() - first_verified >= LIVE_FROM_START_STABILITY_SECONDS:
                fromstart_stable.set()

        def stopping():
            return task_id in stopped_tasks or task_id in cancelled_tasks or fromstart_failed.is_set()

        consumer = asyncio.create_task(_consume_archive_chunks())
        try:
            for attempt, proxy in enumerate(get_proxy_list(), 1):
                if stopping():
                    break
                prefix = os.path.join(DOWNLOAD_DIR, f"live_{bg_id}_{attempt}")
                dvr_proxy = proxy
                replayed_count = 0
                first_verified = None
                try:
                    await record_dvr(record_url, proxy, get_cookie_file(), prefix, accept_chunk,
                                     stopping, lambda: task_id in cancelled_tasks)
                    if replayed_count < verified_count and not stopping():
                        raise ValueError('DVR ended before reaching the previous recording')
                    archive_finished_cleanly = (first_verified is not None and not fromstart_failed.is_set()
                                               and task_id not in cancelled_tasks)
                    break
                except (ValueError, OSError) as exc:
                    fromstart_stable.clear()
                    logger.warning("[LIVE:%s] DVR attempt %d: %s", bg_id, attempt, exc)
                    if stopping():
                        break
                    await live_status(f"⚠️ DVR attempt {attempt}: {exc}\nUsing live-edge recording during recovery.")
            else:
                if verified_count:
                    fromstart_failed.set()
                await live_status("⏩ No verified DVR connection available. Continuing at the live edge.")
        finally:
            dvr_proxy = None
            fromstart_recording_done.set()
            fromstart_stable.clear()
            if task_id in cancelled_tasks:
                consumer.cancel()
            else:
                await segment_queue.put(None)
            await asyncio.gather(consumer, return_exceptions=task_id in cancelled_tasks)
            archive_delivered = verified_count > 0 and delivered_batches and not fromstart_failed.is_set()
            _cleanup_live_files(bg_id, preserve_paths=failed_paths)

    def _segment_parts(part_files):
        """Closed recorder parts that hold media, in recording order."""
        return [p for p in part_files if os.path.exists(p) and os.path.getsize(p) > 1024]

    async def _remux_and_upload(ts_parts, mp4_path, seg_num, is_final=False, completion_reason='confirmed_end'):
        """Background task: remux one segment's .ts parts to .mp4 and upload.

        MPEG-TS parts are read back to back through FFmpeg's concat protocol,
        the same byte stream a binary concatenation would produce, without
        first copying ~1.9GB on the event loop.
        """
        ts_parts = list(ts_parts)
        try:
            ts_size = sum(os.path.getsize(p) for p in ts_parts if os.path.exists(p))
            if ts_size == 0:
                for p in ts_parts:
                    _safe_remove(p)
                return
            source = ts_parts[0] if len(ts_parts) == 1 else 'concat:' + '|'.join(ts_parts)

            logger.info(f"[LIVE:{task_id}] BG remux seg {seg_num}: {ts_size/(1024*1024):.1f}MB from {len(ts_parts)} part(s)")
            remux_stderr_tail = bytearray()

            async def _attempt_remux(include_faststart):
                args = [
                    get_ffmpeg_command(), '-y',
                    '-err_detect', 'ignore_err',
                    '-fflags', '+genpts+discardcorrupt',
                    '-i', source, '-c', 'copy',
                ]
                if include_faststart:
                    args += ['-movflags', '+faststart']
                args.append(mp4_path)
                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                stderr_task = asyncio.create_task(
                    _drain_process_stderr(process.stderr, remux_stderr_tail)
                )
                await process.wait()
                await stderr_task
                return process.returncode

            remux_code = await _attempt_remux(True)
            if remux_code != 0:
                _safe_remove(mp4_path)
                remux_code = await _attempt_remux(False)

            output_size = os.path.getsize(mp4_path) if os.path.exists(mp4_path) else 0
            if remux_code != 0 or output_size <= 1024:
                live_failed_paths.update(ts_parts)
                reason = remux_stderr_tail.decode(errors='replace').strip()[-1000:]
                logger.error("[LIVE:%s] Remux produced no valid output for seg %s: %s", task_id, seg_num, reason)
                await live_status(
                    f"❌ Live segment {seg_num} could not be packaged.\n"
                    "Source recording is retained in downloads/.\n"
                    f"{reason or f'ffmpeg exited with code {remux_code}'}"
                )
                _safe_remove(mp4_path)
                return
            if supports_from_start:
                try:
                    await asyncio.to_thread(probe_media, mp4_path)
                except ValueError as exc:
                    live_failed_paths.update(ts_parts)
                    _safe_remove(mp4_path)
                    await live_status(f"⚠️ Live segment {seg_num}: {exc}. Source retained in downloads/.")
                    return
            for p in ts_parts:
                _safe_remove(p)

            upload_size = os.path.getsize(mp4_path) if os.path.exists(mp4_path) else 0
            if upload_size > 0:
                title = f"\U0001f534 {channel_name} - LIVE Part {seg_num}"
                if is_final:
                    title += {
                        'stopped': ' (Stopped by user)',
                        'proxy_exhausted': ' (Proxy interrupted)',
                    }.get(completion_reason, ' (End)')
                logger.info(f"[LIVE:{task_id}] BG uploading seg {seg_num}: {upload_size/(1024*1024):.1f}MB")
                retry_job_ids = set()

                async def _upload_status(text, force=False, retry_job_id=None):
                    del force
                    if retry_job_id:
                        retry_job_ids.add(retry_job_id)
                        pending_live_upload_retries.add(retry_job_id)
                    elif text.startswith("✅ Upload complete"):
                        pending_live_upload_retries.difference_update(retry_job_ids)
                    await live_status(text, retry_job_id=retry_job_id)

                completed = await handle_upload(
                    application, chat_id, mp4_path, title, url, False, _upload_status,
                    channel_name, message_id, allow_audio_download=False,
                )
                if not completed:
                    live_failed_paths.add(mp4_path)
                    for retry_id in retry_job_ids:
                        job = _upload_retry_jobs.get(retry_id)
                        if job:
                            live_failed_paths.update(job.files_to_upload)
                            live_failed_paths.add(job.source_file_path)
                logger.info(f"[LIVE:{task_id}] BG upload returned completed={completed} for seg {seg_num}")
            else:
                logger.warning(f"[LIVE:{task_id}] BG remux produced empty file seg {seg_num}")
        except Exception as e:
            live_failed_paths.update(p for p in (*ts_parts, mp4_path) if os.path.exists(p))
            logger.error(f"[LIVE:{task_id}] BG remux/upload seg {seg_num} error: {e}", exc_info=True)

    async def _start_recording(part_path, proxy_list):
        """Start a streamlink recording process, trying each proxy.
        Requires media data before handing off from a working recorder.
        Returns (process, proxy) or (None, None)."""
        for proxy in proxy_list:
            cmd = _build_record_cmd(part_path, proxy)
            route = "configured proxy" if proxy else "direct"
            logger.info("[LIVE:%s] Starting Streamlink via %s", task_id, route)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                logger.info(f"[LIVE:{task_id}] streamlink pid={proc.pid} route={route}")
            except Exception as e:
                logger.error(f"[LIVE:{task_id}] Spawn failed route={route}: {e}")
                continue

            # A running process can still be waiting for a playlist or proxy.
            # Never retire a working recorder until the replacement has data.
            file_size = 0
            for _ in range(30):
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
                logger.warning(f"[LIVE:{task_id}] streamlink died immediately via {route} rc={proc.returncode}: {error_hint(stderr_out.decode(errors='replace')) or 'source unavailable'}")
                if os.path.exists(part_path) and os.path.getsize(part_path) == 0:
                    try: os.remove(part_path)
                    except: pass
                continue

            if file_size == 0:
                logger.warning(f"[LIVE:{task_id}] streamlink produced no data via {route}; trying next proxy")
                asyncio.create_task(_drain_stderr(proc, proxy))
                await _kill_process(proc, task_id)
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

    def _proxy_round(proxies, previous_proxy):
        """Try a different configured proxy first on every recovery round."""
        if not proxies or previous_proxy not in proxies:
            return list(proxies)
        start = (proxies.index(previous_proxy) + 1) % len(proxies)
        return list(proxies[start:]) + list(proxies[:start])

    async def _probe_state():
        """Run synchronous yt-dlp probing without blocking the bot reactor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: probe_live_state(record_url, expected_live_video_id)
        )

    async def _recover_recording(part_path, previous_proxy, note_gap=False):
        """Recover recording, returning a verified process or a terminal reason.

        A recorder exit is never considered an end-of-stream signal by itself.
        Only two successful metadata probes may return ``confirmed_end``.
        """
        config = load_config()
        window = max(1, config['live_proxy_recovery_window_seconds'])
        initial_delay = max(1, config['live_proxy_retry_initial_seconds'])
        max_delay = max(initial_delay, config['live_proxy_retry_max_seconds'])
        warp_cooldown = max(1, config['live_warp_restart_cooldown_seconds'])
        end_required = max(2, config['live_end_confirmations'])
        end_interval = max(1, config['live_end_confirmation_interval_seconds'])
        started = time.monotonic()
        lost_at = datetime.now(timezone.utc)
        delay = initial_delay
        end_confirmations = 0
        last_warp_restart = float('-inf')
        last_status_at = 0.0

        while True:
            if task_id in cancelled_tasks:
                return None, None, 'cancelled'
            if task_id in stopped_tasks:
                return None, None, 'stopped'

            round_proxies = _proxy_round(get_proxy_list(), previous_proxy)
            process, used_proxy = await _start_recording(part_path, round_proxies)
            if process is not None:
                if note_gap:
                    until = datetime.now(timezone.utc)
                    recovery_gaps.append(f"{lost_at:%Y-%m-%d %H:%M:%S} – {until:%H:%M:%S} UTC")
                return process, used_proxy, 'recovered'

            probe = await _probe_state()
            if probe.state == 'ENDED':
                end_confirmations += 1
                logger.info(
                    "[LIVE:%s] End probe %s/%s confirmed for expected video",
                    task_id, end_confirmations, end_required,
                )
                if end_confirmations >= end_required:
                    return None, None, 'confirmed_end'
                await live_status(
                    f"⏳ Verifying whether the live stream ended: {channel_name}\n"
                    f"Confirmation {end_confirmations}/{end_required}..."
                )
                await asyncio.sleep(end_interval)
                continue

            # A successful LIVE probe is useful diagnostic evidence, but it
            # does not reset the bounded outage timer: streamlink may still be
            # unable to establish its media connection through the proxy.
            end_confirmations = 0
            elapsed = time.monotonic() - started
            if elapsed >= window:
                logger.error("[LIVE:%s] Recovery window exhausted after %.0fs", task_id, elapsed)
                return None, None, 'proxy_exhausted'

            auth_problem = any(classify_error(error) in ('cookie_expired', 'cookie_format', 'auth', 'blocked')
                               for error in probe.errors)
            if (
                not auth_problem and not active_live_tasks.difference({task_id})
                and not (dvr_proxy and 'warp-proxy' in dvr_proxy)
                and any(proxy and 'warp-proxy' in proxy for proxy in round_proxies)
                and time.monotonic() - last_warp_restart >= warp_cooldown
            ):
                logger.warning("[LIVE:%s] All recorder proxies failed; restarting WARP", task_id)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, restart_warp_proxy)
                last_warp_restart = time.monotonic()

            now = time.monotonic()
            if now - last_status_at >= 30:
                remaining = max(0, int(window - elapsed))
                await live_status(
                    f"⚠️ Reconnecting live recording: {channel_name}\n"
                    f"Proxy/source unavailable; retrying for up to {remaining}s.\n"
                    + next((error_hint(error) for error in probe.errors if error_hint(error)), "")
                )
                last_status_at = now
            await asyncio.sleep(delay)
            delay = min(max_delay, delay * 2)

    try:
        supports_from_start = not is_twitch_url(url)
        host = urlsplit(url).hostname or ''
        is_youtube = host in ('youtu.be', 'youtube.com') or host.endswith('.youtube.com')
        record_url = (f"https://www.youtube.com/watch?v={expected_live_video_id}"
                      if expected_live_video_id and is_youtube else url)
        await live_status(
            f"\U0001f534 Starting live recording: {channel_name}\n"
            + ("⏪ Starting a parallel archive from the beginning..." if supports_from_start else "")
        )
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

        proxy_list = get_proxy_list()
        logger.info("[LIVE:%s] %d connection route(s)", task_id, len(proxy_list))
        segment_num = 0
        uploaded_segments = []
        bg_tasks = []
        live_edge_retired = False
        # YouTube may provide a DVR archive. Twitch URLs use only the live-edge
        # recorder because yt-dlp's YouTube archive path does not apply there.
        fromstart_task = asyncio.create_task(_download_from_start()) if supports_from_start else asyncio.create_task(asyncio.sleep(0))
        bg_tasks.append(fromstart_task)

        # Parts accumulate until size limit, then get concat'd into a segment
        part_num = 0
        part_files = []

        # Start first part through the same recovery path used after an outage.
        # This prevents a proxy flap at startup from being reported as a stream
        # that never existed.
        part_num = 1
        current_part = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_p{part_num:03d}.ts")
        part_files.append(current_part)
        process, used_proxy, termination_reason = await _recover_recording(current_part, None)
        if process is None:
            if termination_reason == 'cancelled':
                await live_status("❌ Live recording cancelled.")
                return 'cancelled'
            if termination_reason == 'proxy_exhausted':
                await live_status("⚠️ Proxy/source recovery timed out before recording could start.\nLive status was not confirmed.")
            elif termination_reason == 'confirmed_end':
                await live_status("✅ Live stream end was confirmed before recording could start.")
            if supports_from_start and not fromstart_recording_done.is_set():
                await live_status("⏪ Live-edge unavailable. Finishing the independent DVR recording.")
        else:
            await live_status(
                f"\U0001f534 Recording live stream: {channel_name}\n"
                + ("⏪ Archiving from the beginning in parallel" if supports_from_start else "")
            )
        poll_count = 0

        while process is not None or live_edge_retired:
            # The archive may be primary while its live-edge process has been
            # intentionally stopped. Manual actions still need to be handled.
            if process is None and task_id in cancelled_tasks:
                await live_status("❌ Live recording cancelled.")
                return 'cancelled'
            if process is None and task_id in stopped_tasks:
                termination_reason = 'stopped'
                break

            if fromstart_stable.is_set() and time.monotonic() - last_verified_at > CHUNK_SECONDS + MAX_DATA_GAP:
                fromstart_stable.clear()

            if (fromstart_stable.is_set() and not fromstart_failed.is_set()
                    and not fromstart_recording_done.is_set()
                    and not fromstart_task.done() and not live_edge_retired):
                logger.info(
                    f"[LIVE:{task_id}] From-start archive is stable; "
                    "retaining the recorded live-edge backup"
                )
                await _kill_process(process, task_id)
                reserve_parts.extend(part_files)
                part_files = []
                live_edge_retired = True
                await live_status(
                    f"✅ From-start archive is stable: {channel_name}\n"
                    f"⏹ Live-edge backup stopped and retained. {archive_info.describe()}"
                )
                process = None
                continue

            # The archived recorder may itself lose its proxy after it became
            # primary. Fall back to the live edge and make a fresh, verified
            # connection instead of treating that worker's EOF as stream end.
            if live_edge_retired:
                if (fromstart_stable.is_set() and not fromstart_failed.is_set()
                        and not fromstart_recording_done.is_set()
                        and not fromstart_task.done()):
                    await asyncio.sleep(3)
                    continue
                logger.warning(f"[LIVE:{task_id}] Archive stopped or stalled; restoring live-edge recording without waiting for uploads")
                fromstart_stable.clear()
                live_edge_retired = False
                part_num += 1
                current_part = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_p{part_num:03d}.ts")
                part_files.append(current_part)
                process, used_proxy, termination_reason = await _recover_recording(current_part, used_proxy, note_gap=supports_from_start)
                if process is None:
                    if termination_reason == 'cancelled':
                        await live_status("❌ Live recording cancelled.")
                        return 'cancelled'
                    break
                await live_status(f"🔴 Recording live stream: {channel_name}")
                continue

            if process.returncode is not None:
                logger.warning(f"[LIVE:{task_id}] streamlink exited rc={process.returncode}; entering recovery")
                if not os.path.exists(current_part) or os.path.getsize(current_part) == 0:
                    part_files = [p for p in part_files if p != current_part]
                    try: os.remove(current_part)
                    except: pass
                part_num += 1
                current_part = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_p{part_num:03d}.ts")
                part_files.append(current_part)
                process, used_proxy, termination_reason = await _recover_recording(current_part, used_proxy, note_gap=supports_from_start)
                if process is None:
                    part_files = [p for p in part_files if p != current_part]
                    try: os.remove(current_part)
                    except: pass
                    if termination_reason == 'cancelled':
                        await live_status("❌ Live recording cancelled.")
                        return 'cancelled'
                    break
                await live_status(f"🔴 Recording live stream: {channel_name}")
                poll_count = 0
                continue

            # Check cancel
            if task_id in cancelled_tasks:
                logger.info(f"[LIVE:{task_id}] Cancel signal")
                await _kill_process(process, task_id)
                # Leave the signal in place so the from-start worker cancels too.
                await live_status("❌ Live recording cancelled.")
                return 'cancelled'

            # Check stop & upload
            if task_id in stopped_tasks:
                termination_reason = 'stopped'
                logger.info(f"[LIVE:{task_id}] Stop & Upload signal")
                await _kill_process(process, task_id)
                # Leave the signal in place so the from-start worker finalizes too.
                # Empty/tiny parts carry no media and are dropped.
                valid_parts = _segment_parts(part_files)
                if valid_parts:
                    segment_num += 1
                    seg_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.mp4")
                    bg_tasks.append(asyncio.create_task(_remux_and_upload(valid_parts, seg_mp4, segment_num, is_final=True, completion_reason='stopped')))
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
                new_process, new_proxy = await _start_recording(next_part, _proxy_round(proxy_list, used_proxy))

                if new_process is None:
                    # Keep the working recorder alive. A failed handoff is a
                    # proxy problem, not a reason to close the live recording.
                    logger.warning(f"[LIVE:{task_id}] Could not start rollover recorder; retaining current recorder")
                    part_num -= 1
                    try: os.remove(next_part)
                    except: pass
                    await live_status(f"⚠️ Segment handoff delayed; still recording: {channel_name}")
                    await asyncio.sleep(3)
                    continue

                # 2. Kill old process
                await _kill_process(process, task_id)

                # 3. Hand the closed parts to the background remux/upload
                segment_num += 1
                seg_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.mp4")
                segment_parts = _segment_parts(part_files)
                for p in part_files:
                    if p not in segment_parts:
                        _safe_remove(p)
                bg_tasks.append(asyncio.create_task(_remux_and_upload(segment_parts, seg_mp4, segment_num)))
                uploaded_segments.append(segment_num)

                # 4. Reset parts for next segment
                part_files = [next_part]
                current_part = next_part

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

        # Finish DVR batches before deciding whether a replay should replace the remaining edge parts.
        if bg_tasks:
            logger.info(f"[LIVE:{task_id}] Waiting for {len(bg_tasks)} background tasks...")
            if not fromstart_task.done():
                if live_edge_retired:
                    await live_status(
                        f"⏪ Archiving from the beginning: {channel_name}\n"
                        "✅ Live-edge backup was stopped after the 10-minute safety check."
                    )
                else:
                    await live_status("⬆️ Uploading live segment(s) and completing the from-start archive...")
            else:
                await live_status(f"⬆️ Uploading {len(bg_tasks)} segment(s)...")
            await asyncio.gather(*bg_tasks, return_exceptions=True)
            bg_tasks.clear()

        if task_id in cancelled_tasks:
            return 'cancelled'
        replay_delivered = False
        if is_youtube and termination_reason == 'confirmed_end' and expected_live_video_id:
            replay_path = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_replay.mp4")
            await live_status("⏪ Checking for a complete YouTube replay...")
            try:
                info = await fetch_replay(
                    expected_live_video_id, get_proxy_list(), get_cookie_file(), replay_path,
                    lambda: task_id in cancelled_tasks or task_id in stopped_tasks,
                    archive_duration if archive_delivered and archive_finished_cleanly else 0,
                    archive_height, archive_bits / archive_duration if archive_duration else 0,
                )
            except OSError as exc:
                logger.warning("[LIVE:%s] Could not prepare replay: %s", task_id, exc)
                info = None
            if task_id in cancelled_tasks:
                return 'cancelled'
            if info:
                await live_status(f"✅ Replay verified: {info.describe()}")
                replay_delivered = await handle_upload(
                    application, chat_id, replay_path, f"{channel_name} - Full replay", url, False,
                    live_status, channel_name, message_id, allow_audio_download=False,
                )
                if not replay_delivered:
                    live_failed_paths.add(replay_path)
                    for retry_id in pending_live_upload_retries:
                        job = _upload_retry_jobs.get(retry_id)
                        if job:
                            live_failed_paths.update(job.files_to_upload)
            else:
                await live_status("Keeping recorded segments. No replacement replay was selected.")

        # Upload remaining parts if any
        if part_files and not replay_delivered:
            valid_parts = _segment_parts(part_files)
            if valid_parts:
                segment_num += 1
                seg_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.mp4")
                bg_tasks.append(asyncio.create_task(_remux_and_upload(valid_parts, seg_mp4, segment_num, is_final=True, completion_reason=termination_reason)))
                uploaded_segments.append(segment_num)
            # Clean up empty parts
            for p in part_files:
                if p not in valid_parts and os.path.exists(p):
                    try: os.remove(p)
                    except: pass

        if bg_tasks:
            await asyncio.gather(*bg_tasks)
            bg_tasks.clear()

        archive_accepted = archive_delivered and archive_finished_cleanly
        if not replay_delivered and not archive_accepted:
            for backup in reserve_parts:
                if os.path.isfile(backup) and os.path.getsize(backup) > 1024:
                    segment_num += 1
                    await _remux_and_upload([backup], backup + '.mp4', segment_num)
        logger.info(f"[LIVE:{task_id}] COMPLETE. Segments: {len(uploaded_segments)}, reason={termination_reason}")
        if task_id in cancelled_tasks:
            return 'cancelled'
        gap_note = ""
        if recovery_gaps and not (archive_accepted or replay_delivered):
            gaps = "\n".join(recovery_gaps[-10:])
            gap_note = f"\nPossible live-edge gaps during reconnect:\n{gaps}"
        if termination_reason == 'proxy_exhausted':
            recovery_minutes = max(1, (load_config()['live_proxy_recovery_window_seconds'] + 59) // 60)
            await live_status(
                f"⚠️ Proxy/source recovery timed out after {recovery_minutes} minutes.\n"
                "Recorded segment(s) were uploaded; the live stream end was not confirmed." + gap_note
            )
        elif pending_live_upload_retries or live_failed_paths or (fromstart_failed.is_set() and not replay_delivered):
            await live_status(
                "⚠️ Live recording ended, but one or more segments need attention.\n"
                "Pending uploads will retry automatically; failed recordings are retained in downloads/." + gap_note
            )
        elif gap_note:
            await live_status("⚠️ Live-edge recording finished." + gap_note)
        elif status_msg:
            # A queued edit must not land after (or race) the deletion.
            await live_status_publisher.flush()
            try: await tg_retry(status_msg.delete)
            except Exception as e:
                logger.warning(f"[LIVE:{task_id}] Could not delete status msg: {e}")
        return termination_reason

    except Exception as e:
        live_failed_paths.update(reserve_parts)
        live_failed_paths.update(glob.glob(os.path.join(DOWNLOAD_DIR, f"live_{task_id}_p*.ts")))
        logger.error(f"[LIVE:{task_id}] UNHANDLED EXCEPTION: {e}", exc_info=True)
        try:
            await live_status(f"\U0001f525 Live recording error: {e}")
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
        # The task is finished only once its final status has reached Telegram.
        await live_status_publisher.flush()
        stopped_tasks.discard(task_id)
        cancelled_tasks.discard(task_id)
        _cleanup_live_files(task_id, preserve_paths=live_failed_paths)
        _cleanup_partial_downloads()
        _free_memory()


def _cleanup_live_files(task_id, preserve_paths=None):
    """Remove any leftover live recording segments and temp files.
    When called with main task_id, skips fromstart files (they manage their own cleanup)."""
    is_fromstart = 'fromstart' in task_id
    preserve_paths = set(preserve_paths or ())
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"live_{task_id}*")):
        if not is_fromstart and 'fromstart' in f:
            continue
        if f in preserve_paths:
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
            status_editor = StatusMessageEditor()
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
                        await status_editor.edit(status_msg, text, reply_markup=reply_markup)
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
