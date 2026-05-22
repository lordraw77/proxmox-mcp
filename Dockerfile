# ── Stage 1: dependency builder ──────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.12-slim

ARG VERSION=dev
ARG BUILD_DATE
ARG VCS_REF

LABEL org.opencontainers.image.title="proxmox-mcp"
LABEL org.opencontainers.image.description="MCP server exposing 69 Proxmox VE management tools over stdio"
LABEL org.opencontainers.image.vendor="lordraw"
LABEL org.opencontainers.image.url="https://hub.docker.com/r/lordraw/proxmox-mcp"
LABEL org.opencontainers.image.source="https://github.com/lordraw77/proxmox-mcp"
LABEL org.opencontainers.image.version="${VERSION}"
LABEL org.opencontainers.image.created="${BUILD_DATE}"
LABEL org.opencontainers.image.revision="${VCS_REF}"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# .env is intentionally excluded — pass credentials via --env-file or -e flags.
COPY --from=builder /install /usr/local
COPY server.py util.py ./

# The MCP server communicates over stdio (stdin/stdout).
# No network port is exposed — the parent process spawns this container
# with -i and exchanges JSON-RPC 2.0 over the pipe.
ENTRYPOINT ["python", "server.py"]
