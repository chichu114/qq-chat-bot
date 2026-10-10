from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class MemoryDomainTests(unittest.TestCase):
    def test_parse_and_replace_preserve_non_memory_lines(self):
        from qq_openclaw_bridge.domain.memory import (
            extract_memory_items,
            replace_memory_items,
        )

        content = "# M\n\n## 长期记忆\n- 2026-07-01: 先给结论\n<!-- keep -->\n"
        items = extract_memory_items(content)
        updated = replace_memory_items(content, items)

        self.assertEqual(items[0].text, "先给结论")
        self.assertIn("<!-- keep -->", updated)


if __name__ == "__main__":
    unittest.main()
