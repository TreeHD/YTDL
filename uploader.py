"""
Uploader module for YTDL Telegram Bot.
Handles Telegram file uploads with streaming support.
"""

import os
import json
import logging
import subprocess
import aiohttp
import aiofiles

from config import load_config, LOCAL_API_LIMIT, get_ffmpeg_command, check_ffmpeg

logger = logging.getLogger(__name__)

# --- Video Splitting ---
def split_video(file_path, max_size_bytes=None):
    """Split video into parts if it exceeds max size."""
    config = load_config()
    api_url = config.get('api_url', '')
    
    if max_size_bytes is None:
        if api_url and 'api.telegram.org' not in api_url:
            max_size_bytes = LOCAL_API_LIMIT
        else:
            from config import STANDARD_API_LIMIT
            max_size_bytes = STANDARD_API_LIMIT
    
    file_size = os.path.getsize(file_path)
    
    if file_size <= max_size_bytes:
        return [file_path]
    
    logger.info(f"File {file_path} is {file_size / 1024 / 1024:.2f} MB, splitting...")
    
    num_parts = (file_size // max_size_bytes) + 1
    duration_result = subprocess.run(
        [get_ffmpeg_command(), '-i', file_path, '-hide_banner'],
        capture_output=True, text=True
    )
    
    duration_str = None
    for line in duration_result.stderr.split('\n'):
        if 'Duration:' in line:
            duration_str = line.split('Duration:')[1].split(',')[0].strip()
            break
    
    if not duration_str:
        logger.error("Could not determine video duration")
        return [file_path]
    
    parts = duration_str.split(':')
    total_seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    segment_duration = total_seconds / num_parts
    
    base, ext = os.path.splitext(file_path)
    output_parts = []
    
    for i in range(num_parts):
        start_time = i * segment_duration
        output_path = f"{base}_part{i+1}{ext}"
        
        cmd = [
            get_ffmpeg_command(),
            '-i', file_path,
            '-ss', str(start_time),
            '-t', str(segment_duration),
            '-c', 'copy',
            '-y',
            output_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and os.path.exists(output_path):
            output_parts.append(output_path)
            logger.info(f"Created part {i+1}/{num_parts}: {output_path}")
        else:
            logger.error(f"Failed to create part {i+1}: {result.stderr}")
    
    return output_parts if output_parts else [file_path]

# --- Streaming Upload Functions ---
async def upload_video_streaming(bot_token, api_url, chat_id, file_path, caption="", reply_markup=None, reply_to_message_id=None, thumb_path=None):
    """Upload video using streaming to minimize RAM usage."""
    endpoint = f"{api_url}{bot_token}/sendVideo"
    
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    
    logger.info(f"Chunked streaming upload: {file_name} ({file_size / 1024 / 1024:.2f} MB)")
    
    with aiohttp.MultipartWriter('form-data') as mpwriter:
        part = mpwriter.append(str(chat_id))
        part.set_content_disposition('form-data', name='chat_id')
        
        part = mpwriter.append(caption)
        part.set_content_disposition('form-data', name='caption')
        
        part = mpwriter.append('true')
        part.set_content_disposition('form-data', name='supports_streaming')
        
        if reply_markup:
            part = mpwriter.append(json.dumps(reply_markup))
            part.set_content_disposition('form-data', name='reply_markup')
        
        if reply_to_message_id:
            part = mpwriter.append(str(reply_to_message_id))
            part.set_content_disposition('form-data', name='reply_to_message_id')
        
        if thumb_path and os.path.exists(thumb_path):
            thumb_part = mpwriter.append(open(thumb_path, 'rb'))
            # Note: Telegram usually uses 'thumbnail' or 'thumb'
            thumb_part.set_content_disposition('form-data', name='thumbnail', filename=os.path.basename(thumb_path))
            thumb_part.headers['Content-Type'] = 'image/jpeg'
        
        file_part = mpwriter.append(open(file_path, 'rb'))
        file_part.set_content_disposition('form-data', name='video', filename=file_name)
        file_part.headers['Content-Type'] = 'video/mp4'
        
        timeout = aiohttp.ClientTimeout(total=7200)
        connector = aiohttp.TCPConnector(limit=1)
        
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.post(endpoint, data=mpwriter) as response:
                result = await response.json()
                if response.status == 200 and result.get('ok'):
                    if thumb_path and os.path.exists(thumb_path):
                        try: os.remove(thumb_path)
                        except: pass
                    return result.get('result')
                else:
                    error_msg = result.get('description', 'Unknown error')
                    raise Exception(f"Telegram API error: {error_msg}")

async def upload_audio_streaming(bot_token, api_url, chat_id, file_path, title="", caption="", reply_to_message_id=None, thumb_path=None):
    """Upload audio using streaming to minimize RAM usage."""
    endpoint = f"{api_url}{bot_token}/sendAudio"
    
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    
    logger.info(f"Streaming audio upload: {file_name} ({file_size / 1024 / 1024:.2f} MB)")
    
    with aiohttp.MultipartWriter('form-data') as mpwriter:
        part = mpwriter.append(str(chat_id))
        part.set_content_disposition('form-data', name='chat_id')
        
        part = mpwriter.append(caption)
        part.set_content_disposition('form-data', name='caption')
        
        part = mpwriter.append(title)
        part.set_content_disposition('form-data', name='title')
        
        if reply_to_message_id:
            part = mpwriter.append(str(reply_to_message_id))
            part.set_content_disposition('form-data', name='reply_to_message_id')
            
        if thumb_path and os.path.exists(thumb_path):
            thumb_part = mpwriter.append(open(thumb_path, 'rb'))
            thumb_part.set_content_disposition('form-data', name='thumbnail', filename=os.path.basename(thumb_path))
            thumb_part.headers['Content-Type'] = 'image/jpeg'
        
        file_part = mpwriter.append(open(file_path, 'rb'))
        file_part.set_content_disposition('form-data', name='audio', filename=file_name)
        file_part.headers['Content-Type'] = 'audio/mp4'
        
        timeout = aiohttp.ClientTimeout(total=3600)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(endpoint, data=mpwriter) as response:
                result = await response.json()
                if response.status == 200 and result.get('ok'):
                    if thumb_path and os.path.exists(thumb_path):
                        try: os.remove(thumb_path)
                        except: pass
                    return result.get('result')
                else:
                    error_msg = result.get('description', 'Unknown error')
                    raise Exception(f"Telegram API error: {error_msg}")
