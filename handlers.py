"""
Bot handlers module for YTDL Telegram Bot.
Contains all Telegram command and callback handlers.
"""

import os
import gc
import asyncio
import time
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import load_config, is_user_allowed, check_disk_space, check_ffmpeg
from database import (
    add_subscription, remove_subscription, get_user_subscriptions,
    get_all_subscriptions, get_user_settings, update_user_settings
)
from downloader import (
    download_content, get_video_info, 
    get_channel_info, get_playlist_info, is_playlist
)
from uploader import upload_video_streaming, upload_audio_streaming, split_video

logger = logging.getLogger(__name__)

# Track cancelled tasks
cancelled_tasks = set()

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start and /help commands."""
    chat_id = update.effective_chat.id
    if not is_user_allowed(chat_id):
        await context.bot.send_message(chat_id=chat_id, text="Sorry, you are not authorized to use this bot.")
        return
    
    help_text = """🎬 **YouTube Downloader Bot**

Send me a URL to download in 1080p (default).

**Video Quality Commands:**
• `/1080 <URL>` - Download in 1080p
• `/720 <URL>` - Download in 720p
• `/480 <URL>` - Download in 480p
• `/240 <URL>` - Download in 240p

**Audio Command:**
• `/music <URL>` - Download audio only (M4A)

**Playlist Command:**
• `/playlist <URL>` - Download entire playlist

**Subscription Commands:**
• `/subscribe <channel_url>` - Subscribe to a channel
• `/unsubscribe <channel_url>` - Unsubscribe from a channel
• `/subscriptions` - List your subscriptions

Or just send a URL directly for 1080p video.
"""
    await context.bot.send_message(chat_id=chat_id, text=help_text, parse_mode='Markdown')

async def handle_music_command(update: Update, context: ContextTypes.DEFAULT_TYPE, request_queue):
    """Handle /music command for audio-only downloads."""
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    
    if not is_user_allowed(chat_id):
        await context.bot.send_message(chat_id=chat_id, text="Sorry, you are not authorized.", reply_to_message_id=message_id)
        return
    
    if not context.args:
        await context.bot.send_message(chat_id=chat_id, text="Usage: `/music <URL>`", reply_to_message_id=message_id, parse_mode='Markdown')
        return
    
    url = context.args[0]
    if not url.startswith(('http://', 'https://')):
        await context.bot.send_message(chat_id=chat_id, text="Please send a valid URL.", reply_to_message_id=message_id)
        return
    
    await request_queue.put((chat_id, url, message_id, -1))  # -1 = audio only
    q_size = request_queue.qsize()
    await context.bot.send_message(chat_id=chat_id, text=f"🎵 Added to queue (Audio M4A). Queue Depth: {q_size}", reply_to_message_id=message_id)

async def handle_quality_command(update: Update, context: ContextTypes.DEFAULT_TYPE, request_queue):
    """Handle quality-specific download commands like /720, /480, /240."""
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    
    if not is_user_allowed(chat_id):
        await context.bot.send_message(chat_id=chat_id, text="Sorry, you are not authorized.", reply_to_message_id=message_id)
        return
    
    command = update.message.text.split()[0].lower()
    quality_map = {'/1080': 1080, '/720': 720, '/480': 480, '/360': 360, '/240': 240}
    max_height = quality_map.get(command, 1080)
    
    if not context.args:
        await context.bot.send_message(chat_id=chat_id, text=f"Usage: `{command} <URL>`", reply_to_message_id=message_id, parse_mode='Markdown')
        return
    
    url = context.args[0]
    if not url.startswith(('http://', 'https://')):
        await context.bot.send_message(chat_id=chat_id, text="Please send a valid URL.", reply_to_message_id=message_id)
        return
    
    await request_queue.put((chat_id, url, message_id, max_height))
    q_size = request_queue.qsize()
    await context.bot.send_message(chat_id=chat_id, text=f"📥 Added to queue ({max_height}p). Queue Depth: {q_size}", reply_to_message_id=message_id)

async def handle_playlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE, playlist_queue):
    """Handle /playlist command for downloading playlists."""
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    
    if not is_user_allowed(chat_id):
        await context.bot.send_message(chat_id=chat_id, text="Sorry, you are not authorized.", reply_to_message_id=message_id)
        return
    
    if not context.args:
        await context.bot.send_message(chat_id=chat_id, text="Usage: `/playlist <URL>`", reply_to_message_id=message_id, parse_mode='Markdown')
        return
    
    url = context.args[0]
    max_height = 1080
    
    # Optional quality argument
    if len(context.args) > 1:
        try:
            max_height = int(context.args[1])
        except ValueError:
            pass
    
    if not url.startswith(('http://', 'https://')):
        await context.bot.send_message(chat_id=chat_id, text="Please send a valid URL.", reply_to_message_id=message_id)
        return
    
    # Get playlist info first
    try:
        status_msg = await context.bot.send_message(chat_id=chat_id, text="📋 Getting playlist info...", reply_to_message_id=message_id)
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, lambda: get_playlist_info(url))
        await status_msg.edit_text(
            f"📋 **Playlist Found**\n\n"
            f"📁 {info['title']}\n"
            f"🎬 {info['count']} videos\n\n"
            f"Starting download in {max_height}p..."
        )
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Failed to get playlist info: {e}", reply_to_message_id=message_id)
        return
    
    await playlist_queue.put((chat_id, url, message_id, max_height))
    await status_msg.edit_text(f"📋 Playlist added to queue. This may take a while...")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE, request_queue):
    """Handle direct URL messages."""
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    
    if not is_user_allowed(chat_id):
        await context.bot.send_message(chat_id=chat_id, text="Sorry, you are not authorized.", reply_to_message_id=message_id)
        return

    url = update.message.text
    if not url.startswith(('http://', 'https://')):
        await context.bot.send_message(chat_id=chat_id, text="Please send a valid URL.", reply_to_message_id=message_id)
        return

    status_msg = await context.bot.send_message(chat_id=chat_id, text="🔍 Analyzing URL...", reply_to_message_id=message_id)
    
    loop = asyncio.get_running_loop()
    is_pl = await loop.run_in_executor(None, is_playlist, url)
    
    # Fetch user settings
    settings = get_user_settings(chat_id)
    mode = settings.get('download_mode', 'video')
    res = settings.get('resolution', 1080)
    
    if is_pl:
        await status_msg.edit_text(f"📋 Playlist detected. Adding to queue ({res}p)...")
        await context.application.bot_data['playlist_queue'].put((chat_id, url, message_id, res))
        await status_msg.edit_text(f"📋 Playlist added to queue ({res}p).")
    else:
        # If mode is audio, set resolution to -1 (marker for audio in our processor)
        final_res = -1 if mode == 'audio' else res
        await request_queue.put((chat_id, url, message_id, final_res))
        q_size = request_queue.qsize()
        mode_str = "Audio M4A" if mode == 'audio' else f"{res}p"
        await status_msg.edit_text(f"📥 Added to queue ({mode_str}). Queue Depth: {q_size}")

# --- Subscription Handlers ---
async def handle_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /subscribe command."""
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    
    if not is_user_allowed(chat_id):
        await context.bot.send_message(chat_id=chat_id, text="Sorry, you are not authorized.", reply_to_message_id=message_id)
        return
    
    if not context.args:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Usage: `/subscribe <channel_url> [quality]`\n\nExample:\n`/subscribe https://www.youtube.com/@ChannelName 720`",
            reply_to_message_id=message_id,
            parse_mode='Markdown'
        )
        return
    
    channel_url = context.args[0]
    max_quality = 1080
    if len(context.args) > 1:
        try:
            max_quality = int(context.args[1])
        except ValueError:
            pass
    
    status_msg = await context.bot.send_message(chat_id=chat_id, text="🔍 Getting channel info...", reply_to_message_id=message_id)
    
    try:
        loop = asyncio.get_running_loop()
        channel_info = await loop.run_in_executor(None, lambda: get_channel_info(channel_url))
        
        success = add_subscription(
            channel_id=channel_info['channel_id'],
            channel_name=channel_info['channel_name'],
            chat_id=chat_id,
            max_quality=max_quality
        )
        
        if success:
            await status_msg.edit_text(
                f"✅ **Subscribed!**\n\n"
                f"📺 Channel: {channel_info['channel_name']}\n"
                f"🎬 Quality: {max_quality}p\n\n"
                f"You'll receive new videos automatically!",
                parse_mode='Markdown'
            )
        else:
            await status_msg.edit_text("❌ Failed to save subscription.")
            
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}")

async def handle_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unsubscribe command."""
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    
    if not is_user_allowed(chat_id):
        return
    
    if not context.args:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Usage: `/unsubscribe <channel_url>`",
            reply_to_message_id=message_id,
            parse_mode='Markdown'
        )
        return
    
    channel_url = context.args[0]
    
    try:
        loop = asyncio.get_running_loop()
        channel_info = await loop.run_in_executor(None, lambda: get_channel_info(channel_url))
        success = remove_subscription(channel_info['channel_id'], chat_id)
        
        if success:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Unsubscribed from {channel_info['channel_name']}",
                reply_to_message_id=message_id
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Subscription not found.",
                reply_to_message_id=message_id
            )
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Error: {e}", reply_to_message_id=message_id)

async def handle_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /subscriptions command to list all subscriptions."""
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    
    if not is_user_allowed(chat_id):
        return
    
    subs = get_user_subscriptions(chat_id)
    
    if not subs:
        await context.bot.send_message(
            chat_id=chat_id,
            text="📭 You have no subscriptions.\n\nUse `/subscribe <channel_url>` to add one.",
            reply_to_message_id=message_id,
            parse_mode='Markdown'
        )
        return
    
    text = "📺 **Your Subscriptions**\n\n"
    for channel_id, channel_name, max_quality, created_at in subs:
        text += f"• **{channel_name}** ({max_quality}p)\n"
    
    text += f"\n_Total: {len(subs)} subscriptions_"
    
    await context.bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=message_id, parse_mode='Markdown')

# --- Settings Handlers ---
def get_settings_keyboard(chat_id):
    """Create settings keyboard based on user preferences."""
    settings = get_user_settings(chat_id)
    mode = settings.get('download_mode', 'video')
    res = settings.get('resolution', 1080)
    
    # Mode buttons
    mode_text = "🎬 Video" if mode == 'video' else "🎵 Audio (M4A)"
    mode_btn = InlineKeyboardButton(f"Mode: {mode_text}", callback_data="set_mode")
    
    # Resolution buttons (only relevant for video)
    res_buttons = []
    if mode == 'video':
        for r in [1080, 720, 480, 360, 240]:
            label = f"✅ {r}p" if r == res else f"{r}p"
            res_buttons.append(InlineKeyboardButton(label, callback_data=f"set_res:{r}"))
    
    # Arrange keyboard
    keyboard = [[mode_btn]]
    if res_buttons:
        # Split resolutions into rows of 3
        keyboard.append(res_buttons[:3])
        keyboard.append(res_buttons[3:])
    
    return InlineKeyboardMarkup(keyboard)

async def handle_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /settings command."""
    chat_id = update.effective_chat.id
    if not is_user_allowed(chat_id):
        return
        
    await context.bot.send_message(
        chat_id=chat_id,
        text="⚙️ **Bot Settings**\nConfigure your default download preferences:",
        reply_markup=get_settings_keyboard(chat_id),
        parse_mode='Markdown'
    )

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle settings button clicks."""
    query = update.callback_query
    chat_id = query.message.chat_id
    data = query.data
    
    if data == "set_mode":
        current = get_user_settings(chat_id).get('download_mode', 'video')
        new_mode = 'audio' if current == 'video' else 'video'
        update_user_settings(chat_id, download_mode=new_mode)
        await query.answer(f"Mode changed to {new_mode}")
    
    elif data.startswith("set_res:"):
        new_res = int(data.split(':')[1])
        update_user_settings(chat_id, resolution=new_res)
        await query.answer(f"Resolution set to {new_res}p")
    
    else:
        return
        
    # Update the keyboard
    try:
        await query.edit_message_reply_markup(reply_markup=get_settings_keyboard(chat_id))
    except Exception:
        pass

# --- Callback Handlers ---
async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle cancel button callback."""
    query = update.callback_query
    await query.answer("Cancelling...")
    
    data = query.data
    if not data.startswith("cancel:"):
        return
    
    task_id = data[7:]
    cancelled_tasks.add(task_id)
    
    try:
        await query.edit_message_text("⏳ Cancelling download...")
    except Exception as e:
        logger.warning(f"Could not edit cancel message: {e}")

async def audio_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Audio download button callback."""
    query = update.callback_query
    await query.answer()
    
    chat_id = query.message.chat_id
    
    if not is_user_allowed(chat_id):
        await query.edit_message_reply_markup(reply_markup=None)
        return
    
    data = query.data
    if not data.startswith("audio:"):
        return
    
    url = data[6:]
    
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🎵 Converting to Audio...", callback_data="noop")]
    ]))
    
    try:
        loop = asyncio.get_running_loop()
        status_msg = await context.bot.send_message(chat_id=chat_id, text="🎵 Downloading audio...")
        
        last_progress_update = 0
        def progress_cb(d):
            nonlocal last_progress_update
            if d['status'] == 'downloading':
                now = time.time()
                if now - last_progress_update < 20:
                    return
                p = d.get('_percent_str', '0%')
                asyncio.run_coroutine_threadsafe(
                    status_msg.edit_text(f"🎵 Downloading audio: {p}"),
                    loop
                )
                last_progress_update = now
        
        file_path, title, video_id = await loop.run_in_executor(None, lambda: download_content(url, progress_cb, audio_only=True))
        
        await status_msg.edit_text("⬆️ Uploading audio...")
        
        config = load_config()
        api_url = config.get('api_url', '')
        bot_token = config.get('bot_token', '')
        is_local_api = api_url and 'api.telegram.org' not in api_url
        
        if is_local_api:
            await upload_audio_streaming(bot_token, api_url, chat_id, file_path, title, f"🎵 {title}")
        else:
            with open(file_path, 'rb') as f:
                await context.bot.send_audio(chat_id=chat_id, audio=f, title=title, caption=f"🎵 {title}")
        
        if os.path.exists(file_path):
            os.remove(file_path)
        
        await status_msg.edit_text("✅ Audio sent!")
        await query.edit_message_reply_markup(reply_markup=None)
        
    except Exception as e:
        logger.error(f"Audio conversion failed: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Audio download failed: {e}")
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎵 Download Audio (Retry)", callback_data=f"audio:{url}")]
        ]))
    finally:
        gc.collect()
