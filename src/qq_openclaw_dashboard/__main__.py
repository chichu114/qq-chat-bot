import argparse
import os
from pathlib import Path

from qq_openclaw_bridge.runtime.logging import (
    configure_process_logging,
    logging_options_from_env,
    write_safe_bootstrap_error,
)

from .controller import DashboardController, _read_env_file
from .server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="chichu QQ 智能体本地管理面板")
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port", type=int, default=os.environ.get("DASHBOARD_PORT", "18790")
    )
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        parser.error("dashboard only supports host 127.0.0.1")

    logger = None
    server = None
    try:
        project = Path(args.project_dir).expanduser().resolve()
        logger = configure_process_logging(
            "dashboard", project / "logs", "info", 10485760, 5
        )
        env = _read_env_file(project / ".env")
        level, max_bytes, backup_count = logging_options_from_env(env)
        logger = configure_process_logging(
            "dashboard", project / "logs", level, max_bytes, backup_count
        )
        controller = DashboardController.from_project(project)
        server = create_server(controller, args.host, args.port)
        print(
            "chichu dashboard: http://%s:%d" % (args.host, server.server_port),
            flush=True,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        if logger is None:
            write_safe_bootstrap_error("dashboard", error)
        else:
            logger.critical(
                "startup_failed",
                extra={"error_class": error.__class__.__name__},
            )
        raise SystemExit(1) from None
    finally:
        if server is not None:
            server.server_close()


if __name__ == "__main__":
    main()
