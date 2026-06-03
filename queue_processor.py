import os
import gc
import asyncio
import time
import logging
import glob

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TelegramError

from config import load_config, check_disk_space, check_ffmpeg, DOWNLOAD_DIR, get_ffmpeg_command, get_proxy_list, get_cookie_file
from downloader import download_content, get_video_info, get_playlist_info
from uploader import upload_video_streaming, upload_audio_streaming, split_video, crop_to_square
from handlers import cancelled_tasks, stopped_tasks

logger = logging.getLogger(__name__)

async def tg_retry(func, *args, **kwargs):
    """Retry Telegram API calls up to 10 times on RateLimit."""
    max_retries = 10
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except RetryAfter as e:
            wait_time = e.retry_after
            logger.warning(f"Flood control: Waiting {wait_time}s (Attempt {attempt+1}/10)")
            await asyncio.sleep(wait_time)
        except TelegramError as e:
            if "Flood control" in str(e):
                logger.warning(f"Flood caught via error msg: {e} (Attempt {attempt+1}/10)")
                await asyncio.sleep(5)
                continue
            raise e
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            logger.warning(f"Unexpected error in tg_retry: {e}. Retrying...")
            await asyncio.sleep(2)
    raise Exception("Max retries exceeded for Telegram API call")

async def handle_upload(application, chat_id, file_path, title, url, audio_only=False, update_status_func=None, channel_name=None, reply_to_message_id=None, thumb_path=None):
    """Helper to handle video/audio upload with splitting and cleanup."""
    try:
        if audio_only:
            if update_status_func:
                await update_status_func("⬆️ Uploading audio...", force=True)
            
            config = load_config()
            api_url = config.get('api_url', '')
            bot_token = config.get('bot_token', '')
            is_local_api = api_url and 'api.telegram.org' not in api_url
            
            if channel_name:
                full_caption = f"{channel_name}\n{title}\n{url}"
            else:
                full_caption = f"{title}\n{url}"
            if is_local_api:
                await upload_audio_streaming(bot_token, api_url, chat_id, file_path, title, full_caption, reply_to_message_id=reply_to_message_id, thumb_path=thumb_path)
            else:
                with open(file_path, 'rb') as f:
                    if thumb_path and os.path.exists(thumb_path):
                        thumb_path = crop_to_square(thumb_path)
                        thumb = open(thumb_path, 'rb')
                    else:
                        thumb = None
                    await tg_retry(application.bot.send_audio, chat_id=chat_id, audio=f, title=title, caption=full_caption, reply_to_message_id=reply_to_message_id, thumbnail=thumb)
                    if thumb: thumb.close()
            
            if os.path.exists(file_path):
                os.remove(file_path)
        
        else:
            # Video upload with splitting
            if update_status_func:
                await update_status_func("✂️ Checking file size...", force=True)
            
            loop = asyncio.get_running_loop()
            if not check_ffmpeg():
                files_to_upload = [file_path]
            else:
                files_to_upload = await loop.run_in_executor(None, split_video, file_path)
            
            total_parts = len(files_to_upload)
            
            for i, f_path in enumerate(files_to_upload):
                if channel_name:
                    caption = f"{channel_name}\n{title}\n{url}"
                else:
                    caption = f"{title}\n{url}"
                    
                if total_parts > 1:
                    if channel_name:
                        caption = f"{channel_name}\n{title} (Part {i+1}/{total_parts})\n{url}"
                    else:
                        caption = f"{title} (Part {i+1}/{total_parts})\n{url}"
                
                if update_status_func:
                    await update_status_func(f"⬆️ Uploading part {i+1}/{total_parts}...", force=True)
                
                try:
                    keyboard = [[{"text": "🎵 Download Audio", "callback_data": f"audio:{url}"}]]
                    reply_markup_dict = {"inline_keyboard": keyboard}
                    
                    config = load_config()
                    api_url = config.get('api_url', '')
                    bot_token = config.get('bot_token', '')
                    is_local_api = api_url and 'api.telegram.org' not in api_url
                    if is_local_api:
                        await upload_video_streaming(bot_token, api_url, chat_id, f_path, caption, reply_markup_dict, reply_to_message_id=reply_to_message_id, thumb_path=thumb_path)
                    else:
                        keyboard = [[InlineKeyboardButton("🎵 Download Audio", callback_data=f"audio:{url}")]]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        
                        with open(f_path, 'rb') as f:
                            if thumb_path and os.path.exists(thumb_path):
                                thumb_path = crop_to_square(thumb_path)
                                thumb = open(thumb_path, 'rb')
                            else:
                                thumb = None
                            
                            await tg_retry(application.bot.send_video,
                                chat_id=chat_id, video=f, caption=caption, 
                                supports_streaming=True, reply_markup=reply_markup,
                                reply_to_message_id=reply_to_message_id,
                                thumbnail=thumb
                            )
                            if thumb: thumb.close()
                except Exception as e:
                    logger.error(f"Upload failed for part {i+1}: {e}")
                    await tg_retry(application.bot.send_message, chat_id=chat_id, text=f"❌ Upload failed for part {i+1}: {e}")
            
            # Cleanup
            if thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)
                
            if update_status_func:
                await update_status_func("🧹 Cleaning up...", force=True)
            
            if os.path.exists(file_path):
                os.remove(file_path)
            for f_path in files_to_upload:
                if os.path.exists(f_path) and f_path != file_path:
                    os.remove(f_path)
                    
    except Exception as e:
        logger.error(f"Error in handle_upload: {e}")
        error_text = f"🔥 Upload error: {e}"
        if update_status_func:
            await update_status_func(error_text, force=True)
        else:
            await application.bot.send_message(chat_id=chat_id, text=error_text)
    finally:
        gc.collect()

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
            
            async def update_status_msg(text, force=False, show_cancel=False):
                nonlocal status_msg, last_edit_time
                now = time.time()
                if not force and (now - last_edit_time < 20):
                    return
                try:
                    reply_markup = None
                    if show_cancel:
                        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{task_id}")]]
                        reply_markup = InlineKeyboardMarkup(keyboard)
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
                asyncio.create_task(process_live_stream(application, chat_id, url, message_id, status_msg, task_id, update_status_msg, channel_name))
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
                    asyncio.create_task(process_live_stream(application, chat_id, url, message_id, status_msg, task_id, update_status_msg, channel_name))
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
                await handle_upload(application, chat_id, file_path, title, url, audio_only, update_status_msg, channel_name, message_id, thumb_path)
            except Exception as e:
                # Cleanup potential partial files on failure
                logger.error(f"Download failed for {url}: {e}")
                for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"*{task_id}*")):
                    try: os.remove(f)
                    except: pass
                
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
            gc.collect()
async def process_live_stream(application, chat_id, url, message_id, status_msg, task_id, update_status_msg, channel_name):
    """Handle live stream recording using yt-dlp subprocess (handles cookies, proxies, token refresh natively)."""
    SEGMENT_SIZE_BYTES = 1900 * 1024 * 1024  # 1.9GB per segment
    logger.info(f"[LIVE:{task_id}] START url={url}, chat_id={chat_id}, channel={channel_name}")

    live_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏹ Stop & Upload", callback_data=f"stoplive:{task_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{task_id}"),
        ]
    ])

    async def live_status(text):
        nonlocal status_msg
        try:
            logger.info(f"[LIVE:{task_id}] live_status: '{text}', has_msg={status_msg is not None}")
            if status_msg:
                if status_msg.text != text:
                    await tg_retry(status_msg.edit_text, text, reply_markup=live_keyboard)
            else:
                status_msg = await tg_retry(
                    application.bot.send_message,
                    chat_id=chat_id, text=text, reply_to_message_id=message_id, reply_markup=live_keyboard
                )
                logger.info(f"[LIVE:{task_id}] Sent new status msg_id={status_msg.message_id if status_msg else None}")
        except Exception as e:
            logger.error(f"[LIVE:{task_id}] live_status failed: {e}", exc_info=True)

    def _build_ytdlp_cmd(output_path, proxy=None):
        cmd = [
            'yt-dlp',
            '--no-part',
            '--format', 'best[height<=1080]/best',
            '--hls-use-mpegts',
            '--ffmpeg-location', get_ffmpeg_command(),
            '--socket-timeout', '30',
            '--retries', '10',
            '--fragment-retries', '10',
            '--no-check-certificates',
            '--no-playlist',
            '-o', output_path,
        ]
        cookie_file = get_cookie_file()
        if cookie_file:
            cmd += ['--cookies', cookie_file]
        if proxy:
            cmd += ['--proxy', proxy]
        cmd.append(url)
        return cmd

    try:
        await live_status(f"\U0001f534 Recording live stream: {channel_name}")
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

        proxy_list = get_proxy_list()
        logger.info(f"[LIVE:{task_id}] Proxies: {proxy_list}")
        segment_num = 0
        uploaded_segments = []

        while True:
            if task_id in cancelled_tasks:
                logger.info(f"[LIVE:{task_id}] Cancelled before segment start")
                cancelled_tasks.discard(task_id)
                await update_status_msg("❌ Live recording cancelled.", force=True)
                _cleanup_live_files(task_id)
                return

            segment_num += 1
            seg_path_ts = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.ts")
            seg_path = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.mp4")
            logger.info(f"[LIVE:{task_id}] Segment {segment_num}: ts={seg_path_ts}")

            process = None
            success = False
            user_stopped = False
            size_limit_hit = False

            for proxy_idx, proxy in enumerate(proxy_list):
                if task_id in cancelled_tasks:
                    logger.info(f"[LIVE:{task_id}] Cancelled in proxy loop")
                    break

                cmd = _build_ytdlp_cmd(seg_path_ts, proxy)
                logger.info(f"[LIVE:{task_id}] Proxy [{proxy_idx+1}/{len(proxy_list)}] cmd: {' '.join(cmd)}")

                try:
                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    logger.info(f"[LIVE:{task_id}] yt-dlp pid={process.pid}")
                except Exception as e:
                    logger.error(f"[LIVE:{task_id}] Failed to spawn yt-dlp: {e}", exc_info=True)
                    continue

                size_limit_hit = False
                user_stopped = False
                poll_count = 0

                while True:
                    if process.returncode is not None:
                        logger.info(f"[LIVE:{task_id}] Process exited rc={process.returncode}")
                        break

                    if task_id in cancelled_tasks:
                        logger.info(f"[LIVE:{task_id}] Cancel signal, terminating")
                        try:
                            process.terminate()
                            await process.wait()
                        except Exception as e:
                            logger.error(f"[LIVE:{task_id}] terminate error: {e}", exc_info=True)
                        cancelled_tasks.discard(task_id)
                        await update_status_msg("❌ Live recording cancelled.", force=True)
                        _cleanup_live_files(task_id)
                        return

                    if task_id in stopped_tasks:
                        logger.info(f"[LIVE:{task_id}] Stop & Upload signal, terminating")
                        user_stopped = True
                        try:
                            process.terminate()
                            await process.wait()
                        except Exception as e:
                            logger.error(f"[LIVE:{task_id}] terminate error: {e}", exc_info=True)
                        stopped_tasks.discard(task_id)
                        break

                    try:
                        file_size = os.path.getsize(seg_path_ts) if os.path.exists(seg_path_ts) else 0
                    except OSError:
                        file_size = 0

                    if file_size >= SEGMENT_SIZE_BYTES:
                        logger.info(f"[LIVE:{task_id}] Size limit hit: {file_size/(1024*1024):.1f}MB")
                        size_limit_hit = True
                        try:
                            process.terminate()
                            await process.wait()
                        except Exception as e:
                            logger.error(f"[LIVE:{task_id}] terminate error: {e}", exc_info=True)
                        break

                    poll_count += 1
                    if poll_count % 10 == 0:
                        logger.info(f"[LIVE:{task_id}] Recording file_size={file_size/(1024*1024):.1f}MB polls={poll_count}")

                    await asyncio.sleep(3)

                if user_stopped or size_limit_hit:
                    success = True
                    logger.info(f"[LIVE:{task_id}] Loop break: user_stopped={user_stopped} size_limit={size_limit_hit}")
                    break

                if process.returncode == 0:
                    success = True
                    logger.info(f"[LIVE:{task_id}] yt-dlp exited cleanly (stream ended)")
                    break

                # Read stderr on failure
                stderr_out = b''
                try:
                    stderr_out = await asyncio.wait_for(process.stderr.read(), timeout=5)
                except Exception as e:
                    logger.warning(f"[LIVE:{task_id}] stderr read error: {e}")
                stderr_str = stderr_out.decode(errors='replace')
                logger.warning(f"[LIVE:{task_id}] yt-dlp FAILED proxy={proxy} rc={process.returncode}: {stderr_str[:1000]}")

                if 'is not currently live' in stderr_str or 'live event will begin' in stderr_str:
                    ts_size = os.path.getsize(seg_path_ts) if os.path.exists(seg_path_ts) else 0
                    logger.info(f"[LIVE:{task_id}] Not live. ts_size={ts_size}, uploaded={len(uploaded_segments)}")
                    if not uploaded_segments and ts_size == 0:
                        await update_status_msg("❌ Stream is not currently live.", force=True)
                        _cleanup_live_files(task_id)
                        return
                    success = True
                    break

                # Clean empty file before next proxy
                if os.path.exists(seg_path_ts) and os.path.getsize(seg_path_ts) == 0:
                    try: os.remove(seg_path_ts)
                    except: pass
                continue

            if not success and not uploaded_segments:
                logger.error(f"[LIVE:{task_id}] All proxies failed")
                await update_status_msg("❌ Could not record live stream. All proxies failed.", force=True)
                _cleanup_live_files(task_id)
                return

            # Remux .ts -> .mp4
            ts_exists = os.path.exists(seg_path_ts)
            ts_size = os.path.getsize(seg_path_ts) if ts_exists else 0
            logger.info(f"[LIVE:{task_id}] Remux: ts_exists={ts_exists} ts_size={ts_size/(1024*1024):.1f}MB")

            if ts_exists and ts_size > 0:
                try:
                    remux = await asyncio.create_subprocess_exec(
                        get_ffmpeg_command(), '-y', '-i', seg_path_ts,
                        '-c', 'copy', '-movflags', '+faststart', seg_path,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    remux_stderr = await remux.stderr.read()
                    await remux.wait()
                    logger.info(f"[LIVE:{task_id}] Remux rc={remux.returncode}")
                    if remux.returncode != 0:
                        logger.error(f"[LIVE:{task_id}] Remux FAILED: {remux_stderr.decode(errors='replace')[:500]}")
                        seg_path = seg_path_ts  # fallback: upload .ts directly
                    else:
                        try: os.remove(seg_path_ts)
                        except: pass
                except Exception as e:
                    logger.error(f"[LIVE:{task_id}] Remux exception: {e}", exc_info=True)
                    seg_path = seg_path_ts
            elif ts_exists and ts_size == 0:
                logger.warning(f"[LIVE:{task_id}] .ts is empty, skipping")
                try: os.remove(seg_path_ts)
                except: pass

            # Upload
            upload_exists = os.path.exists(seg_path)
            upload_size = os.path.getsize(seg_path) if upload_exists else 0
            logger.info(f"[LIVE:{task_id}] Upload: path={seg_path} exists={upload_exists} size={upload_size/(1024*1024):.1f}MB")

            if upload_exists and upload_size > 0:
                try:
                    is_final = not size_limit_hit or user_stopped
                    title = f"\U0001f534 {channel_name} - LIVE Part {segment_num}"
                    if is_final and (segment_num > 1 or uploaded_segments):
                        title += " (End)"
                    await update_status_msg(f"⬆️ Uploading segment {segment_num}...", force=True)
                    logger.info(f"[LIVE:{task_id}] Uploading: title='{title}' size={upload_size/(1024*1024):.1f}MB")
                    await handle_upload(application, chat_id, seg_path, title, url, False, update_status_msg, channel_name, message_id)
                    uploaded_segments.append(seg_path)
                    logger.info(f"[LIVE:{task_id}] Upload done for segment {segment_num}")
                except Exception as e:
                    logger.error(f"[LIVE:{task_id}] Upload FAILED: {e}", exc_info=True)
                    await update_status_msg(f"❌ Upload failed: {e}", force=True)
            else:
                logger.warning(f"[LIVE:{task_id}] Nothing to upload for segment {segment_num}")

            if not size_limit_hit or user_stopped:
                logger.info(f"[LIVE:{task_id}] Done. size_limit_hit={size_limit_hit} user_stopped={user_stopped}")
                break

            await live_status(f"\U0001f534 Recording continues... (Segment {segment_num} uploaded)")

        logger.info(f"[LIVE:{task_id}] COMPLETE. Segments uploaded: {len(uploaded_segments)}")
        if status_msg:
            try: await tg_retry(status_msg.delete)
            except Exception as e:
                logger.warning(f"[LIVE:{task_id}] Could not delete status msg: {e}")

    except Exception as e:
        logger.error(f"[LIVE:{task_id}] UNHANDLED EXCEPTION: {e}", exc_info=True)
        _cleanup_live_files(task_id)
        try:
            await update_status_msg(f"\U0001f525 Live recording error: {e}", force=True)
        except Exception as e2:
            logger.error(f"[LIVE:{task_id}] Could not send error msg: {e2}", exc_info=True)


def _cleanup_live_files(task_id):
    """Remove any leftover live recording segments."""
    for ext in ['*.mp4', '*.ts']:
        pattern = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{ext}")
        for f in glob.glob(pattern):
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
                info = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: get_playlist_info(url)),
                    timeout=60
                )
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
                        break
                    
                    v_url = entry['url']
                    v_title = entry['title']
                    
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
                        continue

                
                await update_status_msg(f"✨ Playlist complete! Finished {total_videos} videos.", send_new=True)

            except asyncio.TimeoutError:
                await update_status_msg("❌ Timeout getting playlist info.")
            except Exception as e:
                await update_status_msg(f"❌ Failed to get playlist info: {e}")

        except Exception as e:
            logger.error(f"Playlist error: {e}")
            await update_status_msg(f"🔥 Error: {e}")
        finally:
            playlist_queue.task_done()
            gc.collect()
