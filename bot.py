import os
import json
import logging
import asyncio
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
import yt_dlp
import gc

# --- Configuration & Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# Suppress httpx logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

CONFIG_FILE = 'config.json'
DOWNLOAD_DIR = 'downloads'
MAX_RETRIES = 3

# Default limit for standard Telegram API is 50MB
# For Local API Server, it can be up to 2000MB
STANDARD_API_LIMIT = 50 * 1024 * 1024 - 1024 * 1024  # 49MB (buffer)
LOCAL_API_LIMIT = 2000 * 1024 * 1024 - 1024 * 1024 * 50 # ~1.95GB

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

def load_config():
    return {
        'bot_token': os.getenv('BOT_TOKEN'),
        'ffmpeg_path': "/usr/bin/ffmpeg",
        'api_url': os.getenv('API_URL', 'https://api.telegram.org/bot'),
        'proxy': os.getenv('PROXY'),
        'allowed_chat_ids': os.getenv('ALLOWED_CHAT_IDS', '')
    }

def is_user_allowed(chat_id):
    config = load_config()
    allowed_ids = config.get('allowed_chat_ids', '')
    if not allowed_ids:
        return True # Empty list means allow all
    
    try:
        # Parse "123, 456" into [123, 456]
        allowed_list = [int(x.strip()) for x in allowed_ids.split(',') if x.strip()]
        return chat_id in allowed_list
    except ValueError:
        logger.error("Invalid ALLOWED_CHAT_IDS format. Allowing all.")
        return True


# --- FFmpeg Helper ---
def get_ffmpeg_command():
    config = load_config()
    # Return configured path or default 'ffmpeg'
    return config.get('ffmpeg_path', 'ffmpeg')

def check_ffmpeg():
    cmd = get_ffmpeg_command()
    try:
        subprocess.run([cmd, '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False

def get_video_duration(file_path):
    try:
        # We need ffprobe too. Usually in same dir as ffmpeg.
        ffmpeg_cmd = get_ffmpeg_command()
        ffprobe_cmd = 'ffprobe'
        
        if os.path.isabs(ffmpeg_cmd):
             # If ffmpeg is a full path, try to find ffprobe in the same specific directory
            bin_dir = os.path.dirname(ffmpeg_cmd)
            candidate = os.path.join(bin_dir, 'ffprobe.exe')
            if os.path.exists(candidate):
                ffprobe_cmd = candidate
        
        cmd = [
            ffprobe_cmd, 
            '-v', 'error', 
            '-show_entries', 'format=duration', 
            '-of', 'default=noprint_wrappers=1:nokey=1', 
            file_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return float(result.stdout)
    except Exception as e:
        logger.error(f"Error getting duration: {e}")
        return None

def split_video(file_path):
    """Splits video into chunks based on API limit."""
    config = load_config()
    api_url = config.get('api_url', '')
    
    # Determine max size based on API URL
    if 'api.telegram.org' in api_url or not api_url:
        max_size = STANDARD_API_LIMIT
        logger.info("Using Standard API limit (50MB)")
    else:
        max_size = LOCAL_API_LIMIT
        logger.info("Using Local API limit (2GB)")

    file_size = os.path.getsize(file_path)
    if file_size <= max_size:
        return [file_path]
    
    logger.info(f"File {file_path} is too large ({file_size} bytes). Splitting...")
    
    duration = get_video_duration(file_path)
    if not duration:
        logger.error("Could not determine video duration for splitting.")
        return [file_path]

    target_size = max_size * 0.95 # 95% of max to be safe
    parts = int(file_size // target_size) + 1
    segment_time = int(duration / parts)
    
    # Ensure segment time is at least 1 second
    if segment_time < 1:
        segment_time = 1
    
    base_name, ext = os.path.splitext(file_path)
    output_pattern = f"{base_name}_part%03d{ext}"
    
    # FFmpeg split command
    ffmpeg_cmd = get_ffmpeg_command()
    cmd = [
        ffmpeg_cmd, '-i', file_path, 
        '-c', 'copy', 
        '-map', '0', 
        '-segment_time', str(segment_time), 
        '-f', 'segment', 
        '-reset_timestamps', '1', 
        output_pattern
    ]
    
    try:
        subprocess.run(cmd, check=True)
        
        # Find generated files
        split_files = []
        directory = os.path.dirname(file_path)
        base_filename = os.path.basename(base_name)
        
        for f in os.listdir(directory):
            if f.startswith(base_filename + "_part") and f.endswith(ext):
                split_files.append(os.path.join(directory, f))
        
        split_files.sort()
        return split_files
        
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg split failed: {e}")
        return [file_path]

# --- Download Logic ---
def download_progress_hook(d, status_msg, loop):
    """Updates the status message with download progress."""
    if d['status'] == 'downloading':
        try:
            p = d.get('_percent_str', '0%').replace('%','')
            # Update every 10% or so to avoid spamming API
            # This is a basic check; robust implementations use time-based checks.
            pass 
        except Exception:
            pass
    elif d['status'] == 'finished':
        pass

def download_content(url, progress_callback=None):
    config = load_config()
    proxy = config.get('proxy')

    # Progress hook wrapper
    def progress_adapter(d):
        if progress_callback:
            progress_callback(d)

    ydl_opts = {
        'outtmpl': f'{DOWNLOAD_DIR}/%(title)s [%(id)s].%(ext)s',
        'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'ffmpeg_location': get_ffmpeg_command(),
        'writethumbnail': False,
        'overwrites': True,
        'buffer_size': 1024 * 16,
        'http_chunk_size': 10485760,
        'progress_hooks': [progress_adapter],
    }
    
    if proxy:
        ydl_opts['proxy'] = proxy
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            # Check if merged file exists (sometimes extension changes)
            if not os.path.exists(filename):
                base, _ = os.path.splitext(filename)
                # potentially it became .mkv or .mp4 depending on merge
                # Let's verify commonly used containers
                for ext in ['.mp4', '.mkv', '.webm']:
                    if os.path.exists(base + ext):
                        filename = base + ext
                        break
            return filename, info.get('title', 'video')
        except Exception as e:
            logger.error(f"Download error: {e}")
            raise e

# --- Queue System ---
request_queue = asyncio.Queue()

async def process_queue(application):
    logger.info("Queue processor started.")
    while True:
        task = await request_queue.get()
        chat_id, url, message_id = task
        
        # Status message management
        status_msg = None
        last_edit_time = 0
        
        async def update_status(text, force=False):
            nonlocal status_msg, last_edit_time
            now = time.time()
            # Rate limit edits: max 1 per 3 seconds unless forced
            if not force and (now - last_edit_time < 3):
                return

            try:
                if status_msg:
                    if status_msg.text != text:
                        await status_msg.edit_text(text)
                        last_edit_time = now
                else:
                    status_msg = await application.bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=message_id)
                    last_edit_time = now
            except Exception as e:
                logger.warning(f"Failed to update status: {e}")

        try:
            await update_status(f"🚀 Processing: {url}", force=True)
            
            # Progress Callback for yt-dlp
            loop = asyncio.get_running_loop()
            
            def progress_cb(d):
                if d['status'] == 'downloading':
                    p = d.get('_percent_str', '0%')
                    eta = d.get('_eta_str', '?')
                    # We use run_coroutine_threadsafe to update from the sync hook
                    asyncio.run_coroutine_threadsafe(
                        update_status(f"⬇️ Downloading: {p} (ETA: {eta})"),
                        loop
                    )
                elif d['status'] == 'finished':
                     asyncio.run_coroutine_threadsafe(
                        update_status("✅ Download complete. Processing..."),
                        loop
                    )

            # Download
            try:
                # Use functools.partial to pass the callback if needed, but here we define wrapper in download_content
                # Actually, we need to pass the callback function
                file_path, title = await loop.run_in_executor(None, lambda: download_content(url, progress_cb))
            except Exception as e:
                await update_status(f"❌ Download failed: {str(e)}", force=True)
                continue
            
            # Split
            await update_status("✂️ Checking file size...", force=True)
            if not check_ffmpeg():
                 await update_status("⚠️ FFmpeg not found. Uploading original...", force=True)
                 files_to_upload = [file_path]
            else:
                 files_to_upload = await loop.run_in_executor(None, split_video, file_path)
            
            # Upload
            total_parts = len(files_to_upload)
            for i, f_path in enumerate(files_to_upload):
                caption = title
                if total_parts > 1:
                    caption += f" (Part {i+1}/{total_parts})"
                
                await update_status(f"⬆️ Uploading part {i+1}/{total_parts}...", force=True)
                
                try:
                    # RAM OPTIMIZATION:
                    # If using Local API Server, we can pass 'file://<path>' to avoid loading into RAM.
                    # We check config for api_url and if it contains 'localhost' or 'host.docker.internal'
                    # THIS IS A GUESS, but standard python-telegram-bot supports pathlib.Path or file object.
                    # To strictly stream, we use open(..., 'rb')
                    
                    # However, users reported 1GB RAM usage. 
                    # If using Local Bot API, the efficient way is passing a string path if the server has access to the file.
                    # Since we are in Docker and likely sharing volumes, we can try.
                    
                    config = load_config()
                    api_url = config.get('api_url', '')
                    
                    # If we suspect Local API, try to reduce overhead
                    # For now, we continue using file object but rely on gc
                    
                    with open(f_path, 'rb') as f:
                        await application.bot.send_video(
                            chat_id=chat_id, 
                            video=f, 
                            caption=caption, 
                            supports_streaming=True,
                            read_timeout=300, 
                            write_timeout=300, 
                            pool_timeout=300
                        )
                except Exception as e:
                    logger.error(f"Upload failed: {e}")
                    await application.bot.send_message(chat_id=chat_id, text=f"❌ Upload failed for part {i+1}: {e}", reply_to_message_id=message_id)
            
            # Cleanup
            await update_status("🧹 Cleaning up...", force=True)
            try:
                if os.path.exists(file_path):
                    os.remove(file_path) 
                for f_path in files_to_upload:
                    if os.path.exists(f_path) and f_path != file_path:
                        os.remove(f_path)
            except Exception as e:
                logger.error(f"Cleanup failed: {e}")

            await update_status("✨ Task completed!", force=True)

        except Exception as e:
            logger.error(f"Unexpected error in worker: {e}")
            if status_msg:
                 await status_msg.edit_text(f"🔥 System error: {e}")
            else:
                 await application.bot.send_message(chat_id=chat_id, text=f"🔥 System error: {e}", reply_to_message_id=message_id)
        
        finally:
            request_queue.task_done()
            gc.collect() 

# --- Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_user_allowed(chat_id):
        await context.bot.send_message(chat_id=chat_id, text="Sorry, you are not authorized to use this bot.")
        return
    await context.bot.send_message(chat_id=chat_id, text="Hi! Send me a link and I'll download it for you.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    
    if not is_user_allowed(chat_id):
        await context.bot.send_message(chat_id=chat_id, text="Sorry, you are not authorized to use this bot.", reply_to_message_id=message_id)
        return

    url = update.message.text
    
    # Basic URL validation
    if not url.startswith(('http://', 'https://')):
        await context.bot.send_message(chat_id=chat_id, text="Please send a valid URL.", reply_to_message_id=message_id)
        return

    await request_queue.put((chat_id, url, message_id))
    
    # qsize is approximate, but gives a hint
    q_size = request_queue.qsize()
    # If q_size is 1, it might be picked up immediately, so we can say "Processing..." or "Queued (Position 1)"
    # But since process_queue is async, it might have already picked it up if it was idle.
    # To be safe, we just say added.
    await context.bot.send_message(chat_id=chat_id, text=f"Added to queue. Current Queue Depth: {q_size}", reply_to_message_id=message_id)

if __name__ == '__main__':
    config = load_config()
    if not config.get('bot_token') or config.get('bot_token') == 'YOUR_BOT_TOKEN_HERE':
        print("ERROR: BOT_TOKEN is missing. Set it via environment variable.")
        exit(1)
    
    api_url = config.get('api_url', 'https://api.telegram.org/bot')
    # ApplicationBuilder uses base_url (default is https://api.telegram.org/bot)
    
    builder = ApplicationBuilder().token(config['bot_token'])
    if api_url:
        builder.base_url(api_url)
        print(f"Using API Server: {api_url}")
        
    application = builder.build()
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    # Run queue processor as a background task
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # We use a slight hack to run the queue processor alongside the bot
    # but application.run_polling is blocking. 
    # Better to use job_queue or just start the loop task before run_polling if using async context, 
    # but run_polling manages the loop.
    # The clean way in v20+ is creating a task in post_init.
    
    async def post_init(app):
        asyncio.create_task(process_queue(app))

    application.post_init = post_init
    
    print("Bot is running...")
    if not check_ffmpeg():
        print("WARNING: FFmpeg not detected! >2GB splitting will not work.")
        
    application.run_polling()
