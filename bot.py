#!/usr/bin/env python3
"""
YTDL Telegram Bot - Main Entry Point
A Telegram bot for downloading videos from YouTube and other platforms.
"""

import asyncio
import logging
from functools import partial

from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, filters
)

from config import load_config, DOWNLOAD_DIR, check_ffmpeg
from database import init_db
from handlers import (
    start, handle_music_command, handle_quality_command,
    handle_playlist_command, handle_message, handle_subscribe,
    handle_unsubscribe, handle_subscriptions, cancel_callback, audio_callback,
    handle_settings, settings_callback
)
from queue_processor import process_queue, process_playlist_queue
from subscription import SubscriptionMonitor

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global queues
request_queue = asyncio.Queue()
playlist_queue = asyncio.Queue()

def main():
    """Main entry point."""
    # Load config
    config = load_config()
    
    if not config.get('bot_token') or config.get('bot_token') == 'YOUR_BOT_TOKEN_HERE':
        print("ERROR: BOT_TOKEN is missing. Set it via environment variable.")
        exit(1)
    
    # Initialize database
    init_db()
    
    api_url = config.get('api_url', 'https://api.telegram.org/bot')
    
    # Build application
    builder = ApplicationBuilder().token(config['bot_token'])
    builder.connection_pool_size(1024)
    builder.read_timeout(300)
    builder.write_timeout(300)
    builder.connect_timeout(300)
    builder.pool_timeout(300)

    if api_url:
        builder.base_url(api_url)
        print(f"Using API Server: {api_url}")
        
    application = builder.build()
    
    # --- Register Handlers ---
    
    # Basic commands
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', start))
    
    # Quality commands
    for quality in ['1080', '720', '480', '360', '240']:
        application.add_handler(CommandHandler(
            quality, 
            lambda update, context: handle_quality_command(update, context, request_queue)
        ))
    
    # Music command
    application.add_handler(CommandHandler(
        'music', 
        lambda update, context: handle_music_command(update, context, request_queue)
    ))
    
    # Playlist command
    application.add_handler(CommandHandler(
        'playlist', 
        lambda update, context: handle_playlist_command(update, context, playlist_queue)
    ))
    
    # Subscription commands
    application.add_handler(CommandHandler('subscribe', handle_subscribe))
    application.add_handler(CommandHandler('unsubscribe', handle_unsubscribe))
    application.add_handler(CommandHandler('subscriptions', handle_subscriptions))
    application.add_handler(CommandHandler('subs', handle_subscriptions))  # Alias
    application.add_handler(CommandHandler('settings', handle_settings))
    
    # Message handler for direct URLs
    application.add_handler(MessageHandler(
        filters.TEXT & (~filters.COMMAND), 
        lambda update, context: handle_message(update, context, request_queue)
    ))
    
    # Callback handlers
    application.add_handler(CallbackQueryHandler(cancel_callback, pattern="^cancel:"))
    application.add_handler(CallbackQueryHandler(audio_callback, pattern="^audio:"))
    application.add_handler(CallbackQueryHandler(settings_callback, pattern="^(set_mode|set_res:)"))
    
    # Task holders for graceful shutdown
    task_holders = {
        'queue_task': None,
        'playlist_task': None,
        'subscription_monitor': None
    }

    async def post_init(app):
        """Called after the Application is fully initialized."""
        # Store queues in bot_data for accessibility in handlers
        app.bot_data['request_queue'] = request_queue
        app.bot_data['playlist_queue'] = playlist_queue

        # Start queue processors
        task_holders['queue_task'] = asyncio.create_task(process_queue(app, request_queue))
        task_holders['playlist_task'] = asyncio.create_task(process_playlist_queue(app, playlist_queue))
        
        # Start subscription monitor
        monitor = SubscriptionMonitor(app, request_queue)
        await monitor.start()
        task_holders['subscription_monitor'] = monitor
        
        logger.info("All background tasks started.")

    async def post_shutdown(app):
        """Called before the Application shuts down."""
        # Stop subscription monitor
        monitor = task_holders.get('subscription_monitor')
        if monitor:
            await monitor.stop()
        
        # Cancel queue tasks
        for task_name in ['queue_task', 'playlist_task']:
            task = task_holders.get(task_name)
            if task and not task.done():
                logger.info(f"Canceling {task_name}...")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        logger.info("All background tasks stopped.")

    application.post_init = post_init
    application.post_shutdown = post_shutdown
    
    # Print startup info
    print("=" * 50)
    print("🎬 YTDL Telegram Bot")
    print("=" * 50)
    print(f"📁 Download directory: {DOWNLOAD_DIR}")
    print(f"🌐 API URL: {api_url}")
    
    if not check_ffmpeg():
        print("⚠️  WARNING: FFmpeg not detected! >2GB splitting will not work.")
    else:
        print("✅ FFmpeg detected")
    
    print("=" * 50)
    print("Bot is running...")
    print("=" * 50)
    
    application.run_polling()

if __name__ == '__main__':
    main()
