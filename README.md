# Immich MCP Server

Exposes a self-hosted Immich photo library to ChatGPT (and any other MCP client)
over Streamable HTTP, so you can ask questions like *"find the photos from the
Kigali site visit in March"* and get real answers from your own NAS.

```
ChatGPT  ──HTTPS──▶  Cloudflare Tunnel  ──▶  immich_mcp:8080  ──▶  immich_server:2283
          bearer token                        MCP → REST            x-api-key
```

## Why it's built this way

ChatGPT custom connectors only accept a **remote HTTPS endpoint**. There is no
stdio or localhost option, so the server has to be reachable from the internet —
hence the tunnel — and it has to defend itself, hence the bearer token.

## Tools

| Tool | Purpose |
|---|---|
| `search` | CLIP semantic search over image content |
| `fetch` | Full EXIF for one asset by UUID |
| `search_by_metadata` | Filter by date, place, camera, person, favorite |
| `list_albums` | All albums with counts |
| `get_album` | One album's details and contents |
| `list_people` | Recognized faces, with IDs for filtering |
| `library_stats` | Photo/video counts and disk usage |
| `server_info` | Immich version and enabled features |
| `create_share_link` | Public link to specific assets — **off by default** |

### Restricting access to specific albums

By default the server exposes everything the API key can see. To share only a
subset, put album UUIDs in `ALLOWED_ALBUM_IDS`:

```bash
python list_albums.py          # prints every album with its UUID
```

```ini
ALLOWED_ALBUM_IDS=a1b2c3d4-...,e5f6g7h8-...
```

Every tool is then confined to assets inside those albums. Immich's smart search
has no album filter, so the server builds the set of allowed asset IDs itself and
drops anything outside it — including direct `fetch` by UUID, which returns a
refusal rather than the asset. The set is cached for `SCOPE_TTL` seconds so
photos added to an allowed album appear without a restart.

Two layers are worth combining: scope the Immich API key to a dedicated user,
*and* set `ALLOWED_ALBUM_IDS`. The key limits what this server could ever reach;
the album list limits what it actually exposes.

`search` and `fetch` are named deliberately: ChatGPT's Deep Research mode ignores
every other tool, so those two carry the load if Developer Mode is unavailable.

---

## Setup

### 1. Get an Immich API key

Immich → Account Settings → API Keys → New API Key. Scope it read-only unless
you plan to enable share links.

### 2. Configure

```bash
cp .env.example .env
openssl rand -hex 32          # paste into MCP_BEARER_TOKEN
$EDITOR .env
```

Find the Docker network Immich already runs on and put its name in
`docker-compose.yml` under `networks.immich-net.name`:

```bash
docker network ls | grep -i immich
```

It's usually `immich_default`. If the MCP container can't join it, set
`IMMICH_URL` to the NAS LAN address instead (`http://192.168.1.50:2283`) and
drop the `networks:` block.

### 3. Test before Docker

Two scripts validate everything without building an image:

```bash
pip install httpx python-dotenv
python preflight.py
```

`preflight.py` checks the project layout, validates `.env`, opens a TCP
connection to Immich, authenticates the API key, and reports which optional
features (smart search, face recognition) are actually enabled. It names the
exact fix for each failure.

Once it passes, run the server with no Docker at all:

```bash
pip install -r app/requirements.txt
python run_local.py
```

That serves on `http://127.0.0.1:8099` with auto-reload. In a second terminal:

```bash
python smoke_test.py http://127.0.0.1:8099 <your-bearer-token>
```

Much faster loop than rebuilding the image on every config change. Move to
Docker once this passes.

### 4. Build and run

```bash
docker compose up -d --build
docker compose logs -f immich-mcp
```

Verify locally before exposing anything:

```bash
curl http://127.0.0.1:8099/healthz
# {"status":"ok","immich":{"major":1,"minor":...}}

pip install httpx
python smoke_test.py http://127.0.0.1:8099 <your-bearer-token>
```

The smoke test runs the exact handshake ChatGPT does — initialize, tools/list,
then a live tool call — and confirms unauthenticated requests get a 401.

### 5. Expose through Cloudflare Tunnel

Add a public hostname to your existing tunnel pointing at
`http://immich_mcp:8080`. See `cloudflared/config.example.yml`. If you manage the
tunnel from the Zero Trust dashboard, add it there instead.

**Do not put Cloudflare Access in front of this hostname.** ChatGPT can't
complete an interactive Access login.

Re-run the smoke test against the public URL:

```bash
python smoke_test.py https://immich-mcp.example.com <your-bearer-token>
```

### 6. Connect ChatGPT

Settings → Connectors → Advanced settings → enable **Developer Mode**
(requires a paid plan), then Create:

- **Name**: Immich Photos
- **Description**: this matters — the model reads it to decide whether to invoke
  the connector. Something like *"Personal photo and video library. Use for
  finding, describing, or listing photos, albums, and recognized people."*
- **URL**: `https://immich-mcp.example.com/mcp`
- **Authentication**: API key / custom header → `Authorization: Bearer <token>`

Then enable the connector in the chat composer.

---

## Notes from actual use

**Name the tool in your prompt.** ChatGPT won't reliably guess when to reach for
a custom connector. "Use immich search to find photos of the drying racks" works
where "find my drying rack photos" often doesn't.

**ChatGPT can't see your photos.** Tool results are text — descriptions and
metadata, not pixels. `create_share_link` exists to bridge that gap, but a share
link is public to anyone holding the URL, which is why it's disabled by default.
Turn it on only if you're comfortable with that.

**Pin your Immich version.** The API shifts between releases — `/server/statistics`
was `/server-info/statistics` not long ago. Your own instance publishes the exact
spec at `https://photos.example.com/api/docs`; check there before debugging a 404.

**Rotate the bearer token** by editing `.env` and running
`docker compose up -d --force-recreate`, then updating the connector in ChatGPT.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `/healthz` returns 503 | MCP container can't reach Immich — wrong `IMMICH_URL` or not on the same Docker network |
| 401 on every request | Bearer token mismatch between `.env` and the connector config |
| ChatGPT says "search action not found" | Connector was added in Deep Research mode; enable Developer Mode |
| Connector added but never fires | Description too vague, or the tool isn't toggled on in the chat |
| `search` returns nothing ever | Immich machine learning is disabled — check `server_info` |
| Immich rejects the key (401 in logs) | Key was revoked, or belongs to a different Immich user |