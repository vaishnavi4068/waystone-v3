-- Run as: waystone_load
\set ON_ERROR_STOP on

-- session_date everywhere follows the workbook rule: a trade belongs to the
-- session of its exit time, and exits at/after session_roll_hour (18:00 ET)
-- count toward the next trading day (holidays skipped).

-- PAPER --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.paper_session (
    strategy_id        smallint NOT NULL REFERENCES ref.strategy,
    session_date       date NOT NULL,
    file_id            bigint REFERENCES raw.source_file ON DELETE SET NULL,
    params_fp          text,
    broker_account     text,
    broker_client_id   integer,
    instrument_id      integer REFERENCES ref.instrument,
    first_line_at      timestamptz,
    last_line_at       timestamptz,
    summary_present    boolean NOT NULL DEFAULT false,
    triggers_reported  integer,
    entries_reported   integer,
    wins_reported      integer,
    win_rate_reported  numeric(6, 4),
    net_pnl_reported   numeric(14, 2),
    nlv_start          numeric(16, 2),
    nlv_end            numeric(16, 2),
    gate_blocks        integer,
    loss_cap_blocks    integer,
    loss_cap_hit       boolean,
    PRIMARY KEY (strategy_id, session_date)
);

CREATE TABLE IF NOT EXISTS core.account_snapshot (
    snapshot_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    strategy_id      smallint NOT NULL REFERENCES ref.strategy,
    session_date     date NOT NULL,
    snapshot_ts      timestamptz NOT NULL,
    broker_account   text NOT NULL DEFAULT '',
    nlv              numeric(16, 2),
    cash             numeric(16, 2),
    excess_liquidity numeric(16, 2),
    init_margin      numeric(16, 2),
    maint_margin     numeric(16, 2),
    file_id          bigint REFERENCES raw.source_file ON DELETE CASCADE,
    line_no          integer,
    UNIQUE (strategy_id, snapshot_ts, broker_account)
);

CREATE TABLE IF NOT EXISTS core.signal_event (
    signal_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    strategy_id   smallint NOT NULL REFERENCES ref.strategy,
    session_date  date NOT NULL,
    signal_bar_ts timestamptz NOT NULL,
    side          text NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    signal_px     numeric(14, 4),
    outcome       text NOT NULL CHECK (outcome IN ('ENTERED', 'BLOCKED', 'SKIPPED')),
    block_reason  text,
    file_id       bigint REFERENCES raw.source_file ON DELETE CASCADE,
    line_no       integer,
    UNIQUE (strategy_id, signal_bar_ts, side)
);
CREATE INDEX IF NOT EXISTS signal_event_day_ix ON core.signal_event (strategy_id, session_date);

-- The paper log truncates IB exec ids, so fills are keyed on their content.
CREATE TABLE IF NOT EXISTS core.paper_fill (
    fill_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    strategy_id   smallint NOT NULL REFERENCES ref.strategy,
    session_date  date NOT NULL,
    fill_ts       timestamptz NOT NULL,
    instrument_id integer REFERENCES ref.instrument,
    exec_id       text,
    order_ref     text,
    action        text NOT NULL CHECK (action IN ('BUY', 'SELL')),
    quantity      numeric(12, 2) NOT NULL,
    price         numeric(14, 4) NOT NULL,
    commission    numeric(10, 2),
    leg_role      text CHECK (leg_role IN ('ENTRY', 'EXIT', 'STOP', 'FLATTEN', 'ADJUST')),
    file_id       bigint REFERENCES raw.source_file ON DELETE CASCADE,
    line_no       integer,
    UNIQUE (strategy_id, fill_ts, action, price, quantity)
);
CREATE INDEX IF NOT EXISTS paper_fill_day_ix ON core.paper_fill (strategy_id, session_date);

-- One row per round turn: the workbook's "4. Paper Trade Log".
CREATE TABLE IF NOT EXISTS core.paper_trade (
    trade_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    strategy_id     smallint NOT NULL REFERENCES ref.strategy,
    session_date    date NOT NULL,
    trade_no        integer,
    instrument_id   integer REFERENCES ref.instrument,
    direction       text NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    contracts       numeric(12, 2) NOT NULL,
    signal_bar_ts   timestamptz,
    entry_ts        timestamptz NOT NULL,
    exit_ts         timestamptz,
    entry_px        numeric(14, 4) NOT NULL,
    exit_px         numeric(14, 4),
    entry_signal_px numeric(14, 4),
    exit_signal_px  numeric(14, 4),
    entry_slip_pts  numeric(10, 4),
    exit_slip_pts   numeric(10, 4),
    fill_latency_s  numeric(8, 3),
    exit_reason     text,
    points          numeric(12, 4),
    gross_pnl       numeric(14, 2),
    commission      numeric(10, 2),
    slippage_cost   numeric(14, 2),
    pnl_at_signal   numeric(14, 2),
    net_pnl         numeric(14, 2),
    hold_min        numeric(10, 2),
    mae_pts         numeric(10, 4),
    mfe_pts         numeric(10, 4),
    params_fp       text,
    is_closed       boolean GENERATED ALWAYS AS (exit_ts IS NOT NULL) STORED,
    file_id         bigint REFERENCES raw.source_file ON DELETE SET NULL,
    line_no         integer,
    UNIQUE (strategy_id, entry_ts, direction)
);
CREATE INDEX IF NOT EXISTS paper_trade_day_ix ON core.paper_trade (strategy_id, session_date);

-- Option legs (and greeks when logged). Futures trades have no leg rows.
CREATE TABLE IF NOT EXISTS core.paper_trade_leg (
    trade_id      bigint NOT NULL REFERENCES core.paper_trade ON DELETE CASCADE,
    leg_no        smallint NOT NULL,
    instrument_id integer NOT NULL REFERENCES ref.instrument,
    action        text NOT NULL CHECK (action IN ('BUY', 'SELL')),
    quantity      numeric(12, 2) NOT NULL,
    entry_px      numeric(14, 4),
    exit_px       numeric(14, 4),
    entry_delta   numeric(12, 6),
    entry_gamma   numeric(12, 6),
    entry_theta   numeric(12, 6),
    entry_vega    numeric(12, 6),
    entry_iv      numeric(12, 6),
    PRIMARY KEY (trade_id, leg_no)
);

CREATE TABLE IF NOT EXISTS core.ops_event (
    event_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    strategy_id  smallint NOT NULL REFERENCES ref.strategy,
    session_date date NOT NULL,
    event_ts     timestamptz NOT NULL,
    category     text NOT NULL CHECK (category IN ('BROKER', 'CONNECTIVITY', 'RISK', 'SIGNAL', 'SYSTEM')),
    code         text,
    severity     text NOT NULL DEFAULT 'INFO' CHECK (severity IN ('INFO', 'WARN', 'ERROR')),
    message      text NOT NULL,
    file_id      bigint REFERENCES raw.source_file ON DELETE CASCADE,
    line_no      integer,
    UNIQUE (file_id, line_no)
);
CREATE INDEX IF NOT EXISTS ops_event_day_ix ON core.ops_event (strategy_id, session_date);

-- BACKTEST REPLAY ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.backtest_run (
    run_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    strategy_id        smallint NOT NULL REFERENCES ref.strategy,
    session_date       date NOT NULL,
    file_id            bigint REFERENCES raw.source_file ON DELETE SET NULL,
    config_label       text,
    params_fp          text,
    bar_file           text,
    bar_count          integer,
    vol_index          text,
    sentiment_source   text,
    point_value        numeric(12, 4),
    flatten_time       time,
    daily_loss_cap     numeric(12, 2),
    trades_reported    integer,
    total_net_reported numeric(14, 2),
    maxdd_reported     numeric(8, 4),
    status             text NOT NULL CHECK (status IN ('COMPLETE', 'INCOMPLETE', 'DATA_INCOMPLETE', 'FAILED')),
    loaded_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (strategy_id, session_date)
);

CREATE TABLE IF NOT EXISTS core.backtest_trade (
    bt_trade_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id             bigint NOT NULL REFERENCES core.backtest_run ON DELETE CASCADE,
    strategy_id        smallint NOT NULL REFERENCES ref.strategy,
    session_date       date NOT NULL,
    trade_seq          integer NOT NULL,
    direction          text NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    entry_ts           timestamptz NOT NULL,
    exit_ts            timestamptz,
    entry_px           numeric(14, 4),
    exit_px            numeric(14, 4),
    points             numeric(12, 4),
    contracts          numeric(12, 2),
    contracts_inferred boolean NOT NULL DEFAULT false,
    gross_pnl          numeric(14, 2),
    commission         numeric(10, 2),
    net_pnl            numeric(14, 2),
    net_pnl_derived    boolean NOT NULL DEFAULT false,
    exit_reason        text,
    hold_min           numeric(10, 2),
    UNIQUE (run_id, trade_seq)
);
CREATE INDEX IF NOT EXISTS backtest_trade_day_ix ON core.backtest_trade (strategy_id, session_date);

-- COMPARISON (computed by the loader; replaces the VM comparison file) -------
CREATE TABLE IF NOT EXISTS core.comparison_trade (
    strategy_id       smallint NOT NULL REFERENCES ref.strategy,
    session_date      date NOT NULL,
    match_seq         integer NOT NULL,
    match_type        text NOT NULL CHECK (match_type IN ('MATCHED', 'PAPER_ONLY', 'BACKTEST_ONLY')),
    paper_trade_id    bigint REFERENCES core.paper_trade ON DELETE CASCADE,
    bt_trade_id       bigint REFERENCES core.backtest_trade ON DELETE CASCADE,
    unmatched_reason  text,
    entry_gap_s       numeric(10, 2),
    exit_gap_min      numeric(10, 2),
    points_gap        numeric(12, 4),
    pnl_delta         numeric(14, 2),
    pnl_delta_pct     numeric(10, 4),
    exit_reason_match boolean,
    computed_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, session_date, match_seq)
);

-- One row per strategy per day: the workbook's "3. Daily Sync Log".
CREATE TABLE IF NOT EXISTS core.daily_sync (
    strategy_id       smallint NOT NULL REFERENCES ref.strategy,
    session_date      date NOT NULL,
    instrument_symbol text,
    live_trades       integer,
    live_win_rate     numeric(6, 4),
    live_points       numeric(12, 4),
    live_gross_pnl    numeric(14, 2),
    live_commission   numeric(10, 2),
    live_net_pnl      numeric(14, 2),
    live_entry_ts     timestamptz,
    live_exit_ts      timestamptz,
    live_exit_reason  text,
    bt_trades         integer,
    bt_points         numeric(12, 4),
    bt_net_pnl        numeric(14, 2),
    bt_entry_ts       timestamptz,
    bt_exit_ts        timestamptz,
    bt_exit_reason    text,
    pnl_delta         numeric(14, 2),
    pnl_delta_pct     numeric(10, 4),
    exit_reason_match char(1) CHECK (exit_reason_match IN ('Y', 'N')),
    exit_time_gap_min numeric(10, 2),
    loss_cap_hit      char(1) CHECK (loss_cap_hit IN ('Y', 'N')),
    sync_status       text NOT NULL DEFAULT 'PENDING' CHECK (sync_status IN ('PENDING', 'OK', 'FLAG', 'N/A')),
    notes_auto        text,
    computed_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, session_date)
);

-- Human notes live apart from daily_sync so a recompute never overwrites them.
CREATE TABLE IF NOT EXISTS core.daily_sync_note (
    strategy_id  smallint NOT NULL REFERENCES ref.strategy,
    session_date date NOT NULL,
    note         text NOT NULL,
    updated_by   text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, session_date)
);

-- One row per trading day from paper start: the workbook's "9. Daily Calc".
CREATE TABLE IF NOT EXISTS core.daily_pnl (
    strategy_id    smallint NOT NULL REFERENCES ref.strategy,
    session_date   date NOT NULL,
    week_end       date NOT NULL,
    month_start    date NOT NULL,
    trades         integer NOT NULL DEFAULT 0,
    gross_pnl      numeric(14, 2) NOT NULL DEFAULT 0,
    commission     numeric(14, 2) NOT NULL DEFAULT 0,
    slippage_cost  numeric(14, 2) NOT NULL DEFAULT 0,
    net_pnl        numeric(14, 2) NOT NULL DEFAULT 0,
    hold_min_total numeric(12, 2) NOT NULL DEFAULT 0,
    equity_start   numeric(16, 2) NOT NULL,
    equity_end     numeric(16, 2) NOT NULL,
    daily_return   numeric(14, 10) NOT NULL,
    peak_itd       numeric(16, 2) NOT NULL,
    dd_itd         numeric(14, 10) NOT NULL,
    uw_days_itd    integer NOT NULL,
    peak_week      numeric(16, 2) NOT NULL,
    dd_week        numeric(14, 10) NOT NULL,
    uw_days_week   integer NOT NULL,
    peak_month     numeric(16, 2) NOT NULL,
    dd_month       numeric(14, 10) NOT NULL,
    uw_days_month  integer NOT NULL,
    computed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, session_date)
);
