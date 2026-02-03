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
from downloader import get_latest_videos
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
        
        logger.info(f"Checking {len(subscriptions)} channel subscriptions...")
        
        # Group by channel to avoid duplicate API calls
        channels = {}
        for channel_id, channel_name, chat_id, max_quality in subscriptions:
            if channel_id not in channels:
                channels[channel_id] = {
                    'name': channel_name,
                    'subscribers': []
                }
            channels[channel_id]['subscribers'].append({
                'chat_id': chat_id,
                'max_quality': max_quality
            })
        
        for channel_id, data in channels.items():
            try:
                await self._check_channel(channel_id, data['name'], data['subscribers'])
            except Exception as e:
                logger.error(f"Error checking channel {channel_id}: {e}")
            
            # Small delay between channels to avoid rate limiting
            await asyncio.sleep(5)
    
    async def _check_channel(self, channel_id, channel_name, subscribers):
        """Check a specific channel for new videos."""
        try:
            loop = asyncio.get_running_loop()
            videos = await loop.run_in_executor(None, lambda: get_latest_videos(channel_id, limit=3))
            
            for video in videos:
                video_id = video['id']
                
                if is_video_processed(video_id):
                    continue
                
                logger.info(f"New video found: {video['title']} from {channel_name}")
                
                # Mark as processed first to avoid duplicates
                mark_video_processed(video_id, channel_id, video['title'])
                
                # Queue download for each subscriber
                for sub in subscribers:
                    # Notify subscriber
                    try:
                        await self.application.bot.send_message(
                            chat_id=sub['chat_id'],
                            text=f"🔔 **New Video from {channel_name}**\n\n"
                                 f"📹 {video['title']}\n\n"
                                 f"⬇️ Starting download ({sub['max_quality']}p)...",
                            parse_mode='Markdown'
                        )
                    except Exception as e:
                        logger.error(f"Failed to notify chat {sub['chat_id']}: {e}")
                        continue
                    
                    # Add to download queue
                    await self.request_queue.put((
                        sub['chat_id'],
                        video['url'],
                        None,  # No reply message
                        sub['max_quality']
                    ))
                    
                    logger.info(f"Queued auto-download for chat {sub['chat_id']}: {video['title']}")
                
        except Exception as e:
            logger.error(f"Failed to check channel {channel_id}: {e}")

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
            
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"📹 Found: {video['title']}\n⬇️ Starting download..."
            )
            
            await request_queue.put((chat_id, video['url'], None, max_quality))
        
        return new_count
    except Exception as e:
        logger.error(f"Manual channel check failed: {e}")
        raise
