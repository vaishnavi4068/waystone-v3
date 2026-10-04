-- Run as: waystone_load
-- Stable read surface for the dashboard API and the data MCP. Every view is
-- keyed by strategy_code so callers never need internal ids.
\set ON_ERROR_STOP on

CREATE OR REPLACE VIEW api.v_strategy AS
SELECT s.strategy_code, s.display_name, s.asset_class, s.instrument_root,
       s.paper_start_date, s.is_active, s.notes
FROM ref.strategy s;

CREATE OR REPLACE VIEW api.v_day_status AS
SELECT s.strategy_code, d.session_date, d.paper_status, d.backtest_status, d.sync_status,
       d.paper_loaded_at, d.backtest_loaded_at, d.finalized_at, d.checks
FROM ops.day_status d
JOIN ref.strategy s USING (strategy_id);

CREATE OR REPLACE VIEW api.v_paper_trades AS
SELECT s.strategy_code, t.session_date, t.trade_no, i.symbol AS instrument, t.direction,
       t.contracts, t.signal_bar_ts, t.entry_ts, t.exit_ts, t.entry_px, t.exit_px,
       t.entry_signal_px, t.exit_signal_px, t.entry_slip_pts, t.exit_slip_pts,
       t.fill_latency_s, t.exit_reason, t.points, t.gross_pnl, t.commission,
       t.slippage_cost, t.pnl_at_signal, t.net_pnl, t.hold_min, t.mae_pts, t.mfe_pts,
       t.params_fp, t.is_closed
FROM core.paper_trade t
JOIN ref.strategy s USING (strategy_id)
LEFT JOIN ref.instrument i USING (instrument_id);

CREATE OR REPLACE VIEW api.v_backtest_trades AS
SELECT s.strategy_code, t.session_date, t.trade_seq, t.direction, t.entry_ts, t.exit_ts,
       t.entry_px, t.exit_px, t.points, t.contracts, t.contracts_inferred, t.net_pnl,
       t.net_pnl_derived, t.exit_reason, t.hold_min,
       r.config_label, r.params_fp, r.vol_index, r.bar_count, r.status AS run_status
FROM core.backtest_trade t
JOIN core.backtest_run r USING (run_id)
JOIN ref.strategy s ON s.strategy_id = t.strategy_id;

CREATE OR REPLACE VIEW api.v_comparison AS
SELECT s.strategy_code, c.session_date, c.match_seq, c.match_type, c.unmatched_reason,
       p.entry_ts AS live_entry_ts, b.entry_ts AS bt_entry_ts,
       p.exit_ts AS live_exit_ts, b.exit_ts AS bt_exit_ts,
       p.exit_reason AS live_exit_reason, b.exit_reason AS bt_exit_reason,
       p.points AS live_points, b.points AS bt_points,
       p.net_pnl AS live_net_pnl, b.net_pnl AS bt_net_pnl,
       c.entry_gap_s, c.exit_gap_min, c.points_gap, c.pnl_delta, c.pnl_delta_pct,
       c.exit_reason_match
FROM core.comparison_trade c
JOIN ref.strategy s USING (strategy_id)
LEFT JOIN core.paper_trade p ON p.trade_id = c.paper_trade_id
LEFT JOIN core.backtest_trade b ON b.bt_trade_id = c.bt_trade_id;

CREATE OR REPLACE VIEW api.v_daily_sync AS
SELECT s.strategy_code, s.display_name, d.session_date, d.instrument_symbol,
       d.live_trades, d.live_win_rate, d.live_points, d.live_gross_pnl, d.live_commission,
       d.live_net_pnl, d.live_entry_ts, d.live_exit_ts, d.live_exit_reason,
       d.bt_trades, d.bt_points, d.bt_net_pnl, d.bt_entry_ts, d.bt_exit_ts, d.bt_exit_reason,
       d.pnl_delta, d.pnl_delta_pct, d.exit_reason_match, d.exit_time_gap_min,
       d.loss_cap_hit, d.sync_status, d.notes_auto, n.note AS notes_manual,
       ds.paper_status, ds.backtest_status, d.computed_at
FROM core.daily_sync d
JOIN ref.strategy s USING (strategy_id)
LEFT JOIN core.daily_sync_note n USING (strategy_id, session_date)
LEFT JOIN ops.day_status ds USING (strategy_id, session_date);

CREATE OR REPLACE VIEW api.v_daily_pnl AS
SELECT s.strategy_code, d.session_date, d.week_end, d.month_start, d.trades, d.gross_pnl,
       d.commission, d.slippage_cost, d.net_pnl, d.equity_start, d.equity_end,
       d.daily_return, d.peak_itd, d.dd_itd, d.uw_days_itd
FROM core.daily_pnl d
JOIN ref.strategy s USING (strategy_id);

CREATE OR REPLACE VIEW api.v_kpi AS
SELECT s.strategy_code, v.as_of_date, v.kpi_window, d.section, d.section_name, d.sort_order,
       v.kpi_code, d.label, d.description, d.direction, d.green_at, d.amber_at, d.unit,
       v.num_value, v.text_value, v.status, v.window_start, v.window_end, v.computed_at
FROM kpi.kpi_value v
JOIN ref.strategy s USING (strategy_id)
JOIN ref.kpi_definition d USING (kpi_code);

CREATE OR REPLACE VIEW api.v_kpi_latest AS
SELECT k.*
FROM api.v_kpi k
WHERE k.as_of_date = (
    SELECT max(v.as_of_date)
    FROM kpi.kpi_value v
    JOIN ref.strategy s USING (strategy_id)
    WHERE s.strategy_code = k.strategy_code
);

CREATE OR REPLACE VIEW api.v_scorecard AS
SELECT s.strategy_code, c.as_of_date, c.kpi_window, c.window_start, c.window_end,
       c.trading_days, c.trades, c.net_pnl, c.return_pct, c.equity_end,
       c.red_count, c.amber_count, c.green_count, c.overall_gate, c.computed_at
FROM kpi.scorecard c
JOIN ref.strategy s USING (strategy_id);

CREATE OR REPLACE VIEW api.v_returns_weekly AS
SELECT s.strategy_code, r.week_no, r.week_start, r.week_end, r.trading_days, r.trades,
       r.net_pnl, r.start_equity, r.return_pct, r.max_dd, r.win_rate, r.cum_pnl,
       r.account_value, r.cum_return, r.peak_account, r.dd_from_peak
FROM kpi.returns_weekly r
JOIN ref.strategy s USING (strategy_id);

CREATE OR REPLACE VIEW api.v_returns_monthly AS
SELECT s.strategy_code, r.month_no, r.month_start, r.trading_days, r.trades, r.net_pnl,
       r.start_equity, r.return_pct, r.max_dd, r.sharpe, r.win_rate, r.profit_factor,
       r.cum_pnl, r.account_value, r.cum_return, r.dd_from_peak
FROM kpi.returns_monthly r
JOIN ref.strategy s USING (strategy_id);

CREATE OR REPLACE VIEW api.v_sync_rolling AS
SELECT s.strategy_code, r.as_of_date, r.days_logged, r.all_time_trades, r.all_time_live_pnl,
       r.all_time_bt_pnl, r.avg_pnl_delta_pct, r.all_time_flags, r.flag_rate,
       r.last7_live_pnl, r.last7_flags
FROM kpi.sync_rolling r
JOIN ref.strategy s USING (strategy_id);

CREATE OR REPLACE VIEW api.v_load_health AS
SELECT DISTINCT ON (job) job, run_id, triggered_by, started_at, finished_at, status,
       files_seen, files_loaded, error
FROM ops.load_run
ORDER BY job, started_at DESC;

GRANT SELECT ON ALL TABLES IN SCHEMA ref, raw, ops, core, kpi, api TO waystone_read;
