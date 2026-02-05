"""
Subscription monitor module for YTDL Telegram Bot.
Monitors YouTube channels for new videos.
"""

import asyncio
import logging
from datetime import datetime

from database import (
    get_all_subscriptions, 
    is_video_processed, 
    mark_video_processed,
    cleanup_old_processed
)
from downloader import get_latest_videos, get_live_info
from config import load_config

logger = logging.getLogger(__name__)

class SubscriptionMonitor:
    """Monitor subscribed channels for new videos."""
    
    def __init__(self, application, request_queue):
        self.application = application
        self.request_queue = request_queue
        self.running = False
        self.task = None
    
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
            if channel_id not in channels:
                channels[channel_id] = {
                    'name': channel_name,
                    'video_subs': [],
                    'live_subs': []
                }
            
            sub_info = {'chat_id': chat_id, 'max_quality': max_quality}
            if sub_type == 'live':
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
                    try:
                        status_msg = await self.application.bot.send_message(
                            chat_id=sub['chat_id'],
                            text=f"🔔 **New Video from {channel_name}**\n\n"
                                 f"📹 {video['title']}\n\n"
                                 f"⬇️ Starting download ({sub['max_quality']}p)...",
                            parse_mode='Markdown'
                        )
                        await self.request_queue.put((
                            sub['chat_id'],
                            video['url'],
                            None,
                            sub['max_quality'],
                            status_msg,
                            channel_name
                        ))
                    except Exception as e:
                        logger.error(f"Failed to notify chat {sub['chat_id']}: {e}")
        except Exception as e:
            logger.error(f"Error checking videos for {channel_id}: {e}")

    async def _check_live(self, channel_id, channel_name, subscribers):
        """Check a specific channel for active live streams."""
        try:
            loop = asyncio.get_running_loop()
            live_info = await loop.run_in_executor(None, lambda: get_live_info(channel_id))
            if live_info:
                live_id = f"live_{live_info['id']}"
                if not is_video_processed(live_id):
                    logger.info(f"New LIVE stream found: {live_info['title']} from {channel_name}")
                    mark_video_processed(live_id, channel_id, live_info['title'])
                    
                    for sub in subscribers:
                        try:
                            msg = await self.application.bot.send_message(
                                chat_id=sub['chat_id'],
                                text=f"🔴 **LIVE: {channel_name} is streaming now!**\n\n"
                                     f"📹 {live_info['title']}\n\n"
                                     f"⏳ Recording and segmented upload started (1.9GB chunks)...",
                                parse_mode='Markdown'
                            )
                            await self.request_queue.put((
                                sub['chat_id'],
                                live_info['url'],
                                None,
                                sub['max_quality'],
                                msg,
                                channel_name,
                                True # is_live
                            ))
                        except Exception as e:
                            logger.error(f"Failed to notify live for chat {sub['chat_id']}: {e}")
        except Exception as e:
            logger.error(f"Error checking live for {channel_id}: {e}")

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
            
            status_msg = await application.bot.send_message(
                chat_id=chat_id,
                text=f"📹 Found: {video['title']}\n⬇️ Starting download..."
            )
            
            await request_queue.put((chat_id, video['url'], None, max_quality, status_msg, channel_name))
        
        return new_count
    except Exception as e:
        logger.error(f"Manual channel check failed: {e}")
        raise
