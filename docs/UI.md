# Read-only dashboard UI

A per-user, read-only web dashboard for the Arena. Each player signs in with **their own
token** (the same bearer token they use for the MCP connector) and sees **their own**
account, positions, and strategy — plus the shared leaderboard, signals, charts,
backtests, and news. Nothing here mutates state.

## Two pieces

| Piece | What | Run |
|---|---|---|
| **API** (`src/waystone3/api/`) | FastAPI, read-only, per-user auth, reads the live competition DB | `uv run waystone3 api-serve` (port 9200) |
| **Frontend** (`frontend/`) | Next.js 16 + React 19 + Tailwind + TanStack Query + Lightweight Charts | `npm run dev` (port 3000) |

## Screens (per user)

| Screen | Shows |
|---|---|
| **Daily** (`/ibkr`) | IBKR blotter: NLV, day P&L, futures vs options, fills (GCS dump) |
| **Options KPIs** (`/options-kpis`) | Strategy 5 weekly paper scorecard (stages 1–5 + slippage) |
| **Leaderboard** (`/leaderboard`) | all players ranked by paper-account return |
| **Signals** (`/signals`) | composite momentum score (−10…+10) + contributor breakdown per symbol |
| **Charts** (`/charts`) | candlesticks for any symbol |
| **Backtests** (`/backtests`) | run a config (weights + dates) → metrics + equity curve |
| **News** (`/news`) | Polygon headlines (empty unless a Polygon key is configured) |

## API endpoints (all GET, all require `Authorization: Bearer <player-token>` except health)

`/api/health` · `/api/me` · `/api/standings` · `/api/signals` · `/api/bars` ·
`/api/backtest` · `/api/news`

## Run locally

```sh
# 1) Seed players + start the API against that DB (stub data if no POLYGON_API_KEY)
export WAYSTONE_DB=./arena.db WAYSTONE_ADMIN_TOKEN=$(openssl rand -hex 16)
uv run waystone3 arena-seed --players "Manoj,Mark,Brent,Akash,Cole"   # copy a token
uv run waystone3 api-serve                                            # http://localhost:9200

# 2) Frontend
cd frontend
cp .env.example .env.local        # NEXT_PUBLIC_API_BASE=http://localhost:9200
npm install
npm run dev                       # http://localhost:3000  -> paste a player token to sign in
```

For live market data, set `POLYGON_API_KEY` before `api-serve` (the API uses the same
env-driven data source as the rest of the platform).

## Auth model

The dashboard authenticates with the **per-user bearer token** — the same credential used
for the MCP connector. `/api/me` and the leaderboard are scoped/visible only to a valid
token; one player cannot see another's private positions. The token is stored only in the
browser's `localStorage`. Always serve over HTTPS in production.

## Deploying (notes)

- **API:** containerize like the Arena (it's the same image/deps) and run `api-serve` as a
  second Deployment/Service on GKE, behind the same Ingress on a path or a second host.
  Point it at the same `WAYSTONE_DB` (read) — use a `ReadOnlyMany`-capable volume, or run
  the API in the same pod as the Arena to share the `ReadWriteOnce` PVC.
- **Frontend:** `npm run build` produces a static-friendly Next app — host it on Vercel,
  Cloud Run, or as a container; set `NEXT_PUBLIC_API_BASE` to the API's public URL.
- Verified: `npm run build` compiles all 7 screens; the API passes its full test suite.
