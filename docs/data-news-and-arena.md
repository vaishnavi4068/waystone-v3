# Polygon data, sentiment, and the Claude Arena

Three connected additions: a paid **Polygon** data feed, **news-driven sentiment** as a
signal, and a hosted **multi-player strategy competition** ("Arena") exposed to Claude over
MCP. Each plugs into an existing v3 seam — none touches the core pipeline.

## 1. Polygon market data

[`PolygonDataSource`](../src/waystone3/data/polygon.py) implements the same
`MarketDataSource` protocol as yfinance, so it drops into `run` / `backtest` / the Arena
unchanged:

```sh
export POLYGON_API_KEY=...
uv run waystone3 backtest --source polygon --symbols SPY --start 2023-01-01 --end 2024-01-01
uv run waystone3 run --source polygon --symbols AAPL,MSFT
```

The HTTP layer is injectable (`fetch_fn`) so tests run with canned responses — no key, no
network. Without a key it raises a clear error only when actually used.

## 2. News-driven sentiment

Sentiment is now a real, **opt-in** `SignalContributor` (the seam from
[adding-a-signal.md](adding-a-signal.md)):

- [`PolygonNewsSource`](../src/waystone3/news/polygon_news.py) — fetches articles per ticker
  from the same Polygon subscription.
- [`ClaudeSentimentScorer`](../src/waystone3/signals/sentiment_scorer.py) — scores each
  article via Claude (forced tool-use, prompt-cached, Haiku by default — the fast/cheap
  classification path; pass `model=` for a larger model). **Key-optional**: no
  `ANTHROPIC_API_KEY` ⇒ disabled, and it contributes nothing.
- [`SentimentContributor`](../src/waystone3/signals/sentiment.py) — stays a *pure* function
  over bars by taking an injected `{symbol → score}` map. The async pre-step
  `build_sentiment_scores(...)` fetches + scores + aggregates to the normalized −10..+10
  scale.

Wiring it in (technical-first stays the default; add sentiment as one more weighted key):

```python
scores, drivers = await build_sentiment_scores(
    news_source=PolygonNewsSource(), scorer=ClaudeSentimentScorer(), symbols=watchlist
)
contributors = [*default_contributors(), SentimentContributor(scores, drivers)]
weights = {**default_weights(), "sentiment": 0.2}
```

It is registered (`registry.CONTRIBUTORS["sentiment"]`) but absent from `default_weights()`,
so it never affects existing runs until you opt in.

## 3. The Claude Arena (multi-player competition)

A paper-trading competition for up to 5 players, each bringing a strategy, accessed from
their own Claude via a hosted MCP server.

- [`Competition`](../src/waystone3/competition/competition.py) — per-player paper account +
  RiskGuard; `run_cycle` / `run_backtest` use the player's config; `standings()` ranks by
  paper-account return.
- [`CompetitionService`](../src/waystone3/competition/service.py) — token-authenticated
  facade (all logic, fully tested); the organizer holds an admin token to register players.
- [`mcp_server.py`](../src/waystone3/mcp_server.py) — FastMCP transport exposing the tools
  `submit_strategy`, `run_cycle`, `run_backtest`, `my_account`, `standings`,
  `register_player`. Per-user auth: a middleware reads `Authorization: Bearer <token>` into
  a contextvar each tool reads, so each player acts as themselves.

A player's **strategy** is just engine knobs: contributor weights, watchlist, thresholds,
notional. Example submission via Claude: *"submit a strategy with ma_crossover 0.5,
price_action 0.5 on AAPL, MSFT, NVDA"*.

### Running it

```sh
# Organizer hosts the server (HTTP for remote players):
export WAYSTONE_ADMIN_TOKEN=$(openssl rand -hex 16)
uv run python -c "from waystone3.mcp_server import run; run(transport='http', host='0.0.0.0', port=9100)"

# Register the 5 players (returns each player's bearer token to hand out):
#   call the register_player tool with the admin token, or script CompetitionService.register
```

Each player adds the server as a remote MCP connector in Claude with their own bearer token,
then talks to it in natural language. Everything is paper money.

### Hosting / productionization (not built)

The server runs locally today. For real hosting: put it behind TLS, persist the
`Competition` (it's in-memory — a restart resets accounts and the leaderboard), and
optionally swap the static data source for Polygon. `/healthz` is open for load-balancer
checks; every other route requires a bearer token.
