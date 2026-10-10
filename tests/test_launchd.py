import plistlib
import os
import shutil
import stat
import tempfile
from pathlib import Path
from unittest import mock
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_LABELS = {
    "com.chichu.qq-bridge",
    "com.chichu.qq-dashboard",
    "com.chichu.qq-style-learner",
    "com.chichu.qq-watchdog",
}


class LaunchAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.project_dir = root / "project"
        self.home_dir = root / "home"
        (self.project_dir / "launchd").mkdir(parents=True)
        self.home_dir.mkdir()
        for template in (ROOT / "launchd").glob("*.plist.template"):
            shutil.copy2(template, self.project_dir / "launchd" / template.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_rendered_plists_use_fixed_absolute_programs_and_safe_runtime_fields(self):
        from scripts.install_launch_agents import render_launch_agents

        with mock.patch("scripts.install_launch_agents._lint_plist") as lint:
            paths = render_launch_agents(self.project_dir, self.home_dir)

        self.assertEqual(len(paths), 4)
        values = [plistlib.loads(path.read_bytes()) for path in paths]
        self.assertEqual({item["Label"] for item in values}, EXPECTED_LABELS)
        self.assertEqual(lint.call_count, 4)
        for path, item in zip(paths, values):
            self.assertEqual(
                path.parent,
                (self.home_dir / "Library" / "LaunchAgents").resolve(),
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(Path(item["WorkingDirectory"]).is_absolute())
            self.assertEqual(Path(item["WorkingDirectory"]), self.project_dir.resolve())
            self.assertTrue(Path(item["ProgramArguments"][0]).is_absolute())
            self.assertNotIn("/bin/sh -c", " ".join(item["ProgramArguments"]))
            self.assertEqual(item["StandardOutPath"], "/dev/null")
            self.assertEqual(item["StandardErrorPath"], "/dev/null")
            self.assertEqual(
                item["EnvironmentVariables"]["PATH"],
                "/usr/bin:/bin:/usr/sbin:/sbin",
            )
            self.assertEqual(
                item["EnvironmentVariables"]["PYTHONPATH"],
                str(self.project_dir.resolve() / "src"),
            )

        by_label = {item["Label"]: item for item in values}
        for label in (
            "com.chichu.qq-bridge",
            "com.chichu.qq-style-learner",
            "com.chichu.qq-watchdog",
        ):
            self.assertEqual(by_label[label]["KeepAlive"], {"SuccessfulExit": False})
            self.assertGreaterEqual(by_label[label]["ThrottleInterval"], 30)
        dashboard = by_label["com.chichu.qq-dashboard"]
        self.assertTrue(dashboard["RunAtLoad"])
        self.assertNotIn("KeepAlive", dashboard)
        dashboard_args = dashboard["ProgramArguments"]
        self.assertEqual(dashboard_args[dashboard_args.index("--host") + 1], "127.0.0.1")

        learner = by_label["com.chichu.qq-style-learner"]
        self.assertEqual(
            learner["ProgramArguments"],
            [
                str(self.project_dir.resolve() / ".venv" / "bin" / "python"),
                "-m",
                "qq_openclaw_style_learner",
                "--project-dir",
                str(self.project_dir.resolve()),
            ],
        )
        self.assertFalse(learner["RunAtLoad"])

    def test_install_is_idempotent_with_style_learner_template(self):
        from scripts.install_launch_agents import render_launch_agents

        with mock.patch("scripts.install_launch_agents._lint_plist"):
            first = render_launch_agents(self.project_dir, self.home_dir)
            first_bytes = {path.name: path.read_bytes() for path in first}
            second = render_launch_agents(self.project_dir, self.home_dir)

        self.assertEqual(tuple(path.name for path in first), tuple(path.name for path in second))
        self.assertEqual(first_bytes, {path.name: path.read_bytes() for path in second})

    def test_relative_project_or_home_paths_are_rejected(self):
        from scripts.install_launch_agents import render_launch_agents

        with self.assertRaises(ValueError):
            render_launch_agents(Path("relative-project"), self.home_dir)
        with self.assertRaises(ValueError):
            render_launch_agents(self.project_dir, Path("relative-home"))

    def test_template_program_arguments_must_match_the_label_allowlist(self):
        from scripts.install_launch_agents import render_launch_agents

        template = self.project_dir / "launchd" / "com.chichu.qq-bridge.plist.template"
        payload = plistlib.loads(template.read_bytes())
        payload["ProgramArguments"] = ["/bin/bash", "-c", "echo unsafe"]
        template.write_bytes(plistlib.dumps(payload))

        with mock.patch("scripts.install_launch_agents._lint_plist"), self.assertRaises(
            ValueError
        ):
            render_launch_agents(self.project_dir, self.home_dir)

    def test_template_environment_must_match_the_minimal_allowlist(self):
        from scripts.install_launch_agents import render_launch_agents

        template = self.project_dir / "launchd" / "com.chichu.qq-bridge.plist.template"
        payload = plistlib.loads(template.read_bytes())
        payload["EnvironmentVariables"]["DYLD_INSERT_LIBRARIES"] = "/tmp/unsafe"
        template.write_bytes(plistlib.dumps(payload))

        with mock.patch("scripts.install_launch_agents._lint_plist"), self.assertRaises(
            ValueError
        ):
            render_launch_agents(self.project_dir, self.home_dir)

    def test_lint_failure_does_not_install_partial_plist(self):
        from scripts.install_launch_agents import render_launch_agents

        with mock.patch(
            "scripts.install_launch_agents._lint_plist",
            side_effect=(None, RuntimeError("invalid plist")),
        ), self.assertRaises(RuntimeError):
            render_launch_agents(self.project_dir, self.home_dir)

        target = self.home_dir / "Library" / "LaunchAgents"
        self.assertEqual(list(target.glob("*.plist")), [])
        self.assertEqual(list(target.glob("*.tmp")), [])

    def test_replace_failure_restores_the_previous_complete_generation(self):
        from scripts.install_launch_agents import render_launch_agents

        with mock.patch("scripts.install_launch_agents._lint_plist"):
            paths = render_launch_agents(self.project_dir, self.home_dir)
        original = {path.name: path.read_bytes() for path in paths}

        dashboard_template = (
            self.project_dir
            / "launchd"
            / "com.chichu.qq-dashboard.plist.template"
        )
        payload = plistlib.loads(dashboard_template.read_bytes())
        payload["ProcessType"] = "Interactive"
        dashboard_template.write_bytes(plistlib.dumps(payload))

        real_replace = os.replace
        failed = False

        def fail_dashboard_once(source, target):
            nonlocal failed
            if Path(target).name == "com.chichu.qq-dashboard.plist" and not failed:
                failed = True
                raise OSError("replace failed")
            return real_replace(source, target)

        with mock.patch("scripts.install_launch_agents._lint_plist"), mock.patch(
            "scripts.install_launch_agents._replace_file",
            side_effect=fail_dashboard_once,
        ), self.assertRaises(RuntimeError):
            render_launch_agents(self.project_dir, self.home_dir)

        self.assertEqual({path.name: path.read_bytes() for path in paths}, original)

    def test_bad_owner_aborts_before_any_existing_generation_is_replaced(self):
        from scripts.install_launch_agents import render_launch_agents

        with mock.patch("scripts.install_launch_agents._lint_plist"):
            paths = render_launch_agents(self.project_dir, self.home_dir)
        original = {path.name: path.read_bytes() for path in paths}
        bad_metadata = mock.Mock(
            st_mode=stat.S_IFREG | 0o600,
            st_uid=os.getuid() + 1,
        )

        with mock.patch(
            "scripts.install_launch_agents._lint_plist"
        ), mock.patch(
            "scripts.install_launch_agents.os.fstat", return_value=bad_metadata
        ), mock.patch(
            "scripts.install_launch_agents._replace_file"
        ) as replace, self.assertRaisesRegex(ValueError, "owner"):
            render_launch_agents(self.project_dir, self.home_dir)

        replace.assert_not_called()
        self.assertEqual({path.name: path.read_bytes() for path in paths}, original)

    def test_existing_target_swap_to_symlink_is_rejected_without_following_it(self):
        from scripts.install_launch_agents import _stage_existing

        target = self.home_dir / "existing.plist"
        target.write_text("trusted", encoding="utf-8")
        outside = self.home_dir / "outside.plist"
        outside.write_text("outside", encoding="utf-8")
        real_open = os.open
        swapped = False

        def swap_then_open(path, flags, *arguments, **keywords):
            nonlocal swapped
            if Path(path) == target and not swapped:
                target.unlink()
                target.symlink_to(outside)
                swapped = True
            return real_open(path, flags, *arguments, **keywords)

        with mock.patch(
            "scripts.install_launch_agents.os.open", side_effect=swap_then_open
        ), self.assertRaisesRegex(ValueError, "regular file"):
            _stage_existing(target)

        self.assertTrue(swapped)
        self.assertTrue(target.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_symlink_target_aborts_before_any_generation_is_replaced(self):
        from scripts.install_launch_agents import render_launch_agents

        with mock.patch("scripts.install_launch_agents._lint_plist"):
            paths = render_launch_agents(self.project_dir, self.home_dir)
        outside = self.home_dir / "outside.plist"
        outside.write_text("outside", encoding="utf-8")
        victim = paths[2]
        victim.unlink()
        victim.symlink_to(outside)
        unchanged = {
            path.name: path.read_bytes()
            for path in paths
            if path != victim
        }

        with mock.patch(
            "scripts.install_launch_agents._lint_plist"
        ), mock.patch(
            "scripts.install_launch_agents._replace_file"
        ) as replace, self.assertRaisesRegex(ValueError, "regular file"):
            render_launch_agents(self.project_dir, self.home_dir)

        replace.assert_not_called()
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in paths
                if path != victim
            },
            unchanged,
        )


if __name__ == "__main__":
    unittest.main()
