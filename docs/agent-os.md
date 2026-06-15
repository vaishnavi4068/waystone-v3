# The Agent OS

A reactive control plane on top of the trading core. The core publishes events; agents
observe and — when permitted — act, with paper-only enforcement, an approval policy, and a
full audit trail. The cardinal rule is preserved: **the trading core never imports the
agent layer.** `run_cycle` gains only an optional `bus` (and a plain `trading_enabled`
bool); everything reactive lives in `bus/`, `agents/`, and `alerts/`, wired together by the
single `agent_os.py` orchestrator.

## Pieces

| Layer | Module | Role |
|---|---|---|
| Event bus | [bus/](../src/waystone3/bus/) | Async pub/sub; typed events; failing handlers isolated into `AgentError` |
| Agents | [agents/](../src/waystone3/agents/) | `Agent` protocol + registry; observe and acting agents |
| Action gateway | [agents/actions.py](../src/waystone3/agents/actions.py) | The **only** way agents change state — paper-only, approval policy, audit |
| Alerts | [alerts/](../src/waystone3/alerts/) | Channels (log / Twilio SMS / WhatsApp), role+severity router, recipients, audit |
| Orchestrator | [agent_os.py](../src/waystone3/agent_os.py) | Wires bus + state + gateway + alerts + roster; runs cycles |

## Events

`DecisionMade`, `StrongSignal`, `OrderFilled`, `OrderBlocked`, `CycleCompleted`,
`CycleJudged`, `AgentAction`, `AlertRaised`, `AgentError`. Subscribing to the base `Event`
receives all of them. `run_cycle` publishes the first six; agents publish the rest.

## The roster

- **LoggingAgent** (observe) — logs cycles, strong signals, blocks.
- **RiskSupervisorAgent** (act) — halts trading via the gateway after N risk blocks.
- **AnalystAgent** (observe, Claude) — judges each cycle → `CycleJudged`. Key-optional.
- **TuningAgent** (act, Claude) — on a concerning judgement, picks a weight preset and
  submits it through the gateway. Key-optional.
- **NotifierAgent** (observe) — turns events into alerts and dispatches them.

## Acting safely — the gateway

Acting agents never touch the broker, weights, or switches directly. They submit an
`ActionRequest` (`AdjustWeights`, `GateTrading`, `ScaleExposure`, `TriggerRetune`) to the
`ActionGateway`, which enforces, in order:

1. **Paper-only** — refuses to apply anything when `state.is_paper` is false, regardless of
   policy.
2. **Approval policy** — `AUTO` applies immediately (paper/demo); `MANUAL` queues for human
   `approve()`; `DENY` blocks all acting.
3. **Audit** — every request is recorded (`applied` / `pending` / `denied`) and an
   `AgentAction` event is published.

Approved actions mutate `RuntimeState` (weights, `trading_enabled`, `exposure_scale`), which
the next cycle reads — so a change takes effect on the following pass, never mid-cycle.

## Claude agents — key-optional

`AnalystAgent` and `TuningAgent` extend `ClaudeAgent`. Without an `ANTHROPIC_API_KEY` (and
without an injected `complete_fn`) they self-disable and the bus simply never routes to
them. They use `claude-opus-4-8` by default via structured output (`output_config.format`).
Tests inject a `complete_fn` so no key or network is needed.

## Run it

```sh
uv run waystone3 serve --cycles 3                      # zero-config (stub data, paper broker)
uv run waystone3 serve --source yfinance --symbols SPY,QQQ --cycles 5
ANTHROPIC_API_KEY=sk-... uv run waystone3 serve --cycles 5   # Claude analyst + tuner active
```

Approval policy defaults to `AUTO` for paper/demo; switch to `MANUAL` in `agent_os.serve(...)`
to require human approval of every agent action (pending actions wait for
`gateway.approve(id)`).

## Extending

A new agent is one class implementing the `Agent` protocol (`name`, `kind`,
`subscribes_to`, `handle`) plus one `registry.register(...)` line in `build_agent_os`. A new
event is one frozen dataclass in [bus/events.py](../src/waystone3/bus/events.py). A new alert
channel implements the `Channel` protocol. None of these touch the trading core.
