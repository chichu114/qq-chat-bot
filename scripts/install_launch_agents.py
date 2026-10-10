#!/usr/bin/env python3
import argparse
import os
import plistlib
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional


LABELS = (
    "com.chichu.qq-bridge",
    "com.chichu.qq-dashboard",
    "com.chichu.qq-style-learner",
    "com.chichu.qq-watchdog",
)
_PLACEHOLDERS = ("__PROJECT_DIR__", "__PYTHON_BIN__")


def _absolute_directory(path: Path, name: str) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ValueError("%s must be absolute" % name)
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ValueError("%s must be an existing directory" % name)
    return resolved


def _substitute(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        result = value
        for marker, replacement in replacements.items():
            result = result.replace(marker, replacement)
        if any(marker in result for marker in _PLACEHOLDERS):
            raise ValueError("unresolved LaunchAgent template placeholder")
        return result
    if isinstance(value, list):
        return [_substitute(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, replacements) for key, item in value.items()}
    return value


def _validate_payload(payload: dict[str, Any], project_dir: Path, label: str) -> None:
    if payload.get("Label") != label:
        raise ValueError("LaunchAgent label does not match its template")
    arguments = payload.get("ProgramArguments")
    if not isinstance(arguments, list) or not arguments:
        raise ValueError("LaunchAgent ProgramArguments must be a non-empty array")
    expected_arguments = {
        "com.chichu.qq-bridge": [
            "/bin/bash",
            str(project_dir / "scripts" / "start_bridge.sh"),
            "--skip-check",
        ],
        "com.chichu.qq-dashboard": [
            str(project_dir / ".venv" / "bin" / "python"),
            "-m",
            "qq_openclaw_dashboard",
            "--project-dir",
            str(project_dir),
            "--host",
            "127.0.0.1",
            "--port",
            "18790",
        ],
        "com.chichu.qq-style-learner": [
            str(project_dir / ".venv" / "bin" / "python"),
            "-m",
            "qq_openclaw_style_learner",
            "--project-dir",
            str(project_dir),
        ],
        "com.chichu.qq-watchdog": [
            str(project_dir / ".venv" / "bin" / "python"),
            "-m",
            "qq_openclaw_watchdog",
            "--project-dir",
            str(project_dir),
        ],
    }
    if arguments != expected_arguments[label]:
        raise ValueError("LaunchAgent ProgramArguments do not match the allowlist")
    expected_environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONPATH": str(project_dir / "src"),
    }
    if payload.get("EnvironmentVariables") != expected_environment:
        raise ValueError("LaunchAgent environment does not match the allowlist")
    if Path(payload.get("WorkingDirectory", "")) != project_dir:
        raise ValueError("LaunchAgent WorkingDirectory must be the project directory")
    if payload.get("StandardOutPath") != "/dev/null":
        raise ValueError("LaunchAgent stdout must be owned by application logging")
    if payload.get("StandardErrorPath") != "/dev/null":
        raise ValueError("LaunchAgent stderr must be owned by application logging")


def _lint_plist(path: Path) -> None:
    result = subprocess.run(
        ["/usr/bin/plutil", "-lint", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("generated LaunchAgent plist failed validation")


def _stage_payload(payload: dict[str, Any], target: Path) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % target.stem,
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        _lint_plist(temporary)
        return temporary
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _stage_existing(target: Path) -> Optional[Path]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        raise RuntimeError("secure LaunchAgent target validation unavailable")
    try:
        source_descriptor = os.open(
            str(target), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError(
            "existing LaunchAgent target owner must match and target must be a regular file"
        ) from None
    try:
        metadata = os.fstat(source_descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ValueError(
                "existing LaunchAgent target owner must match and target must be a regular file"
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".%s." % target.stem,
            suffix=".rollback",
            dir=str(target.parent),
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(
                source_descriptor, "rb", closefd=False
            ) as source, os.fdopen(descriptor, "wb") as destination:
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            return temporary
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
    finally:
        os.close(source_descriptor)


def _replace_file(source: Path, target: Path) -> None:
    os.replace(str(source), str(target))


def render_launch_agents(project_dir: Path, home_dir: Path) -> tuple[Path, ...]:
    project = _absolute_directory(project_dir, "project_dir")
    home = _absolute_directory(home_dir, "home_dir")
    template_dir = (project / "launchd").resolve()
    if not template_dir.is_dir() or template_dir.parent != project:
        raise ValueError("project LaunchAgent template directory is missing")

    target_dir = (home / "Library" / "LaunchAgents").resolve()
    if home not in target_dir.parents:
        raise ValueError("LaunchAgent destination must remain inside home_dir")
    target_dir.mkdir(parents=True, exist_ok=True)

    replacements = {
        "__PROJECT_DIR__": str(project),
        "__PYTHON_BIN__": str(project / ".venv" / "bin" / "python"),
    }
    staged = []
    rollbacks = []
    try:
        for label in LABELS:
            template = (template_dir / (label + ".plist.template")).resolve()
            if template.parent != template_dir or not template.is_file():
                raise ValueError("LaunchAgent template is missing")
            payload = _substitute(plistlib.loads(template.read_bytes()), replacements)
            _validate_payload(payload, project, label)
            target = target_dir / (label + ".plist")
            staged.append((_stage_payload(payload, target), target))

        for _temporary, target in staged:
            rollbacks.append((target, _stage_existing(target)))

        installed = []
        try:
            for temporary, target in staged:
                _replace_file(temporary, target)
                installed.append(target)
                os.chmod(target, 0o600)
        except Exception:
            rollback_failed = False
            for target, rollback in reversed(rollbacks):
                if target not in installed:
                    continue
                try:
                    if rollback is None:
                        target.unlink(missing_ok=True)
                    else:
                        _replace_file(rollback, target)
                        os.chmod(target, 0o600)
                except Exception:
                    rollback_failed = True
            if rollback_failed:
                raise RuntimeError(
                    "LaunchAgent install failed and rollback was incomplete"
                ) from None
            raise RuntimeError(
                "LaunchAgent install failed; the previous generation was restored"
            ) from None
        return tuple(installed)
    finally:
        for temporary, _target in staged:
            temporary.unlink(missing_ok=True)
        for _target, rollback in rollbacks:
            if rollback is not None:
                rollback.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install managed chichu LaunchAgents")
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--home-dir", type=Path, default=Path.home())
    args = parser.parse_args()
    for path in render_launch_agents(args.project_dir, args.home_dir):
        print(path)


if __name__ == "__main__":
    main()
