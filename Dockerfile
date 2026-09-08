# Waystone v3 Arena — hosted MCP strategy competition.
FROM python:3.12-slim

# uv for fast, reproducible installs from the lockfile.
RUN pip install --no-cache-dir uv

WORKDIR /app

# Dependency layer (cached unless pyproject/lock change).
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# HQ GET /api/strategies loads waystone_backtests/catalog.json. Without this
# the GKE image 500s (src-only COPY) even when GCS Workload Identity works.
COPY waystone_backtests/catalog.json /app/waystone_backtests/catalog.json
ENV WAYSTONE_BACKTESTS_ROOT=/app/waystone_backtests

# SQLite lives on a mounted volume so players/leaderboard survive restarts.
ENV WAYSTONE_DB=/data/arena.db
EXPOSE 9100

# Serves the Arena MCP server over HTTP (configured from env: POLYGON_API_KEY,
# WAYSTONE_DB, WAYSTONE_ADMIN_TOKEN). /healthz is open; every other route needs a token.
CMD ["uv", "run", "waystone3", "arena-serve", "--host", "0.0.0.0", "--port", "9100"]
