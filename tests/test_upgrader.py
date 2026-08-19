import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from upgrader import (
    PIP_FALLBACK_COMMAND, UPDATE_COMMAND, get_next_daily_update, upgrade_yt_dlp
)


class TestUpgrader(unittest.TestCase):
    @patch('upgrader._run_command')
    def test_native_nightly_update_success(self, run_command):
        run_command.return_value = (0, 'updated')

        success, output = upgrade_yt_dlp()

        self.assertTrue(success)
        self.assertEqual(output, 'updated')
        run_command.assert_called_once_with(UPDATE_COMMAND)

    @patch('upgrader._run_command')
    def test_pip_install_fallback(self, run_command):
        run_command.side_effect = [
            (1, 'self-update unavailable for pip'),
            (0, 'pip update complete'),
        ]

        success, output = upgrade_yt_dlp()

        self.assertTrue(success)
        self.assertIn('pip update complete', output)
        self.assertEqual(run_command.call_args_list[1].args[0], PIP_FALLBACK_COMMAND)

    def test_next_daily_update_uses_taipei_by_default(self):
        now = datetime(2026, 8, 19, 19, 0, tzinfo=ZoneInfo('UTC'))

        next_run = get_next_daily_update(now, '04:00', 'Asia/Taipei')

        self.assertEqual(next_run.isoformat(), '2026-08-20T04:00:00+08:00')

    def test_passed_update_time_schedules_tomorrow(self):
        now = datetime(2026, 8, 19, 21, 0, tzinfo=ZoneInfo('UTC'))

        next_run = get_next_daily_update(now, '04:00', 'Asia/Taipei')

        self.assertEqual(next_run.isoformat(), '2026-08-21T04:00:00+08:00')


if __name__ == '__main__':
    unittest.main()
