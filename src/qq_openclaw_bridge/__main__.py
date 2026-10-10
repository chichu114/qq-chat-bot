import asyncio
import os
from pathlib import Path

from .config import Config
from .runtime.composition import build_application
from .runtime.logging import configure_process_logging, write_safe_bootstrap_error


def main() -> None:
    logger = None
    try:
        project = Path.cwd().resolve()
        logger = configure_process_logging(
            "bridge", project / "logs", "info", 10485760, 5
        )
        config = Config.from_env(os.environ)
        logger = configure_process_logging(
            "bridge",
            project / "logs",
            config.log_level,
            config.log_max_bytes,
            config.log_backup_count,
        )

        async def run() -> None:
            bridge = build_application(config)
            await bridge.run_forever()

        asyncio.run(run())
    except KeyboardInterrupt:
        return
    except Exception as error:
        if logger is None:
            write_safe_bootstrap_error("bridge", error)
        else:
            logger.critical(
                "startup_failed",
                extra={"error_class": error.__class__.__name__},
            )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
