-- Run as: postgres, connected to the waystone database. Needs psql 15+.
-- Environment: WAYSTONE_DB, WAYSTONE_LOAD_PW, WAYSTONE_READ_PW
\set ON_ERROR_STOP on
\getenv dbname WAYSTONE_DB
\getenv load_pw WAYSTONE_LOAD_PW
\getenv read_pw WAYSTONE_READ_PW

SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waystone_load') AS need_load \gset
\if :need_load
CREATE ROLE waystone_load LOGIN PASSWORD :'load_pw';
\else
ALTER ROLE waystone_load LOGIN PASSWORD :'load_pw';
\endif

SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waystone_read') AS need_read \gset
\if :need_read
CREATE ROLE waystone_read LOGIN PASSWORD :'read_pw';
\else
ALTER ROLE waystone_read LOGIN PASSWORD :'read_pw';
\endif

-- waystone_load owns every schema and table; waystone_read can only SELECT.
GRANT CONNECT, TEMPORARY, CREATE ON DATABASE :"dbname" TO waystone_load;
GRANT CONNECT ON DATABASE :"dbname" TO waystone_read;

ALTER ROLE waystone_read SET default_transaction_read_only = on;
ALTER ROLE waystone_read SET statement_timeout = '15s';
ALTER ROLE waystone_read SET search_path = api, kpi, core, ref, ops;
ALTER ROLE waystone_load SET search_path = core, ref, raw, ops, kpi, api;

ALTER DATABASE :"dbname" SET timezone TO 'America/New_York';
