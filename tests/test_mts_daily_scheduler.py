import unittest
from datetime import datetime, timezone
from mts_daily_scheduler import due_day

class ScheduleTests(unittest.TestCase):
    def test_before_seven_minsk(self):
        self.assertIsNone(due_day(datetime(2026,10,24,3,59,tzinfo=timezone.utc)))
    def test_at_seven_minsk(self):
        self.assertEqual(str(due_day(datetime(2026,10,24,4,0,tzinfo=timezone.utc))), '2026-10-24')
    def test_after_seven_utc_day_boundary(self):
        self.assertIsNone(due_day(datetime(2026,10,24,22,30,tzinfo=timezone.utc)))

if __name__ == '__main__':
    unittest.main()
