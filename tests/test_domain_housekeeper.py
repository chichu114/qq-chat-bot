from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class HousekeeperServiceTests(unittest.TestCase):
    def test_delegates_without_changing_command_result(self):
        from qq_openclaw_bridge.domain.housekeeper import HousekeeperService

        class Handler:
            def handle(self, text, user_id=None):
                return "%s:%s" % (user_id, text)

        service = HousekeeperService(Handler())
        self.assertEqual(service.handle("查看待办", 7), "7:查看待办")


if __name__ == "__main__":
    unittest.main()
