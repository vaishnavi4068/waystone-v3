# Waystone v3

A nimble, technical-momentum trading platform. Greenfield rebuild.

## Why

The strategy is momentum / trend-following: buy strength, exit when momentum is lost.
No mean reversion. The previous version buried its technical inputs (volume, moving-average
crossovers, price action) inside a sentiment-centric pipeline, so adding a plain technical
signal meant editing many files. v3 inverts that: every signal is a `SignalContributor` —
a pure function over OHLCV bars returning a normalized `-10..+10` score. Adding a new
momentum input is **one file + one registry line**.

## Pipeline

```
data → signals (contributors) → fusion (weighted blend) → decision → risk → paper order
                                                          ↓ events
                              Agent OS: bus → agents (observe / act) → alerts
```

## Quickstart

```sh
uv sync
uv run waystone3 run                                         # zero-config: stub uptrend, always works offline
uv run waystone3 run --source yfinance --symbols AAPL,MSFT   # real market bars
uv run waystone3 backtest --source yfinance --symbols SPY --start 2023-01-01 --end 2024-01-01
uv run waystone3 serve --cycles 3                            # Agent OS: cycles + reactive agents
```

By default it runs against an in-process `PaperBroker` (no credentials needed). Pass
`--broker alpaca` with Alpaca paper keys to route to Alpaca paper trading.

## Agent OS

A reactive control plane sits on top of the core: each cycle publishes events to an event
bus, and agents react — judging cycles (Claude), halting trading after repeated risk blocks,
re-tuning signal weights, and dispatching alerts. **Acting** agents never touch the broker
directly; they go through an action gateway that enforces paper-only, an approval policy, and
an audit trail. The trading core never imports the agent layer — it only gains an optional
`bus`. Claude agents are key-optional (they self-disable without `ANTHROPIC_API_KEY`). See
[docs/agent-os.md](docs/agent-os.md).

## Layout

- `core/` — shared types (`Bar`, `Order`, `Position`, …)
- `data/` — market data sources (yfinance)
- `brokers/` — `Broker` protocol, `PaperBroker`, `AlpacaBroker`
- `indicators/` — pure indicator functions (ema, sma, roc, …)
- `signals/` — `SignalContributor` protocol + volume / MA-crossover / price-action contributors
- `fusion/` — weighted blend into a composite momentum score
- `decision/` — momentum-only entry/exit engine
- `risk/` — single-gate `RiskGuard` (paper-only)
- `runner/` — cycle + backtest
- `news/` — news sources (Polygon) feeding sentiment
- `bus/` — event bus + typed events (Agent OS)
- `agents/` — agent protocol, registry, action gateway, observe/acting agents
- `alerts/` — channels, router, recipients, audit
- `competition/` — multi-player strategy competition + token-authed service
- `agent_os.py` — wires the control plane to the core
- `mcp_server.py` — hosted MCP server for the Claude Arena
- `cli.py` — `run`, `backtest`, `serve`

## Data, sentiment, and the Claude Arena

- **Polygon market data** — paid feed for backtesting-grade bars: `--source polygon`
  (needs `POLYGON_API_KEY`).
- **News-driven sentiment** — an opt-in `SignalContributor` fed by Polygon news + a Claude
  scorer (key-optional). Technical signals stay the default; sentiment is one more weight.
- **Claude Arena** — a hosted multi-player paper-trading competition: up to 5 people submit
  strategies and compete on a leaderboard, all from their own Claude via an MCP server.

See [docs/data-news-and-arena.md](docs/data-news-and-arena.md).

## Adding a signal

A new momentum input is **one file + one registry line** — fusion, decision, risk, and the
runner are untouched. Sentiment is just another (optional) contributor, not a special path.
See [docs/adding-a-signal.md](docs/adding-a-signal.md).

## Dev

```sh
uv run ruff check
uv run mypy src
uv run pytest
```
