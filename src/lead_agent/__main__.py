"""Run the server: ``python -m lead_agent`` or the ``lead-agent`` command."""

from __future__ import annotations

import argparse
import logging

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ai-lead-agent server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--reload", action="store_true", help="Reload on code changes.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # httpx logs full request URLs, which can contain tokens (e.g. Telegram).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(
        "lead_agent.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
