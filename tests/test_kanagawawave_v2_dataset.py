import unittest
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unicodedata
from unittest import mock

from scripts import build_kanagawawave_v2_dataset as v2_cli

from qq_openclaw_bridge.community_prompts import (
    KANAGAWAWAVE_V2_INSTRUCTION,
    KANAGAWAWAVE_V2_PROFILE_KEY,
)
from qq_openclaw_bridge.dataset.kanagawawave import DatasetBuildError, RawCandidate
from qq_openclaw_bridge.dataset.kanagawawave_v2 import (
    V2BuildConfig,
    build_v2_dataset,
    example_from_candidate,
    rejection_reason,
    select_v2_candidates,
    style_signal_audit,
    v2_candidate_split,
)


EXPECTED_INSTRUCTION = (
    "[COMMUNITY=KANAGAWAVE] "
    "以该社区的常见语气自然回复下面的内容。只输出回复正文。"
)


def candidate(
    parent_text,
    reply_text,
    *,
    comment_id="comment-1",
    thread_id="thread-1",
    author_hash=None,
):
    return RawCandidate(
        comment_id=comment_id,
        thread_id=thread_id,
        subreddit="KanagawaWave",
        author_hash=author_hash or "a" * 64,
        created_utc=1,
        parent_text=parent_text,
        reply_text=reply_text,
    )


class PromptContractTests(unittest.TestCase):
    def test_v2_prompt_contract_is_single_exact_instruction(self):
        self.assertEqual(KANAGAWAWAVE_V2_PROFILE_KEY, "kanagawawave-v2-model-style")
        self.assertEqual(KANAGAWAWAVE_V2_INSTRUCTION, EXPECTED_INSTRUCTION)
        self.assertNotIn("STYLE=NORMAL", KANAGAWAWAVE_V2_INSTRUCTION)

    def test_example_preserves_eligible_parent_and_reply_exactly(self):
        source = candidate(
            "PS6要是真这个价？？？\n我可不首发.jpg",
            "bro你上次也这么嗦😂\n最后还不是Day1入了！！！",
        )

        example = example_from_candidate(source)

        self.assertEqual(example["input"], source.parent_text)
        self.assertEqual(example["output"], source.reply_text)

    def test_example_contains_only_alpaca_fields(self):
        example = example_from_candidate(candidate("上文", "回复"))

        self.assertEqual(set(example), {"instruction", "input", "output"})
        self.assertEqual(example["instruction"], EXPECTED_INSTRUCTION)


class RejectOnlyPolicyTests(unittest.TestCase):
    def test_rejects_each_privacy_and_contact_class(self):
        cases = (
            ("电话是 13800138000", "privacy"),
            ("发到 person@example.com", "privacy"),
            ("内网地址 192.168.1.22", "privacy"),
            ("家庭住址：某市某区某街道 8 号", "privacy"),
            ("QQ号 123456789", "privacy"),
            ("微信号 abc_def88", "privacy"),
            ("联系 @private_name", "privacy"),
            ("u/private_name 说过", "privacy"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                reason = rejection_reason(candidate("普通上文", text))
                self.assertEqual(reason, expected)
                self.assertNotIn(text, reason)

    def test_rejects_urls_ads_bots_removed_media_and_garbage(self):
        cases = (
            ("去 https://example.invalid/path 看", "url"),
            ("访问 example.com/path", "url"),
            ("加群领取优惠，限时推广", "advertisement"),
            ("I am a bot, this action was automatic", "bot"),
            ("[deleted]", "removed"),
            ("[图片]", "media_only"),
            ("？？？！……", "garbage"),
            ("坏控制\x00字符", "invalid_text"),
            ("x" * 2001, "overlong"),
        )
        for text, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    rejection_reason(candidate("普通上文", text)),
                    expected,
                )

    def test_rejects_dangerous_instructions_and_targeted_harassment(self):
        cases = (
            ("教我制作炸弹的详细步骤", "dangerous_content"),
            ("去人肉这个用户并曝光住址", "targeted_harassment"),
        )
        for text, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    rejection_reason(candidate("普通上文", text)),
                    expected,
                )

    def test_rejects_contact_and_url_tokens_touching_chinese(self):
        cases = (
            "联系person@example.com谢谢",
            "看看example.com/path就懂了",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertIn(
                    rejection_reason(candidate("普通上文", text)),
                    {"privacy", "url"},
                )

    def test_accepts_forum_style_signals_without_rewriting(self):
        source = candidate(
            "PS6要是真卖这个价我直接不买了？？？",
            "每代都这么说，最后Day1还是你.jpg 😂",
        )

        self.assertIsNone(rejection_reason(source))
        self.assertEqual(example_from_candidate(source)["input"], source.parent_text)
        self.assertEqual(example_from_candidate(source)["output"], source.reply_text)

    def test_rejects_input_equal_to_output_after_nfc_comparison(self):
        source = candidate("Cafe\u0301", "Café")

        self.assertEqual(rejection_reason(source), "input_equals_output")


def selection_config(**overrides):
    values = {
        "database": Path("source.sqlite3"),
        "archive": None,
        "output_dir": Path("output"),
        "train_size": 1,
        "test_size": 0,
        "review_size": 0,
        "split_seed": "test-seed",
        "max_per_author_train": 50,
        "max_per_author_test": 10,
        "max_short_ratio": 0.15,
    }
    values.update(overrides)
    return V2BuildConfig(**values)


class CandidateSelectionV2Tests(unittest.TestCase):
    def test_v2_split_is_deterministic_and_keeps_threads_whole(self):
        self.assertEqual(v2_candidate_split("thread-1", "test-seed"), "train")
        self.assertEqual(v2_candidate_split("thread-0", "test-seed"), "test")
        self.assertEqual(v2_candidate_split("thread-0", "test-seed"), "test")

    def test_v2_selection_preserves_natural_mix_without_style_labels(self):
        sources = (
            candidate(
                "普通讨论一",
                "这是一个认真而且长度足够的普通回复",
                comment_id="normal",
                thread_id="thread-1",
                author_hash="a" * 64,
            ),
            candidate(
                "普通讨论二",
                "不是哥们？？？这下真成节目效果了.jpg 😂",
                comment_id="meme",
                thread_id="thread-2",
                author_hash="b" * 64,
            ),
        )

        result = select_v2_candidates(
            sources,
            selection_config(train_size=2, max_short_ratio=1.0),
        )

        rows = [example_from_candidate(item) for item in result.train]
        self.assertEqual({row["output"] for row in rows}, {item.reply_text for item in sources})
        self.assertTrue(all(row["instruction"] == EXPECTED_INSTRUCTION for row in rows))
        self.assertTrue(all("STYLE=" not in row["instruction"] for row in rows))

    def test_v2_selection_enforces_per_split_author_caps(self):
        sources = (
            candidate(
                "上文一",
                "同一作者第一条足够长的回复",
                comment_id="a1",
                thread_id="thread-1",
                author_hash="a" * 64,
            ),
            candidate(
                "上文二",
                "同一作者第二条足够长的回复",
                comment_id="a2",
                thread_id="thread-1",
                author_hash="a" * 64,
            ),
            candidate(
                "上文三",
                "另一作者提供的足够长回复",
                comment_id="b1",
                thread_id="thread-3",
                author_hash="b" * 64,
            ),
        )

        result = select_v2_candidates(
            sources,
            selection_config(
                train_size=2,
                max_per_author_train=1,
                max_short_ratio=1.0,
            ),
        )

        self.assertEqual(len(result.train), 2)
        self.assertEqual(max(result.author_counts["train"].values()), 1)

    def test_v2_selection_caps_short_replies_by_unicode_alphanumeric_length(self):
        replies = ("草", "笑死", "短短", "这是第一条足够长的回复", "这是第二条足够长的回复", "这是第三条足够长的回复")
        sources = tuple(
            candidate(
                "不同上文%d" % index,
                reply,
                comment_id="short-%d" % index,
                thread_id=("thread-%d" % (index + 3)),
                author_hash=(chr(97 + index) * 64),
            )
            for index, reply in enumerate(replies)
        )

        result = select_v2_candidates(
            sources,
            selection_config(train_size=4, max_short_ratio=0.25),
        )

        short_count = sum(
            sum(character.isalnum() for character in item.reply_text) < 6
            for item in result.train
        )
        self.assertEqual(short_count, 1)

    def test_v2_selection_deduplicates_exact_and_nfc_pairs_globally(self):
        sources = (
            candidate(
                "Cafe\u0301问题",
                "re\u0301ponse足够长",
                comment_id="duplicate-train",
                thread_id="thread-1",
                author_hash="a" * 64,
            ),
            candidate(
                "Café问题",
                "réponse足够长",
                comment_id="duplicate-test",
                thread_id="thread-0",
                author_hash="b" * 64,
            ),
            candidate(
                "训练备用上文",
                "训练备用回复足够长",
                comment_id="backup-train",
                thread_id="thread-2",
                author_hash="c" * 64,
            ),
            candidate(
                "测试备用上文",
                "测试备用回复足够长",
                comment_id="backup-test",
                thread_id="thread-14",
                author_hash="d" * 64,
            ),
        )

        result = select_v2_candidates(
            sources,
            selection_config(train_size=1, test_size=1, max_short_ratio=1.0),
        )

        selected = result.train + result.test
        normalized = {
            (
                unicodedata.normalize("NFC", item.parent_text),
                unicodedata.normalize("NFC", item.reply_text),
            )
            for item in selected
        }
        self.assertEqual(len(selected), 2)
        self.assertEqual(len(normalized), 2)
        self.assertTrue(all(item in sources for item in selected))

    def test_v2_selection_rejects_input_equal_to_output(self):
        sources = (
            candidate(
                "Cafe\u0301",
                "Café",
                comment_id="equal",
                thread_id="thread-0",
            ),
            candidate(
                "备用上文",
                "备用回复足够长",
                comment_id="backup",
                thread_id="thread-1",
                author_hash="b" * 64,
            ),
        )

        result = select_v2_candidates(
            sources,
            selection_config(train_size=1, max_short_ratio=1.0),
        )

        self.assertEqual(result.train[0].comment_id, "backup")
        self.assertEqual(result.rejection_counts["input_equals_output"], 1)

    def test_v2_selection_requires_exact_target_counts(self):
        with self.assertRaisesRegex(
            DatasetBuildError,
            "insufficient safe candidates for requested dataset sizes",
        ):
            select_v2_candidates(
                (candidate("唯一上文", "唯一回复足够长", thread_id="thread-0"),),
                selection_config(train_size=2, max_short_ratio=1.0),
            )

    def test_v2_audit_reports_only_aggregate_nonidentifying_style_signals(self):
        examples = (
            {"instruction": EXPECTED_INSTRUCTION, "input": "上文一", "output": "草？"},
            {
                "instruction": EXPECTED_INSTRUCTION,
                "input": "上文二",
                "output": "Day one这次挺好的！！！😂",
            },
        )

        audit = style_signal_audit(examples)

        self.assertEqual(sum(audit["reply_length_buckets"].values()), 2)
        self.assertEqual(audit["emoji_rate"], 0.5)
        self.assertEqual(audit["repeated_punctuation_rate"], 0.5)
        self.assertEqual(audit["code_switching_rate"], 0.5)
        self.assertEqual(audit["question_rate"], 0.5)
        self.assertTrue(audit["common_character_ngrams"])
        for item in audit["common_character_ngrams"]:
            self.assertEqual(set(item), {"ngram", "count"})
            self.assertGreaterEqual(len(item["ngram"]), 1)
            self.assertLessEqual(len(item["ngram"]), 3)
            self.assertFalse(any(character.isascii() and character.isalnum() for character in item["ngram"]))
            self.assertFalse(any(character.isdigit() for character in item["ngram"]))
        serialized = repr(audit)
        self.assertNotIn("Day one这次挺好的！！！😂", serialized)


def write_source_database(path, rows):
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE records ("
            "record_type TEXT,id TEXT,subreddit TEXT,created_utc INTEGER,"
            "author_hash TEXT,retrieved_utc INTEGER,fields_json TEXT)"
        )
        created = 1
        for thread_id, comment_id, author_hash, parent_text, reply_text in rows:
            connection.execute(
                "INSERT INTO records VALUES (?,?,?,?,?,?,?)",
                (
                    "submission",
                    thread_id,
                    "KanagawaWave",
                    created,
                    "submission-author",
                    created,
                    json.dumps(
                        {"title": parent_text, "selftext": ""},
                        ensure_ascii=False,
                    ),
                ),
            )
            created += 1
            connection.execute(
                "INSERT INTO records VALUES (?,?,?,?,?,?,?)",
                (
                    "comment",
                    comment_id,
                    "KanagawaWave",
                    created,
                    author_hash,
                    created,
                    json.dumps(
                        {
                            "body": reply_text,
                            "parent_id": "t3_" + thread_id,
                            "link_id": "t3_" + thread_id,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            created += 1


def build_fixture_config(database, output_dir, **overrides):
    values = {
        "database": database,
        "archive": None,
        "output_dir": output_dir,
        "train_size": 2,
        "test_size": 1,
        "review_size": 1,
        "split_seed": "test-seed",
        "max_per_author_train": 2,
        "max_per_author_test": 1,
        "max_short_ratio": 1.0,
    }
    values.update(overrides)
    return V2BuildConfig(**values)


def publication_rows():
    return (
        ("thread-1", "comment-train-1", "a" * 64, "训练上文一", "训练回复一足够长！！！"),
        ("thread-2", "comment-train-2", "b" * 64, "训练上文二", "训练回复二也足够长.jpg"),
        ("thread-0", "comment-test-1", "c" * 64, "测试上文一", "测试回复足够长😂"),
    )


class DatasetPublicationV2Tests(unittest.TestCase):
    def make_source(self, root, rows=None):
        database = Path(root) / "source.sqlite3"
        write_source_database(database, rows or publication_rows())
        return database

    def test_v2_build_publishes_exact_atomic_artifact_set(self):
        with tempfile.TemporaryDirectory() as root:
            database = self.make_source(root)
            output = Path(root) / "dataset"

            manifest = build_v2_dataset(build_fixture_config(database, output))

            self.assertEqual(
                {path.name for path in output.iterdir()},
                {
                    "forum_train.json",
                    "forum_test.json",
                    "manifest.json",
                    "human_review.json",
                    "inference_prompt.txt",
                    "checkpoint.json",
                },
            )
            train = json.loads((output / "forum_train.json").read_text("utf-8"))
            test = json.loads((output / "forum_test.json").read_text("utf-8"))
            self.assertEqual((len(train), len(test)), (2, 1))
            self.assertTrue(
                all(set(item) == {"instruction", "input", "output"} for item in train + test)
            )
            self.assertTrue(
                all(item["instruction"] == EXPECTED_INSTRUCTION for item in train + test)
            )
            self.assertEqual(manifest["counts"], {"train": 2, "test": 1, "review": 1})

    def test_v2_manifest_hashes_train_test_review_and_prompt_files(self):
        with tempfile.TemporaryDirectory() as root:
            database = self.make_source(root)
            output = Path(root) / "dataset"
            manifest = build_v2_dataset(build_fixture_config(database, output))

            expected_files = {
                "forum_train.json",
                "forum_test.json",
                "human_review.json",
                "inference_prompt.txt",
            }
            self.assertEqual(set(manifest["file_sha256"]), expected_files)
            for filename in expected_files:
                digest = hashlib.sha256((output / filename).read_bytes()).hexdigest()
                self.assertEqual(manifest["file_sha256"][filename], digest)

    def test_v2_review_rows_are_deterministic_subset_of_test(self):
        with tempfile.TemporaryDirectory() as root:
            database = self.make_source(root)
            output = Path(root) / "dataset"
            build_v2_dataset(build_fixture_config(database, output))

            test_rows = json.loads((output / "forum_test.json").read_text("utf-8"))
            review_rows = json.loads((output / "human_review.json").read_text("utf-8"))
            self.assertEqual(len(review_rows), 1)
            self.assertIn(review_rows[0], test_rows)

    def test_v2_checkpoint_contains_only_private_tokens_and_safe_status(self):
        with tempfile.TemporaryDirectory() as root:
            database = self.make_source(root)
            output = Path(root) / "dataset"
            build_v2_dataset(build_fixture_config(database, output))

            checkpoint_path = output / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text("utf-8"))
            self.assertEqual(
                set(checkpoint),
                {"version", "configuration_token", "source_token", "entries"},
            )
            self.assertEqual(os.stat(checkpoint_path).st_mode & 0o777, 0o600)
            for entry in checkpoint["entries"]:
                self.assertEqual(set(entry), {"candidate_token", "split", "status"})
                self.assertRegex(entry["candidate_token"], r"^[0-9a-f]{64}$")
                self.assertIn(entry["split"], {"train", "test"})
                self.assertEqual(entry["status"], "selected")
            serialized = json.dumps(checkpoint, ensure_ascii=False)
            for _thread, _comment, author, parent, reply in publication_rows():
                self.assertNotIn(author, serialized)
                self.assertNotIn(parent, serialized)
                self.assertNotIn(reply, serialized)

    def test_v2_checkpoint_mismatch_fails_closed_without_text(self):
        with tempfile.TemporaryDirectory() as root:
            database = self.make_source(root)
            output = Path(root) / "dataset"
            config = build_fixture_config(database, output)
            build_v2_dataset(config)
            checkpoint_path = output / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text("utf-8"))
            checkpoint["configuration_token"] = "0" * 64
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            before = {
                path.name: path.read_bytes()
                for path in output.iterdir()
            }

            with self.assertRaisesRegex(DatasetBuildError, "existing dataset is invalid") as caught:
                build_v2_dataset(config)

            self.assertNotIn("训练上文", str(caught.exception))
            self.assertEqual(before, {path.name: path.read_bytes() for path in output.iterdir()})

    def test_v2_interruption_preserves_previous_output_and_removes_stage(self):
        with tempfile.TemporaryDirectory() as root:
            database = self.make_source(root)
            output = Path(root) / "dataset"

            with mock.patch(
                "qq_openclaw_bridge.dataset.kanagawawave_v2.os.replace",
                side_effect=OSError("interrupted"),
            ):
                with self.assertRaisesRegex(DatasetBuildError, "dataset publication failed"):
                    build_v2_dataset(build_fixture_config(database, output))

            self.assertFalse(output.exists())
            self.assertEqual(list(Path(root).glob(".dataset.stage-*")), [])

    def test_v2_shortage_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            database = self.make_source(root, rows=publication_rows()[:1])
            output = Path(root) / "dataset"

            with self.assertRaisesRegex(DatasetBuildError, "insufficient safe candidates"):
                build_v2_dataset(build_fixture_config(database, output))

            self.assertFalse(output.exists())
            self.assertEqual(list(Path(root).glob(".dataset.stage-*")), [])

    def test_v2_same_input_seed_and_policy_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as root:
            database = self.make_source(root)
            first = Path(root) / "first"
            second = Path(root) / "second"

            build_v2_dataset(build_fixture_config(database, first))
            build_v2_dataset(build_fixture_config(database, second))

            self.assertEqual(
                {path.name: path.read_bytes() for path in first.iterdir()},
                {path.name: path.read_bytes() for path in second.iterdir()},
            )


class DatasetCliV2Tests(unittest.TestCase):
    def test_v2_cli_requires_exactly_one_source(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                v2_cli.parse_args(["--output-dir", "out"])
            with self.assertRaises(SystemExit):
                v2_cli.parse_args(
                    [
                        "--database",
                        "source.sqlite3",
                        "--archive",
                        "source.tar.zst",
                        "--output-dir",
                        "out",
                    ]
                )

    def test_v2_cli_defaults_match_contract_and_has_no_ollama_arguments(self):
        parsed = v2_cli.parse_args(
            ["--database", "source.sqlite3", "--output-dir", "out"]
        )
        self.assertEqual(parsed.train_size, 10_000)
        self.assertEqual(parsed.test_size, 1_000)
        self.assertEqual(parsed.review_size, 100)
        self.assertEqual(parsed.split_seed, "kanagawave-v2")
        self.assertEqual(parsed.max_per_author_train, 50)
        self.assertEqual(parsed.max_per_author_test, 10)
        self.assertEqual(parsed.max_short_ratio, 0.15)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                v2_cli.parse_args(
                    [
                        "--database",
                        "source.sqlite3",
                        "--output-dir",
                        "out",
                        "--ollama-model",
                        "anything",
                    ]
                )

    def test_v2_cli_uses_safe_failure_text(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as root, redirect_stderr(stderr):
            missing = Path(root) / "secret-source-name.sqlite3"
            result = v2_cli.main(
                [
                    "--database",
                    str(missing),
                    "--output-dir",
                    str(Path(root) / "out"),
                    "--train-size",
                    "1",
                    "--test-size",
                    "0",
                    "--review-size",
                    "0",
                ]
            )
        self.assertEqual(result, 1)
        self.assertEqual(
            stderr.getvalue().strip(),
            "dataset build failed: input database is unavailable or invalid",
        )
        self.assertNotIn("secret-source-name", stderr.getvalue())

    def test_v2_cli_builds_small_fixture_and_prints_safe_summary(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as root, redirect_stdout(stdout):
            database = Path(root) / "source.sqlite3"
            output = Path(root) / "out"
            write_source_database(database, publication_rows())
            result = v2_cli.main(
                [
                    "--database",
                    str(database),
                    "--output-dir",
                    str(output),
                    "--train-size",
                    "2",
                    "--test-size",
                    "1",
                    "--review-size",
                    "1",
                    "--split-seed",
                    "test-seed",
                    "--max-per-author-train",
                    "2",
                    "--max-per-author-test",
                    "1",
                    "--max-short-ratio",
                    "1",
                ]
            )
            summary = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(summary["counts"], {"train": 2, "test": 1, "review": 1})
        self.assertEqual(summary["status"], "ready")
        self.assertNotIn("source", summary)


if __name__ == "__main__":
    unittest.main()
