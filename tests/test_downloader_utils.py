import unittest
from downloader import is_geo_restricted_error

class TestDownloaderUtils(unittest.TestCase):
    def test_geo_restriction_detection(self):
        self.assertTrue(is_geo_restricted_error("This video is not available in your country"))
        self.assertTrue(is_geo_restricted_error("The uploader has not made this video available in your country"))
        self.assertTrue(is_geo_restricted_error("Sign in to confirm your age"))
        self.assertTrue(is_geo_restricted_error("Private video"))
        
        self.assertFalse(is_geo_restricted_error("Could not resolve host"))
        self.assertFalse(is_geo_restricted_error("HTTP Error 404: Not Found"))

if __name__ == '__main__':
    unittest.main()
