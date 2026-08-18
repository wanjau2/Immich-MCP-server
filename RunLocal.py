#!/usr/bin/env python3
"""
Run the MCP server directly on this machine, no Docker.

Fastest way to iterate while you are still sorting out config. Reads .env the
same way the container does, then serves on http://127.0.0.1:8099 with
auto-reload so edits to app/immich_mcp.py take effect immediately.

    pip install -r app/requirements.txt python-dotenv
    python run_local.py

Ctrl-C to stop.
"""

from __future__ import annotations

import os
import sys

PORT = int(os.environ.get("LOCAL_PORT", "8099"))


def load_env(path: str = ".env") -> None:
    if not os.path.isfile(path):
        print(f"ERROR: {path} not found. Run:  copy .env.example .env")
        sys.exit(1)
    try:
        from dotenv import load_dotenv

        load_dotenv(path)
    except ImportError:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ[key.strip()] = value.strip().strip("\"'")


def main() -> int:
    load_env()

    missing = [
        name
        for name in ("IMMICH_URL", "IMMICH_API_KEY", "MCP_BEARER_TOKEN")
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        print(f"ERROR: .env is missing values for: {', '.join(missing)}")
        return 1

    if not os.path.isfile(os.path.join("app", "immich_mcp.py")):
        print("ERROR: app/immich_mcp.py not found.")
        print("       Expected layout:  ./Dockerfile  ./app/immich_mcp.py")
        return 1

    # Make the module importable without installing anything.
    sys.path.insert(0, os.path.abspath("app"))

    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn not installed. Run:")
        print("       pip install -r app/requirements.txt python-dotenv")
        return 1

    token = os.environ["MCP_BEARER_TOKEN"]
    print("=" * 68)
    print(f"  Immich MCP server  ->  http://127.0.0.1:{PORT}")
    print(f"  Upstream Immich    ->  {os.environ['IMMICH_URL']}")
    print()
    print("  Health check:")
    print(f"    curl.exe http://127.0.0.1:{PORT}/healthz")
    print()
    print("  Full handshake test (second terminal):")
    print(f"    python smoke_test.py http://127.0.0.1:{PORT} {token}")
    print("=" * 68)
    print()

    uvicorn.run(
        "immich_mcp:app",
        host="127.0.0.1",
        port=PORT,
        reload=True,
        reload_dirs=["app"],
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)