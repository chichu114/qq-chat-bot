import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "qq_openclaw_bridge"


def _canonical_import_name(value):
    return "".join(character for character in value.casefold() if character.isalnum())


def _import_violations(
    source,
    *,
    module_segments=(),
    module_prefixes=(),
    imported_names=(),
):
    tree = ast.parse(source)
    modules = set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.lstrip(".") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module.lstrip("."))
            else:
                # ``from .. import runtime`` carries the module in alias.name.
                modules.update(alias.name.lstrip(".") for alias in node.names)
            names.update(alias.name for alias in node.names)

    forbidden_segments = {value.casefold() for value in module_segments}
    forbidden_prefixes = {value.casefold() for value in module_prefixes}
    forbidden_names = {
        _canonical_import_name(value) for value in imported_names
    }
    violations = set()
    for module in modules:
        normalized = module.casefold()
        segments = tuple(part for part in normalized.split(".") if part)
        if any(part in forbidden_segments for part in segments):
            violations.add(module)
            continue
        if any(
            normalized == prefix
            or normalized.startswith(prefix + ".")
            or any(
                part == prefix or part.startswith(prefix + "_")
                for part in segments
            )
            for prefix in forbidden_prefixes
        ):
            violations.add(module)
    for name in names:
        module_name = name.casefold().lstrip(".")
        name_segments = tuple(part for part in module_name.split(".") if part)
        if any(part in forbidden_segments for part in name_segments) or any(
            module_name == prefix
            or module_name.startswith(prefix + ".")
            or any(
                part == prefix or part.startswith(prefix + "_")
                for part in name_segments
            )
            for prefix in forbidden_prefixes
        ):
            violations.add(name)
            continue
        normalized = _canonical_import_name(name)
        if any(
            normalized == forbidden or normalized.startswith(forbidden)
            for forbidden in forbidden_names
        ):
            violations.add(name)
    return violations


class ArchitectureTests(unittest.TestCase):
    def test_domain_and_orchestration_contracts_do_not_import_infrastructure(self):
        forbidden = {
            "sqlite3",
            "websockets",
            "subprocess",
            "qq_openclaw_dashboard",
            "qq_openclaw_watchdog",
        }
        files = list((SRC / "domain").glob("*.py")) + [
            SRC / "orchestration" / "models.py",
            SRC / "orchestration" / "ports.py",
        ]
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertFalse(imported & forbidden, "%s imports infrastructure" % path)

    def test_community_style_domain_does_not_import_runtime_persistence_onebot_or_http(self):
        path = SRC / "domain" / "community_style.py"
        self.assertFalse(
            _import_violations(
                path.read_text(encoding="utf-8"),
                module_segments=("runtime", "persistence", "onebot"),
                module_prefixes=(
                    "urllib", "http", "requests", "httpx", "aiohttp",
                ),
            ),
            "community style domain imports infrastructure",
        )

    def test_group_coordinator_does_not_import_source_adapters_or_source_bodies(self):
        path = SRC / "application" / "group_chat.py"
        self.assertFalse(
            _import_violations(
                path.read_text(encoding="utf-8"),
                module_segments=("reddit_source", "tieba_source", "style_sync"),
                module_prefixes=("reddit_source", "tieba_source", "style_sync"),
                imported_names=(
                    "reddit_source", "tieba_source", "style_sync",
                    "AuthorizedContent", "SourceCandidate", "SourcePage",
                ),
            ),
            "group coordinator imports source adapters or body contracts",
        )

    def test_import_guard_detects_aliases_relative_paths_and_http_prefixes(self):
        domain_source = """
from .. import runtime as safe_runtime
import httpx._client as harmless_name
from aiohttp import ClientSession as Transport
from qq_openclaw_bridge import runtime as renamed_runtime
from qq_openclaw_bridge import onebot as renamed_onebot
from transports import httpx as renamed_http
"""
        coordinator_source = """
from ..runtime import reddit_source as renamed
from somewhere import AuthorizedContent as Harmless
import package.tieba_source.adapter as hidden
"""

        self.assertEqual(
            _import_violations(
                domain_source,
                module_segments=("runtime", "persistence", "onebot"),
                module_prefixes=(
                    "urllib", "http", "requests", "httpx", "aiohttp",
                ),
            ),
            {"aiohttp", "httpx", "httpx._client", "onebot", "runtime"},
        )
        self.assertEqual(
            _import_violations(
                coordinator_source,
                module_segments=("reddit_source", "tieba_source", "style_sync"),
                module_prefixes=("reddit_source", "tieba_source", "style_sync"),
                imported_names=(
                    "reddit_source", "tieba_source", "style_sync",
                    "AuthorizedContent", "SourceCandidate", "SourcePage",
                ),
            ),
            {"AuthorizedContent", "package.tieba_source.adapter", "reddit_source"},
        )

    def test_domain_import_from_alias_names_cannot_hide_forbidden_modules(self):
        cases = (
            ("from qq_openclaw_bridge import runtime as harmless\n", "runtime"),
            ("from qq_openclaw_bridge import onebot as harmless\n", "onebot"),
            ("from transports import httpx as harmless\n", "httpx"),
        )
        for source, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    _import_violations(
                        source,
                        module_segments=("runtime", "persistence", "onebot"),
                        module_prefixes=(
                            "urllib", "http", "requests", "httpx", "aiohttp",
                        ),
                    ),
                    {expected},
                )


if __name__ == "__main__":
    unittest.main()
