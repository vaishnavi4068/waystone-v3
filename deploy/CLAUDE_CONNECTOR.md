# Connecting Claude to Waystone

## HQ futures data (read-only): `https://dash.arqflo.ai/api/mcp`

The dashboard API serves a read-only MCP endpoint with the `hq_*` tools: `hq_strategies`,
`hq_sync`, `hq_compare`, `hq_paper_day`, `hq_kpis`, `hq_daily_pnl`, `hq_trades`,
`hq_returns` and `hq_load_status`. It cannot place orders. It ships with the dashboard API
image, so there is nothing extra to deploy.

**Token.** Use your dashboard login token. It stays valid across redeploys because members
live on the API's persistent volume.

```sh
curl -s -X POST https://dash.arqflo.ai/api/login -H 'content-type: application/json' \
  -d '{"username":"<you>","password":"<your password>"}'
# -> {"name": "...", "token": "<TOKEN>"}
```

**Claude Cowork / claude.ai / Claude Desktop (custom connector).** Settings → Connectors →
Add custom connector:
- URL: `https://dash.arqflo.ai/api/mcp`
- Authentication: **No sign-in**
- Request headers: `authorization` = `Bearer <TOKEN>` (with the space)

Request headers are an Anthropic beta. If the dialog only shows OAuth client fields, use
Claude Desktop's local config below or Claude Code.

**Claude Desktop (local bridge).** In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "waystone-hq": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://dash.arqflo.ai/api/mcp",
               "--header", "Authorization:${WAYSTONE_AUTH}"],
      "env": { "WAYSTONE_AUTH": "Bearer <TOKEN>" }
    }
  }
}
```

**Claude Code.**

```sh
claude mcp add --transport http waystone-hq https://dash.arqflo.ai/api/mcp \
  --header "Authorization: Bearer <TOKEN>"
```

**Smoke test without Claude.**

```sh
curl -s https://dash.arqflo.ai/api/mcp -H "Authorization: Bearer <TOKEN>" \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Prompts to try: "Which futures strategies lost money on 2026-10-02 and why?", "Compare NQ live
vs backtest for the latest session", "Show R2's KPI scorecard since paper start", "Is the
paper data fresh?".

---

## Trading Arena (`waystone-arena`, separate deployment)

Each of the 5 players connects their own Claude to `https://arena.example.com/mcp` using
**their personal bearer token** (their secure login). The token is per-user, never shared,
and only travels over TLS. Below: which Claude clients work today, plus an optional Agent
Skill so Claude uses the Arena well.

## Auth reality (read first)

| Claude client | Per-user bearer token? | Notes |
|---|---|---|
| **Claude Code (CLI)** | ✅ works now | adds a remote MCP server with an `Authorization` header |
| **Claude Desktop** | ✅ works (via bridge) | use the `mcp-remote` bridge to attach the header |
| **claude.ai web (Pro/Max) Custom Connector** | ❌ not with a raw token | the web connector flow requires **OAuth 2.0**, not a static bearer |

**If you need the claude.ai *web* connector** (so players add it in the browser app with a
login), the server needs an OAuth 2.1 layer (authorize/token/register + a per-user login).
v2 shipped one (`mcp/oauth.py`); it's an additional build on v3. Until then, use Claude Code
or Claude Desktop, which support per-user bearer tokens directly.

## Claude Code (recommended, simplest)

Each player runs (with their own token):

```sh
claude mcp add --transport http arena https://arena.example.com/mcp \
  --header "Authorization: Bearer <THEIR_TOKEN>"
```

Then, in Claude:
> "submit a strategy with ma_crossover 0.5, price_action 0.5 on AAPL, MSFT, NVDA"
> "run a cycle"
> "show the standings"
> "backtest my strategy from 2023-01-01 to 2024-01-01"

## Claude Desktop (via mcp-remote bridge)

Remote MCP with custom headers goes through the `mcp-remote` helper. In
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "arena": {
      "command": "npx",
      "args": [
        "mcp-remote", "https://arena.example.com/mcp",
        "--header", "Authorization: Bearer <THEIR_TOKEN>"
      ]
    }
  }
}
```

Restart Claude Desktop; the Arena tools appear.

## Tools each player gets

`submit_strategy`, `run_cycle`, `run_backtest`, `my_account`, `standings`
(plus `register_player`, which only works with the organizer's admin token).

## Optional: an Agent Skill for nicer UX

Drop a `SKILL.md` in the player's Claude (Agent Skills) so it knows the Arena conventions —
valid contributor names, that everything is paper money, and to check standings after a cycle:

```markdown
---
name: waystone-arena
description: Play the Waystone paper-trading strategy competition via the arena MCP tools.
---
Use the arena MCP tools. A strategy = contributor weights + a watchlist + thresholds.
Valid contributors: ma_crossover, price_action, volume, sentiment. Everything is PAPER money.
After run_cycle, show the player their my_account and the standings. Backtest before submitting
a big change. Never claim real-money trading.
```

This is optional polish — the MCP tools work without it.
