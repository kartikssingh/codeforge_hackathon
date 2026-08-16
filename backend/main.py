#!/usr/bin/env python3
"""ARIA backend entry point.

Usage::

    python backend/main.py                 # 127.0.0.1:8000
    python backend/main.py --port 8100
    python backend/main.py --reload        # development

The Electron main process spawns exactly this file and waits for ``/health``.
Host and port also come from ``ARIA_API_HOST`` / ``ARIA_API_PORT`` (or the
legacy ``DL_API_HOST`` / ``DL_API_PORT`` that Electron sets), so the two sides
cannot disagree about where the API lives — they did before, because the old
config hard-coded 127.0.0.1:8000 and ignored the environment entirely.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make ``aria`` importable no matter which directory the process was started in.
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from aria import __version__  # noqa: E402  (import after sys.path fix)
from aria.api import create_app  # noqa: E402
from aria.config import settings  # noqa: E402

app = create_app()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the ARIA backend")
    parser.add_argument("--host", default=settings.api.host)
    parser.add_argument("--port", type=int, default=settings.api.port)
    parser.add_argument(
        "--reload", action="store_true", help="Auto-reload on code changes (development)"
    )
    parser.add_argument("--log-level", default=settings.observability.log_level.lower())
    parser.add_argument("--version", action="version", version=f"ARIA {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is not installed. Install the backend requirements first:\n"
            "  pip install -r backend/requirements.txt",
            file=sys.stderr,
        )
        return 1

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"WARNING: binding to {args.host} exposes ARIA beyond this machine. "
            "The API has no authentication — keep it on 127.0.0.1.",
            file=sys.stderr,
        )

    if args.reload:
        # The reloader re-imports "main:app" in a child process, so it has to be
        # able to find this file. Every path in config is absolute, so moving
        # the working directory is safe.
        os.chdir(BACKEND_DIR)

    uvicorn.run(
        "main:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        access_log=False,  # the audit trail is the record that matters here
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
