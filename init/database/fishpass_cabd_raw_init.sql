-- Manual, one-time (or occasional) setup script for the FishPass cabd_raw schema.
-- Run as a SUPERUSER (or a role that is a member of the fishpass role), e.g.:
--   psql \
--   -v fishpass='fishpass' \
--   -v cabd_host=...' -v cabd_port='5432' -v cabd_dbname='cabd' \
--   -v cabd_user='...' -v cabd_password='...' \
--   -f init/database/fishpass_cabd_raw_init.sql \
--   "host=... port=5432 dbname=... user=<superuser>"
--
-- Not run by any GitHub Action. Safe to re-run (all statements are idempotent).

-- ============================================================================
-- SUPERUSER-ONLY STEPS (extensions + privileges the fishpass user can't grant itself)
-- ============================================================================
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgres_fdw;

GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw TO :"fishpass";

-- ============================================================================
-- Everything below is created AS the fishpass user, so it is owned by fishpass.
-- ============================================================================
SET ROLE :"fishpass";

-- 1. Foreign server to CHyF2 / CABD
DROP SERVER IF EXISTS cabd_fdw_server CASCADE;
CREATE SERVER cabd_fdw_server
    FOREIGN DATA WRAPPER postgres_fdw
    OPTIONS (host :'cabd_host', port :'cabd_port', dbname :'cabd_dbname');
ALTER SERVER cabd_fdw_server OWNER TO :"fishpass";

-- 2. User mapping for the fishpass user
CREATE USER MAPPING FOR CURRENT_USER
    SERVER cabd_fdw_server
    OPTIONS (user :'cabd_user', password :'cabd_password');

-- 3. Local schema for the imported foreign tables
CREATE SCHEMA IF NOT EXISTS cabd_fdw;
ALTER SCHEMA cabd_fdw OWNER TO :"fishpass";

-- 4. Import the three cabd views as foreign tables
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.foreign_tables
        WHERE foreign_table_schema = 'cabd_fdw' AND foreign_table_name = 'dams_view_en'
    ) THEN
        EXECUTE 'IMPORT FOREIGN SCHEMA cabd '
             || 'LIMIT TO (dams_view_en, stream_crossings_sites_structures_view_en, waterfalls_view_en) '
             || 'FROM SERVER cabd_fdw_server INTO cabd_fdw';
    END IF;
END
$$;

RESET ROLE;