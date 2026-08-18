"""
Immich MCP Server
=================
Exposes a self-hosted Immich photo library to MCP clients (ChatGPT custom
connectors, Claude Desktop via mcp-remote, etc.) over Streamable HTTP.

Transport : Streamable HTTP at /mcp
Auth      : static bearer token (MCP_BEARER_TOKEN)
Upstream  : Immich REST API, authenticated with x-api-key
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("immich-mcp")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


IMMICH_URL = _required("IMMICH_URL").rstrip("/")
IMMICH_API_KEY = _required("IMMICH_API_KEY")
MCP_BEARER_TOKEN = _required("MCP_BEARER_TOKEN")

# Public browser-facing Immich URL, used to build clickable links in results.
# Optional: if unset, results simply omit URLs.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

# Safety switch. Share links make photos publicly reachable to anyone holding
# the URL, so the tool is off unless you deliberately turn it on.
ALLOW_SHARE_LINKS = os.environ.get("ALLOW_SHARE_LINKS", "false").lower() == "true"

# Stateless mode avoids server-side session affinity, which is what you want
# behind a tunnel or any load balancer. Set to false only if you need
# resumable streams.
STATELESS_HTTP = os.environ.get("STATELESS_HTTP", "true").lower() == "true"

DEFAULT_PAGE_SIZE = int(os.environ.get("DEFAULT_PAGE_SIZE", "20"))
MAX_PAGE_SIZE = int(os.environ.get("MAX_PAGE_SIZE", "100"))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))

# --------------------------------------------------------------------------
# Upstream HTTP client
# --------------------------------------------------------------------------

client = httpx.AsyncClient(
    base_url=f"{IMMICH_URL}/api",
    headers={
        "x-api-key": IMMICH_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    },
    timeout=HTTP_TIMEOUT,
    transport=httpx.AsyncHTTPTransport(retries=2),
)


class ImmichError(RuntimeError):
    """Raised when Immich returns an error. Message is surfaced to the model."""


async def _call(method: str, path: str, **kwargs: Any) -> Any:
    """Call the Immich API and translate failures into readable messages."""
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.RequestError as exc:
        raise ImmichError(
            f"Could not reach Immich at {IMMICH_URL}: {exc.__class__.__name__}"
        ) from exc

    if response.status_code == 401:
        raise ImmichError("Immich rejected the API key (401). Check IMMICH_API_KEY.")
    if response.status_code == 403:
        raise ImmichError("Immich API key lacks permission for this operation (403).")
    if response.status_code == 404:
        raise ImmichError(f"Not found: {path}")
    if response.status_code >= 400:
        detail = response.text[:300]
        raise ImmichError(f"Immich returned {response.status_code}: {detail}")

    if not response.content:
        return None
    return response.json()


def _clamp(size: int) -> int:
    return max(1, min(size, MAX_PAGE_SIZE))


def _asset_url(asset_id: str) -> str:
    return f"{PUBLIC_URL}/photos/{asset_id}" if PUBLIC_URL else ""


def _summarize(asset: dict) -> dict:
    """Compact one asset into the {id, title, text, url} shape MCP clients expect."""
    exif = asset.get("exifInfo") or {}
    parts: list[str] = []

    if asset.get("type"):
        parts.append(str(asset["type"]).lower())
    if asset.get("fileCreatedAt"):
        parts.append(f"taken {asset['fileCreatedAt'][:10]}")

    place = ", ".join(
        p for p in (exif.get("city"), exif.get("state"), exif.get("country")) if p
    )
    if place:
        parts.append(place)

    camera = " ".join(p for p in (exif.get("make"), exif.get("model")) if p)
    if camera:
        parts.append(camera)

    if exif.get("description"):
        parts.append(exif["description"])

    names = [p["name"] for p in (asset.get("people") or []) if p.get("name")]
    if names:
        parts.append("people: " + ", ".join(names))

    if asset.get("isFavorite"):
        parts.append("favorite")

    return {
        "id": asset["id"],
        "title": asset.get("originalFileName") or asset["id"],
        "text": " | ".join(parts),
        "url": _asset_url(asset["id"]),
    }


# --------------------------------------------------------------------------
# MCP server
# --------------------------------------------------------------------------

mcp = FastMCP(
    name="immich",
    instructions=(
        "Tools for a self-hosted Immich photo and video library. "
        "Use `search` for content-based questions ('photos of a dog in snow'), "
        "`search_by_metadata` for date, place, camera, or person filters, and "
        "`fetch` to pull full EXIF for a single asset by UUID. "
        "Asset IDs are UUIDs returned by the search tools."
    ),
)


@mcp.tool()
async def search(query: str, limit: int = DEFAULT_PAGE_SIZE) -> dict:
    """Semantic search over photo and video content using Immich's CLIP model.

    Describe what is visually in the frame rather than filenames. Good queries:
    "dog running in snow", "whiteboard with a diagram", "sunset over water",
    "person holding a coffee cup".

    Requires Immich machine learning to be enabled. Returns matching assets
    ordered by relevance.
    """
    payload = await _call(
        "POST",
        "/search/smart",
        json={
            "query": query,
            "size": _clamp(limit),
            "page": 1,
            "withExif": True,
        },
    )
    assets = payload["assets"]
    return {
        "query": query,
        "total": assets.get("total", 0),
        "results": [_summarize(a) for a in assets.get("items", [])],
    }


@mcp.tool()
async def fetch(id: str) -> dict:
    """Retrieve full metadata for a single asset by its Immich UUID.

    Returns EXIF detail: capture time, camera body and lens, exposure settings,
    dimensions, GPS coordinates, recognized people, and album membership.
    """
    asset = await _call("GET", f"/assets/{id}")
    exif = asset.get("exifInfo") or {}

    exposure = " ".join(
        p
        for p in (
            exif.get("exposureTime"),
            f"f/{exif['fNumber']}" if exif.get("fNumber") else None,
            f"ISO {exif['iso']}" if exif.get("iso") else None,
            f"{exif['focalLength']}mm" if exif.get("focalLength") else None,
        )
        if p
    )
    dimensions = (
        f"{exif['exifImageWidth']}x{exif['exifImageHeight']}"
        if exif.get("exifImageWidth")
        else None
    )
    gps = (
        f"{exif['latitude']}, {exif['longitude']}"
        if exif.get("latitude") is not None
        else None
    )
    place = ", ".join(
        p for p in (exif.get("city"), exif.get("state"), exif.get("country")) if p
    )
    people = ", ".join(p["name"] for p in (asset.get("people") or []) if p.get("name"))

    fields = {
        "File": asset.get("originalFileName"),
        "Type": asset.get("type"),
        "Captured": asset.get("fileCreatedAt"),
        "Uploaded": asset.get("createdAt"),
        "Duration": asset.get("duration") if asset.get("type") == "VIDEO" else None,
        "Camera": " ".join(
            p for p in (exif.get("make"), exif.get("model")) if p
        ).strip()
        or None,
        "Lens": exif.get("lensModel"),
        "Exposure": exposure or None,
        "Dimensions": dimensions,
        "File size": exif.get("fileSizeInByte"),
        "Location": place or None,
        "GPS": gps,
        "Description": exif.get("description"),
        "People": people or None,
        "Favorite": asset.get("isFavorite"),
        "Archived": asset.get("isArchived"),
    }
    text = "\n".join(f"{k}: {v}" for k, v in fields.items() if v not in (None, "", False))

    return {
        "id": asset["id"],
        "title": asset.get("originalFileName") or asset["id"],
        "text": text,
        "url": _asset_url(asset["id"]),
        "metadata": {
            "type": asset.get("type"),
            "createdAt": asset.get("fileCreatedAt"),
            "latitude": exif.get("latitude"),
            "longitude": exif.get("longitude"),
        },
    }


@mcp.tool()
async def search_by_metadata(
    taken_after: str = "",
    taken_before: str = "",
    city: str = "",
    country: str = "",
    make: str = "",
    model: str = "",
    person_ids: list[str] | None = None,
    is_favorite: bool | None = None,
    asset_type: str = "",
    limit: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """Filter assets by metadata rather than image content.

    Dates are ISO 8601 strings, e.g. "2024-06-01T00:00:00.000Z".
    asset_type is "IMAGE" or "VIDEO". Get person_ids from `list_people` first.
    Every argument is optional; omitting all of them returns the most recent assets.
    """
    body: dict[str, Any] = {"size": _clamp(limit), "page": 1, "withExif": True}
    optional = {
        "takenAfter": taken_after,
        "takenBefore": taken_before,
        "city": city,
        "country": country,
        "make": make,
        "model": model,
        "type": asset_type.upper() if asset_type else "",
    }
    body.update({k: v for k, v in optional.items() if v})
    if person_ids:
        body["personIds"] = person_ids
    if is_favorite is not None:
        body["isFavorite"] = is_favorite

    payload = await _call("POST", "/search/metadata", json=body)
    assets = payload["assets"]
    return {
        "filters": {k: v for k, v in body.items() if k not in ("size", "page", "withExif")},
        "total": assets.get("total", 0),
        "results": [_summarize(a) for a in assets.get("items", [])],
    }


@mcp.tool()
async def list_albums() -> list[dict]:
    """List every album in the library with its name, asset count, and dates."""
    albums = await _call("GET", "/albums")
    return [
        {
            "id": a["id"],
            "name": a.get("albumName"),
            "description": a.get("description") or None,
            "assetCount": a.get("assetCount"),
            "createdAt": a.get("createdAt"),
            "shared": a.get("shared"),
        }
        for a in albums
    ]


@mcp.tool()
async def get_album(album_id: str, limit: int = 50) -> dict:
    """Get one album's details plus a sample of the assets it contains.

    Album IDs come from `list_albums`.
    """
    album = await _call("GET", f"/albums/{album_id}")
    assets = album.get("assets", [])
    return {
        "id": album["id"],
        "name": album.get("albumName"),
        "description": album.get("description"),
        "assetCount": album.get("assetCount", len(assets)),
        "showing": min(len(assets), _clamp(limit)),
        "assets": [_summarize(a) for a in assets[: _clamp(limit)]],
    }


@mcp.tool()
async def list_people(name: str = "", limit: int = 100) -> list[dict]:
    """List people recognized by Immich's face detection.

    Pass `name` to filter by a case-insensitive substring. Use the returned IDs
    with `search_by_metadata` to find every photo containing that person.
    """
    payload = await _call("GET", "/people", params={"size": 500})
    people = payload.get("people", []) if isinstance(payload, dict) else payload
    named = [p for p in people if p.get("name")]
    if name:
        needle = name.lower()
        named = [p for p in named if needle in p["name"].lower()]
    return [
        {
            "id": p["id"],
            "name": p.get("name"),
            "birthDate": p.get("birthDate"),
            "thumbnailPath": None,
        }
        for p in named[:limit]
    ]


@mcp.tool()
async def library_stats() -> dict:
    """Total photo count, video count, and disk usage for the library."""
    try:
        return await _call("GET", "/server/statistics")
    except ImmichError:
        # Older Immich releases used a different path.
        return await _call("GET", "/server-info/statistics")


@mcp.tool()
async def server_info() -> dict:
    """Immich server version and which optional features are enabled."""
    version = await _call("GET", "/server/version")
    features = await _call("GET", "/server/features")
    return {"version": version, "features": features}


if ALLOW_SHARE_LINKS:

    @mcp.tool()
    async def create_share_link(asset_ids: list[str], description: str = "") -> dict:
        """Create a public link so specific photos can actually be viewed in a browser.

        WARNING: anyone with the URL can view and download these assets without
        logging in. Only use when the user explicitly asks to share or view images.
        """
        if not asset_ids:
            raise ImmichError("asset_ids cannot be empty.")
        if not PUBLIC_URL:
            raise ImmichError("PUBLIC_URL is not configured, cannot build a share URL.")
        result = await _call(
            "POST",
            "/shared-links",
            json={
                "type": "INDIVIDUAL",
                "assetIds": asset_ids,
                "description": description or "Created via MCP",
                "allowDownload": True,
                "allowUpload": False,
            },
        )
        return {
            "url": f"{PUBLIC_URL}/share/{result['key']}",
            "assetCount": len(asset_ids),
            "note": "This link is public to anyone who has it.",
        }


# --------------------------------------------------------------------------
# ASGI app: bearer auth + health check wrapped around the MCP transport
# --------------------------------------------------------------------------


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request without the shared bearer token, except /healthz."""

    OPEN_PATHS = {"/healthz"}

    async def dispatch(self, request, call_next):
        if request.url.path in self.OPEN_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or token != MCP_BEARER_TOKEN:
            log.warning("Rejected unauthenticated request to %s", request.url.path)
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


async def healthz(request):
    """Liveness probe that also verifies the Immich connection."""
    try:
        version = await _call("GET", "/server/version")
        return JSONResponse({"status": "ok", "immich": version})
    except ImmichError as exc:
        return JSONResponse({"status": "degraded", "detail": str(exc)}, status_code=503)


app = mcp.http_app(path="/mcp", stateless_http=STATELESS_HTTP)
app.router.routes.append(Route("/healthz", healthz, methods=["GET"]))
app.add_middleware(BearerAuthMiddleware)

log.info("Immich MCP server configured for %s (share links: %s)", IMMICH_URL, ALLOW_SHARE_LINKS)
