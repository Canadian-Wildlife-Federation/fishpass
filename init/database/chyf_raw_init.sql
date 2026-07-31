-- Manual, one-time (or occasional) setup script for the FishPass chyf_raw schema.
-- Run by hand against the target FishPass database, e.g.:
--   psql "host=... dbname=... user=..." \
--     -v chyf2_host='...' -v chyf2_port='5432' -v chyf2_dbname='chyf2' \
--     -v chyf2_user='...' -v chyf2_password='...' \
--     -f init/database/chyf_raw_init.sql
--
-- Not run by any GitHub Action. Safe to re-run (all statements are idempotent).

CREATE EXTENSION IF NOT EXISTS postgis;

-- 1. Schema
CREATE SCHEMA IF NOT EXISTS chyf_raw;

-- 2. Foreign data wrapper to CHyF2
CREATE EXTENSION IF NOT EXISTS postgres_fdw;

DO $$
BEGIN
	IF NOT EXISTS (SELECT 1 FROM pg_foreign_server WHERE srvname = 'chyf2_fdw_server') THEN
		CREATE SERVER chyf2_fdw_server
			FOREIGN DATA WRAPPER postgres_fdw
			OPTIONS (host :'chyf2_host', port :'chyf2_port', dbname :'chyf2_dbname');
	END IF;
END
$$;

DO $$
BEGIN
	IF NOT EXISTS (
		SELECT 1
		FROM pg_user_mappings
		WHERE srvname = 'chyf2_fdw_server' AND usename = CURRENT_USER
	) THEN
		EXECUTE format(
			'CREATE USER MAPPING FOR CURRENT_USER SERVER chyf2_fdw_server OPTIONS (user %L, password %L)',
			:'chyf2_user', :'chyf2_password'
		);
	END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS chyf2_fdw;

DO $$
BEGIN
	IF NOT EXISTS (
		SELECT 1 FROM information_schema.foreign_tables
		WHERE foreign_table_schema = 'chyf2_fdw' AND foreign_table_name = 'eflowpath'
	) THEN
		EXECUTE 'IMPORT FOREIGN SCHEMA chyf2 LIMIT TO (eflowpath, eflowpath_properties, aoi, shoreline) FROM SERVER chyf2_fdw_server INTO chyf2_fdw';
	END IF;
END
$$;

-- 3. Target tables

CREATE TABLE IF NOT EXISTS chyf_raw.aoi (
	id uuid PRIMARY KEY,
	short_name varchar NOT NULL,
	full_name varchar,
	geometry public.geometry(polygon, 4617),
	display_status int2
);

CREATE TABLE IF NOT EXISTS chyf_raw.flowpath (
	id uuid PRIMARY KEY,
	nid varchar(32),
	ef_type int2 NOT NULL,
	ef_subtype int2,
	"rank" int2 NOT NULL,
	length float8 NOT NULL,
	length_km float8,
	rivernameid1 uuid,
	rivernameid2 uuid,
	aoi_id uuid REFERENCES chyf_raw.aoi(id),
	from_nexus_id uuid,
	to_nexus_id uuid,
	ecatchment_id uuid,
	geometry public.geometry NOT NULL,
	strahler_order int4,
	graph_id int4,
	mainstem_id uuid,
	max_uplength float8,
	hack_order int4,
	horton_order int4,
	mainstem_seq int4,
	shreve_order int4,
	is_isolated boolean NOT NULL DEFAULT false,
	loaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS flowpath_geometry_idx ON chyf_raw.flowpath USING gist (geometry);
CREATE INDEX IF NOT EXISTS flowpath_aoi_id_idx ON chyf_raw.flowpath (aoi_id);
CREATE INDEX IF NOT EXISTS flowpath_graph_id_idx ON chyf_raw.flowpath (graph_id);
CREATE INDEX IF NOT EXISTS aoi_short_name_idx ON chyf_raw.aoi (short_name);

CREATE TABLE chyf_raw.shoreline (
	id uuid NOT NULL,
	aoi_id uuid NOT NULL,
	geometry public.geometry(linestring, 4617) NOT NULL,
	CONSTRAINT shoreline_pkey PRIMARY KEY (id),
	CONSTRAINT shoreline_aoi_id_fkey FOREIGN KEY (aoi_id) REFERENCES chyf_raw.aoi(id)
);
CREATE INDEX shoreline_aoi_id_idx ON chyf_raw.shoreline USING btree (aoi_id);
CREATE INDEX shoreline_geometry_idx ON chyf_raw.shoreline USING gist (geometry);