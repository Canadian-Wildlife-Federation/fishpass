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

-- =================================================================================
--  Tracking table ENUMs
-- =================================================================================
-- Set up ENUM types for tracking table dropdowns
-- In QGIS, fields with an ENUM type can be edited via
-- a user-friendly dropdown.
-- This allows for columns where the value must be one of
-- a predefined set of values and the options will
-- conveniently show up when a user edits the field in QGIS

--Allowable values from the BC Tracking Table Guidance on Notion: 
--https://app.notion.com/p/cwf-spatial/Tracking-Table-Guidance-32941376668e809799a3f5e4d0a893d2?source=copy_link

drop type if exists
	support.tt_structure_type,
	support.tt_structure_list_status_type,
	support.tt_passability_asmt_type,
	support.tt_assessment_step_type,
	support.tt_excl_reason_type,
	support.tt_excl_method_type,
	support.tt_partial_passability_type,
	support.tt_partial_passability_notes_type,
	support.tt_upstr_hab_quality_type,
	support.tt_constructability_type,
	support.tt_priority_type,
	support.tt_rehab_type,
	support.tt_next_steps_type;
	
CREATE TYPE support.tt_structure_type AS ENUM
    ('Dam', 'Stream crossing - OBS', 'Stream crossing - CBS', 'Stream crossing - Ford', 'Other', 'None', '');
	
CREATE TYPE support.tt_structure_list_status_type AS ENUM
    ('Excluded structure', 'Data-deficient barrier', 'Non-actionable barrier', 'Priority barrier', 'Rehabilitated barrier', '');
	
create type support.tt_passability_asmt_type as enum
	('Informal assessment', 'Rapid assessment', 'Full assessment', '');
	
CREATE TYPE support.tt_assessment_step_type AS ENUM
    ('Informal assessment', 
	 'Passability assessment', 
	 'Habitat confirmation', 
	 'Detailed habitat investigation', 
	 'Engineering design', 
	 'Rehabilitated', 
	 'Post-rehabilitation monitoring', 
	 'Other',
	 '');
	 
CREATE TYPE support.tt_excl_reason_type AS ENUM
    ('Passable', 'No structure', 'No key upstream habitat', 'No structure and key upstream habitat', '');
	
CREATE TYPE support.tt_excl_method_type AS ENUM
    ('Imagery review', 'Informal assessment', 'Field assessment', 'Local knowledge', '');
	
CREATE TYPE support.tt_partial_passability_type AS ENUM
    ('Yes', 'No', 'Unknown', '');
	
CREATE TYPE support.tt_partial_passability_notes_type AS ENUM
    ('Proportion of individuals', 'Proportion of time', '');
	
CREATE TYPE support.tt_upstr_hab_quality_type AS ENUM
    ('High', 'Medium', 'Low', 'N/A or unassessed', '');
	
create type support.tt_constructability_type as ENUM 
	('Difficult', 'Moderate', 'Easy', '');

CREATE TYPE support.tt_priority_type AS ENUM
    ('High', 'Medium', 'Low', '');
	
CREATE TYPE support.tt_rehab_type AS ENUM
    ('Removal/decommissioned', 'Replacement - OBS', 'Replacement - CBS', 'Retrofit', '');
	
CREATE TYPE support.tt_next_steps_type AS ENUM
	('Barrier assessment',
	 'Barrier reassessment',
	 'In-depth passage assessment',
	 'Habitat confirmation',
	 'In-depth habitat investigation',
	 'Identify barrier owner',
	 'Engage with barrier owner',
	 'Engage with partners',
	 'Engage in public consultation',
	 'Bring barrier to regulator',
	 'Commission engineering designs',
	 'Fundraise',
	 'Rehabilitation',
	 'Post-rehabilitation monitoring',
	 'Correct deficiencies',
	 'Leave until end of lifecycle',
	 'Non-actionable',
	 'N/A - project complete',
	 ''
	);