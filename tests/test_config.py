import os
import unittest
from unittest.mock import patch
from config import get_proxy_list, is_user_allowed, load_config

class TestConfig(unittest.TestCase):
    def setUp(self):
        # Clear env variables that might affect tests
        self.env_patcher = patch.dict(os.environ, {})
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def test_get_proxy_list_empty(self):
        with patch.dict(os.environ, {'PROXY': '', 'PROXY_LIST': ''}):
            proxies = get_proxy_list()
            self.assertEqual(proxies, [None])

    def test_get_proxy_list_single(self):
        with patch.dict(os.environ, {'PROXY': 'http://proxy1:8080'}):
            proxies = get_proxy_list()
            self.assertEqual(proxies, ['http://proxy1:8080'])

    def test_get_proxy_list_multiple(self):
        with patch.dict(os.environ, {
            'PROXY': 'http://proxy1:8080',
            'PROXY_LIST': 'http://proxy2:8080, http://proxy3:8080'
        }):
            proxies = get_proxy_list()
            self.assertIn('http://proxy1:8080', proxies)
            self.assertIn('http://proxy2:8080', proxies)
            self.assertIn('http://proxy3:8080', proxies)
            self.assertEqual(len(proxies), 3)

    def test_is_user_allowed_all(self):
        with patch.dict(os.environ, {'ALLOWED_CHAT_IDS': ''}):
            self.assertTrue(is_user_allowed(12345))

    def test_is_user_allowed_specific(self):
        with patch.dict(os.environ, {'ALLOWED_CHAT_IDS': '123, 456'}):
            self.assertTrue(is_user_allowed(123))
            self.assertTrue(is_user_allowed(456))
            self.assertFalse(is_user_allowed(789))

    def test_ytdlp_auto_update_defaults_to_enabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(load_config()['ytdlp_auto_update'])

    def test_ytdlp_auto_update_can_be_disabled(self):
        with patch.dict(os.environ, {'YTDLP_AUTO_UPDATE': 'false'}, clear=True):
            self.assertFalse(load_config()['ytdlp_auto_update'])

    def test_daily_update_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
            self.assertTrue(config['ytdlp_daily_update'])
            self.assertEqual(config['ytdlp_update_time'], '04:00')
            self.assertEqual(config['ytdlp_update_timezone'], 'Asia/Taipei')

if __name__ == '__main__':
    unittest.main()
