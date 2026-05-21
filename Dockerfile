# ── Stage 1: dependency builder ──────────────────────────────────────────────
# Use a full image to compile any C extensions (e.g. cryptography for proxmoxer
# token auth) and install wheels.  The result is copied to the final slim image
# so the shipped layer contains no build tools.
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools needed by some wheels (cryptography, cffi).
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only the requirements file first so Docker can cache the pip install
# layer independently of source code changes.
COPY requirements.txt .

# Install into a prefix directory that will be copied to the final image.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="proxmox-mcp"
LABEL org.opencontainers.image.description="MCP server exposing 69 Proxmox VE management tools over stdio"
LABEL org.opencontainers.image.source="https://github.com/youruser/proxmox-mcp"

WORKDIR /app

# Copy installed packages from the builder stage.
COPY --from=builder /install /usr/local

# Copy only the files the server needs at runtime.
# .env is intentionally excluded — pass credentials via --env-file or -e flags.
COPY server.py util.py ./

# The MCP server communicates over stdio (stdin/stdout).
# It does not bind any network port — the parent process (agent.py or an MCP
# client) spawns this container with -i and exchanges JSON-RPC over the pipe.
ENTRYPOINT ["python", "server.py"]
