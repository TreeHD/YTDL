"""
Uploader module for YTDL Telegram Bot.
Handles Telegram file uploads with streaming support.
"""

import os
import json
import logging
import subprocess
import asyncio
import aiohttp
import aiofiles

from config import load_config, LOCAL_API_LIMIT, get_ffmpeg_command, check_ffmpeg
from PIL import Image

logger = logging.getLogger(__name__)


class UploadRetryableError(Exception):
    """A temporary Telegram or transport failure that may be retried."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class UploadPermanentError(Exception):
    """A Telegram API failure that should not be retried automatically."""


RAW_UPLOAD_MAX_RETRIES = 5
RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
RETRYABLE_TRANSPORT_ERRORS = (
    aiohttp.ClientConnectionError,
    aiohttp.ClientPayloadError,
    aiohttp.ServerTimeoutError,
)


def _retry_after_from_response(payload, headers):
    """Extract Telegram's retry delay from a raw Bot API response."""
    retry_after = None
    if isinstance(payload, dict):
        retry_after = payload.get('parameters', {}).get('retry_after')
    if retry_after is None:
        retry_after = headers.get('Retry-After')
    try:
        return max(0, float(retry_after)) if retry_after is not None else None
    except (TypeError, ValueError):
        return None


async def _post_raw_telegram(endpoint, mpwriter, timeout):
    """Send one raw Bot API request and classify its response for retrying."""
    connector = aiohttp.TCPConnector(limit=1)
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.post(endpoint, data=mpwriter) as response:
                body = await response.text()
                try:
                    result = json.loads(body)
                except json.JSONDecodeError:
                    result = {}

                if response.status == 200 and result.get('ok'):
                    return result.get('result')

                description = result.get('description') or body or 'Unknown error'
                retry_after = _retry_after_from_response(result, response.headers)
                if response.status in RETRYABLE_HTTP_STATUSES or retry_after is not None:
                    raise UploadRetryableError(
                        f"Telegram API error: {description}", retry_after=retry_after
                    )
                raise UploadPermanentError(f"Telegram API error: {description}")
    except RETRYABLE_TRANSPORT_ERRORS as exc:
        raise UploadRetryableError(f"Telegram transport error: {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise UploadRetryableError("Telegram upload timed out") from exc


async def _retry_raw_upload(make_request, operation_name, max_retries=RAW_UPLOAD_MAX_RETRIES):
    """Retry a raw upload while rebuilding its multipart stream on every attempt."""
    last_error = None
    for attempt in range(max_retries):
        try:
            return await make_request()
        except UploadRetryableError as exc:
            last_error = exc
            if attempt == max_retries - 1:
                break
            delay = exc.retry_after
            if delay is None:
                delay = min(2 ** (attempt + 1), 60)
            logger.warning(
                "%s temporarily failed (%s); retrying in %ss (attempt %s/%s)",
                operation_name,
                exc,
                delay,
                attempt + 1,
                max_retries,
            )
            await asyncio.sleep(delay)

    if last_error:
        raise last_error
    raise RuntimeError(f"{operation_name} failed without an upload error")

def crop_to_square(image_path):
    """Crop an image to a 1:1 square ratio centered for Telegram thumbnails."""
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            if width == height:
                return image_path
            
            # Calculate coordinates for center cropping
            new_size = min(width, height)
            left = (width - new_size) / 2
            top = (height - new_size) / 2
            right = (width + new_size) / 2
            bottom = (height + new_size) / 2
            
            # Crop and save back
            img_cropped = img.crop((left, top, right, bottom))
            # Convert to RGB if needed (e.g. from RGBA/WebP)
            if img_cropped.mode != 'RGB':
                img_cropped = img_cropped.convert('RGB')
            img_cropped.save(image_path, 'JPEG')
            logger.info(f"Cropped thumbnail to square: {image_path}")
            return image_path
    except Exception as e:
        logger.warning(f"Failed to crop thumbnail {image_path}: {e}")
        return image_path

# --- Video Splitting ---
def _get_video_duration(file_path):
    """Read media duration in seconds from FFmpeg's probe output."""
    result = subprocess.run(
        [get_ffmpeg_command(), '-i', file_path, '-hide_banner'],
        capture_output=True,
        text=True,
    )
    for line in result.stderr.split('\n'):
        if 'Duration:' not in line:
            continue
        duration_str = line.split('Duration:')[1].split(',')[0].strip()
        parts = duration_str.split(':')
        try:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        except (IndexError, ValueError):
            break
    return None


def split_video(file_path, max_size_bytes=None):
    """Split video into size-limited, stream-copy parts."""
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
    
    total_seconds = _get_video_duration(file_path)
    if not total_seconds:
        logger.error("Could not determine video duration")
        return [file_path]

    base, ext = os.path.splitext(file_path)
    output_parts = []
    start_time = 0.0
    part_number = 1

    while start_time < total_seconds - 1:
        output_path = f"{base}_part{part_number}{ext}"
        cmd = [
            get_ffmpeg_command(),
            '-i', file_path,
            '-ss', str(start_time),
            '-c', 'copy',
            '-fs', str(max_size_bytes),
            '-y',
            output_path
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(output_path):
            logger.error(f"Failed to create part {part_number}: {result.stderr}")
            break

        part_duration = _get_video_duration(output_path)
        if not part_duration or part_duration <= 0:
            logger.error(f"Could not determine duration of part {part_number}")
            try:
                os.remove(output_path)
            except Exception:
                pass
            break

        output_parts.append(output_path)
        part_size = os.path.getsize(output_path)
        logger.info(
            "Created part %s: %s (%.2f MiB, %.2fs)",
            part_number,
            output_path,
            part_size / 1024 / 1024,
            part_duration,
        )

        start_time += part_duration
        part_number += 1

    if output_parts and start_time >= total_seconds - 1:
        return output_parts

    logger.error("Video split was incomplete; removing partial outputs")
    for output_path in output_parts:
        try:
            os.remove(output_path)
        except Exception:
            pass
    return [file_path]

# --- Streaming Upload Functions ---
async def upload_video_streaming(bot_token, api_url, chat_id, file_path, caption="", reply_markup=None, reply_to_message_id=None, thumb_path=None, max_retries=RAW_UPLOAD_MAX_RETRIES):
    """Upload video using streaming to minimize RAM usage."""
    endpoint = f"{api_url}{bot_token}/sendVideo"
    
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    
    logger.info(f"Chunked streaming upload: {file_name} ({file_size / 1024 / 1024:.2f} MB)")
    
    async def make_request():
        # A multipart writer consumes its streams.  It must be made afresh for
        # every retry, otherwise Telegram receives an empty body on attempt 2.
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

            thumb_fh = None
            video_fh = None
            try:
                if thumb_path and os.path.exists(thumb_path):
                    cropped_thumb_path = crop_to_square(thumb_path)
                    thumb_fh = open(cropped_thumb_path, 'rb')
                    thumb_part = mpwriter.append(thumb_fh)
                    thumb_part.set_content_disposition('form-data', name='thumbnail', filename=os.path.basename(cropped_thumb_path))
                    thumb_part.headers['Content-Type'] = 'image/jpeg'

                video_fh = open(file_path, 'rb')
                file_part = mpwriter.append(video_fh)
                file_part.set_content_disposition('form-data', name='video', filename=file_name)
                file_part.headers['Content-Type'] = 'video/mp4'

                timeout = aiohttp.ClientTimeout(total=7200)
                return await _post_raw_telegram(endpoint, mpwriter, timeout)
            finally:
                if video_fh:
                    video_fh.close()
                if thumb_fh:
                    thumb_fh.close()

    return await _retry_raw_upload(make_request, f"Video upload {file_name}", max_retries=max_retries)

async def upload_audio_streaming(bot_token, api_url, chat_id, file_path, title="", caption="", reply_to_message_id=None, thumb_path=None, max_retries=RAW_UPLOAD_MAX_RETRIES):
    """Upload audio using streaming to minimize RAM usage."""
    endpoint = f"{api_url}{bot_token}/sendAudio"
    
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    
    logger.info(f"Streaming audio upload: {file_name} ({file_size / 1024 / 1024:.2f} MB)")
    
    async def make_request():
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

            thumb_fh = None
            audio_fh = None
            try:
                if thumb_path and os.path.exists(thumb_path):
                    cropped_thumb_path = crop_to_square(thumb_path)
                    thumb_fh = open(cropped_thumb_path, 'rb')
                    thumb_part = mpwriter.append(thumb_fh)
                    thumb_part.set_content_disposition('form-data', name='thumbnail', filename=os.path.basename(cropped_thumb_path))
                    thumb_part.headers['Content-Type'] = 'image/jpeg'

                audio_fh = open(file_path, 'rb')
                file_part = mpwriter.append(audio_fh)
                file_part.set_content_disposition('form-data', name='audio', filename=file_name)
                file_part.headers['Content-Type'] = 'audio/mp4'

                timeout = aiohttp.ClientTimeout(total=3600)
                return await _post_raw_telegram(endpoint, mpwriter, timeout)
            finally:
                if audio_fh:
                    audio_fh.close()
                if thumb_fh:
                    thumb_fh.close()

    return await _retry_raw_upload(make_request, f"Audio upload {file_name}", max_retries=max_retries)
