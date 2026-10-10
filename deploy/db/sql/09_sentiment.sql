-- Run as: waystone_load
-- Futures sentiment layer (WAYSTONE_SENTIMENT_THESIS): every score is its own row.
--   sentiment.headline        one row per headline, with its FinBERT / lexicon score
--   sentiment.score           one row per score per session and interval: every F&G
--                             component, positioning, flow, regime and narrative reading
--   sentiment.snapshot        the per-interval (and whole-day) summary built from those rows
--   sentiment.recommendation  one row per strategy per interval: gates kept as separate columns
-- Text never supplies direction on an index future; it can only halt (thesis §5, §10).
\set ON_ERROR_STOP on

CREATE SCHEMA IF NOT EXISTS sentiment;
COMMENT ON SCHEMA sentiment IS 'Futures sentiment: headlines, every layer score, interval snapshots, strategy recommendations';
GRANT USAGE ON SCHEMA sentiment TO waystone_read;
ALTER DEFAULT PRIVILEGES IN SCHEMA sentiment GRANT SELECT ON TABLES TO waystone_read;

CREATE TABLE IF NOT EXISTS sentiment.run (
    run_id       bigserial PRIMARY KEY,
    job          text NOT NULL CHECK (job IN ('intraday', 'daily', 'backfill')),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text NOT NULL DEFAULT 'RUNNING'
                 CHECK (status IN ('RUNNING', 'OK', 'PARTIAL', 'FAILED')),
    scorer          text,
    policy_version  text,
    config          jsonb NOT NULL DEFAULT '{}'::jsonb,
    sessions        date[],
    sources      jsonb NOT NULL DEFAULT '{}'::jsonb,
    error        text
);

CREATE TABLE IF NOT EXISTS sentiment.source_health (
    source         text PRIMARY KEY,
    last_ok_at     timestamptz,
    last_error_at  timestamptz,
    last_error     text,
    last_rows      integer
);

CREATE TABLE IF NOT EXISTS sentiment.macro_event (
    event_id      bigserial PRIMARY KEY,
    kind          text NOT NULL CHECK (kind IN ('CPI', 'NFP', 'PCE', 'FOMC')),
    title         text NOT NULL,
    event_ts      timestamptz NOT NULL,
    session_date  date NOT NULL,
    source        text NOT NULL,
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, event_ts)
);
CREATE INDEX IF NOT EXISTS macro_event_day ON sentiment.macro_event (session_date);

CREATE TABLE IF NOT EXISTS sentiment.headline (
    headline_id   bigserial PRIMARY KEY,
    url_hash      text NOT NULL UNIQUE,
    url           text NOT NULL,
    title         text NOT NULL,
    publisher     text,
    feed          text NOT NULL,
    tier          numeric(4,2) NOT NULL,
    published_at  timestamptz NOT NULL,
    session_date  date NOT NULL,
    scorer        text NOT NULL,
    prob_pos      numeric(6,4),
    prob_neg      numeric(6,4),
    prob_neu      numeric(6,4),
    score         numeric(6,4) NOT NULL,
    novelty       numeric(6,4) NOT NULL,
    kill_terms    text[] NOT NULL DEFAULT '{}',
    macro_tags    text[] NOT NULL DEFAULT '{}',
    is_speculative boolean NOT NULL DEFAULT false,
    kill_eligible  boolean NOT NULL DEFAULT false,
    fetched_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS headline_day ON sentiment.headline (session_date, published_at);
CREATE INDEX IF NOT EXISTS headline_ts ON sentiment.headline (published_at);

-- One row per score. slot_label is 'HH:MM' (ET) for intraday intervals, 'DAY' for the session.
-- layer: narrative | positioning | flow | regime | fng | drift | efficacy (gate effect on P&L)
CREATE TABLE IF NOT EXISTS sentiment.score (
    session_date  date NOT NULL,
    slot_label    text NOT NULL,
    layer         text NOT NULL,
    component     text NOT NULL,
    value         numeric(18,6),
    score         numeric(10,4),
    state         text,
    source        text NOT NULL,
    detail        jsonb NOT NULL DEFAULT '{}'::jsonb,
    run_id        bigint REFERENCES sentiment.run (run_id),
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_date, slot_label, layer, component)
);
CREATE INDEX IF NOT EXISTS score_component ON sentiment.score (layer, component, session_date);

CREATE TABLE IF NOT EXISTS sentiment.snapshot (
    session_date          date NOT NULL,
    slot_label            text NOT NULL,
    slot_ts               timestamptz NOT NULL,
    is_final              boolean NOT NULL DEFAULT false,
    fng_cnn               numeric(6,2),
    fng_replica           numeric(6,2),
    fng_prior_day         numeric(6,2),
    vix                   numeric(10,4),
    vix_term_ratio        numeric(8,4),
    vol_spike             boolean,
    narrative_score       numeric(6,4),
    narrative_dispersion  numeric(6,4),
    narrative_n           integer NOT NULL DEFAULT 0,
    kill_hits             integer NOT NULL DEFAULT 0,
    events                jsonb NOT NULL DEFAULT '[]'::jsonb,
    regime                jsonb NOT NULL DEFAULT '{}'::jsonb,
    data_gaps             text[] NOT NULL DEFAULT '{}',
    best_strategy         text,
    policy_version        text NOT NULL,
    inputs_hash           text NOT NULL,
    headline              text NOT NULL,
    summary               text NOT NULL,
    run_id                bigint REFERENCES sentiment.run (run_id),
    computed_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_date, slot_label)
);

CREATE TABLE IF NOT EXISTS sentiment.recommendation (
    session_date   date NOT NULL,
    slot_label     text NOT NULL,
    strategy_id    integer NOT NULL REFERENCES ref.strategy (strategy_id),
    verdict        text NOT NULL CHECK (verdict IN ('TRADE', 'REDUCE', 'STAND_DOWN')),
    size_mult      numeric(4,2) NOT NULL,
    rank           integer NOT NULL,
    fit_score      numeric(12,2),
    fit_n          integer NOT NULL DEFAULT 0,
    fit_regime     text,
    gate_data      text NOT NULL,
    gate_event     text NOT NULL,
    gate_kill      text NOT NULL,
    gate_engine    text NOT NULL,
    gate_vol       text NOT NULL,
    positioning    text NOT NULL,
    reasons        text[] NOT NULL DEFAULT '{}',
    policy_version text NOT NULL,
    inputs_hash    text NOT NULL,
    run_id         bigint REFERENCES sentiment.run (run_id),
    computed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_date, slot_label, strategy_id)
);

-- Audit trail: one row per gate per strategy per interval, exactly what the policy saw.
CREATE TABLE IF NOT EXISTS sentiment.gate_decision (
    session_date   date NOT NULL,
    slot_label     text NOT NULL,
    strategy_id    integer NOT NULL REFERENCES ref.strategy (strategy_id),
    gate           text NOT NULL CHECK (gate IN ('data', 'event', 'kill', 'engine', 'vol', 'positioning')),
    state          text NOT NULL CHECK (state IN ('OPEN', 'CAUTION', 'HALT', 'BLOCKED', 'UNKNOWN')),
    reason         text NOT NULL DEFAULT '',
    size_mult      numeric(4,2) NOT NULL,
    confidence     numeric(4,2) NOT NULL,
    evidence       text[] NOT NULL DEFAULT '{}',
    inputs_as_of   timestamptz,
    expires_at     timestamptz,
    override       text,
    policy_version text NOT NULL,
    inputs_hash    text NOT NULL,
    run_id         bigint REFERENCES sentiment.run (run_id),
    computed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_date, slot_label, strategy_id, gate)
);
CREATE INDEX IF NOT EXISTS gate_decision_state ON sentiment.gate_decision (gate, state, session_date);

-- Operator overrides (logged, time-boxed). FORCE_OPEN never applies to the data gate.
CREATE TABLE IF NOT EXISTS sentiment.gate_override (
    override_id    bigserial PRIMARY KEY,
    strategy_id    integer REFERENCES ref.strategy (strategy_id),
    gate           text NOT NULL CHECK (gate IN ('data', 'event', 'kill', 'engine', 'vol', 'positioning')),
    action         text NOT NULL CHECK (action IN ('FORCE_HALT', 'FORCE_OPEN')),
    reason         text NOT NULL,
    created_by     text NOT NULL,
    valid_from     timestamptz NOT NULL DEFAULT now(),
    valid_to       timestamptz,
    revoked_at     timestamptz,
    CHECK (NOT (gate = 'data' AND action = 'FORCE_OPEN'))
);

GRANT SELECT ON ALL TABLES IN SCHEMA sentiment TO waystone_read;

CREATE OR REPLACE VIEW api.v_sentiment_snapshot AS
SELECT * FROM sentiment.snapshot;

CREATE OR REPLACE VIEW api.v_sentiment_score AS
SELECT * FROM sentiment.score;

CREATE OR REPLACE VIEW api.v_sentiment_headline AS
SELECT headline_id, title, url, publisher, feed, tier, published_at, session_date, scorer,
       prob_pos, prob_neg, prob_neu, score, novelty, kill_terms, macro_tags, is_speculative,
       kill_eligible
FROM sentiment.headline;

CREATE OR REPLACE VIEW api.v_sentiment_recommendation AS
SELECT r.session_date, r.slot_label, s.strategy_code, s.display_name, s.instrument_root,
       r.verdict, r.size_mult, r.rank, r.fit_score, r.fit_n, r.fit_regime,
       r.gate_data, r.gate_event, r.gate_kill, r.gate_engine, r.gate_vol, r.positioning,
       r.reasons, r.policy_version, r.inputs_hash, r.computed_at
FROM sentiment.recommendation r
JOIN ref.strategy s USING (strategy_id);

CREATE OR REPLACE VIEW api.v_sentiment_gate AS
SELECT g.session_date, g.slot_label, s.strategy_code, g.gate, g.state, g.reason, g.size_mult,
       g.confidence, g.evidence, g.inputs_as_of, g.expires_at, g.override, g.policy_version,
       g.inputs_hash, g.computed_at
FROM sentiment.gate_decision g
JOIN ref.strategy s USING (strategy_id);

CREATE OR REPLACE VIEW api.v_sentiment_override AS
SELECT o.override_id, s.strategy_code, o.gate, o.action, o.reason, o.created_by, o.valid_from,
       o.valid_to, o.revoked_at
FROM sentiment.gate_override o
LEFT JOIN ref.strategy s USING (strategy_id);

CREATE OR REPLACE VIEW api.v_macro_event AS
SELECT kind, title, event_ts, session_date, source FROM sentiment.macro_event;

CREATE OR REPLACE VIEW api.v_sentiment_health AS
SELECT h.*, (SELECT max(finished_at) FROM sentiment.run r WHERE r.status IN ('OK', 'PARTIAL'))
       AS last_run_at
FROM sentiment.source_health h;

GRANT SELECT ON ALL TABLES IN SCHEMA api TO waystone_read;
