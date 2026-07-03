"""Entry point for running the bridge with `python -m agui_on_acp`."""

import uvicorn

from agui_on_acp.config import load_config


def main() -> None:
    """Run the AG-UI on ACP bridge server."""
    config = load_config()
    uvicorn.run(
        "agui_on_acp.main:app",
        host="0.0.0.0",
        port=config.backend_port,
        reload=True,
        log_level="info",
    )


if __name__ == "__main__":
    main()
