# `ml/allocator.py` — specification

**Purpose.** Turn the set of promoted sleeves into one book. Every evening it decides, for each sleeve, a size
multiplier for the next session and writes it to a file the two bots read at startup. It never picks "today's
best strategy"; it sets risk budgets monthly, and applies a small set of pre-agreed daily switches (regime,
calendar, drawdown). It is also a backtest: the allocation rule is replayed walk-forward on the sleeves'
history and scored on the same KPI card as everything else, and it is only used if it beats equal sizing.

**Not in scope.** Signal generation, order routing, per-trade meta sizing (that stays inside `meta_label`
models and the bots), intraday changes (one decision per session, before the open).

## 1. Where it sits in the day

16:00 ET session closes
16:35 tools/nightly.sh
17:00 bots export live P&L
17:30 ml/allocator.py writes results/book/book_instruction.json
06:00 bots start and read the instruction
09:25 optional re-read

## 2. Output contract — `results/book/book_instruction.json`

schema 1. Keys: generated_at, for_session, valid_until, nav, book {target_daily_vol_pct, realised_daily_vol_pct_20d, drawdown_pct, brake, kill_switch, regime_state, regime_name, events_today}, sleeves {id: {status, multiplier, units, unit_type, risk_weight, reasons, shadow}}.
status ∈ {live, paper, shadow, off, brake}. Never remove a key. Also write book_state.json and book_history.csv.

Field rules:
- multiplier is relative to the sleeve unit size. units is rounded absolute size. Contracts/options to integers, stock notional to $1,000 steps. A multiplier that rounds to 0 units sets shadow: true.
- reasons is a list of short strings.
- valid_until: past this time bots use fallback (§7).

## 3. Inputs

### 3.1 book.yaml (hand-maintained)

nav: 100000
target_daily_vol_pct: 0.8
max_gross_multiplier: 1.5
max_risk_share: 0.45
kelly_fraction: 0.5
shrink_corr: 0.3
vol_halflife_days: 20
corr_window_days: 120
rebalance: {schedule: monthly, hysteresis_pct: 20}
regime: {name: spx}
book_brake: {dd_half_pct: 6, dd_off_pct: 10, monthly_loss_off_pct: 5}
sleeves:
  v221_mnq:
    status: paper   # THIS PR: paper not live
    results_dir: results/base_v221
    live_dir: results/live/v221_mnq
    unit_type: contracts
    unit_size: 2
    unit_step: 1
    instrument: MNQ
    margin_per_unit: 2500
    regime_rules: {off_in_states: [], half_in_states: [2]}
    event_blackout: []
    dd_half_pct: 8
    dd_off_pct: 12
    recover_sessions: 10
    min_incubation_months: 3
  vwap_options:
    status: paper
    results_dir: results/base_vwap
    live_dir: results/live/vwap_options
    unit_type: contracts
    unit_size: 1
    unit_step: 1
    instrument: OPTIONS
    margin_per_unit: null
    regime_rules: {off_in_states: [2], half_in_states: [1]}
    event_blackout: [FOMC]
    dd_half_pct: 6
    dd_off_pct: 10
    recover_sessions: 10
    min_incubation_months: 3
  pullback_bb:
    status: shadow
    results_dir: results/01_mean_reversion_bb
    unit_type: notional_usd
    unit_size: 25000
    unit_step: 1000
    instrument: STOCKS
    regime_rules: {off_in_states: [2]}
    event_blackout: [FOMC]
    dd_half_pct: 6
    dd_off_pct: 10
    recover_sessions: 10
    min_incubation_months: 3

### 3.2 results/<sleeve>/equity.csv — equity, daily_ret, daily_pnl ($ at unit size)
### 3.3 kpi.json + metrics.json — eligibility only (sharpe>=1.5, dsr>=0.95, oosis>=0.6, coststress>=1.0, ntrades>=200, paramsens<=30 or null; holdout_unlocked)
### 3.4 live equity optional; if absent reason pnl_source=backtest. Do not build export_live_equity.py this PR.
### 3.5 data/regime/<name>_states.csv date,state. Missing/stale (>3 sessions) → state 1, reason regime=stale:assume_1
### 3.6 data/fomc_dates.csv; optional data/events_calendar.csv
### 3.7 holdout: sleeve with holdout locked cannot be live; paper allowed

## 4. Algorithm

Step 0 Eligibility: allocator may demote live→paper→shadow, never promote.
eligible_live = status_yaml==live AND kpi gates AND holdout_unlocked AND incubation_months>=min
eligible_paper = status_yaml in (live, paper)
else shadow (or off if status_yaml==off or results_dir missing)

Step 1 Risk: stitch live after backtest; need >=60 sessions else history<60 paper/shadow. Prefer last 252.
σ = EWMA_std(p, halflife=20) floored at 0.3*std(p,252)
C = corr(P[-120:]); C'=(1-λ)C+λI; Σ=diag(σ)C'diag(σ)
Kelly k=mean(p[-252:])/var(p[-252:]) then k *= n/(n+252)

Step 2 ERC: σ_book = target_daily_vol_pct/100 * nav
Solve w>=0 so w_i (Σw)_i = σ_book²/N (Jacobi, start inverse-vol, 200 steps, tol 1e-6)
N=1 vol targeting; N=2 equals inverse-vol
risk_weight_i = w_i(Σw)_i / w'Σw
Caps in order then re-scale once: (1) max_risk_share 0.45 clip+re-solve ERC on rest (2) kelly_fraction*k (3) max_gross_multiplier 1.5
m_raw_i = w_i

Step 3 Regime/calendar: g_regime 0/0.5/1 from off_in_states/half_in_states; g_event=0 if event_blackout hits today's events

Step 4 DD brake persisted per sleeve and book:
dd = (peak-equity)/nav*100 over last 252
state normal|half|brake
normal→half at dd_half; half→brake at dd_off; half→normal when dd < dd_half/2
brake→half after recover_sessions consecutive shadow new highs OR human reset
g_dd = {normal:1, half:0.5, brake:0}
Book: dd on sum of live sleeves at applied sizes; monthly_loss_off until first session of next month; kill_switch never auto-cleared

Step 5 m = m_raw * g_regime * g_event * g_dd * g_book; round to unit_step; shadow if units==0

Step 6 Hysteresis: apply m_raw only on first session of month OR |Δm_raw| > hysteresis_pct OR any gate change. Gates always apply immediately.

Step 7 Write three files; print one-screen report; exit 0; exit 2 if instruction could not be written.

## 8. Edge cases (unit tests)
- One eligible sleeve → vol targeting, risk_weight=1
- Zero eligible → all shadow, book.brake=no_eligible_sleeves, exit 0
- <60 sessions → paper/shadow, history<60
- Missing equity.csv → off, results_missing
- Regime stale/missing → state 1
- σ=0 → shadow, sigma=0
- cov not PD after shrink → inverse-vol, cov_not_pd
- rounds to 0 units → shadow, rounds_to_zero
- Holdout locked → cannot be live
- Brake recovery resets on new low
- Month roll: monthly_loss_off clears; book DD brake does not
- --for-session on weekend/holiday → next session

## 9. Tests required this PR
1. ERC correctness (3 sleeves known σ,C; N=1 vol targeting; N=2 inverse-vol)
2. Caps (Kelly, max_gross, max_risk_share re-solve)
3. Walk-forward purity
4. Brake state machine scripted path
5. Hysteresis
6. Rounding 0.4→0 shadow, 0.6→1
7. Fallback stale instruction
9. Schema of written JSON

## 10. Implementation notes
Pure numpy/pandas; no scipy; deterministic; <5s; no network.
Dates ET. Derive for_session as next session after latest completed in the data; warn if that is not tomorrow.
Round numbers in instruction to 4 dp. Every decision has a reason string.
Keep allocator.py under ~500 lines.

## 11. Worked example
Three sleeves σ=[$900,$600,$400]/day, corr 0.1/0.0/0.3, target $800. ERC ≈ w [0.49, 0.64, 1.02], risk shares ~0.33, w'Σw=800².

## Done
pytest tests/test_allocator.py passes; python ml/allocator.py --dry-run prints a coherent report (may warn on missing results dirs — that is OK); PR opened.
