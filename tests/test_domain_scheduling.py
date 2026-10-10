from datetime import datetime
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class SchedulingDomainTests(unittest.TestCase):
    def test_absolute_relative_and_month_end_recurrence(self):
        from qq_openclaw_bridge.domain.scheduling import (
            next_recurring_due,
            parse_due_at,
            parse_recurrence_metadata,
        )

        timezone = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 2, 27, 10, tzinfo=timezone)
        self.assertEqual(
            parse_due_at("明天晚上", now, timezone).hour,
            20,
        )
        self.assertEqual(
            parse_due_at("2026-03-03 08:30", now, timezone).minute,
            30,
        )
        metadata = parse_recurrence_metadata("每月31号 09:00")
        self.assertEqual(
            next_recurring_due(metadata, now, timezone).day,
            28,
        )


if __name__ == "__main__":
    unittest.main()
