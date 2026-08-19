import os
import unittest
from unittest.mock import patch, MagicMock
from uploader import split_video

class TestUploaderUtils(unittest.TestCase):
    @patch('os.path.getsize')
    @patch('uploader.load_config')
    def test_split_video_no_split_needed(self, mock_load_config, mock_getsize):
        # Setup: file size 10MB, limit 50MB
        mock_getsize.return_value = 10 * 1024 * 1024
        mock_load_config.return_value = {'api_url': 'https://api.telegram.org/bot'}
        
        parts = split_video("dummy.mp4")
        self.assertEqual(parts, ["dummy.mp4"])

    @patch('os.path.getsize')
    @patch('os.path.exists')
    @patch('subprocess.run')
    @patch('uploader.load_config')
    def test_split_video_split_needed(self, mock_load_config, mock_run, mock_exists, mock_getsize):
        # Setup: file size 120MB, limit 50MB -> should split into 3 parts
        mock_getsize.return_value = 120 * 1024 * 1024
        mock_load_config.return_value = {'api_url': 'https://api.telegram.org/bot'}
        mock_exists.return_value = True
        
        # Source duration, split command, and each generated part duration.
        mock_run.side_effect = [
            MagicMock(returncode=1, stderr="Duration: 00:03:00.00, start: 0.0"),
            MagicMock(returncode=0, stderr=""),
            MagicMock(returncode=1, stderr="Duration: 00:01:10.00, start: 0.0"),
            MagicMock(returncode=0, stderr=""),
            MagicMock(returncode=1, stderr="Duration: 00:01:10.00, start: 0.0"),
            MagicMock(returncode=0, stderr=""),
            MagicMock(returncode=1, stderr="Duration: 00:00:40.00, start: 0.0"),
        ]
        
        with patch('uploader.get_ffmpeg_command', return_value='ffmpeg'):
            parts = split_video("dummy.mp4")
            
            self.assertEqual(len(parts), 3)
            self.assertEqual(parts[0], "dummy_part1.mp4")
            self.assertEqual(parts[1], "dummy_part2.mp4")
            self.assertEqual(parts[2], "dummy_part3.mp4")

            split_commands = [
                call.args[0] for call in mock_run.call_args_list
                if '-fs' in call.args[0]
            ]
            self.assertEqual(len(split_commands), 3)
            self.assertTrue(all('-t' not in command for command in split_commands))
            self.assertTrue(all(
                command[command.index('-fs') + 1] == str(49 * 1024 * 1024)
                for command in split_commands
            ))

if __name__ == '__main__':
    unittest.main()
