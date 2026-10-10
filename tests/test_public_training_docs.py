from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PublicDocumentationIntegrityTests(unittest.TestCase):
    def test_readme_local_markdown_links_resolve_inside_repository(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        targets = re.findall(r"\[[^]]+\]\(([^)]+)\)", readme)

        local_targets = [
            target.split("#", 1)[0]
            for target in targets
            if target and "://" not in target and not target.startswith("#")
        ]

        self.assertGreater(len(local_targets), 0)
        for target in local_targets:
            with self.subTest(target=target):
                self.assertTrue((ROOT / target).exists(), target)


if __name__ == "__main__":
    unittest.main()
