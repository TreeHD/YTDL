import unittest
from downloader import is_retryable_error

class TestDownloaderUtils(unittest.TestCase):
    def test_retryable_error_detection(self):
        self.assertTrue(is_retryable_error("This video is not available in your country"))
        self.assertTrue(is_retryable_error("The uploader has not made this video available in your country"))
        self.assertTrue(is_retryable_error("Sign in to confirm your age"))
        self.assertTrue(is_retryable_error("Private video"))
        self.assertTrue(is_retryable_error("Sign in to confirm you're not a bot"))
        self.assertTrue(is_retryable_error("cookies are required"))
        self.assertTrue(is_retryable_error("Connection to www.youtube.com timed out"))
        self.assertTrue(is_retryable_error("Unable to download API page"))

        self.assertFalse(is_retryable_error("Could not resolve host"))
        self.assertFalse(is_retryable_error("HTTP Error 404: Not Found"))

if __name__ == '__main__':
    unittest.main()
