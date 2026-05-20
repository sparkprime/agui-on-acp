"""Entry point for running the bridge with `python -m backend`."""

import uvicorn

from backend.config import load_config


def main() -> None:
    """Run the ACP → AG-UI bridge server."""
    config = load_config()
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=config.backend_port,
        reload=True,
        log_level="info",
    )


if __name__ == "__main__":
    main()
