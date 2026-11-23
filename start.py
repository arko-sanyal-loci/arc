"""Portable Arc launcher for Ubuntu, macOS, and Windows."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the Arc runtime")
    parser.add_argument("--host", default=os.environ.get("ARC_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ARC_PORT", "7860")))
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-heartbeat", action="store_true")
    parser.add_argument("--no-gpu", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    if args.state_dir:
        os.environ["ARC_STATE_DIR"] = str(args.state_dir.expanduser())
    if args.data_dir:
        os.environ["ARC_DATA_DIR"] = str(args.data_dir.expanduser())
    if args.no_heartbeat:
        os.environ["ARC_DISABLE_HEARTBEAT"] = "1"
    if args.no_gpu:
        os.environ["ARC_DISABLE_GPU"] = "1"

    from core.platform_paths import ensure_runtime_dirs

    ensure_runtime_dirs()
    import uvicorn

    uvicorn.run(
        "app.server:app",
        host=args.host,
        port=args.port,
        log_level=os.environ.get("ARC_LOG_LEVEL", "info"),
        access_log=False,
    )


if __name__ == "__main__":
    main()
