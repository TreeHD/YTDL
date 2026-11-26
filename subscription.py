"""
Subscription monitor module for YTDL Telegram Bot.
Monitors YouTube channels for new videos.
"""

import asyncio
import functools
import html
import logging
from datetime import datetime

from database import (
    get_all_subscriptions, 
    is_video_processed, 
    mark_video_processed,
    cleanup_old_processed
)
from downloader import get_latest_videos, get_live_info, is_twitch_channel_id, is_legacy_numeric_channel_id
from config import load_config
from telegram_utils import tg_retry

logger = logging.getLogger(__name__)

# Recording outcomes after which the same broadcast must not be recorded again
# for that chat. Any other outcome (proxy outage, crash, bot shutdown) leaves
# the broadcast eligible, so the next poll restarts it while it is still live.
FINAL_LIVE_OUTCOMES = ('confirmed_end', 'stopped', 'cancelled')
MAX_LIVE_RECORDING_ATTEMPTS = 3


def _live_done_key(video_id, chat_id):
    return f"live_{video_id}_{chat_id}"

class SubscriptionMonitor:
    """Monitor subscribed channels for new videos."""

    def __init__(self, application, request_queue):
        self.application = application
        self.request_queue = request_queue
        self.running = False
        self.task = None
        self.active_live_recordings = set()
        self._live_attempts = {}  # rec_key -> (channel_id, abnormal endings so far)
    
    async def start(self):
        """Start the subscription monitor."""
        self.running = True
        self.task = asyncio.create_task(self._monitor_loop())
        logger.info("Subscription monitor started")
    
    async def stop(self):
        """Stop the subscription monitor."""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("Subscription monitor stopped")
    
    async def _monitor_loop(self):
        """Main monitoring loop."""
        config = load_config()
        check_interval = config.get('subscription_check_interval', 300)
        
        # Initial delay to let bot start up
        await asyncio.sleep(30)
        
        while self.running:
            try:
                await self._check_subscriptions()
                
                # Cleanup old records periodically
                cleanup_old_processed(days=30)
                
            except Exception as e:
                logger.error(f"Error in subscription monitor: {e}")
            
            await asyncio.sleep(check_interval)
    
    async def _check_subscriptions(self):
        """Check all subscriptions for new videos."""
        subscriptions = get_all_subscriptions()
        
        if not subscriptions:
            return
        
        logger.info(f"Checking {len(subscriptions)} unique subscription entries...")
        
        # Group by channel
        channels = {}
        for channel_id, channel_name, chat_id, max_quality, sub_type in subscriptions:
            if is_legacy_numeric_channel_id(channel_id):
                logger.warning(
                    "Skipping legacy subscription ID %s; remove it and subscribe again with the Twitch URL",
                    channel_id,
                )
                continue
            if channel_id not in channels:
                channels[channel_id] = {
                    'name': channel_name,
                    'video_subs': [],
                    'live_subs': []
                }
            
            sub_info = {'chat_id': chat_id, 'max_quality': max_quality}
            if sub_type == 'live' or is_twitch_channel_id(channel_id):
                channels[channel_id]['live_subs'].append(sub_info)
            else:
                channels[channel_id]['video_subs'].append(sub_info)
        
        for channel_id, data in channels.items():
            try:
                # Check videos if there are video subscribers
                if data['video_subs']:
                    await self._check_videos(channel_id, data['name'], data['video_subs'])
                
                # Check live if there are live subscribers
                if data['live_subs']:
                    await self._check_live(channel_id, data['name'], data['live_subs'])
            except Exception as e:
                logger.error(f"Error checking channel {channel_id}: {e}")
            
            await asyncio.sleep(5)

    async def _check_channel(self, channel_id, channel_name, subscribers):
        """No longer used directly, split into _check_videos and _check_live"""
        pass

    async def _check_videos(self, channel_id, channel_name, subscribers):
        """Check a specific channel for new video uploads."""
        try:
            loop = asyncio.get_running_loop()
            videos = await loop.run_in_executor(None, lambda: get_latest_videos(channel_id, limit=3))
            
            for video in videos:
                video_id = video['id']
                if is_video_processed(video_id):
                    continue
                
                logger.info(f"New video found: {video['title']} from {channel_name}")
                mark_video_processed(video_id, channel_id, video['title'])
                
                for sub in subscribers:
                    status_msg = await self._notify(
                        sub['chat_id'],
                        f"🔔 <b>New Video from {html.escape(str(channel_name))}</b>\n\n"
                        f"📹 {html.escape(str(video['title']))}\n\n"
                        f"⬇️ Starting download ({sub['max_quality']}p)...",
                    )
                    # The video is already marked processed, so it must be
                    # queued even when the notification could not be sent.
                    await self.request_queue.put((
                        sub['chat_id'],
                        video['url'],
                        None,
                        sub['max_quality'],
                        status_msg,
                        channel_name
                    ))
        except Exception as e:
            logger.error(f"Error checking videos for {channel_id}: {e}")

    async def _notify(self, chat_id, text):
        """Send an HTML notification; a failure must never drop the queued task."""
        try:
            return await tg_retry(
                self.application.bot.send_message,
                chat_id=chat_id, text=text, parse_mode='HTML',
            )
        except Exception as e:
            logger.error(f"Failed to notify chat {chat_id}: {e}")
            return None

    async def _check_live(self, channel_id, channel_name, subscribers):
        """Check a specific channel for active live streams."""
        try:
            loop = asyncio.get_running_loop()
            live_info = await loop.run_in_executor(None, lambda: get_live_info(channel_id))
            if live_info.state == 'LIVE':
                await self._start_live_recordings(channel_id, channel_name, subscribers, live_info.info)
            elif live_info.state == 'ENDED':
                # Recording locks are released by the recordings themselves.
                # Only forget restart counters for broadcasts that are over.
                for rec_key, (owner, _) in list(self._live_attempts.items()):
                    if owner == channel_id and rec_key not in self.active_live_recordings:
                        del self._live_attempts[rec_key]
            else:
                logger.warning("Live state for %s is unknown; keeping recording locks", channel_id)
        except Exception as e:
            logger.error(f"Error checking live for {channel_id}: {e}")

    async def _start_live_recordings(self, channel_id, channel_name, subscribers, live_info):
        video_id = live_info['id']
        for sub in subscribers:
            chat_id = sub['chat_id']
            # Use video_id + chat_id so same channel can have multiple concurrent streams
            rec_key = f"{video_id}_{chat_id}"
            if rec_key in self.active_live_recordings:
                logger.info(f"Skipping duplicate live recording {video_id} in chat {chat_id}")
                continue
            if is_video_processed(_live_done_key(video_id, chat_id)):
                continue
            self.active_live_recordings.add(rec_key)
            previous_failures = self._live_attempts.get(rec_key, (channel_id, 0))[1]
            if previous_failures:
                logger.warning(
                    "Restarting live recording %s in chat %s (attempt %s/%s)",
                    video_id, chat_id, previous_failures + 1, MAX_LIVE_RECORDING_ATTEMPTS,
                )
                headline = (
                    f"🔁 <b>LIVE: recording of {html.escape(str(channel_name))} ended early; "
                    f"restarting ({previous_failures + 1}/{MAX_LIVE_RECORDING_ATTEMPTS})</b>"
                )
            else:
                logger.info(f"New LIVE stream found: {live_info['title']} from {channel_name}")
                headline = f"🔴 <b>LIVE: {html.escape(str(channel_name))} is streaming now!</b>"
            try:
                msg = await self._notify(
                    chat_id,
                    f"{headline}\n\n"
                    f"📹 {html.escape(str(live_info['title']))}\n\n"
                    f"⏳ Recording and segmented upload started (1.9GB chunks)...",
                )
                await self.request_queue.put((
                    chat_id,
                    live_info['url'],
                    None,
                    sub['max_quality'],
                    msg,
                    channel_name,
                    True,  # is_live
                    video_id,
                    functools.partial(self._live_recording_finished, rec_key, chat_id,
                                      channel_id, video_id, live_info['title']),
                ))
            except Exception as e:
                self.active_live_recordings.discard(rec_key)
                logger.error(f"Failed to queue live recording for chat {chat_id}: {e}")

    def _live_recording_finished(self, rec_key, chat_id, channel_id, video_id, title, outcome):
        """Release the lock; mark the broadcast done only when it really is."""
        self.active_live_recordings.discard(rec_key)
        if outcome in FINAL_LIVE_OUTCOMES:
            self._live_attempts.pop(rec_key, None)
            mark_video_processed(_live_done_key(video_id, chat_id), channel_id, title)
            return
        failures = self._live_attempts.get(rec_key, (channel_id, 0))[1] + 1
        if failures >= MAX_LIVE_RECORDING_ATTEMPTS:
            logger.error(
                "Live recording %s in chat %s ended abnormally %s times (%s); not restarting",
                video_id, chat_id, failures, outcome,
            )
            self._live_attempts.pop(rec_key, None)
            mark_video_processed(_live_done_key(video_id, chat_id), channel_id, title)
            return
        self._live_attempts[rec_key] = (channel_id, failures)
        logger.warning(
            "Live recording %s in chat %s ended with %s; it will restart if still live",
            video_id, chat_id, outcome,
        )

async def check_channel_now(channel_id, channel_name, chat_id, max_quality, application, request_queue):
    """Manually trigger a channel check."""
    try:
        loop = asyncio.get_running_loop()
        videos = await loop.run_in_executor(None, lambda: get_latest_videos(channel_id, limit=3))
        new_count = 0
        
        for video in videos:
            video_id = video['id']
            
            if is_video_processed(video_id):
                continue
            
            new_count += 1
            mark_video_processed(video_id, channel_id, video['title'])
            
            try:
                status_msg = await tg_retry(
                    application.bot.send_message,
                    chat_id=chat_id,
                    text=f"📹 Found: {video['title']}\n⬇️ Starting download..."
                )
            except Exception as e:
                # Already marked processed: queue it even without a status message.
                logger.error(f"Failed to notify chat {chat_id}: {e}")
                status_msg = None
            
            await request_queue.put((chat_id, video['url'], None, max_quality, status_msg, channel_name))
        
        return new_count
    except Exception as e:
        logger.error(f"Manual channel check failed: {e}")
        raise
