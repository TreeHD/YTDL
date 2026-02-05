"""
Downloader module for YTDL Telegram Bot.
Handles yt-dlp downloads with proxy rotation.
"""

import os
import logging
import yt_dlp

from config import DOWNLOAD_DIR, get_proxy_list, load_config, get_ffmpeg_command

logger = logging.getLogger(__name__)

# --- Geo-restriction Detection ---
GEO_RESTRICTION_ERRORS = [
    'Video unavailable',
    'is not available in your country',
    'not made this video available in your country',
    'available in your country',
    'geo',
    'blocked',
    'not available',
    'Sign in to confirm your age',
    'This video is not available',
    'Private video',
    'removed by the uploader',
    'uploader has not made this video available',
    'country',
]

def is_geo_restricted_error(error_msg):
    """Check if error is geo-restriction related."""
    error_lower = str(error_msg).lower()
    matched = any(pattern.lower() in error_lower for pattern in GEO_RESTRICTION_ERRORS)
    if matched:
        logger.info(f"Geo-restriction pattern matched in error: {error_msg[:100]}")
    return matched

# --- Video Info Extraction ---
def get_video_info(url):
    """Extract video info without downloading to check file size."""
    proxy_list = get_proxy_list()
    
    for proxy in proxy_list:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'socket_timeout': 10,
            'retries': 2,
            'nocheckcertificate': True,
        }
        if proxy:
            ydl_opts['proxy'] = proxy
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                filesize = info.get('filesize') or info.get('filesize_approx') or 0
                
                if not filesize and info.get('formats'):
                    for fmt in info.get('formats', []):
                        if fmt.get('filesize'):
                            filesize = max(filesize, fmt.get('filesize', 0))
                        elif fmt.get('filesize_approx'):
                            filesize = max(filesize, fmt.get('filesize_approx', 0))
                
                return {
                    'title': info.get('title', 'Unknown'),
                    'id': info.get('id', ''),
                    'duration': info.get('duration', 0),
                    'filesize_mb': filesize / (1024 * 1024) if filesize else 0,
                    'uploader': info.get('uploader', 'Unknown'),
                    'channel_id': info.get('channel_id', ''),
                    'is_live': info.get('is_live', False),
                }
        except Exception as e:
            if is_geo_restricted_error(str(e)):
                continue
            raise e
    
    raise Exception("Could not extract video info")

def get_channel_info(channel_url):
    """Get channel information including ID and name."""
    proxy_list = get_proxy_list()
    
    for proxy in proxy_list:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'playlist_items': '0',
            'socket_timeout': 10,
            'retries': 2,
            'nocheckcertificate': True,
            'no_color': True,
        }
        if proxy:
            ydl_opts['proxy'] = proxy
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(channel_url, download=False)
                return {
                    'channel_id': info.get('channel_id') or info.get('id', ''),
                    'channel_name': info.get('channel') or info.get('uploader') or info.get('title', 'Unknown'),
                    'url': info.get('webpage_url', channel_url),
                }
        except Exception as e:
            if is_geo_restricted_error(str(e)):
                continue
            raise e
    
    raise Exception("Could not extract channel info")

def get_latest_videos(channel_id, limit=5):
    """Get latest videos from a channel."""
    proxy_list = get_proxy_list()
    channel_url = f"https://www.youtube.com/channel/{channel_id}/videos"
    
    for proxy in proxy_list:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'playlist_items': f'1-{limit}',
            'socket_timeout': 10,
            'retries': 2,
            'nocheckcertificate': True,
            'no_color': True,
        }
        if proxy:
            ydl_opts['proxy'] = proxy
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(channel_url, download=False)
                videos = []
                for entry in info.get('entries', []):
                    if entry:
                        videos.append({
                            'id': entry.get('id', ''),
                            'title': entry.get('title', 'Unknown'),
                            'url': entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}",
                        })
                return videos
        except Exception as e:
            if is_geo_restricted_error(str(e)):
                continue
            logger.warning(f"Failed to get latest videos: {e}")
            return []
    
    return []

# --- Main Download Function ---
def download_content(url, progress_callback=None, audio_only=False, max_height=1080, task_id=None, cancelled_tasks=None):
    """Download content with proxy rotation on geo-restriction errors."""
    proxy_list = get_proxy_list()
    last_error = None
    
    def progress_adapter(d):
        if task_id and cancelled_tasks and task_id in cancelled_tasks:
            raise Exception("Download cancelled by user")
        if progress_callback:
            progress_callback(d)

    for proxy_idx, proxy in enumerate(proxy_list):
        proxy_label = proxy if proxy else "Direct (no proxy)"
        logger.info(f"Attempting download with proxy [{proxy_idx+1}/{len(proxy_list)}]: {proxy_label}")
        
        if audio_only:
            ydl_opts = {
                'outtmpl': f'{DOWNLOAD_DIR}/%(title)s [%(id)s].%(ext)s',
                'format': 'bestaudio/best',
                'postprocessors': [
                    {
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'm4a',
                        'preferredquality': '192',
                    },
                    {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg', 'when': 'before_dl'},
                ],
                'noplaylist': True,
                'quiet': True,
                'ffmpeg_location': get_ffmpeg_command(),
                'writethumbnail': True,
                'overwrites': True,
                'progress_hooks': [progress_adapter],
                'socket_timeout': 30,
                'retries': 3,
                'nocheckcertificate': True,
            }
        else:
            ydl_opts = {
                'outtmpl': f'{DOWNLOAD_DIR}/%(title)s [%(id)s].%(ext)s',
                'format': f'bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]',
                'merge_output_format': 'mp4',
                'noplaylist': True,
                'quiet': True,
                'ffmpeg_location': get_ffmpeg_command(),
                'writethumbnail': True,
                'overwrites': True,
                'buffer_size': 1024 * 16,
                'http_chunk_size': 10485760,
                'progress_hooks': [progress_adapter],
                'postprocessors': [
                    {'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'},
                    {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg', 'when': 'before_dl'},
                    {'key': 'FFmpegExtractAudio', 'nopostoverwrites': False} if audio_only else {'key': 'FFmpegMetadata'}
                ],
                'socket_timeout': 30,
                'retries': 3,
                'nocheckcertificate': True,
            }
        
        if proxy:
            ydl_opts['proxy'] = proxy
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                
                # Handle audio extension
                if audio_only:
                    base, _ = os.path.splitext(filename)
                    filename = base + '.m4a'
                else:
                    if not os.path.exists(filename):
                        base, _ = os.path.splitext(filename)
                        for ext in ['.mp4', '.mkv', '.webm']:
                            if os.path.exists(base + ext):
                                filename = base + ext
                                break
                
                # Handle thumbnail path
                thumb_path = None
                base_no_ext, _ = os.path.splitext(filename)
                # yt-dlp often creates .jpg or .webp
                for t_ext in ['.jpg', '.jpeg', '.webp']:
                    potential_thumb = base_no_ext + t_ext
                    if os.path.exists(potential_thumb):
                        thumb_path = potential_thumb
                        break
                
                logger.info(f"Download successful: {filename} (Thumb: {thumb_path})")
                return filename, info.get('title', 'video'), info.get('id', ''), thumb_path
                
        except Exception as e:
            last_error = e
            error_msg = str(e)
            logger.warning(f"Download failed with proxy {proxy_label}: {error_msg}")
            
            if is_geo_restricted_error(error_msg):
                logger.info(f"Geo-restriction detected. Trying next proxy...")
                continue
            else:
                logger.error(f"Non-geo error, stopping retry: {error_msg}")
                raise e
    
    if len(proxy_list) == 1 and proxy_list[0] is None:
        if is_geo_restricted_error(str(last_error)):
            raise Exception(f"Geo-restricted video. Please configure PROXY or PROXY_LIST in .env")
        raise Exception(f"Download failed: {last_error}")
    else:
        raise Exception(f"All {len(proxy_list)} proxies failed. Last error: {last_error}")

# --- Playlist Info ---
def get_playlist_info(url):
    """Get playlist information without downloading."""
    proxy_list = get_proxy_list()
    
    for proxy in proxy_list:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'socket_timeout': 10,
            'retries': 2,
            'nocheckcertificate': True,
            'no_color': True,
        }
        if proxy:
            ydl_opts['proxy'] = proxy
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                entries = info.get('entries', [])
                return {
                    'title': info.get('title', 'Playlist'),
                    'id': info.get('id', ''),
                    'count': len(entries) if entries else 0,
                    'uploader': info.get('uploader', 'Unknown'),
                    'entries': [{'url': e.get('url') or f"https://www.youtube.com/watch?v={e.get('id')}", 'title': e.get('title', 'Unknown')} for e in entries if e]
                }
        except Exception as e:
            if is_geo_restricted_error(str(e)):
                continue
            raise e
    
    raise Exception("Could not extract playlist info")

def is_playlist(url):
    """Check if URL is a playlist or a single video."""
    proxy_list = get_proxy_list()
    
    for proxy in proxy_list:
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'no_warnings': True,
        }
        if proxy:
            ydl_opts['proxy'] = proxy
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                # Check if it has entries (playlist)
                # ie_key is usually present for nested items, we want the top level to be a playlist
                return 'entries' in info and bool(info.get('entries'))
        except Exception as e:
            if is_geo_restricted_error(str(e)):
                continue
            return False
    return False

def get_live_info(channel_id):
    """Check if a channel is currently live and get video info."""
    proxy_list = get_proxy_list()
    live_url = f"https://www.youtube.com/channel/{channel_id}/live"
    
    for proxy in proxy_list:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'socket_timeout': 10,
            'retries': 1,
            'nocheckcertificate': True,
        }
        if proxy:
            ydl_opts['proxy'] = proxy
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(live_url, download=False)
                if info.get('is_live'):
                    return {
                        'id': info.get('id', ''),
                        'title': info.get('title', 'Live Stream'),
                        'url': info.get('webpage_url', live_url),
                        'uploader': info.get('uploader', 'Unknown'),
                        'is_live': True
                    }
        except:
            continue
    return None

def get_stream_url(url):
    """Get the direct stream URL (HLS/Dash)."""
    proxy_list = get_proxy_list()
    for proxy in proxy_list:
        ydl_opts = {
            'quiet': True,
            'format': 'best',
            'socket_timeout': 15,
            'nocheckcertificate': True,
        }
        if proxy:
            ydl_opts['proxy'] = proxy
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info.get('url')
        except:
            continue
    return None
