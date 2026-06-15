# Adding a signal

The whole point of v3: a new momentum input is **one file + one registry line**. Nothing
in fusion, decision, risk, the runner, or the CLI changes. This is the thing the previous
platform couldn't do — there, every signal had to flow through the sentiment/news pipeline,
so a plain technical input meant editing five files.

## The contract

A signal is a `SignalContributor` ([signals/base.py](../src/waystone3/signals/base.py)):

```python
class SignalContributor(Protocol):
    name: str
    def score(self, bars: dict[str, list[Bar]]) -> dict[str, ContributorScore]: ...
```

* Input: `{symbol -> chronological list[Bar]}` (OHLCV).
* Output: a `ContributorScore` per symbol — `score` on `-10..+10` (bullish positive),
  optional `confidence` (`0..1`), and `drivers` (human-readable reasons).
* Pure and synchronous. No network, no async, no config object. Omit symbols that lack
  warmup. Because it's pure, you test it with hand-built bar lists.

## Steps

1. Create `src/waystone3/signals/<your_signal>.py` implementing the protocol. Use the
   existing contributors as templates:
   [ma_crossover.py](../src/waystone3/signals/ma_crossover.py),
   [price_action.py](../src/waystone3/signals/price_action.py),
   [volume.py](../src/waystone3/signals/volume.py).
2. Add one line to `CONTRIBUTORS` in
   [signals/registry.py](../src/waystone3/signals/registry.py).
3. Give it a weight in `default_weights()`
   ([runner/config.py](../src/waystone3/runner/config.py)) — or pass custom weights.
4. Add a unit test asserting score sign and saturation on synthetic bars.

That's it. Fusion picks it up automatically (it's just another weighted key), the decision
engine consumes the blended composite unchanged, and `run`/`backtest` use it.

## The sentiment seam (intentionally not built yet)

Sentiment is **not special** in v3 — it's just a contributor whose data happens to come
from news rather than bars. To add it later:

* `signals/sentiment.py` — a `SentimentContributor` implementing the same protocol. The one
  wrinkle: it needs article data, not just bars. Two clean options:
  * pre-compute per-symbol sentiment scores and pass them in via the contributor's
    constructor (keeps `score()` pure over its inputs), or
  * widen the contributor input to an optional context bag if several non-bar signals
    appear. Prefer the constructor-injection approach first — it keeps the protocol pure.
* One line in `CONTRIBUTORS`, one entry in `default_weights()` (e.g. `"sentiment": 0.2`).

No change to `fuse()`, `DecisionEngine`, `RiskGuard`, `run_cycle`, `run_backtest`, or the
CLI. That invariant — adding a signal never touches the pipeline — is the design's whole
reason for existing; keep it true.
