-- Run as: waystone_load
\set ON_ERROR_STOP on

-- RAW LAYER ---------------------------------------------------------------
-- One row per distinct version of a GCS object. A changed file (the day's log
-- grows hourly) gets a new row; is_current marks the version the clean layer uses.
CREATE TABLE IF NOT EXISTS raw.source_file (
    file_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    strategy_id    smallint REFERENCES ref.strategy,
    source_kind    text NOT NULL CHECK (source_kind IN
                       ('paper_log', 'paper_info', 'paper_events', 'backtest_daily', 'comparison', 'other')),
    gcs_uri        text NOT NULL,
    gcs_generation bigint NOT NULL,
    sha256         char(64) NOT NULL,
    size_bytes     bigint NOT NULL,
    gcs_updated_at timestamptz,
    session_date   date,
    is_current     boolean NOT NULL DEFAULT true,
    parse_status   text NOT NULL DEFAULT 'PENDING' CHECK (parse_status IN
                       ('PENDING', 'PARSED', 'PARTIAL', 'FAILED', 'SKIPPED')),
    parse_message  text,
    loaded_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (gcs_uri, sha256)
);
CREATE UNIQUE INDEX IF NOT EXISTS source_file_current_uq ON raw.source_file (gcs_uri) WHERE is_current;
CREATE INDEX IF NOT EXISTS source_file_strategy_day_ix ON raw.source_file (strategy_id, session_date);

CREATE TABLE IF NOT EXISTS raw.source_line (
    file_id bigint NOT NULL REFERENCES raw.source_file ON DELETE CASCADE,
    line_no integer NOT NULL,
    line_ts timestamptz,
    content text NOT NULL,
    PRIMARY KEY (file_id, line_no)
);

-- events_*.jsonl files: one row per JSON line.
CREATE TABLE IF NOT EXISTS raw.source_event (
    file_id    bigint NOT NULL REFERENCES raw.source_file ON DELETE CASCADE,
    line_no    integer NOT NULL,
    event_ts   timestamptz,
    event_type text,
    payload    jsonb NOT NULL,
    PRIMARY KEY (file_id, line_no)
);
CREATE INDEX IF NOT EXISTS source_event_type_ix ON raw.source_event (event_type, event_ts);

-- OPS (loader bookkeeping) -------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.load_run (
    run_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job          text NOT NULL CHECK (job IN ('paper', 'backtest', 'backfill', 'recompute')),
    triggered_by text,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text NOT NULL DEFAULT 'RUNNING' CHECK (status IN ('RUNNING', 'OK', 'NOOP', 'PARTIAL', 'FAILED')),
    files_seen   integer NOT NULL DEFAULT 0,
    files_loaded integer NOT NULL DEFAULT 0,
    error        text,
    app_version  text
);
CREATE INDEX IF NOT EXISTS load_run_job_started_ix ON ops.load_run (job, started_at DESC);

CREATE TABLE IF NOT EXISTS ops.load_file (
    run_id  bigint NOT NULL REFERENCES ops.load_run ON DELETE CASCADE,
    file_id bigint NOT NULL REFERENCES raw.source_file ON DELETE CASCADE,
    action  text NOT NULL CHECK (action IN ('INSERTED', 'REPLACED', 'UNCHANGED', 'FAILED')),
    message text,
    PRIMARY KEY (run_id, file_id)
);

CREATE TABLE IF NOT EXISTS ops.day_status (
    strategy_id        smallint NOT NULL REFERENCES ref.strategy,
    session_date       date NOT NULL,
    paper_status       text NOT NULL DEFAULT 'NONE' CHECK (paper_status IN
                           ('NONE', 'INTRADAY', 'PRELIMINARY', 'FINAL', 'PARTIAL')),
    backtest_status    text NOT NULL DEFAULT 'PENDING' CHECK (backtest_status IN
                           ('PENDING', 'LOADED', 'DATA_INCOMPLETE', 'MISSING', 'NOT_APPLICABLE')),
    sync_status        text NOT NULL DEFAULT 'PENDING' CHECK (sync_status IN ('PENDING', 'OK', 'FLAG', 'N/A')),
    paper_loaded_at    timestamptz,
    backtest_loaded_at timestamptz,
    finalized_at       timestamptz,
    checks             jsonb,
    PRIMARY KEY (strategy_id, session_date)
);
