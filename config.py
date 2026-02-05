"""
Configuration module for YTDL Telegram Bot.
Handles environment variables and settings.
"""

import os
import subprocess
import logging

logger = logging.getLogger(__name__)

# Directories
DOWNLOAD_DIR = './downloads'

# Telegram file size limits
STANDARD_API_LIMIT = 50 * 1024 * 1024 - 1024 * 1024  # 49MB
LOCAL_API_LIMIT = 2000 * 1024 * 1024 - 1024 * 1024 * 50  # ~1.95GB

def load_config():
    """Load configuration from environment variables."""
    return {
        'bot_token': os.getenv('BOT_TOKEN'),
        'ffmpeg_path': "/usr/bin/ffmpeg",
        'api_url': os.getenv('API_URL', 'https://api.telegram.org/bot'),
        'proxy': os.getenv('PROXY'),
        'proxy_list': os.getenv('PROXY_LIST', ''),
        'allowed_chat_ids': os.getenv('ALLOWED_CHAT_IDS', ''),
        'max_disk_gb': float(os.getenv('MAX_DISK_GB', '0')),
        'subscription_check_interval': int(os.getenv('SUBSCRIPTION_CHECK_INTERVAL', '300')),  # 5 minutes
    }

def get_ffmpeg_command():
    """Get FFmpeg command path."""
    config = load_config()
    return config.get('ffmpeg_path', '/usr/bin/ffmpeg')

def check_ffmpeg():
    """Check if FFmpeg is available."""
    try:
        result = subprocess.run([get_ffmpeg_command(), '-version'], 
                                capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except:
        return False

def get_proxy_list():
    """Get list of proxies for rotation."""
    config = load_config()
    proxy_list = []
    
    # Add single PROXY first if exists
    single_proxy = config.get('proxy', '')
    if single_proxy:
        proxy_list.append(single_proxy)
    
    # Add PROXY_LIST proxies
    proxy_list_str = config.get('proxy_list', '')
    if proxy_list_str:
        for p in proxy_list_str.split(','):
            p = p.strip()
            if p and p not in proxy_list:
                proxy_list.append(p)
    
    return proxy_list if proxy_list else [None]

def is_user_allowed(chat_id):
    """Check if user is allowed to use the bot."""
    config = load_config()
    allowed_ids = config.get('allowed_chat_ids', '')
    if not allowed_ids:
        return True
    
    try:
        allowed_list = [int(x.strip()) for x in allowed_ids.split(',') if x.strip()]
        return chat_id in allowed_list
    except ValueError:
        logger.error("Invalid ALLOWED_CHAT_IDS format. Allowing all.")
        return True

def get_downloads_size_gb():
    """Get current size of downloads folder in GB."""
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(DOWNLOAD_DIR):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.exists(fp):
                total_size += os.path.getsize(fp)
    return total_size / (1024 * 1024 * 1024)

def check_disk_space(estimated_size_mb):
    """Check if we have enough disk space for the download."""
    config = load_config()
    max_disk_gb = config.get('max_disk_gb', 0)
    
    if max_disk_gb <= 0:
        return True, 0
    
    current_usage_gb = get_downloads_size_gb()
    estimated_gb = estimated_size_mb / 1024
    projected_usage = current_usage_gb + estimated_gb
    
    if projected_usage > max_disk_gb:
        return False, max_disk_gb - current_usage_gb
    return True, max_disk_gb - current_usage_gb

# Ensure download directory exists
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

def clear_downloads():
    """Wipe all files from the downloads directory."""
    try:
        if os.path.exists(DOWNLOAD_DIR):
            import shutil
            for filename in os.listdir(DOWNLOAD_DIR):
                file_path = os.path.join(DOWNLOAD_DIR, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    logger.warning(f"Failed to delete {file_path}: {e}")
            logger.info("Downloads directory cleared.")
    except Exception as e:
        logger.error(f"Error clearing downloads: {e}")
