"""
SQLite Database module for YTDL Telegram Bot.
Handles channel subscriptions persistence.
"""

import sqlite3
import logging
from datetime import datetime
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

DB_PATH = './data/subscriptions.db'

def init_db():
    """Initialize the database and create tables."""
    import os
    os.makedirs('./data', exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Subscriptions table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            channel_name TEXT,
            chat_id INTEGER NOT NULL,
            max_quality INTEGER DEFAULT 1080,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(channel_id, chat_id)
        )
    ''')
    
    # Processed videos table (to avoid duplicates)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS processed_videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL UNIQUE,
            channel_id TEXT NOT NULL,
            title TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # User settings table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            chat_id INTEGER PRIMARY KEY,
            download_mode TEXT DEFAULT 'video',
            resolution INTEGER DEFAULT 1080
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

def add_subscription(channel_id: str, channel_name: str, chat_id: int, max_quality: int = 1080) -> bool:
    """Add a new channel subscription."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO subscriptions (channel_id, channel_name, chat_id, max_quality)
            VALUES (?, ?, ?, ?)
        ''', (channel_id, channel_name, chat_id, max_quality))
        conn.commit()
        conn.close()
        logger.info(f"Subscription added: {channel_name} ({channel_id}) -> chat {chat_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to add subscription: {e}")
        return False

def remove_subscription(channel_id: str, chat_id: int) -> bool:
    """Remove a channel subscription."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM subscriptions WHERE channel_id = ? AND chat_id = ?
        ''', (channel_id, chat_id))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted
    except Exception as e:
        logger.error(f"Failed to remove subscription: {e}")
        return False

def get_all_subscriptions() -> List[Tuple]:
    """Get all active subscriptions."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT channel_id, channel_name, chat_id, max_quality FROM subscriptions')
        results = cursor.fetchall()
        conn.close()
        return results
    except Exception as e:
        logger.error(f"Failed to get subscriptions: {e}")
        return []

def get_user_subscriptions(chat_id: int) -> List[Tuple]:
    """Get subscriptions for a specific user/chat."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT channel_id, channel_name, max_quality, created_at 
            FROM subscriptions WHERE chat_id = ?
        ''', (chat_id,))
        results = cursor.fetchall()
        conn.close()
        return results
    except Exception as e:
        logger.error(f"Failed to get user subscriptions: {e}")
        return []

def is_video_processed(video_id: str) -> bool:
    """Check if a video has already been processed."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM processed_videos WHERE video_id = ?', (video_id,))
        result = cursor.fetchone() is not None
        conn.close()
        return result
    except Exception as e:
        logger.error(f"Failed to check processed video: {e}")
        return False

def mark_video_processed(video_id: str, channel_id: str, title: str) -> bool:
    """Mark a video as processed."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO processed_videos (video_id, channel_id, title)
            VALUES (?, ?, ?)
        ''', (video_id, channel_id, title))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Failed to mark video processed: {e}")
        return False

def cleanup_old_processed(days: int = 30):
    """Remove processed video records older than specified days."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM processed_videos 
            WHERE processed_at < datetime('now', ?)
        ''', (f'-{days} days',))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old processed video records")
    except Exception as e:
        logger.error(f"Failed to cleanup old records: {e}")
def get_user_settings(chat_id: int) -> dict:
    """Get settings for a specific user/chat."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT download_mode, resolution 
            FROM user_settings WHERE chat_id = ?
        ''', (chat_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return dict(row)
        else:
            # Default settings
            return {'download_mode': 'video', 'resolution': 1080}
    except Exception as e:
        logger.error(f"Failed to get user settings: {e}")
        return {'download_mode': 'video', 'resolution': 1080}

def update_user_settings(chat_id: int, download_mode: str = None, resolution: int = None) -> bool:
    """Update user settings."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Check if settings exist
        cursor.execute('SELECT 1 FROM user_settings WHERE chat_id = ?', (chat_id,))
        exists = cursor.fetchone() is not None
        
        if not exists:
            # Insert default first then update or just insert
            mode = download_mode or 'video'
            res = resolution or 1080
            cursor.execute('''
                INSERT INTO user_settings (chat_id, download_mode, resolution)
                VALUES (?, ?, ?)
            ''', (chat_id, mode, res))
        else:
            if download_mode:
                cursor.execute('UPDATE user_settings SET download_mode = ? WHERE chat_id = ?', (download_mode, chat_id))
            if resolution:
                cursor.execute('UPDATE user_settings SET resolution = ? WHERE chat_id = ?', (resolution, chat_id))
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Failed to update user settings: {e}")
        return False
