"""
Queue processor module for YTDL Telegram Bot.
Handles download queue processing and playlist downloads.
"""

import os
import gc
import asyncio
import time
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TelegramError

from config import load_config, check_disk_space, check_ffmpeg
from downloader import download_content, get_video_info, get_playlist_info
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

async def handle_upload(application, chat_id, file_path, title, url, audio_only=False, update_status_func=None):
    """Helper to handle video/audio upload with splitting and cleanup."""
    try:
        if audio_only:
            if update_status_func:
                await update_status_func("⬆️ Uploading audio...", force=True)
            
            config = load_config()
            api_url = config.get('api_url', '')
            bot_token = config.get('bot_token', '')
            is_local_api = api_url and 'api.telegram.org' not in api_url
            
            if is_local_api:
                await upload_audio_streaming(bot_token, api_url, chat_id, file_path, title, f"🎵 {title}")
            else:
                with open(file_path, 'rb') as f:
                    await application.bot.send_audio(chat_id=chat_id, audio=f, title=title, caption=f"🎵 {title}")
            
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
                caption = title
                if total_parts > 1:
                    caption += f" (Part {i+1}/{total_parts})"
                
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
                        await upload_video_streaming(bot_token, api_url, chat_id, f_path, caption, reply_markup_dict)
                    else:
                        keyboard = [[InlineKeyboardButton("🎵 Download Audio", callback_data=f"audio:{url}")]]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        
                        with open(f_path, 'rb') as f:
                            await tg_retry(application.bot.send_video,
                                chat_id=chat_id, video=f, caption=caption, 
                                supports_streaming=True, reply_markup=reply_markup
                            )
                except Exception as e:
                    logger.error(f"Upload failed for part {i+1}: {e}")
                    await tg_retry(application.bot.send_message, chat_id=chat_id, text=f"❌ Upload failed for part {i+1}: {e}")
            
            # Cleanup
            if update_status_func:
                await update_status_func("🧹 Cleaning up...", force=True)
            
            if os.path.exists(file_path):
                os.remove(file_path)
            for f_path in files_to_upload:
                if os.path.exists(f_path) and f_path != file_path:
                    os.remove(f_path)
                    
    except Exception as e:
        logger.error(f"Error in handle_upload: {e}")
        await application.bot.send_message(chat_id=chat_id, text=f"🔥 Upload error: {e}")
    finally:
        gc.collect()

async def process_queue(application, request_queue):
    """Main queue processor for single video downloads."""
    logger.info("Queue processor started.")
    
    while True:
        task = await request_queue.get()
        
        if len(task) == 4:
            chat_id, url, message_id, max_height = task
        else:
            chat_id, url, message_id = task
            max_height = 1080
        
        audio_only = (max_height == -1)
        if audio_only:
            max_height = 1080
        
        task_id = f"{chat_id}_{message_id}_{int(time.time())}"
        status_msg = None
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

        try:
            await update_status_msg(f"🚀 Processing: {url}", force=True, show_cancel=True)
            
            # Disk space check
            config = load_config()
            max_disk_gb = config.get('max_disk_gb', 0)
            if max_disk_gb > 0:
                await update_status_msg("📊 Checking video info...", force=True, show_cancel=True)
                try:
                    loop = asyncio.get_running_loop()
                    video_info = await loop.run_in_executor(None, lambda: get_video_info(url))
                    estimated_mb = video_info.get('filesize_mb', 0)
                    if estimated_mb > 0:
                        can_download, remaining_gb = check_disk_space(estimated_mb)
                        if not can_download:
                            await update_status_msg(f"❌ Low disk space! Need {estimated_mb/1024:.1f}GB, have {remaining_gb:.1f}GB.", force=True)
                            continue
                except: pass

            if task_id in cancelled_tasks:
                await update_status_msg("❌ Cancelled.", force=True)
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
                file_path, title, video_id = await loop.run_in_executor(
                    None, 
                    lambda: download_content(url, progress_cb, audio_only=audio_only, max_height=max_height, task_id=task_id, cancelled_tasks=cancelled_tasks)
                )
            except Exception as e:
                await update_status_msg(f"❌ Download failed: {e}", force=True)
                continue

            # Upload using helper
            await handle_upload(application, chat_id, file_path, title, url, audio_only, update_status_msg)
            await update_status_msg("✨ Task completed!", force=True)

        except Exception as e:
            logger.error(f"Error in process_queue: {e}")
            await application.bot.send_message(chat_id=chat_id, text=f"🔥 Error: {e}")
        finally:
            request_queue.task_done()
            gc.collect()

async def process_playlist_queue(application, playlist_queue):
    """Process playlist download queue SEQUENTIALLY to save space."""
    logger.info("Playlist queue processor started.")
    
    while True:
        task = await playlist_queue.get()
        chat_id, url, message_id, max_height = task
        
        task_id = f"pl_{chat_id}_{int(time.time())}"
        status_msg = None
        
        last_edit_time = 0
        async def update_status_msg(text, force=True):
            nonlocal status_msg, last_edit_time
            now = time.time()
            if not force and (now - last_edit_time < 20):
                return
            try:
                if status_msg:
                    if status_msg.text != text:
                        await tg_retry(status_msg.edit_text, text)
                        last_edit_time = now
                else:
                    status_msg = await tg_retry(application.bot.send_message, chat_id=chat_id, text=text, reply_to_message_id=message_id)
                    last_edit_time = now
            except Exception as e:
                logger.warning(f"Failed to update status: {e}")

        try:
            await update_status_msg("📋 Getting playlist info...")
            loop = asyncio.get_running_loop()
            
            # Step 1: Get entries first
            try:
                info = await loop.run_in_executor(None, lambda: get_playlist_info(url))
                entries = info.get('entries', [])
                playlist_title = info.get('title', 'Playlist')
                total_videos = len(entries)
                
                if total_videos == 0:
                    await update_status_msg("❌ No videos found in playlist.")
                    continue
                
                await update_status_msg(f"📋 Playlist: {playlist_title}\n🎬 Found {total_videos} videos.\n🚀 Starting sequential process...")
                
            except Exception as e:
                await update_status_msg(f"❌ Failed to get playlist info: {e}")
                continue

            # Step 2: Loop Download -> Upload -> Delete
            for i, entry in enumerate(entries):
                if task_id in cancelled_tasks:
                    await update_status_msg("❌ Playlist cancelled.")
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
                    # Download one video
                    file_path, title, video_id = await loop.run_in_executor(
                        None,
                        lambda: download_content(v_url, progress_cb, audio_only=False, max_height=max_height, task_id=task_id, cancelled_tasks=cancelled_tasks)
                    )
                    
                    # Upload and Clean one video
                    await handle_upload(application, chat_id, file_path, f"📋 {playlist_title}\n\n{title}", v_url, False, update_status_msg)
                    
                except Exception as e:
                    logger.error(f"Failed for video {i+1}: {e}")
                    await application.bot.send_message(chat_id=chat_id, text=f"⚠️ Skipped {v_title[:30]}: {e}")
                    continue
            
            await update_status_msg(f"✨ Playlist complete! Finished {total_videos} videos.")

        except Exception as e:
            logger.error(f"Playlist error: {e}")
            await update_status_msg(f"🔥 Error: {e}")
        finally:
            playlist_queue.task_done()
            gc.collect()
