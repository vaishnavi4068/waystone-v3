-- Run as: waystone_load
\set ON_ERROR_STOP on

-- Every KPI value, kept per as-of date so the dashboard date picker can show
-- any past day exactly as it looked then.
CREATE TABLE IF NOT EXISTS kpi.kpi_value (
    strategy_id  smallint NOT NULL REFERENCES ref.strategy,
    as_of_date   date NOT NULL,
    kpi_window   text NOT NULL CHECK (kpi_window IN ('DAY', 'WEEK', 'MTD', 'ITD')),
    kpi_code     text NOT NULL REFERENCES ref.kpi_definition,
    num_value    numeric,
    text_value   text,
    status       text CHECK (status IN ('GREEN', 'AMBER', 'RED', 'NA', 'INFO')),
    window_start date,
    window_end   date,
    computed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, as_of_date, kpi_window, kpi_code)
);

-- Scorecard header rows and the overall gate, per window.
CREATE TABLE IF NOT EXISTS kpi.scorecard (
    strategy_id  smallint NOT NULL REFERENCES ref.strategy,
    as_of_date   date NOT NULL,
    kpi_window   text NOT NULL CHECK (kpi_window IN ('WEEK', 'MTD', 'ITD')),
    window_start date,
    window_end   date,
    trading_days integer,
    trades       integer,
    net_pnl      numeric(14, 2),
    return_pct   numeric(14, 10),
    equity_end   numeric(16, 2),
    red_count    integer,
    amber_count  integer,
    green_count  integer,
    overall_gate text,
    computed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, as_of_date, kpi_window)
);

-- Workbook tabs 5 and 7 (by week).
CREATE TABLE IF NOT EXISTS kpi.returns_weekly (
    strategy_id   smallint NOT NULL REFERENCES ref.strategy,
    week_end      date NOT NULL,
    week_no       integer NOT NULL,
    week_start    date NOT NULL,
    trading_days  integer NOT NULL,
    trades        integer NOT NULL,
    net_pnl       numeric(14, 2) NOT NULL,
    start_equity  numeric(16, 2) NOT NULL,
    return_pct    numeric(14, 10),
    max_dd        numeric(14, 10),
    win_rate      numeric(6, 4),
    cum_pnl       numeric(14, 2) NOT NULL,
    account_value numeric(16, 2) NOT NULL,
    cum_return    numeric(14, 10) NOT NULL,
    peak_account  numeric(16, 2) NOT NULL,
    dd_from_peak  numeric(14, 10) NOT NULL,
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, week_end)
);

-- Workbook tabs 6 and 7 (by month).
CREATE TABLE IF NOT EXISTS kpi.returns_monthly (
    strategy_id   smallint NOT NULL REFERENCES ref.strategy,
    month_start   date NOT NULL,
    month_no      integer NOT NULL,
    trading_days  integer NOT NULL,
    trades        integer NOT NULL,
    net_pnl       numeric(14, 2) NOT NULL,
    start_equity  numeric(16, 2) NOT NULL,
    return_pct    numeric(14, 10),
    max_dd        numeric(14, 10),
    sharpe        numeric(12, 6),
    win_rate      numeric(6, 4),
    profit_factor numeric(12, 6),
    cum_pnl       numeric(14, 2) NOT NULL,
    account_value numeric(16, 2) NOT NULL,
    cum_return    numeric(14, 10) NOT NULL,
    dd_from_peak  numeric(14, 10) NOT NULL,
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, month_start)
);

-- Daily Sync Log "ROLLING KPIs" block.
CREATE TABLE IF NOT EXISTS kpi.sync_rolling (
    strategy_id       smallint NOT NULL REFERENCES ref.strategy,
    as_of_date        date NOT NULL,
    days_logged       integer NOT NULL,
    all_time_trades   integer NOT NULL,
    all_time_live_pnl numeric(14, 2) NOT NULL,
    all_time_bt_pnl   numeric(14, 2),
    avg_pnl_delta_pct numeric(10, 4),
    all_time_flags    integer NOT NULL,
    flag_rate         numeric(6, 4),
    last7_live_pnl    numeric(14, 2) NOT NULL,
    last7_flags       integer NOT NULL,
    computed_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, as_of_date)
);
