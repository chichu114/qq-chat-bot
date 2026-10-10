from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PublicArchitectureDocumentationTests(unittest.TestCase):
    def test_readme_explains_napcat_onebot_foundation_and_upgrade_layers(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("NapCatQQ 提供 QQ 登录与会话层", readme)
        self.assertIn("OneBot v11 提供标准事件与动作协议", readme)
        self.assertIn("不是对 NapCat 或 OneBot 源码的修改", readme)
        self.assertIn(
            "QQ -> NapCatQQ -> OneBot v11 WebSocket -> 本项目桥接与策略层",
            readme,
        )
        for layer in (
            "私聊与群聊分流",
            "KanagawaWave 风格模型",
            "可靠发件箱",
            "受限自愈监控",
        ):
            self.assertIn(layer, readme)

    def test_readme_links_the_overall_design_and_status_document(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn(
            "[总体设计与完成状态](docs/overall-design-status-zh.md)",
            readme,
        )


class OverallDesignStatusDocumentationTests(unittest.TestCase):
    def test_document_separates_implemented_enabled_and_future_work(self):
        document = (
            ROOT / "docs" / "overall-design-status-zh.md"
        ).read_text(encoding="utf-8")

        self.assertIn("# QQ 智能体总体设计与完成状态", document)
        self.assertIn("状态基准：2026-10-11", document)
        for marker in (
            "✅ 已完成",
            "🟡 已实现但默认关闭",
            "🟠 部分完成",
            "⬜ 尚未实现",
        ):
            self.assertIn(marker, document)
        for component in (
            "NapCatQQ",
            "OneBot v11",
            "OpenClaw",
            "可靠发件箱",
            "KanagawaWave",
            "F1 影子路由",
            "E4-B",
            "E4-C",
            "双模型议会",
            "DAG",
        ):
            self.assertIn(component, document)
        self.assertIn("代码完成不等于当前机器已经启用", document)
        self.assertIn("公开仓库不包含训练后的模型权重", document)
        self.assertIn("不是通用全模型训练器", document)


class PublicKanagawaTrainingDocumentationTests(unittest.TestCase):
    def test_root_readme_points_to_existing_pinned_training_bundle(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("training/kanagawawave_v2", readme)
        self.assertIn("Qwen/Qwen3-4B-Instruct-2507", readme)
        self.assertIn("4-bit NF4 QLoRA", readme)
        self.assertIn("scripts/package_kanagawave_v2_training.py", readme)
        self.assertIn("llamafactory-cli train configs/train_4060_8gb.yaml", readme)
        self.assertIn("不是全模型通用训练器", readme)

    def test_training_readme_documents_exact_ollama_compatibility_boundary(self):
        guide = (
            ROOT / "training" / "kanagawawave_v2" / "README_zh.md"
        ).read_text(encoding="utf-8")

        self.assertIn("本训练配方直接适配", guide)
        self.assertIn("Qwen/Qwen3-4B-Instruct-2507", guide)
        self.assertIn("qwen3_nothink", guide)
        self.assertIn("不能直接套到其他模型家族", guide)
        self.assertIn("Safetensors", guide)
        self.assertIn("GGUF", guide)
        self.assertIn("ollama create qwen3-4b-kanagawawave-v2", guide)


if __name__ == "__main__":
    unittest.main()
