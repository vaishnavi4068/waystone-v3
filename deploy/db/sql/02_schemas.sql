-- Run as: waystone_load
\set ON_ERROR_STOP on

CREATE SCHEMA IF NOT EXISTS ref;
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS kpi;
CREATE SCHEMA IF NOT EXISTS api;

COMMENT ON SCHEMA ref  IS 'Reference data: strategies, settings, instruments, holidays, KPI definitions, backtest reference figures';
COMMENT ON SCHEMA raw  IS 'Raw layer: every file copied from GCS, stored line by line exactly as written';
COMMENT ON SCHEMA ops  IS 'Loader bookkeeping: load runs, per-file outcomes, per-day status';
COMMENT ON SCHEMA core IS 'Cleansed layer: paper trades, fills, backtest trades, comparison, daily P&L';
COMMENT ON SCHEMA kpi  IS 'Precomputed KPI tables read by the dashboard and MCP';
COMMENT ON SCHEMA api  IS 'Stable read-only views for the dashboard API and the data MCP';

GRANT USAGE ON SCHEMA ref, raw, ops, core, kpi, api TO waystone_read;
ALTER DEFAULT PRIVILEGES IN SCHEMA ref, raw, ops, core, kpi, api
    GRANT SELECT ON TABLES TO waystone_read;
