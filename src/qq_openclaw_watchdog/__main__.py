import argparse
from pathlib import Path

from qq_openclaw_bridge.runtime.logging import (
    configure_process_logging,
    logging_options_from_env,
    write_safe_bootstrap_error,
)

from .watchdog import Watchdog, WatchdogSettings, _read_env


def main() -> None:
    parser = argparse.ArgumentParser(description="chichu QQ bridge watchdog")
    parser.add_argument("--project-dir", required=True)
    args = parser.parse_args()
    logger = None
    try:
        project = Path(args.project_dir).expanduser().resolve()
        logger = configure_process_logging(
            "watchdog", project / "logs", "info", 10485760, 5
        )
        env = _read_env(project / ".env")
        level, max_bytes, backup_count = logging_options_from_env(env)
        logger = configure_process_logging(
            "watchdog", project / "logs", level, max_bytes, backup_count
        )
        Watchdog(project, WatchdogSettings.from_env(env)).run_forever()
    except KeyboardInterrupt:
        return
    except Exception as error:
        if logger is None:
            write_safe_bootstrap_error("watchdog", error)
        else:
            logger.critical(
                "startup_failed",
                extra={"error_class": error.__class__.__name__},
            )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
