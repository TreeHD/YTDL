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
from handlers import cancelled_tasks, stopped_tasks, fromstart_tasks

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
                    config = load_config()
                    api_url = config.get('api_url', '')
                    bot_token = config.get('bot_token', '')
                    is_local_api = api_url and 'api.telegram.org' not in api_url

                    # Only add audio button if callback_data fits Telegram's 64-byte limit
                    audio_cb_data = f"audio:{url}"
                    if len(audio_cb_data.encode('utf-8')) <= 64:
                        reply_markup_dict = {"inline_keyboard": [[{"text": "🎵 Download Audio", "callback_data": audio_cb_data}]]}
                        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🎵 Download Audio", callback_data=audio_cb_data)]])
                    else:
                        reply_markup_dict = None
                        reply_markup = None

                    if is_local_api:
                        await upload_video_streaming(bot_token, api_url, chat_id, f_path, caption, reply_markup_dict, reply_to_message_id=reply_to_message_id, thumb_path=thumb_path)
                    else:
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

async def _kill_process(process, task_id):
    """Terminate process, wait with timeout, kill if stuck."""
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=5)
        logger.info(f"[LIVE:{task_id}] Process terminated gracefully")
    except asyncio.TimeoutError:
        logger.warning(f"[LIVE:{task_id}] Process didn't exit after SIGTERM, sending SIGKILL")
        try:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=3)
        except Exception:
            logger.error(f"[LIVE:{task_id}] SIGKILL also failed, process may be orphaned")
    except Exception as e:
        logger.error(f"[LIVE:{task_id}] _kill_process error: {e}", exc_info=True)

async def _extract_stream_url(url, proxy_list):
    """Use yt-dlp to extract the direct stream URL (handles auth, cookies, geo)."""
    for proxy in proxy_list:
        cmd = [
            'yt-dlp',
            '--format', 'best[height<=1080]/best',
            '--no-check-certificates',
            '--no-playlist',
            '--print', 'urls',
            '--socket-timeout', '15',
        ]
        cookie_file = get_cookie_file()
        if cookie_file:
            cmd += ['--cookies', cookie_file]
        if proxy:
            cmd += ['--proxy', proxy]
        cmd.append(url)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                stream_url = stdout.decode().strip().split('\n')[0]
                if stream_url:
                    logger.info(f"[LIVE] Extracted stream URL via proxy={proxy}: {stream_url[:100]}...")
                    return stream_url, proxy
            else:
                stderr_str = stderr.decode(errors='replace')
                logger.warning(f"[LIVE] yt-dlp extract failed proxy={proxy} rc={proc.returncode}: {stderr_str[:300]}")
                if 'is not currently live' in stderr_str or 'live event will begin' in stderr_str:
                    return None, 'not_live'
                if 'not available in your country' in stderr_str:
                    continue
        except asyncio.TimeoutError:
            logger.warning(f"[LIVE] yt-dlp extract timed out proxy={proxy}")
            continue
        except Exception as e:
            logger.error(f"[LIVE] yt-dlp extract error: {e}")
            continue
    return None, None


async def process_live_stream(application, chat_id, url, message_id, status_msg, task_id, update_status_msg, channel_name):
    """Record live stream: yt-dlp extracts URL, ffmpeg records directly to mpegts.
    Default: record from now. Button to download from start in background."""
    SEGMENT_SIZE_BYTES = 1900 * 1024 * 1024  # 1.9GB per segment
    logger.info(f"[LIVE:{task_id}] START url={url}, chat_id={chat_id}, channel={channel_name}")

    fromstart_triggered = False

    def _make_keyboard():
        buttons = []
        if not fromstart_triggered:
            buttons.append(InlineKeyboardButton("⏪ From Start", callback_data=f"fromstart:{task_id}"))
        buttons.append(InlineKeyboardButton("⏹ Stop & Upload", callback_data=f"stoplive:{task_id}"))
        buttons.append(InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{task_id}"))
        return InlineKeyboardMarkup([buttons])

    async def live_status(text):
        nonlocal status_msg
        try:
            keyboard = _make_keyboard()
            logger.info(f"[LIVE:{task_id}] live_status: '{text}', has_msg={status_msg is not None}")
            if status_msg:
                if status_msg.text != text:
                    await tg_retry(status_msg.edit_text, text, reply_markup=keyboard)
            else:
                status_msg = await tg_retry(
                    application.bot.send_message,
                    chat_id=chat_id, text=text, reply_to_message_id=message_id, reply_markup=keyboard
                )
                logger.info(f"[LIVE:{task_id}] Sent new status msg_id={status_msg.message_id if status_msg else None}")
        except Exception as e:
            logger.error(f"[LIVE:{task_id}] live_status failed: {e}", exc_info=True)

    def _build_ffmpeg_cmd(stream_url, output_path, proxy=None):
        """Build ffmpeg command for direct stream recording."""
        cmd = [get_ffmpeg_command(), '-y']
        # Reconnect options for HLS/DASH streams
        cmd += ['-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '5']
        # Proxy via environment (ffmpeg uses http_proxy for HTTP streams)
        # Input
        cmd += ['-i', stream_url]
        # Copy codecs, output as mpegts (always valid when truncated)
        cmd += ['-c', 'copy', '-f', 'mpegts', output_path]
        return cmd

    def _build_ytdlp_cmd(output_path, proxy=None, from_start=False):
        """yt-dlp command for background from-start download."""
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
        if from_start:
            cmd.append('--live-from-start')
        cookie_file = get_cookie_file()
        if cookie_file:
            cmd += ['--cookies', cookie_file]
        if proxy:
            cmd += ['--proxy', proxy]
        cmd.append(url)
        return cmd

    async def _download_from_start():
        """Background task: download the stream from beginning using yt-dlp."""
        bg_id = f"{task_id}_fromstart"
        logger.info(f"[LIVE:{bg_id}] Background from-start download starting")
        proxy_list = get_proxy_list()

        seg_num = 0
        while True:
            seg_num += 1
            bg_ts = os.path.join(DOWNLOAD_DIR, f"live_{bg_id}_{seg_num:03d}.ts")
            bg_mp4 = os.path.join(DOWNLOAD_DIR, f"live_{bg_id}_{seg_num:03d}.mp4")

            success = False
            for proxy in proxy_list:
                cmd = _build_ytdlp_cmd(bg_ts, proxy, from_start=True)
                logger.info(f"[LIVE:{bg_id}] cmd: {' '.join(cmd)}")

                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    logger.info(f"[LIVE:{bg_id}] pid={proc.pid}")
                except Exception as e:
                    logger.error(f"[LIVE:{bg_id}] Spawn failed: {e}", exc_info=True)
                    continue

                # Monitor for task cancel (no size limit for from-start, it ends naturally)
                while True:
                    if proc.returncode is not None:
                        break
                    if task_id in cancelled_tasks:
                        await _kill_process(proc, bg_id)
                        _cleanup_live_files(bg_id)
                        logger.info(f"[LIVE:{bg_id}] Cancelled")
                        return
                    await asyncio.sleep(5)

                if proc.returncode == 0:
                    success = True
                    break

                stderr_out = b''
                try:
                    stderr_out = await asyncio.wait_for(proc.stderr.read(), timeout=5)
                except Exception:
                    pass
                stderr_str = stderr_out.decode(errors='replace')
                logger.warning(f"[LIVE:{bg_id}] Failed proxy={proxy} rc={proc.returncode}: {stderr_str[:500]}")

                if 'not available' in stderr_str.lower() or 'requested format' in stderr_str.lower():
                    logger.info(f"[LIVE:{bg_id}] No DVR/from-start support")
                    try:
                        await tg_retry(
                            application.bot.send_message,
                            chat_id=chat_id,
                            text="⚠️ This stream doesn't support playback from start (no DVR).",
                            reply_to_message_id=message_id
                        )
                    except Exception:
                        pass
                    _cleanup_live_files(bg_id)
                    return

                if os.path.exists(bg_ts) and os.path.getsize(bg_ts) == 0:
                    try: os.remove(bg_ts)
                    except: pass
                continue

            if not success:
                logger.error(f"[LIVE:{bg_id}] All proxies failed")
                try:
                    await tg_retry(
                        application.bot.send_message,
                        chat_id=chat_id,
                        text="❌ From-start download failed.",
                        reply_to_message_id=message_id
                    )
                except Exception:
                    pass
                _cleanup_live_files(bg_id)
                return

            # Remux and upload
            ts_size = os.path.getsize(bg_ts) if os.path.exists(bg_ts) else 0
            if ts_size > 0:
                try:
                    remux = await asyncio.create_subprocess_exec(
                        get_ffmpeg_command(), '-y', '-i', bg_ts,
                        '-c', 'copy', '-movflags', '+faststart', bg_mp4,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await remux.stderr.read()
                    await remux.wait()
                    if remux.returncode != 0:
                        remux2 = await asyncio.create_subprocess_exec(
                            get_ffmpeg_command(), '-y', '-i', bg_ts,
                            '-c', 'copy', bg_mp4,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await remux2.wait()
                    if os.path.exists(bg_mp4) and os.path.getsize(bg_mp4) > 0:
                        try: os.remove(bg_ts)
                        except: pass
                    else:
                        logger.error(f"[LIVE:{bg_id}] Remux produced no output")
                        try: os.remove(bg_ts)
                        except: pass
                        break
                except Exception as e:
                    logger.error(f"[LIVE:{bg_id}] Remux error: {e}", exc_info=True)
                    try: os.remove(bg_ts)
                    except: pass
                    break

                try:
                    title = f"⏪ {channel_name} - From Start"
                    logger.info(f"[LIVE:{bg_id}] Uploading: {title}")
                    await handle_upload(application, chat_id, bg_mp4, title, url, False, update_status_msg, channel_name, message_id)
                    logger.info(f"[LIVE:{bg_id}] Upload done")
                except Exception as e:
                    logger.error(f"[LIVE:{bg_id}] Upload failed: {e}", exc_info=True)

            break  # from-start downloads once (the whole DVR content)

        logger.info(f"[LIVE:{bg_id}] Background from-start download complete")

    try:
        await live_status(f"\U0001f534 Getting stream URL: {channel_name}")
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

            # Extract fresh stream URL for each segment (tokens expire)
            stream_url, extract_proxy = await _extract_stream_url(url, proxy_list)
            if not stream_url:
                if extract_proxy == 'not_live':
                    if not uploaded_segments:
                        await update_status_msg("❌ Stream is not currently live.", force=True)
                    _cleanup_live_files(task_id)
                    return
                if not uploaded_segments:
                    await update_status_msg("❌ Could not get stream URL. All proxies failed.", force=True)
                _cleanup_live_files(task_id)
                return

            segment_num += 1
            seg_path_ts = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.ts")
            seg_path = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_{segment_num:03d}.mp4")
            logger.info(f"[LIVE:{task_id}] Segment {segment_num}: ts={seg_path_ts}")

            # Build ffmpeg command
            cmd = _build_ffmpeg_cmd(stream_url, seg_path_ts, extract_proxy)
            logger.info(f"[LIVE:{task_id}] ffmpeg cmd: {' '.join(cmd[:6])}... -i <stream> ... {seg_path_ts}")

            # Set proxy env for ffmpeg — only HTTP(S) proxies work, not SOCKS5
            env = os.environ.copy()
            if extract_proxy and extract_proxy.startswith('http'):
                env['http_proxy'] = extract_proxy
                env['https_proxy'] = extract_proxy

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                logger.info(f"[LIVE:{task_id}] ffmpeg pid={process.pid}")
            except Exception as e:
                logger.error(f"[LIVE:{task_id}] Failed to spawn ffmpeg: {e}", exc_info=True)
                if not uploaded_segments:
                    await update_status_msg(f"\U0001f525 Failed to start recording: {e}", force=True)
                _cleanup_live_files(task_id)
                return

            await live_status(f"\U0001f534 Recording live stream: {channel_name}")

            user_stopped = False
            size_limit_hit = False
            poll_count = 0

            while True:
                if process.returncode is not None:
                    logger.info(f"[LIVE:{task_id}] ffmpeg exited rc={process.returncode}")
                    break

                if task_id in cancelled_tasks:
                    logger.info(f"[LIVE:{task_id}] Cancel signal")
                    await _kill_process(process, task_id)
                    cancelled_tasks.discard(task_id)
                    await update_status_msg("❌ Live recording cancelled.", force=True)
                    _cleanup_live_files(task_id)
                    return

                if task_id in stopped_tasks:
                    logger.info(f"[LIVE:{task_id}] Stop & Upload signal")
                    user_stopped = True
                    await _kill_process(process, task_id)
                    stopped_tasks.discard(task_id)
                    break

                if task_id in fromstart_tasks and not fromstart_triggered:
                    logger.info(f"[LIVE:{task_id}] 'From Start' triggered, spawning background download")
                    fromstart_triggered = True
                    fromstart_tasks.discard(task_id)
                    asyncio.create_task(_download_from_start())
                    await live_status(f"\U0001f534 Recording: {channel_name}\n⏪ Downloading from start in background...")

                try:
                    file_size = os.path.getsize(seg_path_ts) if os.path.exists(seg_path_ts) else 0
                except OSError:
                    file_size = 0

                if file_size >= SEGMENT_SIZE_BYTES:
                    logger.info(f"[LIVE:{task_id}] Size limit hit: {file_size/(1024*1024):.1f}MB")
                    size_limit_hit = True
                    await _kill_process(process, task_id)
                    break

                poll_count += 1
                if poll_count % 10 == 0:
                    logger.info(f"[LIVE:{task_id}] Recording file_size={file_size/(1024*1024):.1f}MB polls={poll_count}")

                await asyncio.sleep(3)

            # ffmpeg exited or was stopped — check if stream ended with error
            if process.returncode is not None and process.returncode != 0 and not user_stopped and not size_limit_hit:
                stderr_out = b''
                try:
                    stderr_out = await asyncio.wait_for(process.stderr.read(), timeout=5)
                except Exception:
                    pass
                stderr_str = stderr_out.decode(errors='replace')
                logger.warning(f"[LIVE:{task_id}] ffmpeg ended rc={process.returncode}: {stderr_str[-500:]}")
                # If file has content, still try to upload it
                ts_size = os.path.getsize(seg_path_ts) if os.path.exists(seg_path_ts) else 0
                if ts_size == 0 and not uploaded_segments:
                    await update_status_msg(f"❌ Recording failed (ffmpeg exit {process.returncode})", force=True)
                    _cleanup_live_files(task_id)
                    return
                # If has content, fall through to remux/upload, then try next segment

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
                        logger.warning(f"[LIVE:{task_id}] Remux faststart failed, retrying: {remux_stderr.decode(errors='replace')[:200]}")
                        remux2 = await asyncio.create_subprocess_exec(
                            get_ffmpeg_command(), '-y', '-i', seg_path_ts,
                            '-c', 'copy', seg_path,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        await remux2.stderr.read()
                        await remux2.wait()
                        if remux2.returncode != 0:
                            logger.error(f"[LIVE:{task_id}] Remux retry also failed")
                        else:
                            try: os.remove(seg_path_ts)
                            except: pass
                    else:
                        try: os.remove(seg_path_ts)
                        except: pass
                except Exception as e:
                    logger.error(f"[LIVE:{task_id}] Remux exception: {e}", exc_info=True)
            elif ts_exists and ts_size == 0:
                try: os.remove(seg_path_ts)
                except: pass

            # Upload
            upload_exists = os.path.exists(seg_path)
            upload_size = os.path.getsize(seg_path) if upload_exists else 0
            logger.info(f"[LIVE:{task_id}] Upload: exists={upload_exists} size={upload_size/(1024*1024):.1f}MB")

            if upload_exists and upload_size > 0:
                try:
                    is_final = not size_limit_hit or user_stopped
                    title = f"\U0001f534 {channel_name} - LIVE Part {segment_num}"
                    if is_final and (segment_num > 1 or uploaded_segments):
                        title += " (End)"
                    await update_status_msg(f"⬆️ Uploading segment {segment_num}...", force=True)
                    logger.info(f"[LIVE:{task_id}] Uploading: '{title}' size={upload_size/(1024*1024):.1f}MB")
                    await handle_upload(application, chat_id, seg_path, title, url, False, update_status_msg, channel_name, message_id)
                    uploaded_segments.append(seg_path)
                    logger.info(f"[LIVE:{task_id}] Upload done segment {segment_num}")
                except Exception as e:
                    logger.error(f"[LIVE:{task_id}] Upload FAILED: {e}", exc_info=True)
                    await update_status_msg(f"❌ Upload failed: {e}", force=True)
            else:
                logger.warning(f"[LIVE:{task_id}] Nothing to upload for segment {segment_num}")

            if user_stopped:
                logger.info(f"[LIVE:{task_id}] User stopped, finishing")
                break

            if not size_limit_hit and process.returncode != 0:
                # ffmpeg exited unexpectedly (token expired, network error)
                # Check if stream is still live by trying to extract a fresh URL
                ts_size_check = os.path.getsize(seg_path_ts) if os.path.exists(seg_path_ts) else 0
                if ts_size_check == 0 and not uploaded_segments:
                    # No data at all — stream likely not available
                    logger.info(f"[LIVE:{task_id}] Stream ended (no data recorded), finishing")
                    break
                # Try fresh URL — if extraction fails, stream is truly over
                logger.info(f"[LIVE:{task_id}] ffmpeg exited mid-stream, will retry with fresh URL")
                await asyncio.sleep(3)
                test_url, test_status = await _extract_stream_url(url, proxy_list)
                if not test_url:
                    logger.info(f"[LIVE:{task_id}] Stream no longer available (status={test_status}), finishing")
                    break
                # Stream still live — continue to next segment with fresh URL
                logger.info(f"[LIVE:{task_id}] Stream still live, continuing recording")
                await live_status(f"\U0001f534 Reconnecting: {channel_name}")

            if size_limit_hit:
                await live_status(f"\U0001f534 Recording: {channel_name} — Segment {segment_num} uploaded")

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
        for prefix in [f"live_{task_id}_{ext}", f"live_{task_id}_fromstart_{ext}"]:
            pattern = os.path.join(DOWNLOAD_DIR, prefix)
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
