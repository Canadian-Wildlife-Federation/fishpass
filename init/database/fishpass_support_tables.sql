-- Manual, one-time (or occasional) setup script for the FishPass support schema tables:
-- support.structure_updates, support.new_structures, support.habitat_updates.
--
-- See:
--   fishpass_engine/docs/inputs/structure_updates_dataset.md
--   fishpass_engine/docs/inputs/structure_new_dataset.md
--   fishpass_engine/docs/inputs/habitat_updates_dataset.md
--
-- Run by hand against the target FishPass database, e.g.:
--   psql "host=... dbname=... user=..." -f init/database/support_tables.sql
--
-- Not run by any GitHub Action. Safe to re-run (all statements are idempotent).

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS support;

-- support.structure_updates
--
-- Overrides/updates to barrier information sourced from CABD (or from
-- support.new_structures). barrier_id = cabd_id for CABD features, or
-- new_structure_id for support.new_structures features. There can be
-- multiple entries for the same barrier_id.

CREATE TABLE IF NOT EXISTS support.structure_updates (
	id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
	barrier_id uuid NOT NULL,
	feature_type varchar,
	update_type varchar NOT NULL CHECK (update_type IN ('authoritative', 'local_override')),
	update_scope varchar[] NOT NULL DEFAULT ARRAY['all']::varchar[],
	passability_status_spawn jsonb,
	passability_status_rear jsonb,
	update_source varchar,
	update_date date,
	notes varchar
);

CREATE INDEX IF NOT EXISTS structure_updates_barrier_id_idx ON support.structure_updates (barrier_id);

-- support.new_structures
--
-- Structures not tracked in CABD (e.g. barrier beaches, beaver dams).
-- Generally only used for WCRP reporting. Updates to these structures are
-- recorded in support.structure_updates (barrier_id = new_structure_id).

CREATE TABLE IF NOT EXISTS support.new_structures (
	new_structure_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
	feature_type varchar NOT NULL,
	update_scope varchar[] NOT NULL DEFAULT ARRAY['all']::varchar[],
	passability_status_spawn jsonb,
	passability_status_rear jsonb,
	point public.geometry(point, 4617),
	source varchar,
	notes varchar
);

CREATE INDEX IF NOT EXISTS new_structures_point_idx ON support.new_structures USING gist (point);

-- support.habitat_updates
--
-- Manual habitat additions/exclusions applied on top of computed habitat.
-- `points` is a multipoint (one or two points) used with `location_type`:
-- upstream/downstream require exactly one point, between requires exactly
-- two. Enforced below with a check constraint rather than a trigger.

CREATE TABLE IF NOT EXISTS support.habitat_updates (
	id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
	species_lifestage varchar[] NOT NULL CHECK (
		array_to_string(species_lifestage, ',') ~ '^(not_)?[a-z]+(_(spawn|rear))?(,(not_)?[a-z]+(_(spawn|rear))?)*$'
	),
	update_scope varchar NOT NULL DEFAULT 'all',
	points public.geometry(multipoint, 4617),
	location_type varchar NOT NULL CHECK (location_type IN ('upstream', 'downstream', 'between')),
	chyf_upstream_edge_id uuid,
	chyf_downstream_edge_id uuid,
	update_source varchar,
	update_date date,
	notes varchar,
	CONSTRAINT habitat_updates_points_count_chk CHECK (
		(location_type IN ('upstream', 'downstream') AND ST_NumGeometries(points) = 1)
		OR (location_type = 'between' AND ST_NumGeometries(points) = 2)
	)
);

CREATE INDEX IF NOT EXISTS habitat_updates_points_idx ON support.habitat_updates USING gist (points);
