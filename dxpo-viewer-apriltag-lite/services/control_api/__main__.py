"""CLI entry point for ``python -m services.control_api``."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from .application import ROOT, create_app
from .observability import configure_json_logging, log_event


MIN_CONTROL_TOKEN_LENGTH = 32


def main() -> None:
    parser = argparse.ArgumentParser(description="Spatial 3DGS FastAPI control plane")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument(
        "--features",
        default=os.environ.get("SPATIAL_APP_FEATURES", "suite"),
        help="Comma-separated feature allowlist; suite enables every registered route.",
    )
    args = parser.parse_args()
    control_token = os.environ.get("SPATIAL_CONTROL_TOKEN", "").strip()
    if len(control_token) < MIN_CONTROL_TOKEN_LENGTH or any(character.isspace() for character in control_token):
        raise SystemExit(
            "SPATIAL_CONTROL_TOKEN must be a non-whitespace token of at least "
            f"{MIN_CONTROL_TOKEN_LENGTH} characters; use scripts/start_system.sh or scripts/start_app.sh"
        )
    configure_json_logging(os.environ.get("SPATIAL_LOG_LEVEL", "INFO"))
    app = create_app(
        args.repo_root,
        args.features,
        control_token=control_token,
        allowed_origins=os.environ.get("SPATIAL_ALLOWED_ORIGINS", ""),
    )
    log_event(
        logging.getLogger("spatial.control_api"),
        logging.INFO,
        "control_api_starting",
        enabled_features=sorted(app.state.enabled_features),
    )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        access_log=False,
        log_config=None,
        server_header=False,
    )


if __name__ == "__main__":
    main()
