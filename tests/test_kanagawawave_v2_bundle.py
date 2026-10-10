import json
import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.package_kanagawawave_v2_training import build_training_bundle
from scripts.validate_kanagawawave_v2_bundle import validate_training_bundle

from qq_openclaw_bridge.community_prompts import KANAGAWAWAVE_V2_INSTRUCTION


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = PROJECT_ROOT / "training" / "kanagawawave_v2"


def load_config(name):
    return json.loads((TEMPLATE_ROOT / "configs" / name).read_text("utf-8"))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def write_dataset(root):
    root.mkdir()
    train = [
        {
            "instruction": KANAGAWAWAVE_V2_INSTRUCTION,
            "input": "训练输入%d" % index,
            "output": "训练回复%d，长度足够" % index,
        }
        for index in range(10_000)
    ]
    test = [
        {
            "instruction": KANAGAWAWAVE_V2_INSTRUCTION,
            "input": "测试输入%d" % index,
            "output": "测试回复%d，长度足够" % index,
        }
        for index in range(1_000)
    ]
    review = test[:100]
    payloads = {
        "forum_train.json": json_bytes(train),
        "forum_test.json": json_bytes(test),
        "human_review.json": json_bytes(review),
        "inference_prompt.txt": KANAGAWAWAVE_V2_INSTRUCTION.encode("utf-8"),
    }
    for filename, payload in payloads.items():
        (root / filename).write_bytes(payload)
    manifest = {
        "version": 2,
        "configuration_token": "a" * 64,
        "human_review_required": True,
        "counts": {"train": 10_000, "test": 1_000, "review": 100},
        "limits": {
            "max_per_author_train": 50,
            "max_per_author_test": 10,
            "max_short_ratio": 0.15,
        },
        "audit": {"thread_overlap_count": 0, "duplicate_example_count": 0},
        "rejection_counts": {},
        "style_signals": {"train": {}, "test": {}},
        "file_sha256": {
            filename: sha256(payload) for filename, payload in payloads.items()
        },
    }
    (root / "manifest.json").write_bytes(json_bytes(manifest))
    return root


def refresh_manifest_hash(dataset_dir, filename):
    path = dataset_dir / "manifest.json"
    manifest = json.loads(path.read_text("utf-8"))
    manifest["file_sha256"][filename] = sha256(
        (dataset_dir / filename).read_bytes()
    )
    path.write_bytes(json_bytes(manifest))


class KanagawaWaveV2TrainingTemplateTests(unittest.TestCase):
    def test_train_configs_pin_the_approved_qwen3_qlora_recipe(self):
        for filename in ("train_4060_8gb.yaml", "train_4060_low_memory.yaml"):
            with self.subTest(filename=filename):
                config = load_config(filename)
                self.assertEqual(
                    config["model_name_or_path"],
                    "Qwen/Qwen3-4B-Instruct-2507",
                )
                self.assertEqual(config["quantization_bit"], 4)
                self.assertEqual(config["quantization_method"], "bnb")
                self.assertEqual(config["quantization_type"], "nf4")
                self.assertIs(config["double_quantization"], True)
                self.assertEqual(config["finetuning_type"], "lora")
                self.assertEqual(config["lora_rank"], 8)
                self.assertEqual(config["lora_alpha"], 16)
                self.assertEqual(config["lora_dropout"], 0.05)
                self.assertEqual(config["lora_target"], "all")
                self.assertEqual(config["learning_rate"], 0.0001)
                self.assertEqual(config["num_train_epochs"], 2.0)
                self.assertEqual(config["template"], "qwen3_nothink")
                self.assertEqual(config["dataset"], "forum_train_v2")
                self.assertEqual(config["eval_dataset"], "forum_test_v2")

    def test_low_memory_config_differs_only_in_documented_settings(self):
        normal = load_config("train_4060_8gb.yaml")
        low_memory = load_config("train_4060_low_memory.yaml")
        allowed_differences = {
            "cutoff_len",
            "gradient_accumulation_steps",
            "preprocessing_num_workers",
            "output_dir",
        }

        self.assertEqual(set(normal), set(low_memory))
        actual_differences = {
            key for key in normal if normal[key] != low_memory[key]
        }
        self.assertEqual(actual_differences, allowed_differences)
        self.assertEqual(normal["cutoff_len"], 2048)
        self.assertEqual(low_memory["cutoff_len"], 1024)
        self.assertEqual(normal["gradient_accumulation_steps"], 8)
        self.assertEqual(low_memory["gradient_accumulation_steps"], 16)
        self.assertEqual(normal["preprocessing_num_workers"], 4)
        self.assertEqual(low_memory["preprocessing_num_workers"], 2)

    def test_prompt_and_alpaca_registry_are_exact(self):
        self.assertEqual(
            (TEMPLATE_ROOT / "INFERENCE_PROMPT.txt").read_bytes(),
            KANAGAWAWAVE_V2_INSTRUCTION.encode("utf-8"),
        )
        registry = json.loads(
            (TEMPLATE_ROOT / "data" / "dataset_info.json").read_text("utf-8")
        )
        self.assertEqual(set(registry), {"forum_train_v2", "forum_test_v2"})
        self.assertEqual(
            registry["forum_train_v2"]["columns"],
            {"prompt": "instruction", "query": "input", "response": "output"},
        )


class KanagawaWaveV2BundleTests(unittest.TestCase):
    def test_bundle_requires_exact_v2_counts_and_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            dataset = write_dataset(temporary_path / "dataset")
            train_path = dataset / "forum_train.json"
            train = json.loads(train_path.read_text("utf-8"))
            train.pop()
            train_path.write_bytes(json_bytes(train))
            refresh_manifest_hash(dataset, "forum_train.json")

            with self.assertRaisesRegex(ValueError, "10000"):
                build_training_bundle(dataset, temporary_path / "bad-count.zip")

        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            dataset = write_dataset(temporary_path / "dataset")
            (dataset / "inference_prompt.txt").write_text("错误指令", "utf-8")
            refresh_manifest_hash(dataset, "inference_prompt.txt")

            with self.assertRaisesRegex(ValueError, "prompt"):
                build_training_bundle(dataset, temporary_path / "bad-prompt.zip")

    def test_bundle_recomputes_and_verifies_dataset_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            dataset = write_dataset(temporary_path / "dataset")
            with (dataset / "forum_test.json").open("ab") as output:
                output.write(b" ")

            with self.assertRaisesRegex(ValueError, "hash"):
                build_training_bundle(dataset, temporary_path / "bad-hash.zip")

    def test_bundle_rejects_extra_alpaca_keys_or_v1_style_labels(self):
        cases = (
            ("extra key", lambda row: row.update({"style": "meme"})),
            (
                "v1 style label",
                lambda row: row.update({"instruction": "[STYLE=NORMAL] old prompt"}),
            ),
        )
        for label, mutate in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                temporary_path = Path(temporary)
                dataset = write_dataset(temporary_path / "dataset")
                train_path = dataset / "forum_train.json"
                train = json.loads(train_path.read_text("utf-8"))
                mutate(train[0])
                train_path.write_bytes(json_bytes(train))
                refresh_manifest_hash(dataset, "forum_train.json")

                with self.assertRaises(ValueError):
                    build_training_bundle(dataset, temporary_path / "bad-row.zip")

    def test_bundle_contains_no_checkpoint_or_source_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            dataset = write_dataset(temporary_path / "dataset")
            (dataset / "checkpoint.json").write_text(
                '{"source_token":"secret"}', "utf-8"
            )
            (dataset / "source.sqlite3").write_bytes(b"private source")
            output_zip = temporary_path / "bundle.zip"

            build_training_bundle(dataset, output_zip)

            with zipfile.ZipFile(output_zip) as archive:
                names = set(archive.namelist())
                payload = b"".join(archive.read(name) for name in names)
            self.assertFalse(any("checkpoint" in name.casefold() for name in names))
            self.assertFalse(any("sqlite" in name.casefold() for name in names))
            self.assertNotIn(b"source_token", payload)
            self.assertNotIn(b"private source", payload)

    def test_bundle_zip_is_deterministic_and_validates_after_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            dataset = write_dataset(temporary_path / "dataset")
            first = temporary_path / "first.zip"
            second = temporary_path / "second.zip"

            first_summary = build_training_bundle(dataset, first)
            second_summary = build_training_bundle(dataset, second)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            extracted = temporary_path / "extracted"
            with zipfile.ZipFile(first) as archive:
                archive.extractall(extracted)
            validation = validate_training_bundle(extracted)
            self.assertEqual(validation["status"], "valid")
            self.assertEqual(
                validation["counts"],
                {"train": 10_000, "test": 1_000, "review": 100},
            )
            self.assertEqual(first_summary, second_summary)


if __name__ == "__main__":
    unittest.main()
