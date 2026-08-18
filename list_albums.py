#!/usr/bin/env python3
"""
List Immich albums with their UUIDs, so you can pick which ones to expose.

    python list_albums.py

Copy the IDs you want into ALLOWED_ALBUM_IDS in .env, comma-separated:

    ALLOWED_ALBUM_IDS=a1b2c3d4-...,e5f6g7h8-...

Leave it empty to expose the whole library.
"""

from __future__ import annotations

import os
import sys

try:
    import httpx
except ImportError:
    print("Missing dependency. Run:  pip install httpx python-dotenv")
    sys.exit(2)


def load_env(path: str = ".env") -> None:
    if not os.path.isfile(path):
        print(f"ERROR: {path} not found.")
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

    url = os.environ.get("IMMICH_URL", "").strip().rstrip("/")
    key = os.environ.get("IMMICH_API_KEY", "").strip()
    if not url or not key:
        print("ERROR: IMMICH_URL and IMMICH_API_KEY must be set in .env")
        return 1

    current = [
        a.strip()
        for a in os.environ.get("ALLOWED_ALBUM_IDS", "").replace(";", ",").split(",")
        if a.strip()
    ]

    try:
        response = httpx.get(
            f"{url}/api/albums",
            headers={"x-api-key": key, "Accept": "application/json"},
            timeout=20,
        )
    except httpx.RequestError as exc:
        print(f"ERROR: cannot reach Immich at {url}: {exc}")
        return 1

    if response.status_code in (401, 403):
        print(f"ERROR: API key rejected ({response.status_code}).")
        print("       The key needs album read permission.")
        return 1
    if response.status_code != 200:
        print(f"ERROR: /api/albums returned {response.status_code}")
        print(response.text[:300])
        return 1

    albums = response.json()
    if not albums:
        print("No albums found for this user.")
        print("Create one in Immich, add the photos you want exposed, then re-run.")
        return 0

    print(f"\n{len(albums)} album(s) visible to this API key:\n")
    width = max(len(a.get("albumName") or "") for a in albums)
    width = min(max(width, 20), 45)

    total_scoped = 0
    for album in sorted(albums, key=lambda a: a.get("albumName") or ""):
        name = (album.get("albumName") or "(unnamed)")[:width]
        count = album.get("assetCount", "?")
        marker = "  <-- currently allowed" if album["id"] in current else ""
        if album["id"] in current and isinstance(count, int):
            total_scoped += count
        print(f"  {name:<{width}}  {count:>6} assets   {album['id']}{marker}")

    print()
    if current:
        print(f"ALLOWED_ALBUM_IDS is set: {len(current)} album(s), ~{total_scoped} assets.")
        stale = [c for c in current if c not in {a["id"] for a in albums}]
        if stale:
            print(f"WARNING: {len(stale)} configured ID(s) not found: {', '.join(stale)}")
    else:
        print("ALLOWED_ALBUM_IDS is empty - the whole library is exposed.")
        print("To restrict, add a line to .env like:")
        print(f"    ALLOWED_ALBUM_IDS={albums[0]['id']}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())