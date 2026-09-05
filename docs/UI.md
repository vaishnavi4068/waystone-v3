# Read-only dashboard UI

A per-user, read-only web dashboard for the Arena. Each of the **5 team members** signs
in with **their name and password**. After login the browser stores the
same bearer token used for the MCP connector. Everyone sees the **shared** account,
positions, and strategy — plus signals, charts, backtests, news, and the IBKR daily /
KPI / compare pages. Algo onboarding on **Compare** writes the registry on the report store.

## Two pieces

| Piece | What | Run |
|---|---|---|
| **API** (`src/waystone3/api/`) | FastAPI, read-only, per-user auth, reads the live competition DB | `uv run waystone3 api-serve` (port 9200) |
| **Frontend** (`frontend/`) | Next.js 16 + React 19 + Tailwind + TanStack Query + Lightweight Charts | `npm run dev` (port **3001**, IPv4 + IPv6) |

## Screens (per user)

| Screen | Shows |
|---|---|
| **Daily** (`/ibkr`) | IBKR blotter: NLV, day P&L, futures vs options, fills (GCS dump) |
| **Compare** (`/compare`) | Per-algo live paper vs same-day replay; onboard more algos |
| **Options KPIs** (`/options-kpis`) | Strategy 5 weekly paper scorecard (stages 1–5 + slippage) |
| **Futures KPIs** (`/futures-kpis`) | NQ v5.1 scorecard (tiers 0–4, GREEN/AMBER/RED) |
| **Leaderboard** (`/leaderboard`) | all players ranked by paper-account return |
| **Signals** (`/signals`) | composite momentum score (−10…+10) + contributor breakdown per symbol |
| **Charts** (`/charts`) | candlesticks for any symbol |
| **Backtests** (`/backtests`) | run a config (weights + dates) → metrics + equity curve |
| **News** (`/news`) | Polygon headlines (empty unless a Polygon key is configured) |

## API endpoints

`/api/health` is open. `POST /api/login` accepts `{username, password}` and returns
`{name, token}`. Other routes require `Authorization: Bearer <token>`:

`/api/account` · `/api/positions` · `/api/orders` · `/api/activity` · `/api/ibkr/days` ·
`/api/ibkr/report` · `/api/ibkr/options-kpis` · `/api/ibkr/futures-kpis` · `/api/algos` ·
`/api/algos/compare-days` · `/api/algos/{id}/compare` · `/api/signals` · `/api/bars` ·
`/api/backtest` · `/api/news`

`POST /api/algos`, `PUT /api/algos/{id}`, and `DELETE /api/algos/{id}` onboard or edit
paper algos (live + replay folder prefixes).

## Run locally

```sh
# One command (API :9200 + UI :3001, staged IBKR sample week)
./scripts/run-dashboard-local.sh
# Open http://127.0.0.1:3001

# Or two terminals:
# 1) Seed players + start the API against that DB (stub data if no POLYGON_API_KEY)
export WAYSTONE_DB=./arena.db WAYSTONE_ADMIN_TOKEN=$(openssl rand -hex 16)
export IBKR_STAGED=1
uv run waystone3 api-serve                                            # http://127.0.0.1:9200
# The five users are created on first start with their default passwords.

# 2) Frontend
cd frontend
cp .env.example .env.local        # NEXT_PUBLIC_API_BASE=http://127.0.0.1:9200
npm install
npm run dev                       # http://127.0.0.1:3001  (proxies /api to :9200)
```

For live market data, set `POLYGON_API_KEY` before `api-serve` (the API uses the same
env-driven data source as the rest of the platform).

## Auth model

The dashboard is limited to **5 members**. Each signs in with their **name + password**
(`POST /api/login`). A successful login returns the member's bearer token,
which is stored only in the browser's `localStorage` and sent as
`Authorization: Bearer <token>` on later API calls. Claude MCP still uses that same
token directly. Always serve over HTTPS in production.

## Deploying (notes)

- **API:** containerize like the Arena (it's the same image/deps) and run `api-serve` as a
  second Deployment/Service on GKE, behind the same Ingress on a path or a second host.
  Point it at the same `WAYSTONE_DB` (read) — use a `ReadOnlyMany`-capable volume, or run
  the API in the same pod as the Arena to share the `ReadWriteOnce` PVC.
- **Frontend:** `npm run build` produces a static-friendly Next app — host it on Vercel,
  Cloud Run, or as a container; set `NEXT_PUBLIC_API_BASE` to the API's public URL.
- Verified: `npm run build` compiles all 7 screens; the API passes its full test suite.
