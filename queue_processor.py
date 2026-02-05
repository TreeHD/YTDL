import os
import gc
import asyncio
import time
import logging
import glob
import subprocess

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TelegramError

from config import load_config, check_disk_space, check_ffmpeg, DOWNLOAD_DIR, get_ffmpeg_command
from downloader import download_content, get_video_info, get_playlist_info, get_stream_url
from uploader import upload_video_streaming, upload_audio_streaming, split_video
from handlers import cancelled_tasks

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
                    thumb = open(thumb_path, 'rb') if thumb_path and os.path.exists(thumb_path) else None
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
                            thumb = open(thumb_path, 'rb') if thumb_path and os.path.exists(thumb_path) else None
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
            
            audio_only = (max_height == -1)
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
                    mode = "🎵 Audio" if audio_only else f"{max_height}p"
                    asyncio.run_coroutine_threadsafe(update_status_msg(f"⬇️ Downloading ({mode}): {p}\nETA: {eta}", show_cancel=True), loop)

            # Download
            try:
                file_path, title, video_id, thumb_path = await loop.run_in_executor(
                    None, 
                    lambda: download_content(url, progress_cb, audio_only=audio_only, max_height=max_height, task_id=task_id, cancelled_tasks=cancelled_tasks)
                )
            except Exception as e:
                # Cleanup potential partial files on failure
                logger.error(f"Download failed for {url}: {e}")
                import glob
                for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"*{task_id}*")):
                    try: os.remove(f)
                    except: pass
                
                await update_status_msg(f"❌ Download failed: {e}", force=True)
                continue

            # Upload using helper
            await handle_upload(application, chat_id, file_path, title, url, audio_only, update_status_msg, channel_name, message_id, thumb_path)
            
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
    """Handle live stream recording with segmented uploads."""
    try:
        await update_status_msg("🔍 Getting live stream URL...", force=True)
        loop = asyncio.get_running_loop()
        stream_url = await loop.run_in_executor(None, lambda: get_stream_url(url))
        if not stream_url:
            await update_status_msg("❌ Could not extract stream URL.", force=True)
            return

        # Prepare segment template in DOWNLOAD_DIR
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        segment_template = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_%03d.mp4")
        
        # Start ffmpeg as a subprocess
        cmd = [
            get_ffmpeg_command(),
            '-i', stream_url,
            '-c', 'copy',
            '-f', 'segment',
            '-segment_size', '1900M',
            '-reset_timestamps', '1',
            segment_template
        ]
        
        logger.info(f"Starting live recording: {' '.join(cmd)}")
        process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        await update_status_msg(f"🔴 Recording live stream: {channel_name}\nSegments upload at 1.9GB.", force=True)
        
        uploaded_segments = set()
        
        while True:
            if process.returncode is not None:
                break
                
            pattern = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_*.mp4")
            files = sorted(glob.glob(pattern))
            
            if len(files) > 1:
                to_upload = files[:-1]
                for f_path in to_upload:
                    if f_path not in uploaded_segments:
                        seg_num = len(uploaded_segments) + 1
                        await update_status_msg(f"🔴 Recording... (Uploading Segment {seg_num})", force=True)
                        title = f"🔴 {channel_name} - LIVE Part {seg_num}"
                        await handle_upload(application, chat_id, f_path, title, url, False, update_status_msg, channel_name, message_id)
                        uploaded_segments.add(f_path)
            
            for _ in range(5):
                if task_id in cancelled_tasks:
                    try:
                        process.terminate()
                        await process.wait()
                    except: pass
                    await update_status_msg("❌ Live recording cancelled.", force=True)
                    for f in glob.glob(pattern):
                        try: os.remove(f)
                        except: pass
                    cancelled_tasks.discard(task_id)
                    return
                await asyncio.sleep(2)
            
            if process.returncode is not None:
                break
                
        # Final upload
        pattern = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_*.mp4")
        files = sorted(glob.glob(pattern))
        for f_path in files:
            if f_path not in uploaded_segments:
                seg_num = len(uploaded_segments) + 1
                await update_status_msg(f"⬆️ Uploading final segment {seg_num}...", force=True)
                title = f"🔴 {channel_name} - LIVE Part {seg_num} (End)"
                await handle_upload(application, chat_id, f_path, title, url, False, update_status_msg, channel_name, message_id)
                uploaded_segments.add(f_path)
                
        if status_msg:
            try: await tg_retry(status_msg.delete)
            except: pass
            
    except Exception as e:
        logger.error(f"Error in process_live_stream: {e}")
        import glob
        pattern = os.path.join(DOWNLOAD_DIR, f"live_{task_id}_*.mp4")
        for f in glob.glob(pattern):
            try: os.remove(f)
            except: pass
        await update_status_msg(f"🔥 Live recording error: {e}", force=True)

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
            
            last_edit_time = 0
            async def update_status_msg(text, force=True, show_cancel=True):
                nonlocal status_msg, last_edit_time
                now = time.time()
                if not force and (now - last_edit_time < 20):
                    return
                try:
                    reply_markup = None
                    if show_cancel:
                        keyboard = [[InlineKeyboardButton("❌ Cancel Playlist", callback_data=f"cancel:{task_id}")]]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        
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
                
                await update_status_msg(f"📋 Playlist: {playlist_title}\n🎬 Found {total_videos} videos.\n🚀 Starting sequential process...")
                
                for i, entry in enumerate(entries):
                    if task_id in cancelled_tasks:
                        await update_status_msg("❌ Playlist cancelled.")
                        cancelled_tasks.discard(task_id)
                        break
                    
                    v_url = entry['url']
                    v_title = entry['title']
                    
                    await update_status_msg(f"🔄 Processing {i+1}/{total_videos}: {v_title[:30]}...")
                    
                    def progress_cb(d):
                        if task_id in cancelled_tasks: raise Exception("Cancelled")
                        if d['status'] == 'downloading':
                            p = d.get('_percent_str', '0%')
                            asyncio.run_coroutine_threadsafe(update_status_msg(f"📋 Playlist: {i+1}/{total_videos}\n⬇️ Video: {p}", force=False), loop)

                    try:
                        file_path, title, video_id, thumb_path = await loop.run_in_executor(
                            None,
                            lambda: download_content(v_url, progress_cb, audio_only=False, max_height=max_height, task_id=task_id, cancelled_tasks=cancelled_tasks)
                        )
                        await handle_upload(application, chat_id, file_path, f"{playlist_title}\n{title}", v_url, False, update_status_msg, reply_to_message_id=message_id, thumb_path=thumb_path)
                    except Exception as e:
                        logger.error(f"Failed for video {i+1}: {e}")
                        await application.bot.send_message(chat_id=chat_id, text=f"⚠️ Skipped {v_title[:30]}: {e}")
                        continue
                
                await update_status_msg(f"✨ Playlist complete! Finished {total_videos} videos.")

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
