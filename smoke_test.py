#!/usr/bin/env python3
"""
Smoke test for the Immich MCP server.

Runs the same handshake ChatGPT does: initialize, then tools/list, then calls
one real tool. Point it at the local container first, then at the public URL.

    pip install httpx
    python smoke_test.py http://127.0.0.1:8099 <bearer-token>
    python smoke_test.py https://immich-mcp.example.com <bearer-token>
"""

import json
import sys

import httpx

PROTOCOL_VERSION = "2025-06-18"


def rpc(client: httpx.Client, url: str, method: str, params: dict, req_id: int) -> dict:
    response = client.post(
        url,
        json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
    )
    response.raise_for_status()

    body = response.text
    # Streamable HTTP may reply as SSE; pull the data line out.
    if body.lstrip().startswith("event:") or body.lstrip().startswith("data:"):
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise RuntimeError(f"No data frame in SSE response:\n{body[:400]}")
    return response.json()


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    base, token = sys.argv[1].rstrip("/"), sys.argv[2]
    mcp_url = f"{base}/mcp"

    with httpx.Client(
        timeout=30,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        follow_redirects=True,
    ) as client:

        print(f"1. GET {base}/healthz")
        health = client.get(f"{base}/healthz")
        print(f"   {health.status_code} {health.text[:200]}\n")
        if health.status_code != 200:
            print("   Health check failed. Immich is unreachable or the key is wrong.")
            return 1

        print("2. Rejecting unauthenticated requests")
        unauth = httpx.post(
            mcp_url,
            json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
            timeout=15,
        )
        status = "PASS" if unauth.status_code == 401 else f"FAIL ({unauth.status_code})"
        print(f"   {status}\n")

        print("3. initialize")
        init = rpc(
            client,
            mcp_url,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "1.0"},
            },
            1,
        )
        server = init.get("result", {}).get("serverInfo", {})
        print(f"   server: {server}\n")

        print("4. tools/list")
        tools = rpc(client, mcp_url, "tools/list", {}, 2)
        names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
        for name in names:
            print(f"   - {name}")
        print()

        if "library_stats" in names:
            print("5. tools/call library_stats")
            stats = rpc(
                client, mcp_url, "tools/call",
                {"name": "library_stats", "arguments": {}}, 3,
            )
            print(f"   {json.dumps(stats.get('result', stats))[:400]}\n")

        if "search" in names:
            print("6. tools/call search('sunset')")
            result = rpc(
                client, mcp_url, "tools/call",
                {"name": "search", "arguments": {"query": "sunset", "limit": 3}}, 4,
            )
            print(f"   {json.dumps(result.get('result', result))[:600]}\n")

    print("Done. If all steps passed, this URL is ready for a ChatGPT connector.")
    return 0


if __name__ == "__main__":
    sys.exit(main())