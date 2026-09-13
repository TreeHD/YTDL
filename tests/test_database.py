import os
import unittest
import sqlite3
from database import (
    init_db, add_subscription, remove_subscription, get_user_subscriptions,
    is_video_processed, mark_video_processed, remove_legacy_numeric_subscriptions,
)

class TestDatabase(unittest.TestCase):
    def setUp(self):
        # Use a separate test database
        self.test_db_dir = './data_test'
        self.test_db_path = f'{self.test_db_dir}/subscriptions.db'
        os.makedirs(self.test_db_dir, exist_ok=True)
        
        # Patch the DB_PATH in database module (monkeypatching)
        import database
        self.original_db_path = database.DB_PATH
        database.DB_PATH = self.test_db_path
        
        init_db()

    def tearDown(self):
        # Restore original path
        import database
        database.DB_PATH = self.original_db_path
        
        # Remove test database
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        if os.path.exists(self.test_db_dir):
            os.rmdir(self.test_db_dir)

    def test_subscription_lifecycle(self):
        # Add
        success = add_subscription("UC123", "Test Channel", 999, 720)
        self.assertTrue(success)
        
        # Get
        subs = get_user_subscriptions(999)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0][0], "UC123")
        self.assertEqual(subs[0][1], "Test Channel")
        self.assertEqual(subs[0][2], 720)
        
        # Remove
        removed = remove_subscription("UC123", 999)
        self.assertTrue(removed)
        
        # Verify removed
        subs = get_user_subscriptions(999)
        self.assertEqual(len(subs), 0)

    def test_processed_videos(self):
        video_id = "vid_abc_123"
        self.assertFalse(is_video_processed(video_id))
        
        mark_video_processed(video_id, "UC123", "Test Video")
        self.assertTrue(is_video_processed(video_id))

    def test_legacy_numeric_cleanup_is_limited_to_current_chat(self):
        add_subscription('315460461806', 'Twitch Channel', 999, 720, 'video')
        add_subscription('315460461806', 'Twitch Channel', 999, 1080, 'live')
        add_subscription('315460461806', 'Other Chat', 1000, 1080, 'live')

        self.assertEqual(remove_legacy_numeric_subscriptions('315460461806', 999), 2)
        self.assertEqual(len(get_user_subscriptions(999)), 0)
        self.assertEqual(len(get_user_subscriptions(1000)), 1)

if __name__ == '__main__':
    unittest.main()
