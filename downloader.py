"""
Downloader module for YTDL Telegram Bot.
Handles yt-dlp downloads with proxy rotation.
"""

import os
import logging
import yt_dlp

from config import DOWNLOAD_DIR, get_proxy_list, load_config, get_ffmpeg_command, get_cookie_file

logger = logging.getLogger(__name__)

# Ensure deno is always in PATH for EJS n-parameter solving
if '/opt/deno/bin' not in os.environ.get('PATH', ''):
    os.environ['PATH'] = '/opt/deno/bin:' + os.environ.get('PATH', '')

import urllib.request
import urllib.error
import time

# --- Errors that trigger proxy rotation ---
RETRY_ERRORS = [
    'Video unavailable',
    'is not available in your country',
    'not made this video available in your country',
    'available in your country',
    'geo',
    'blocked',
    'This video is not available',
    'content is not available',
    'Sign in to confirm your age',
    'Private video',
    'removed by the uploader',
    'uploader has not made this video available',
    'country',
    'bot',
    'cookies are required',
    'Sign in to confirm you\'re not a bot',
    '403',
    'timed out',
    'timeout',
    'Connection refused',
    'Unable to download',
    'Requested format is not available',
]

def restart_warp_proxy():
    """Trigger the WARP proxy to rotate its IP."""
    logger.info("Attempting to rotate WARP IP via warp-proxy:9090...")
    try:
        req = urllib.request.Request("http://warp-proxy:9090/restart")
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                logger.info("WARP IP rotation triggered successfully. Waiting 10s for reconnect...")
                time.sleep(10)
                return True
    except Exception as e:
        logger.warning(f"Failed to trigger WARP restart: {e}")
    return False

def is_retryable_error(error_msg, proxy_url=None):
    """Check if error should trigger proxy rotation. Restarts WARP on any failure through warp-proxy."""
    error_lower = str(error_msg).lower()
    matched = any(pattern.lower() in error_lower for pattern in RETRY_ERRORS)
    if matched:
        logger.info(f"Retryable error matched: {error_msg[:100]}")
        if proxy_url and 'warp-proxy' in proxy_url:
            restart_warp_proxy()
    return matched

_cookie_logged = False

def _apply_cookie(ydl_opts):
    """Inject cookiefile, EJS runtime, and common options into ydl_opts."""
    global _cookie_logged
    cookie_file = get_cookie_file()
    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file
        if not _cookie_logged:
            logger.info("Cookie in use: %s", cookie_file)
            _cookie_logged = True
    else:
        if not _cookie_logged:
            logger.info("No cookie file found, proceeding without cookies")
            _cookie_logged = True
    # Always enable EJS for n-parameter solving (needed regardless of cookies)
    ydl_opts['remote_components'] = ['ejs:github']
    ydl_opts['js_runtimes'] = 'deno:/opt/deno/bin/deno'
    ydl_opts['quiet'] = False
    ydl_opts['no_warnings'] = False
    return ydl_opts

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
        _apply_cookie(ydl_opts)

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
            if is_retryable_error(str(e)):
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
        _apply_cookie(ydl_opts)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(channel_url, download=False)
                return {
                    'channel_id': info.get('channel_id') or info.get('id', ''),
                    'channel_name': info.get('channel') or info.get('uploader') or info.get('title', 'Unknown'),
                    'url': info.get('webpage_url', channel_url),
                }
        except Exception as e:
            if is_retryable_error(str(e)):
                continue
            raise e
    
    raise Exception("Could not extract channel info")

def get_latest_videos(channel_id, limit=5):
    """Get latest videos from a channel. Merges results across proxies to catch geo-restricted ones."""
    proxy_list = get_proxy_list()
    channel_url = f"https://www.youtube.com/channel/{channel_id}/videos"
    all_videos = {}

    for proxy in proxy_list:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'playlistend': limit,
            'socket_timeout': 10,
            'retries': 2,
            'nocheckcertificate': True,
            'no_color': True,
        }
        if proxy:
            ydl_opts['proxy'] = proxy
        _apply_cookie(ydl_opts)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(channel_url, download=False)
                for entry in info.get('entries', []):
                    if entry and entry.get('id'):
                        vid_id = entry['id']
                        if vid_id not in all_videos:
                            all_videos[vid_id] = {
                                'id': vid_id,
                                'title': entry.get('title', 'Unknown'),
                                'url': entry.get('url') or f"https://www.youtube.com/watch?v={vid_id}",
                            }
        except Exception as e:
            if is_retryable_error(str(e), proxy):
                continue
            logger.warning(f"Failed to get latest videos via proxy={proxy}: {e}")
            continue

    return list(all_videos.values())[:limit]

# --- Main Download Function ---
def download_content(url, progress_callback=None, audio_only=False, audio_format='m4a', max_height=1080, task_id=None, cancelled_tasks=None):
    """Download content with proxy rotation on geo-restriction errors."""
    proxy_list = get_proxy_list()
    last_error = None
    
    # Track actual downloaded filename from progress hook
    actual_downloaded = [None]
    
    def progress_adapter(d):
        if task_id and cancelled_tasks and task_id in cancelled_tasks:
            raise Exception("Download cancelled by user")
        if d.get('status') == 'finished':
            actual_downloaded[0] = d.get('filename')
        if progress_callback:
            progress_callback(d)

    for proxy_idx, proxy in enumerate(proxy_list):
        proxy_label = proxy if proxy else "Direct (no proxy)"
        logger.info(f"Attempting download with proxy [{proxy_idx+1}/{len(proxy_list)}]: {proxy_label}")
        actual_downloaded[0] = None  # Reset for each proxy attempt
        
        if audio_only:
            ydl_opts = {
                'outtmpl': f'{DOWNLOAD_DIR}/%(title).80s [%(id)s].%(ext)s',
                'format': 'bestaudio/best',
                'postprocessors': [
                    {
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': audio_format,
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
                'outtmpl': f'{DOWNLOAD_DIR}/%(title).80s [%(id)s].%(ext)s',
                'format': f'bestvideo[height<={max_height}][vcodec!~=av01]+bestaudio/best[height<={max_height}]/best',
                'merge_output_format': 'mp4',
                'noplaylist': True,
                'quiet': True,
                'ffmpeg_location': get_ffmpeg_command(),
                'writethumbnail': True,
                'overwrites': True,
                'buffer_size': 1024 * 8,
                'http_chunk_size': 2097152,
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
        _apply_cookie(ydl_opts)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                
                # Handle audio extension
                if audio_only:
                    base, _ = os.path.splitext(filename)
                    filename = base + f'.{audio_format}'
                else:
                    if not os.path.exists(filename):
                        base, _ = os.path.splitext(filename)
                        for ext in ['.mp4', '.mkv', '.webm']:
                            if os.path.exists(base + ext):
                                filename = base + ext
                                break
                
                # Fallback: if file doesn't exist (e.g. album URL where
                # prepare_filename returns the album name instead of the
                # actual track name), use the filename captured from
                # the progress hook.
                if not os.path.exists(filename) and actual_downloaded[0]:
                    hook_base, _ = os.path.splitext(actual_downloaded[0])
                    if audio_only:
                        alt = hook_base + f'.{audio_format}'
                    else:
                        alt = None
                        for ext in ['.mp4', '.mkv', '.webm']:
                            if os.path.exists(hook_base + ext):
                                alt = hook_base + ext
                                break
                        if not alt:
                            alt = actual_downloaded[0]
                    if os.path.exists(alt):
                        logger.info(f"prepare_filename mismatch, using actual file: {alt}")
                        filename = alt
                
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
            last_error = str(e)
            
            if is_retryable_error(last_error, proxy):
                # If geo-restricted or bot detected, try next proxy
                if progress_callback:
                    progress_callback({'status': 'geo_err', 'msg': f'Blocked by YouTube using proxy {proxy or "Direct"}. Trying next.'})
                continue
            else:
                logger.error(f"Non-geo error, stopping retry: {last_error}")
                raise e
    
    if len(proxy_list) == 1 and proxy_list[0] is None:
        if is_retryable_error(str(last_error)):
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
        _apply_cookie(ydl_opts)

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
            if is_retryable_error(str(e), proxy):
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
        _apply_cookie(ydl_opts)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                # Check if it has entries (playlist)
                # ie_key is usually present for nested items, we want the top level to be a playlist
                return 'entries' in info and bool(info.get('entries'))
        except Exception as e:
            if is_retryable_error(str(e), proxy):
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
        _apply_cookie(ydl_opts)
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
    """Get the direct stream URL (HLS/Dash) and the proxy used."""
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
        _apply_cookie(ydl_opts)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info.get('url'), proxy
        except:
            continue
    return None, None
