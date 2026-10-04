-- Run as: waystone_load. Safe to re-run: every insert upserts.
-- Values marked CONFIRM are placeholders until the strategy owner confirms them.
\set ON_ERROR_STOP on

INSERT INTO ref.strategy (strategy_code, display_name, asset_class, instrument_root, registry_algo_id,
                          gcs_paper_prefix, gcs_backtest_prefix, backtest_file_prefix,
                          broker_account, broker_client_id, paper_start_date, notes)
VALUES
    ('es_v221', 'ES V221', 'future', 'ES', 'es_futures',
     'raw/paper/es_v221/', 'raw/backtest/', 'ES_',
     'DUR842609', 53, DATE '2026-09-24',
     'V221-BHQ-BASELINE-v3, 2 contracts, flatten 15:55 ET. Log clientId 53 (registry default is 2).'),
    ('nq_v221', 'NQ V221', 'future', 'NQ', 'nq_futures',
     'raw/paper/nq_v221/', 'raw/backtest/', 'NQ_',
     NULL, 1, NULL,
     'CONFIRM: broker account, paper start date, log folder on the VM.'),
    ('s5_options', 'Strategy 5 options', 'option', 'TBD', 's5_options',
     'raw/paper/s5_options/', 'raw/backtest/', 'S5_',
     NULL, 42, NULL,
     'CONFIRM: underlying, broker account, paper start date, log and replay formats.')
ON CONFLICT (strategy_code) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    asset_class = EXCLUDED.asset_class,
    registry_algo_id = EXCLUDED.registry_algo_id,
    gcs_paper_prefix = EXCLUDED.gcs_paper_prefix,
    gcs_backtest_prefix = EXCLUDED.gcs_backtest_prefix,
    backtest_file_prefix = EXCLUDED.backtest_file_prefix,
    broker_client_id = EXCLUDED.broker_client_id;

-- ES values are the workbook's "ES 8. Settings" tab.
INSERT INTO ref.strategy_settings (strategy_id, valid_from, starting_capital, point_value, tick_size,
       default_contracts, commission_rt_per_contract, model_slip_rt_per_contract, session_minutes,
       session_roll_hour, flatten_time, daily_loss_cap, max_trades_per_day, risk_free_rate, ann_days,
       min_days_ratio, min_days_calmar, sync_pct_threshold, sync_usd_floor, expected_bar_count, notes)
SELECT s.strategy_id, v.valid_from, v.capital, v.point_value, v.tick, v.contracts, v.comm, v.slip,
       v.session_minutes, v.roll_hour, v.flatten, v.loss_cap, v.max_trades, 0, 252, 5, 21, 0.15, 0,
       v.bars, v.notes
FROM (VALUES
    ('es_v221', DATE '2026-09-24', 100000.00, 50.0, 0.25, 2, 4.50, 12.50, 1380, 18, TIME '15:55',
     -2500.00, 12, 961, 'From ES_Futures_KPI_Dashboard.xlsx Settings tab.'),
    ('nq_v221', DATE '2026-09-01', 100000.00, 20.0, 0.25, 2, 4.50, 5.00, 1380, 18, TIME '15:55',
     -2500.00, 12, 961, 'CONFIRM: copied from ES with NQ point value ($20).'),
    ('s5_options', DATE '2026-09-01', 100000.00, 100.0, 0.01, 1, 1.30, 4.00, 390, 18, NULL,
     NULL, NULL, NULL, 'CONFIRM: commission and slippage are placeholders.')
) AS v(code, valid_from, capital, point_value, tick, contracts, comm, slip, session_minutes,
       roll_hour, flatten, loss_cap, max_trades, bars, notes)
JOIN ref.strategy s ON s.strategy_code = v.code
ON CONFLICT (strategy_id, valid_from) DO NOTHING;

INSERT INTO ref.strategy_config (strategy_id, params_fp, config_label, first_seen_date)
SELECT strategy_id, 'f36bb2a138', 'V221-BHQ-BASELINE-v3', DATE '2026-10-02'
FROM ref.strategy WHERE strategy_code = 'es_v221'
ON CONFLICT DO NOTHING;

INSERT INTO ref.instrument (symbol, root, asset_class, exchange, multiplier, expiry)
VALUES
    ('ESZ6', 'ES', 'future', 'CME', 50, DATE '2026-12-18'),
    ('NQZ6', 'NQ', 'future', 'CME', 20, DATE '2026-12-18')
ON CONFLICT (symbol) DO NOTHING;

-- Full-closure days from the workbook Settings tab.
INSERT INTO ref.trading_holiday (exchange, holiday_date, name)
VALUES
    ('CME', DATE '2026-11-26', 'Thanksgiving'),
    ('CME', DATE '2026-12-25', 'Christmas'),
    ('CME', DATE '2027-01-01', 'New Year''s Day'),
    ('CME', DATE '2027-03-26', 'Good Friday'),
    ('CME', DATE '2027-12-24', 'Christmas (observed)'),
    ('CME', DATE '2028-04-14', 'Good Friday'),
    ('CME', DATE '2028-12-25', 'Christmas')
ON CONFLICT DO NOTHING;

INSERT INTO ref.kpi_definition (kpi_code, asset_class, section, section_name, label, description,
                                direction, green_at, amber_at, unit, source, is_critical, sort_order)
VALUES
    ('fut_trade_count', 'future', 'tier0', 'Tier 0 — Statistical validity gate', 'No. of independent trades', 'Sample size. Below ~100 trades every stat is inside its own noise band. Gated on ITD only.', 'Higher', 200, 100, 'count', 'COMPUTED', true, 10),
    ('fut_sharpe', 'future', 'tier1', 'Tier 1 — Risk-adjusted performance', 'Sharpe ratio (ann., net)', 'Mean daily return ÷ st.dev. of daily return × √252, after costs, on paper capital.', 'Higher', 1.5, 1, 'ratio', 'COMPUTED', true, 11),
    ('fut_calmar', 'future', 'tier1', 'Tier 1 — Risk-adjusted performance', 'Calmar / MAR ratio', 'Annualised window return (CAGR) ÷ max drawdown in the same window. Needs ≥ 21 trading days.', 'Higher', 0.75, 0.5, 'ratio', 'COMPUTED', false, 12),
    ('fut_max_dd', 'future', 'tier2', 'Tier 2 — Risk & drawdown', 'Max drawdown', 'Largest peak-to-trough decline in account equity, on daily closes.', 'Lower', 0.15, 0.25, 'pct', 'COMPUTED', true, 13),
    ('fut_dd_duration_mo', 'future', 'tier2', 'Tier 2 — Risk & drawdown', 'Max drawdown duration (mo)', 'Longest run of trading days below a prior equity high, ÷ 21.', 'Lower', 9, 18, 'months', 'COMPUTED', false, 14),
    ('fut_ann_vol', 'future', 'tier2', 'Tier 2 — Risk & drawdown', 'Annualised volatility', 'St.dev. of daily returns × √252 on paper capital.', 'Lower', 0.15, 0.25, 'pct', 'COMPUTED', false, 15),
    ('fut_cvar95', 'future', 'tier2', 'Tier 2 — Risk & drawdown', 'CVaR 95% (per day)', 'Average loss on the worst 5% of days (expected shortfall).', 'Lower', 0.02, 0.035, 'pct', 'COMPUTED', false, 16),
    ('fut_ulcer', 'future', 'tier2', 'Tier 2 — Risk & drawdown', 'Ulcer index', 'RMS of daily drawdown depth (in %).', 'Lower', 5, 10, 'index', 'COMPUTED', false, 17),
    ('fut_profit_factor', 'future', 'tier3', 'Tier 3 — Trade quality & source of edge', 'Profit factor', 'Sum of winning trades'' net P&L ÷ |sum of losing trades'' net P&L|.', 'Higher', 1.5, 1.2, 'ratio', 'COMPUTED', true, 18),
    ('fut_win_rate', 'future', 'tier3', 'Tier 3 — Trade quality & source of edge', 'Win rate', '% of round turns with positive net P&L.', 'Higher', 0.5, 0.4, 'pct', 'COMPUTED', false, 19),
    ('fut_time_in_market', 'future', 'tier3', 'Tier 3 — Trade quality & source of edge', 'Time in market', 'Sum of trade hold minutes ÷ (trading days × session minutes).', 'Lower', 0.6, 0.85, 'pct', 'COMPUTED', false, 20),
    ('fut_cost_drag', 'future', 'tier4', 'Tier 4 — Cost robustness', 'Cost drag (% of gross P&L)', '(Commission + observed slippage) ÷ P&L at signal prices.', 'Lower', 0.3, 0.5, 'pct', 'COMPUTED', true, 21),
    ('fut_slippage_realism', 'future', 'tier4', 'Tier 4 — Cost robustness', 'Slippage realism', 'Backtest''s modelled slippage ÷ slippage observed between signal and fill. ≥ 1 = backtest conservative.', 'Higher', 1, 0.7, 'ratio', 'COMPUTED', false, 22),
    ('hdr_trading_days', 'any', 'header', 'Window summary', 'Trading days', NULL, NULL, NULL, NULL, 'count', 'COMPUTED', false, 1),
    ('hdr_trades', 'any', 'header', 'Window summary', 'Round-turn trades', NULL, NULL, NULL, NULL, 'count', 'COMPUTED', false, 2),
    ('hdr_net_pnl', 'any', 'header', 'Window summary', 'Net P&L ($)', NULL, NULL, NULL, NULL, 'usd', 'COMPUTED', false, 3),
    ('hdr_return', 'any', 'header', 'Window summary', 'Return on window-start equity', NULL, NULL, NULL, NULL, 'pct', 'COMPUTED', false, 4),
    ('hdr_equity_end', 'any', 'header', 'Window summary', 'Equity at window end ($)', NULL, NULL, NULL, NULL, 'usd', 'COMPUTED', false, 5),
    ('opt_weekly_sharpe', 'option', 's1', 'Stage 1 — Backtest Performance', 'Net weekly Sharpe', 'Mean weekly options return / weekly vol, annualized * sqrt(52).', 'Higher', 1.5, 1.0, NULL, 'COMPUTED', true, 100),
    ('opt_sortino', 'option', 's1', 'Stage 1 — Backtest Performance', 'Sortino ratio', 'Annualized daily mean ÷ downside deviation (returns below 0).', 'Higher', 2.0, 1.5, NULL, 'COMPUTED', false, 101),
    ('opt_max_dd', 'option', 's1', 'Stage 1 — Backtest Performance', 'Max drawdown (% NAV)', 'Largest peak-to-trough decline of cumulative options P&L as % of assumed NAV.', 'Lower', 15.0, 25.0, NULL, 'DERIVED', true, 102),
    ('opt_calmar', 'option', 's1', 'Stage 1 — Backtest Performance', 'Calmar (ann. return / MaxDD)', 'Annualized return divided by absolute max drawdown.', 'Higher', 1.0, 0.5, NULL, 'DERIVED', false, 103),
    ('opt_profit_factor', 'option', 's1', 'Stage 1 — Backtest Performance', 'Profit factor', 'Sum of winning option trades ÷ abs(sum of losing option trades).', 'Higher', 1.5, 1.25, NULL, 'COMPUTED', false, 104),
    ('opt_trade_count', 'option', 's1', 'Stage 1 — Backtest Performance', 'Trade count', 'Closed option trades (fills with a realized P&L).', 'Higher', 200.0, 100.0, NULL, 'COMPUTED', true, 105),
    ('opt_worst_month', 'option', 's1', 'Stage 1 — Backtest Performance', 'Worst month loss (% NAV)', 'Most negative calendar-month options P&L as % of assumed NAV.', 'Lower', 8.0, 12.0, NULL, 'DERIVED', false, 106),
    ('opt_cvar95', 'option', 's1', 'Stage 1 — Backtest Performance', 'Daily CVaR 95% (% NAV)', 'Mean of the worst 5% of daily options returns, as % of NAV.', 'Lower', 2.0, 3.0, NULL, 'DERIVED', false, 107),
    ('opt_monthly_skew', 'option', 's1', 'Stage 1 — Backtest Performance', 'Monthly return skewness', 'Skewness of calendar-month options returns.', 'Higher', -1.0, -2.0, NULL, 'COMPUTED', false, 108),
    ('opt_sharpe_cost_stress', 'option', 's1', 'Stage 1 — Backtest Performance', 'Sharpe under cost stress (live)', 'Weekly Sharpe after subtracting assumed round-trip slippage (% of premium).', 'Higher', 1.0, 0.7, NULL, 'DERIVED', true, 109),
    ('opt_ann_return', 'option', 's1', 'Stage 1 — Backtest Performance', 'Annual return (%)', 'Annualized gross options return vs assumed NAV (sheet target 120%).', 'Higher', 120.0, 100.0, NULL, 'DERIVED', true, 110),
    ('opt_oos_is_sharpe', 'option', 's2', 'Stage 2 — Robustness & Overfitting', 'OOS/IS Sharpe (time-split proxy)', 'Last 30% weekly Sharpe ÷ first 70% weekly Sharpe.', 'Higher', 0.6, 0.4, NULL, 'COMPUTED', true, 111),
    ('opt_wf_efficiency', 'option', 's2', 'Stage 2 — Robustness & Overfitting', 'Walk-forward efficiency', 'Same time-split ratio; retained as the sheet''s walk-forward proxy.', 'Higher', 0.6, 0.4, NULL, 'COMPUTED', false, 112),
    ('opt_pbo', 'option', 's2', 'Stage 2 — Robustness & Overfitting', 'Prob. of Backtest Overfitting (%)', 'Estimated chance the strategy is overfit (manual).', 'Lower', 25.0, 40.0, NULL, 'MANUAL', false, 113),
    ('opt_bootstrap_p5_sharpe', 'option', 's2', 'Stage 2 — Robustness & Overfitting', 'Bootstrap 5th-pct Sharpe', '5th percentile of Sharpe from resampling daily P&L.', 'Higher', 0.5, 0.0, NULL, 'COMPUTED', false, 114),
    ('opt_years_covered', 'option', 's2', 'Stage 2 — Robustness & Overfitting', 'Distinct years covered', 'Unique calendar years present in published option dumps.', 'Higher', 3.0, 2.0, NULL, 'COMPUTED', false, 115),
    ('opt_trial_log', 'option', 's2', 'Stage 2 — Robustness & Overfitting', 'Complete trial log maintained', 'Yes/No — is a complete trial log maintained (manual).', 'Higher', 1.0, 1.0, NULL, 'MANUAL', true, 116),
    ('opt_net_vega', 'option', 's3', 'Stage 3 — Options Risk & Attribution', '|Net vega| (% NAV / vol pt)', 'P&L per 1-point implied-vol move as % of NAV (needs IBKR greeks).', 'Lower', 0.1, 0.2, NULL, 'MANUAL', true, 117),
    ('opt_net_delta', 'option', 's3', 'Stage 3 — Options Risk & Attribution', '|Net delta| (% NAV)', 'Net directional exposure to the underlying (needs IBKR greeks).', 'Lower', 10.0, 20.0, NULL, 'MANUAL', false, 118),
    ('opt_payoff_ratio', 'option', 's3', 'Stage 3 — Options Risk & Attribution', 'Payoff ratio (avg win/avg loss)', 'Average winning trade size vs average losing trade size.', 'Higher', 1.8, 1.3, NULL, 'COMPUTED', false, 119),
    ('opt_expectancy_bps', 'option', 's3', 'Stage 3 — Options Risk & Attribution', 'Per-trade expectancy (bps NAV)', 'Average realized P&L per closed option trade, as basis points of NAV.', 'Higher', 20.0, 10.0, NULL, 'DERIVED', false, 120),
    ('opt_peak_margin', 'option', 's3', 'Stage 3 — Options Risk & Attribution', 'Peak margin utilization (%)', 'Highest maint. margin ÷ NLV across published snapshots.', 'Lower', 50.0, 65.0, NULL, 'DERIVED', true, 121),
    ('opt_capital_util', 'option', 's3', 'Stage 3 — Options Risk & Attribution', 'Capital utilization (%)', 'Average options notional ÷ assumed NAV across published days.', 'Higher', 50.0, 40.0, NULL, 'DERIVED', false, 122),
    ('opt_incubation_months', 'option', 's4', 'Stage 4 — Incubation', 'Incubation length (months)', 'Does it hold up in a live/paper trial before real capital?', 'Higher', 3.0, 2.0, NULL, 'MANUAL', true, 123),
    ('opt_incubation_trades', 'option', 's4', 'Stage 4 — Incubation', 'Incubation trade count', 'How many trades happened during incubation.', 'Higher', 50.0, 30.0, NULL, 'MANUAL', false, 124),
    ('opt_slippage_ratio_inc', 'option', 's4', 'Stage 4 — Incubation', 'Realized/modeled slippage ratio', 'Actual slippage vs what the model assumed.', 'Lower', 1.2, 1.5, NULL, 'MANUAL', true, 125),
    ('opt_incubation_sharpe_ratio', 'option', 's4', 'Stage 4 — Incubation', 'Incubation Sharpe / backtest Sharpe', 'Live Sharpe ÷ backtest Sharpe.', 'Higher', 0.7, 0.5, NULL, 'MANUAL', true, 126),
    ('opt_ops_errors', 'option', 's4', 'Stage 4 — Incubation', 'Ops errors per 100 trades', 'Fat-fingers, missed fills, system glitches per 100 trades.', 'Lower', 1.0, 3.0, NULL, 'MANUAL', false, 127),
    ('opt_kill_switch', 'option', 's5', 'Stage 5 — Live-Readiness', 'Daily loss kill switch tested', 'Circuit-breaker actually fires in practice (Yes=1).', 'Higher', 1.0, 1.0, NULL, 'MANUAL', true, 128),
    ('opt_dd_stop', 'option', 's5', 'Stage 5 — Live-Readiness', 'Max-drawdown stop defined', 'Written rule for de-allocating if losses hit a threshold (Yes=1).', 'Higher', 1.0, 1.0, NULL, 'MANUAL', true, 129),
    ('opt_missed_fills', 'option', 'exec', 'Weekly Execution & Slippage Report', 'Missed fills (count)', 'Intended orders that did not receive a fill.', 'Lower', 0.0, 2.0, NULL, 'MANUAL', false, 130),
    ('opt_missed_fills_pct', 'option', 'exec', 'Weekly Execution & Slippage Report', 'Missed fills (% of orders)', 'Missed fills divided by total intended orders for the week.', 'Lower', 5.0, 10.0, NULL, 'MANUAL', false, 131),
    ('opt_avg_slippage_pct', 'option', 'exec', 'Weekly Execution & Slippage Report', 'Average realized slippage (% of premium)', 'Average execution slippage as a percentage of option premium.', 'Lower', 2.0, 4.0, NULL, 'MANUAL', false, 132),
    ('opt_slippage_ratio', 'option', 'exec', 'Weekly Execution & Slippage Report', 'Realized/modeled slippage ratio', 'Actual slippage divided by the slippage assumed in the model.', 'Lower', 1.2, 1.5, NULL, 'MANUAL', false, 133)
ON CONFLICT (kpi_code) DO UPDATE SET
    section = EXCLUDED.section,
    section_name = EXCLUDED.section_name,
    label = EXCLUDED.label,
    description = EXCLUDED.description,
    direction = EXCLUDED.direction,
    green_at = EXCLUDED.green_at,
    amber_at = EXCLUDED.amber_at,
    unit = EXCLUDED.unit,
    source = EXCLUDED.source,
    is_critical = EXCLUDED.is_critical,
    sort_order = EXCLUDED.sort_order;

-- ES research reference (workbook scorecard "Backtest ref." column and BT1/BT2).
-- net_pnl_scorecard (634,585) and net_pnl_bt2 (202,713) disagree in the workbook; both are kept.
INSERT INTO ref.backtest_reference (strategy_id, ref_key, num_value, text_value, source_doc)
SELECT s.strategy_id, v.ref_key, v.num_value, v.text_value, v.source_doc
FROM (VALUES
    ('round_turn_trades', 1380::numeric, NULL, 'KPI Dashboard Backtest ref. column (Jan-2021 to Sep-2026)'),
    ('net_pnl_scorecard', 634585, NULL, 'KPI Dashboard Backtest ref. column (Jan-2021 to Sep-2026)'),
    ('net_pnl_bt2', 202713, NULL, 'BT2 Returns Calculator, 2021-01-01 to 2026-09-07'),
    ('net_pnl_2021', 13796, NULL, 'BT1'), ('max_dd_2021', 0.2204, NULL, 'BT1'),
    ('net_pnl_2022', 159372, NULL, 'BT1'), ('max_dd_2022', 0.1992, NULL, 'BT1'),
    ('net_pnl_2023', 18372, NULL, 'BT1'), ('max_dd_2023', 0.3941, NULL, 'BT1'),
    ('net_pnl_2024', -2525, NULL, 'BT1'), ('max_dd_2024', 0.4916, NULL, 'BT1'),
    ('net_pnl_2025', 46478, NULL, 'BT1'), ('max_dd_2025', 0.4537, NULL, 'BT1'),
    ('net_pnl_2026', -32780, NULL, 'BT1'), ('max_dd_2026', 0.5548, NULL, 'BT1'),
    ('vol_index_open_question', NULL, 'Replay may use VXN instead of VIX for ES; figures provisional.', 'BT3')
) AS v(ref_key, num_value, text_value, source_doc)
CROSS JOIN ref.strategy s
WHERE s.strategy_code = 'es_v221'
ON CONFLICT (strategy_id, ref_key) DO UPDATE SET
    num_value = EXCLUDED.num_value,
    text_value = EXCLUDED.text_value,
    source_doc = EXCLUDED.source_doc;
