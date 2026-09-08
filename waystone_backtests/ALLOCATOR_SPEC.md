# Research book allocator

Combine dated sleeve equity curves from `results/` into one HQ research book. Compute lives on the Mac Studio; HQ reads the published book equity from GCS like any other sleeve.

## Slice 1 (this PR)

Deliverables:

| File | Role |
|---|---|
| `book.yaml` | Book name, NAV, method, sleeve list with equity paths and static weights |
| `ml/allocator.py` | Load YAML, align sleeve `daily_ret` series, combine, write `results/<book>/` |
| `tests/test_allocator.py` | Synthetic alignment, weighting, YAML load |

**Methods implemented**

- `equal_weight` — normalize static weights from YAML (default 1/N per sleeve)
- `sleeve_mean` — unweighted mean of sleeve daily returns (baseline in `regime_hmm.py --sleeves`)

**Outputs** (same contract as other ML scripts)

- `results/<book_name>/equity.csv` — `date, equity, daily_ret, daily_pnl`
- `results/<book_name>/metrics.json` — `strategy`, `params`, `stats`, `extra`
- `results/<book_name>/kpi.json` — Stage-Gate KPI card via `ml/kpi_export.compute_kpis`

**Explicitly out of scope for slice 1**

- Live bot hooks, `export_live_equity`, IBKR order routing
- State-conditional allocation (lift from `regime_hmm.py --sleeves` in slice 2)
- Vol-target / risk-parity across sleeves
- `catalog.json` entry or `research-publish` (slice 2 after gates reviewed)
- Sleeve reruns

## Slice 2 (planned)

- `state_conditional` method using `data/regime/<name>_states.csv`
- Book-level corr KPI vs HQ live book return series
- Catalog + publish integration

## CLI

```bash
cd waystone_backtests
python ml/allocator.py --book book.yaml
python ml/allocator.py --book book.yaml --synthetic   # 3 fake sleeves for tests
```

## book.yaml schema

```yaml
version: 1
name: research_book_v1
nav: 100000
method: equal_weight          # equal_weight | sleeve_mean
warmup_days: 0                # drop first N combined days from KPI stats
sleeves:
  - id: 01_mean_reversion_pullback
    equity: results/01_mean_reversion_pullback/equity.csv
    weight: 1.0
```

Paths are relative to `waystone_backtests/`. Each equity CSV must have a `daily_ret` column (or `daily_pnl` + known NAV).

## Gate policy

Stage 4 in the scorecard requires every critical sleeve gate green before capital allocation. Slice 1 only builds the combined curve; it does not override per-sleeve verdicts.
