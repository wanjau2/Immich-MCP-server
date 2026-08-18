#!/usr/bin/env python3
"""
Preflight check for the Immich MCP server.

Validates your .env and confirms this machine can actually reach Immich,
BEFORE you spend time on Docker. Run it from the project root:

    pip install httpx python-dotenv
    python preflight.py

Exit code 0 means you are clear to build.
"""

from __future__ import annotations

import os
import socket
import sys
from urllib.parse import urlparse

try:
    import httpx
except ImportError:
    print("Missing dependency. Run:  pip install httpx python-dotenv")
    sys.exit(2)

# --------------------------------------------------------------------------
# Output helpers (Windows-safe, no unicode symbols)
# --------------------------------------------------------------------------

FAILURES: list[str] = []
WARNINGS: list[str] = []


def ok(msg: str) -> None:
    print(f"  [ OK ]  {msg}")


def fail(msg: str, fix: str = "") -> None:
    print(f"  [FAIL]  {msg}")
    if fix:
        print(f"          fix: {fix}")
    FAILURES.append(msg)


def warn(msg: str, fix: str = "") -> None:
    print(f"  [WARN]  {msg}")
    if fix:
        print(f"          {fix}")
    WARNINGS.append(msg)


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


# --------------------------------------------------------------------------
# 1. Project layout
# --------------------------------------------------------------------------

section("1. Project layout")

REQUIRED_FILES = {
    "Dockerfile": "Dockerfile",
    "docker-compose.yml": "docker-compose.yml",
    "app/immich_mcp.py": os.path.join("app", "immich_mcp.py"),
    "app/requirements.txt": os.path.join("app", "requirements.txt"),
}

for label, path in REQUIRED_FILES.items():
    if os.path.isfile(path):
        ok(f"{label} present")
    else:
        stray = os.path.basename(path)
        if os.path.isfile(stray):
            fail(
                f"{label} missing, but {stray} is in the project root",
                f"mkdir app  &&  move {stray} app\\",
            )
        else:
            fail(f"{label} missing", "re-download the project files")

if os.path.isfile(".dockerignore"):
    content = open(".dockerignore", encoding="utf-8").read()
    if ".env" in content:
        ok(".dockerignore excludes .env")
    else:
        warn(".dockerignore does not list .env", "secrets would be baked into the image")
else:
    warn(".dockerignore missing", "your .env will be copied into the image layer")


# --------------------------------------------------------------------------
# 2. Environment
# --------------------------------------------------------------------------

section("2. Environment (.env)")

if not os.path.isfile(".env"):
    fail(".env not found", "copy .env.example .env   then fill it in")
    print("\nCannot continue without .env.")
    sys.exit(1)
ok(".env found")

try:
    from dotenv import load_dotenv

    load_dotenv(".env")
except ImportError:
    # Minimal parser so the script still works without python-dotenv.
    for line in open(".env", encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

IMMICH_URL = os.environ.get("IMMICH_URL", "").strip().rstrip("/")
IMMICH_API_KEY = os.environ.get("IMMICH_API_KEY", "").strip()
BEARER = os.environ.get("MCP_BEARER_TOKEN", "").strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")

if IMMICH_URL:
    ok(f"IMMICH_URL = {IMMICH_URL}")
else:
    fail("IMMICH_URL is empty")

if IMMICH_API_KEY:
    ok(f"IMMICH_API_KEY set ({len(IMMICH_API_KEY)} chars)")
else:
    fail("IMMICH_API_KEY is empty", "Immich > Account Settings > API Keys > New")

if len(BEARER) >= 32:
    ok(f"MCP_BEARER_TOKEN set ({len(BEARER)} chars)")
elif BEARER:
    warn(f"MCP_BEARER_TOKEN is short ({len(BEARER)} chars)", "use: openssl rand -hex 32")
else:
    fail("MCP_BEARER_TOKEN is empty", "generate one: openssl rand -hex 32")

if PUBLIC_URL:
    ok(f"PUBLIC_URL = {PUBLIC_URL}")
else:
    warn("PUBLIC_URL empty", "results will have no clickable links (not fatal)")

host = urlparse(IMMICH_URL).hostname or ""
if host in ("immich_server", "immich-server"):
    warn(
        "IMMICH_URL uses a Docker service name",
        "that only resolves on the NAS; use the LAN IP to test from this laptop",
    )

if FAILURES:
    print("\nFix the failures above before continuing.")
    sys.exit(1)


# --------------------------------------------------------------------------
# 3. Network reachability
# --------------------------------------------------------------------------

section("3. Network reachability")

parsed = urlparse(IMMICH_URL)
port = parsed.port or (443 if parsed.scheme == "https" else 80)

try:
    with socket.create_connection((parsed.hostname, port), timeout=6):
        ok(f"TCP connect to {parsed.hostname}:{port}")
except socket.gaierror:
    fail(f"cannot resolve hostname '{parsed.hostname}'", "use the NAS IP address")
    sys.exit(1)
except (socket.timeout, OSError) as exc:
    fail(
        f"cannot reach {parsed.hostname}:{port} ({exc.__class__.__name__})",
        "check the NAS is on, Immich is running, and no firewall blocks the port",
    )
    sys.exit(1)


# --------------------------------------------------------------------------
# 4. Immich API
# --------------------------------------------------------------------------

section("4. Immich API")

client = httpx.Client(
    base_url=f"{IMMICH_URL}/api",
    headers={"x-api-key": IMMICH_API_KEY, "Accept": "application/json"},
    timeout=20,
)

try:
    r = client.get("/server/version")
    if r.status_code == 200:
        v = r.json()
        ok(f"Immich version {v.get('major')}.{v.get('minor')}.{v.get('patch')}")
    else:
        fail(f"/server/version returned {r.status_code}")
except httpx.RequestError as exc:
    fail(f"HTTP request failed: {exc}")
    sys.exit(1)

# The real authentication test: can this key actually read assets? This is the
# only capability the MCP server strictly needs.
r = client.post(
    "/search/metadata",
    json={"size": 1, "page": 1},
    headers={"Content-Type": "application/json"},
)
if r.status_code in (200, 201):
    total = r.json().get("assets", {}).get("total", 0)
    ok("API key authenticates and can read assets")
    if total > 1:
        ok(f"{total} assets visible to this key")
    elif total == 1:
        warn(
            "only 1 asset visible to this key",
            "either the library is nearly empty, or the key belongs to a user "
            "who can see very little",
        )
    else:
        warn("0 assets visible", "is the library empty for this user?")
elif r.status_code in (401, 403):
    fail(
        f"API key cannot read assets ({r.status_code})",
        "regenerate the key in Immich with asset and search read permissions",
    )
else:
    fail(f"search/metadata returned {r.status_code}: {r.text[:150]}")

# Optional: identity. Immich 3.x scopes API keys per endpoint, so a 403 here
# just means user.read was not granted. Not required by any MCP tool.
r = client.get("/users/me")
if r.status_code == 200:
    me = r.json()
    ok(f"identified as {me.get('email') or me.get('name')}")
elif r.status_code == 403:
    warn(
        "key lacks the user.read scope",
        "harmless - no MCP tool needs it. Grant it in Immich only if you want "
        "the identity shown here.",
    )
elif r.status_code == 401:
    fail("API key rejected (401)", "the key is invalid or was revoked")

r = client.get("/server/features")
if r.status_code == 200:
    features = r.json()
    if features.get("smartSearch"):
        ok("smart search (CLIP) enabled - the `search` tool will work")
    else:
        warn(
            "smart search disabled",
            "the `search` tool returns nothing; enable machine learning in Immich",
        )
    if features.get("facialRecognition"):
        ok("facial recognition enabled - `list_people` will return results")
    else:
        warn("facial recognition disabled", "`list_people` will be empty")
else:
    warn(f"/server/features returned {r.status_code}")

client.close()


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------

section("Summary")

if FAILURES:
    print(f"  {len(FAILURES)} failure(s), {len(WARNINGS)} warning(s). Not ready.")
    sys.exit(1)

print(f"  All checks passed ({len(WARNINGS)} warning(s)).")
print("\n  Next, run the server without Docker for a fast test loop:")
print("      python run_local.py")
print("\n  Then in a second terminal:")
print(f"      python smoke_test.py http://127.0.0.1:8099 {BEARER[:8]}...")
sys.exit(0)