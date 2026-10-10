import json
from pathlib import Path
import sys
import tempfile
import tracemalloc
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class GroupHomophoneFilterTests(unittest.TestCase):
    def test_longest_match_wins_and_replacements_are_not_processed_again(self):
        from qq_openclaw_bridge.application.group_homophone_filter import (
            GroupHomophoneFilter,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "group-homophones.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "replacements": {
                            "敏感词": "米感词",
                            "敏感": "明感",
                            "米感词": "不应再次替换",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            sanitizer = GroupHomophoneFilter(path)

            self.assertEqual(
                sanitizer.replace("这个敏感词和敏感都要处理"),
                "这个米感词和明感都要处理",
            )

    def test_valid_changes_reload_and_invalid_changes_keep_last_good_map(self):
        from qq_openclaw_bridge.application.group_homophone_filter import (
            GroupHomophoneFilter,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "group-homophones.json"
            path.write_text(
                json.dumps(
                    {"version": 1, "replacements": {"坏词": "怀词"}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            sanitizer = GroupHomophoneFilter(path)
            self.assertEqual(sanitizer.replace("一个坏词"), "一个怀词")

            path.write_text(
                json.dumps(
                    {"version": 1, "replacements": {"坏词": "淮词"}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertEqual(sanitizer.replace("一个坏词"), "一个淮词")

            path.write_text("{not-json", encoding="utf-8")
            with self.assertLogs(
                "qq_openclaw_bridge.group_homophone_filter", level="WARNING"
            ):
                self.assertEqual(sanitizer.replace("一个坏词"), "一个淮词")

    def test_initial_configuration_is_strict_and_bounded(self):
        from qq_openclaw_bridge.application.group_homophone_filter import (
            GroupHomophoneFilter,
        )

        invalid_values = (
            {"version": 1, "replacements": {}, "extra": True},
            {"version": True, "replacements": {}},
            {"version": 2, "replacements": {}},
            {"version": 1, "replacements": []},
            {"version": 1, "replacements": {"": "词"}},
            {"version": 1, "replacements": {" 坏词": "怀词"}},
            {"version": 1, "replacements": {"坏词": "替" * 65}},
            {"version": 1, "replacements": {"坏词": "怀\u0000词"}},
            {
                "version": 1,
                "replacements": {
                    "词%d" % index: "替%d" % index for index in range(513)
                },
            },
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "group-homophones.json"
            for value in invalid_values:
                with self.subTest(value_type=type(value).__name__):
                    path.write_text(
                        json.dumps(value, ensure_ascii=False), encoding="utf-8"
                    )
                    with self.assertRaises(ValueError):
                        GroupHomophoneFilter(path)

            path.write_text(
                json.dumps({"version": 1, "replacements": {}}),
                encoding="utf-8",
            )
            self.assertEqual(GroupHomophoneFilter(path).replace("原样"), "原样")

    def test_replacement_refuses_output_above_group_message_limit(self):
        from qq_openclaw_bridge.application.group_homophone_filter import (
            GroupHomophoneFilter,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "group-homophones.json"
            path.write_text(
                json.dumps(
                    {"version": 1, "replacements": {"词": "替" * 64}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            sanitizer = GroupHomophoneFilter(path)

            with self.assertRaises(ValueError):
                sanitizer.replace("词" * 65)

    def test_duplicate_json_fields_are_rejected(self):
        from qq_openclaw_bridge.application.group_homophone_filter import (
            GroupHomophoneFilter,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "group-homophones.json"
            path.write_text(
                '{"version":1,"replacements":{"词":"次","词":"辞"}}',
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                GroupHomophoneFilter(path)

    def test_oversized_file_is_rejected_without_reading_it_into_memory(self):
        from qq_openclaw_bridge.application.group_homophone_filter import (
            GroupHomophoneFilter,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "group-homophones.json"
            with path.open("wb") as stream:
                stream.seek(10 * 1024 * 1024 - 1)
                stream.write(b"\0")

            tracemalloc.start()
            try:
                with self.assertRaises(ValueError):
                    GroupHomophoneFilter(path)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

        self.assertLess(peak, 2 * 1024 * 1024)

    def test_rejected_reload_logs_once_without_path_or_terms(self):
        from qq_openclaw_bridge.application.group_homophone_filter import (
            GroupHomophoneFilter,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "group-homophones.json"
            path.write_text(
                json.dumps(
                    {"version": 1, "replacements": {"坏词": "怀词"}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            sanitizer = GroupHomophoneFilter(path)
            path.write_text("{invalid-坏词", encoding="utf-8")

            with self.assertLogs(
                "qq_openclaw_bridge.group_homophone_filter", level="WARNING"
            ) as captured:
                self.assertEqual(sanitizer.replace("坏词"), "怀词")
                self.assertEqual(sanitizer.replace("坏词"), "怀词")

        self.assertEqual(len(captured.output), 1)
        self.assertNotIn("坏词", captured.output[0])
        self.assertNotIn(temp_dir, captured.output[0])


if __name__ == "__main__":
    unittest.main()
