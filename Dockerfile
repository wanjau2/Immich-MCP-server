# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so code edits do not bust the layer cache.
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/immich_mcp.py .

# Run as an unprivileged user. Matches the usual Synology "docker" convention;
# override with `user:` in compose if your NAS uses different IDs.
RUN useradd --create-home --uid 1027 --shell /usr/sbin/nologin mcp
USER mcp

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=8).status==200 else 1)"

CMD ["uvicorn", "immich_mcp:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
