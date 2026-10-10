from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class TodoDomainTests(unittest.TestCase):
    def test_todo_codec_preserves_unknown_metadata_in_stable_order(self):
        from qq_openclaw_bridge.domain.todos import format_todo_line, parse_todo_line

        source = "- [ ] 9 | 2026-07-16 | 写方案 | z=2 | user=1 | a=1"
        item = parse_todo_line(source)
        encoded = format_todo_line(item)

        self.assertEqual(item.item_id, 9)
        self.assertEqual(
            encoded,
            "- [ ] 9 | 2026-07-16 | 写方案 | user=1 | a=1 | z=2",
        )


if __name__ == "__main__":
    unittest.main()
