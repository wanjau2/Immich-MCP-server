# Immich MCP Server

Exposes a self-hosted Immich photo library to ChatGPT (and any other MCP client)
over Streamable HTTP, so you can ask questions like *"find the photos from the
Kigali site visit in March"* and get real answers from your own NAS.

```
ChatGPT  ──HTTPS──▶  Cloudflare Tunnel  ──▶  immich_mcp:8080  ──▶  immich_server:2283
          bearer token                        MCP → REST            x-api-key
```

ChatGPT custom connectors only accept a **remote HTTPS endpoint** — there is no
stdio or localhost option. So the server has to be reachable from the internet,
hence the tunnel, and it has to defend itself, hence the bearer token.

---

## Contents

- [Tools](#tools)
- [Restricting access to specific albums](#restricting-access-to-specific-albums)
- [Part 1 — Get it working on your laptop](#part-1--get-it-working-on-your-laptop)
- [Part 2 — Deploy to the NAS](#part-2--deploy-to-the-nas)
- [Part 3 — Expose it and connect ChatGPT](#part-3--expose-it-and-connect-chatgpt)
- [Operating it](#operating-it)
- [Troubleshooting](#troubleshooting)

---

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

`search` and `fetch` are named deliberately: ChatGPT's Deep Research mode ignores
every other tool, so those two carry the load if Developer Mode is unavailable.

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

### Connecting different clients

The server accepts the token three ways, because MCP clients differ in what they
can send:

| Client | URL | Auth |
|---|---|---|
| ChatGPT connector | `https://host/mcp` | `Authorization: Bearer <token>` |
| Claude connector | `https://host/<token>/mcp` | leave OAuth fields blank |
| Claude Code, Cursor, Zed | `https://host/mcp` | `--header` / config headers |
| Anything URL-only | `https://host/mcp?key=<token>` | — |

Claude's custom connector dialog offers only OAuth Client ID and Secret, which
this server doesn't implement — hence the path form. Security is equivalent given
a high-entropy token, but paths and query strings land in proxy logs and shell
history more readily than headers, so prefer the header where the client allows
it.

### Project layout

```
immich-mcp/
├── Dockerfile
├── docker-compose.yml
├── .env                  <- you create this, never commit it
├── .env.example
├── .dockerignore
├── preflight.py          <- validate config before building
├── run_local.py          <- run without Docker, with auto-reload
├── list_albums.py        <- discover album UUIDs for scoping
├── smoke_test.py         <- full MCP handshake test
├── cloudflared/
│   └── config.example.yml
└── app/
    ├── immich_mcp.py
    └── requirements.txt
```

`app/` matters. If those two files end up next to the Dockerfile instead of
inside `app/`, the build fails with `"/app/immich_mcp.py": not found`.

---

## Part 1 — Get it working on your laptop

Optional, but a much faster loop than rebuilding an image for every config
change. Everything here also works on the NAS.

### 1. Get an Immich API key

Immich → Account Settings → API Keys → New API Key. Scope it read-only unless you
plan to enable share links.

Immich 3.x scopes keys per endpoint. The tools here need read access to assets,
albums, people, and search. `/users/me` is *not* required — preflight reports a
missing `user.read` scope as a warning, not a failure.

### 2. Configure

```bash
cp .env.example .env
openssl rand -hex 32          # paste into MCP_BEARER_TOKEN
$EDITOR .env
```

From the laptop, `IMMICH_URL` must be the NAS **LAN address** — `immich_server`
is a Docker container name that only resolves on the NAS itself:

```ini
IMMICH_URL=http://192.168.0.43:2283
```

Note there's no second `http://`. A doubled scheme is an easy paste error and
produces a confusing hostname failure.

### 3. Preflight

```bash
pip install httpx python-dotenv
python preflight.py
```

Checks the project layout, validates `.env`, opens a TCP connection to Immich,
confirms the API key can read assets, and reports whether smart search and face
recognition are actually enabled. It names the exact fix for each failure.

Worth reading the asset count it reports. If it says *1 asset visible* and your
library has thousands, the key belongs to a user who owns almost nothing —
everything downstream will work perfectly and return nothing.

### 4. Run without Docker

```bash
pip install -r app/requirements.txt
python run_local.py
```

Serves on `http://127.0.0.1:8099` with auto-reload. In a second terminal:

```bash
python smoke_test.py http://127.0.0.1:8099 <your-bearer-token>
```

This runs the exact handshake ChatGPT does — initialize, tools/list, then a live
tool call — and confirms unauthenticated requests get a 401.

### 5. Build the image (optional on the laptop)

If you want to verify the Docker build before shipping it, delete the
`networks:` block from `docker-compose.yml` first — `immich_default` only exists
on the NAS.

```bash
docker compose up -d --build
curl.exe http://127.0.0.1:8099/healthz
```

---

## Part 2 — Deploy to the NAS

Four things change: how the container reaches Immich, which Docker network it
joins, file ownership, and how the image gets built.

### 0. Rotate both secrets first

If the Immich API key or bearer token has been pasted into a chat, an email, or a
shared doc, treat it as burned. This endpoint is about to face the internet.

- Immich → Account Settings → API Keys → delete the old one, create a new one
- New bearer token: `openssl rand -hex 32`

Do **not** copy your laptop `.env` across. It points `IMMICH_URL` at a LAN
address, which works but routes photo metadata out to the LAN and back for no
reason. Start from `.env.example` on the NAS.

### 1. Copy the project across

Put it alongside your other stacks, e.g. `/volume1/docker/immich-mcp/`. Either
drag it in through File Station or:

```powershell
scp -r . wanjau@<nas-ip>:/volume1/docker/immich-mcp/
```

Verify `app/` survived — File Station drag-and-drop sometimes flattens
directories:

```bash
ls -la /volume1/docker/immich-mcp/app/
```

### 2. Find the real container name and network

SSH in (Control Panel → Terminal & SNMP → Enable SSH), then:

```bash
sudo docker ps --format '{{.Names}}\t{{.Image}}' | grep -i immich
sudo docker network ls | grep -i immich
```

Container Manager often prefixes names with the project, so you may get
`immich-immich_server-1` rather than `immich_server`. Prove the name resolves
from *inside* Docker, which is what actually matters:

```bash
sudo docker run --rm --network <network-name> curlimages/curl:latest \
  -s -o /dev/null -w '%{http_code}\n' http://<container-name>:2283/api/server/version
```

A `200` here means the rest of this is boring. Skipping it means a tunnel 502
later that looks like a Cloudflare problem but isn't.

### 3. Write `.env` on the NAS

```bash
cd /volume1/docker/immich-mcp
cp .env.example .env
vi .env
chmod 600 .env
```

Now `IMMICH_URL` uses the container name from step 2, keeping traffic inside
Docker:

```ini
IMMICH_URL=http://immich_server:2283
IMMICH_API_KEY=<the new key>
MCP_BEARER_TOKEN=<the new token>
PUBLIC_URL=https://photos.yourdomain.com
ALLOWED_ALBUM_IDS=
```

### 4. Fix the network name and user

In `docker-compose.yml`, set the network to whatever step 2 reported:

```yaml
networks:
  immich-net:
    external: true
    name: immich_default        # <- from step 2
```

The Dockerfile creates a user with UID 1027, the usual Synology convention.
Check yours with `id`; if it differs, either edit the Dockerfile or override in
compose with `user: "1026:100"`. Only matters once you mount volumes — a
mismatch is harmless for now.

### 5. Preflight and pick albums, from inside a container

DSM's Python is awkward to install packages into, and running preflight on the
host wouldn't test container-to-container name resolution anyway:

```bash
cd /volume1/docker/immich-mcp
sudo docker run --rm -it \
  --network <network-name> \
  -v "$PWD":/work -w /work \
  python:3.12-slim sh -c "pip install -q httpx python-dotenv && python preflight.py"
```

Same one-liner runs `list_albums.py`. Copy the UUIDs you want exposed into
`ALLOWED_ALBUM_IDS`.

### 6. Build and start

```bash
sudo docker compose up -d --build
sudo docker compose logs -f immich-mcp
```

Watch for three lines:

```
Immich MCP server configured for http://immich_server:2283
Scope: restricted to <album-id>
Album scope refreshed: 1 album(s), N asset(s)
```

If `N` is 0, the album ID is wrong or the key can't read that album.

Container Manager GUI works too — Project → Create → point at the folder — but it
sometimes struggles with `external: true` networks. Use SSH if it errors.

### 7. Verify on the NAS before exposing anything

```bash
curl -s http://127.0.0.1:8099/healthz | head -c 300
```

Expect status ok, the Immich version, and your scope summary. Then the full
handshake:

```bash
sudo docker run --rm -it --network host \
  -v "$PWD":/work -w /work \
  python:3.12-slim sh -c "pip install -q httpx && \
    python smoke_test.py http://127.0.0.1:8099 <bearer-token>"
```

A problem found here is a config problem. The same problem found after the next
section looks like a tunnel problem.

---

## Part 3 — Expose it and connect ChatGPT

### 1. Add the Cloudflare Tunnel hostname

Zero Trust dashboard → Networks → Tunnels → your tunnel → Public Hostname → Add:

- **Subdomain**: `immich-mcp`
- **Domain**: `yourdomain.com`
- **Service**: `HTTP` → `immich_mcp:8080`

If cloudflared runs as a container it must share a network with `immich_mcp` for
that name to resolve; if it runs on the host, use `http://127.0.0.1:8099`. See
`cloudflared/config.example.yml` for the config-file equivalent.

**Do not attach an Access policy.** ChatGPT cannot complete an interactive Access
login. The bearer token is the only gate — which is why rotating it mattered.

Verify from off the LAN if you can; a phone hotspot is a good test:

```powershell
python smoke_test.py https://immich-mcp.yourdomain.com <bearer-token>
```

### 2. Create the connector

Settings → Connectors → Advanced settings → enable **Developer Mode** (requires a
paid plan), then Create:

- **Name**: Immich Photos
- **Description**: this matters — the model reads it to decide whether to invoke
  the connector. Something like *"Personal photo and video library. Use for
  finding, describing, or listing photos, albums, and recognized people."*
- **URL**: `https://immich-mcp.yourdomain.com/mcp`
- **Authentication**: API key / custom header → `Authorization: Bearer <token>`

Then enable the connector in the chat composer and test with an explicit tool
name:

> Use immich search to find photos of the drying racks

---

## Operating it

**Name the tool in your prompt.** ChatGPT won't reliably guess when to reach for
a custom connector. "Use immich search to find photos of the drying racks" works
where "find my drying rack photos" often doesn't.

**ChatGPT can't see your photos.** Tool results are text — descriptions and
metadata, not pixels. `create_share_link` bridges that gap, but a share link is
public to anyone holding the URL, which is why it's disabled by default.

**Updating code.** Edit `app/immich_mcp.py`, then `sudo docker compose up -d --build`.

**Rotating the bearer token.** Edit `.env`, `docker compose up -d --force-recreate`,
then update the connector in ChatGPT. There's a window where ChatGPT is broken —
do it when you're not mid-conversation.

**Adding photos to a scoped album.** Nothing to do. The scope cache rebuilds
every `SCOPE_TTL` seconds (default 300).

**Auto-start after a reboot.** `restart: unless-stopped` handles it, but
Container Manager projects sometimes need auto-restart ticked in the GUI. Reboot
once deliberately, at a time that suits you, rather than discovering it while
you're away.

**Pin your Immich version.** The API shifts between releases — `/server/statistics`
was `/server-info/statistics` not long ago. Your instance publishes the exact spec
at `https://photos.yourdomain.com/api/docs`; check there before debugging a 404.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Build: `"/app/immich_mcp.py": not found`, context is 2 B | Files are flat; they belong in `app/` |
| `network immich_default not found` | Wrong network name — redo Part 2 step 2 |
| `/healthz` returns 503 | Can't reach Immich — wrong `IMMICH_URL`, or the containers aren't on the same network |
| Container exits immediately | Missing required env var — check `docker compose logs` |
| `Scope refreshed: 0 assets` | Album ID wrong, or the key can't read that album |
| Preflight: API key rejected 403 on `/users/me` | Missing `user.read` scope — harmless, no tool needs it |
| Preflight: only 1 asset visible | Key belongs to a user who owns almost nothing |
| 401 on every request | Bearer token mismatch between `.env` and the connector |
| Works on the NAS, 502 through the tunnel | cloudflared can't resolve `immich_mcp` — same network, or use the host IP |
| Tunnel returns a login page | An Access policy is attached; remove it |
| ChatGPT: "search action not found" | Added in Deep Research mode; enable Developer Mode |
| Connector added but never fires | Description too vague, or the tool isn't toggled on in the chat |
| `search` returns nothing ever | Immich machine learning disabled — check `server_info` |
| Immich rejects the key (401 in logs) | Key revoked, or belongs to a different Immich user |