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
LABEL org.opencontainers.image.description="MCP server exposing Proxmox VE management tools over stdio or Streamable HTTP (legacy SSE also supported)"
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

# Transport is selected via PROXMOX_MCP_TRANSPORT (stdio | streamable-http | sse, default: stdio).
# In HTTP modes the server listens on PROXMOX_MCP_HTTP_HOST:PROXMOX_MCP_HTTP_PORT (default 0.0.0.0:8080);
# streamable-http serves /mcp, legacy sse serves /sse. PROXMOX_MCP_SSE_HOST/PORT are still honoured as fallback.
ENV PROXMOX_MCP_TRANSPORT=stdio \
    PROXMOX_MCP_HTTP_HOST=0.0.0.0 \
    PROXMOX_MCP_HTTP_PORT=8080

EXPOSE 8080

ENTRYPOINT ["python", "server.py"]
