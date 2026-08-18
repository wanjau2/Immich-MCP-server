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

import asyncio
import logging
import os
import time
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

# Album scoping. Empty means the whole library is visible. Otherwise supply a
# comma-separated list of album UUIDs and every tool is restricted to assets
# inside those albums. Discover IDs with:  python list_albums.py
_raw_albums = os.environ.get("ALLOWED_ALBUM_IDS", "").strip()
ALLOWED_ALBUM_IDS: list[str] = [
    a.strip() for a in _raw_albums.replace(";", ",").split(",") if a.strip()
]
SCOPED = bool(ALLOWED_ALBUM_IDS)

# How long the scoped asset-ID set is cached before being rebuilt, in seconds.
SCOPE_TTL = int(os.environ.get("SCOPE_TTL", "300"))

# Backoff after a completely failed scope refresh, in seconds.
SCOPE_RETRY = min(15, SCOPE_TTL)

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


# --------------------------------------------------------------------------
# Album scope
# --------------------------------------------------------------------------
#
# Immich's smart search has no album filter, so scoping has to happen on this
# side: build the set of asset IDs belonging to the allowed albums, then drop
# anything outside it. The set is cached and rebuilt every SCOPE_TTL seconds so
# newly added photos appear without a restart.


class AlbumScope:
    """Set of asset IDs the server is allowed to expose."""

    def __init__(self, album_ids: list[str]) -> None:
        self.album_ids = album_ids
        self.asset_ids: set[str] = set()
        self.person_ids: set[str] = set()
        self.album_names: dict[str, str] = {}
        # -inf, not 0.0: time.monotonic() starts near zero at process start, so
        # 0.0 would make a never-loaded cache look fresh for the first
        # SCOPE_TTL seconds of uptime and deny every request.
        self.loaded_at: float = float("-inf")
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.album_ids)

    async def refresh(self, force: bool = False) -> None:
        if not self.enabled:
            return
        if not force and (time.monotonic() - self.loaded_at) < SCOPE_TTL:
            return

        async with self._lock:
            # Another coroutine may have refreshed while we waited.
            if not force and (time.monotonic() - self.loaded_at) < SCOPE_TTL:
                return

            assets: set[str] = set()
            people: set[str] = set()
            names: dict[str, str] = {}

            for album_id in self.album_ids:
                try:
                    album = await _call("GET", f"/albums/{album_id}")
                except ImmichError as exc:
                    log.error("Album %s unavailable, skipping: %s", album_id, exc)
                    continue
                names[album_id] = album.get("albumName") or album_id
                for asset in album.get("assets", []):
                    assets.add(asset["id"])
                    for person in asset.get("people") or []:
                        if person.get("id"):
                            people.add(person["id"])

            if not names:
                # Every album failed to load. Do NOT cache this empty result for
                # the full TTL, or a brief Immich hiccup silently blanks every
                # search until it expires. Keep whatever we had and retry soon.
                log.error(
                    "Could not read any configured album. Keeping previous scope "
                    "(%d assets) and retrying in %ds.",
                    len(self.asset_ids),
                    SCOPE_RETRY,
                )
                self.loaded_at = time.monotonic() - max(SCOPE_TTL - SCOPE_RETRY, 0)
                return

            self.asset_ids = assets
            self.person_ids = people
            self.album_names = names
            self.loaded_at = time.monotonic()
            if len(names) < len(self.album_ids):
                log.warning(
                    "Only %d of %d configured albums could be read.",
                    len(names),
                    len(self.album_ids),
                )
            log.info(
                "Album scope refreshed: %d album(s), %d asset(s)",
                len(names),
                len(assets),
            )

    async def contains(self, asset_id: str) -> bool:
        if not self.enabled:
            return True
        await self.refresh()
        if asset_id in self.asset_ids:
            return True
        # Might be a photo added since the last refresh.
        await self.refresh(force=True)
        return asset_id in self.asset_ids

    async def filter(self, assets: list[dict]) -> list[dict]:
        if not self.enabled:
            return assets
        await self.refresh()
        return [a for a in assets if a.get("id") in self.asset_ids]

    def allows_album(self, album_id: str) -> bool:
        return not self.enabled or album_id in self.album_ids

    def describe(self) -> str:
        if not self.enabled:
            return "entire library"
        return f"{len(self.album_names)} album(s): " + ", ".join(
            self.album_names.values()
        )


scope = AlbumScope(ALLOWED_ALBUM_IDS)


def _over_fetch(limit: int) -> int:
    """When scoping, ask Immich for extra rows since many will be filtered out."""
    if not scope.enabled:
        return _clamp(limit)
    return min(_clamp(limit) * 5, MAX_PAGE_SIZE * 5, 1000)


async def _require_in_scope(asset_id: str) -> None:
    if not await scope.contains(asset_id):
        raise ImmichError(
            f"Asset {asset_id} is outside the albums this server is allowed to "
            f"access ({scope.describe()})."
        )


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

_SCOPE_NOTE = (
    " This server is restricted to a specific set of albums; anything outside "
    "them is invisible and cannot be retrieved."
    if SCOPED
    else ""
)

mcp = FastMCP(
    name="immich",
    instructions=(
        "Tools for a self-hosted Immich photo and video library. "
        "Use `search` for content-based questions ('photos of a dog in snow'), "
        "`search_by_metadata` for date, place, camera, or person filters, and "
        "`fetch` to pull full EXIF for a single asset by UUID. "
        "Asset IDs are UUIDs returned by the search tools." + _SCOPE_NOTE
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
            "size": _over_fetch(limit),
            "page": 1,
            "withExif": True,
        },
    )
    assets = payload["assets"]
    items = await scope.filter(assets.get("items", []))
    items = items[: _clamp(limit)]
    result = {
        "query": query,
        "results": [_summarize(a) for a in items],
    }
    if scope.enabled:
        result["scope"] = scope.describe()
        result["total"] = len(items)
    else:
        result["total"] = assets.get("total", 0)
    return result


@mcp.tool()
async def fetch(id: str) -> dict:
    """Retrieve full metadata for a single asset by its Immich UUID.

    Returns EXIF detail: capture time, camera body and lens, exposure settings,
    dimensions, GPS coordinates, recognized people, and album membership.
    """
    await _require_in_scope(id)
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
    body: dict[str, Any] = {"size": _over_fetch(limit), "page": 1, "withExif": True}
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
    items = await scope.filter(assets.get("items", []))
    items = items[: _clamp(limit)]
    result = {
        "filters": {
            k: v for k, v in body.items() if k not in ("size", "page", "withExif")
        },
        "results": [_summarize(a) for a in items],
    }
    if scope.enabled:
        result["scope"] = scope.describe()
        result["total"] = len(items)
    else:
        result["total"] = assets.get("total", 0)
    return result


@mcp.tool()
async def list_albums() -> list[dict]:
    """List every album in the library with its name, asset count, and dates."""
    albums = await _call("GET", "/albums")
    if scope.enabled:
        albums = [a for a in albums if a["id"] in scope.album_ids]
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
    if not scope.allows_album(album_id):
        raise ImmichError(
            f"Album {album_id} is outside the albums this server is allowed to "
            f"access ({scope.describe()})."
        )
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

    if scope.enabled:
        await scope.refresh()
        named = [p for p in named if p["id"] in scope.person_ids]

    if name:
        needle = name.lower()
        named = [p for p in named if needle in p["name"].lower()]
    return [
        {
            "id": p["id"],
            "name": p.get("name"),
            "birthDate": p.get("birthDate"),
        }
        for p in named[:limit]
    ]


@mcp.tool()
async def library_stats() -> dict:
    """Total photo count, video count, and disk usage for the library."""
    if scope.enabled:
        await scope.refresh()
        return {
            "scope": scope.describe(),
            "albums": len(scope.album_names),
            "assets": len(scope.asset_ids),
            "note": "Counts cover only the albums this server may access.",
        }
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
        for asset_id in asset_ids:
            await _require_in_scope(asset_id)
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
        body = {"status": "ok", "immich": version}
        if scope.enabled:
            await scope.refresh()
            body["scope"] = {
                "albums": list(scope.album_names.values()),
                "assets": len(scope.asset_ids),
            }
        return JSONResponse(body)
    except ImmichError as exc:
        return JSONResponse({"status": "degraded", "detail": str(exc)}, status_code=503)


app = mcp.http_app(path="/mcp", stateless_http=STATELESS_HTTP)
app.router.routes.append(Route("/healthz", healthz, methods=["GET"]))
app.add_middleware(BearerAuthMiddleware)

log.info("Immich MCP server configured for %s", IMMICH_URL)
log.info("Scope: %s", "restricted to " + ", ".join(ALLOWED_ALBUM_IDS) if SCOPED else "entire library")
log.info("Share links: %s", "enabled" if ALLOW_SHARE_LINKS else "disabled")