-- Database-wide WCRP support objects:
--   * support.tt_* ENUM types used by every WCRP tracking table
--   * support.blank2null() trigger function attached to every WCRP tracking table
--
-- Applied automatically at the start of every model run (run_model.py, via
-- create_wcrp_tracking_table.apply_wcrp_support), before the plan's tracking table is
-- created or checked. Runs as the database_roles.owner role from config/fishpass.yaml,
-- which must own the support schema, these types, and this function. Any changes it
-- makes (new types, new enum values, blank2null() created) are reported in the log and
-- the GitHub Actions job summary.
--
-- Every statement is idempotent, so running it on every model run is safe, including
-- after tracking tables that use these enums exist. To add an enum value, add it to
-- enum_defs below; the next model run applies it. It can also be run by hand, e.g.:
--   psql "host=... dbname=... user=..." -f init/database/wcrp_support.sql
--
-- Requires the support schema (init/database/fishpass_support_tables.sql creates it; it
-- is also created here if missing).

CREATE SCHEMA IF NOT EXISTS support;

-- =================================================================================
--  Tracking table ENUMs
-- =================================================================================
-- In QGIS, fields with an ENUM type can be edited via a user-friendly dropdown. This allows
-- for columns where the value must be one of a predefined set of values and the options
-- will conveniently show up when a user edits the field in QGIS.
--
-- Allowable values from the BC Tracking Table Guidance on Notion:
-- https://app.notion.com/p/cwf-spatial/Tracking-Table-Guidance-32941376668e809799a3f5e4d0a893d2?source=copy_link
--
-- THE BLANK ('') VALUE: every type below automatically gets '' as its LAST value. It is
-- not listed in enum_defs on purpose -- the DO block always adds it, so it can't be
-- forgotten. It is required because QGIS writes '' (not NULL) when a user clears a
-- dropdown, and PostgreSQL casts the incoming value to the enum BEFORE any trigger runs, so
-- without '' as a valid label the edit would be rejected outright. Once accepted,
-- support.blank2null() (below, attached to each tracking table as a BEFORE INSERT OR
-- UPDATE trigger) converts any '' in an enum column to NULL, so '' is never actually stored.
--
-- HOW RE-RUNS / CHANGES BEHAVE:
--   * Type missing                 -> created with the listed values, then ''.
--   * Type exists                  -> every listed value not already present is added
--                                     (ALTER TYPE ... ADD VALUE IF NOT EXISTS ... BEFORE ''),
--                                     so a new value lands at the end of the dropdown, just
--                                     ahead of ''. Existing values and data are untouched.
--   * Value REMOVED or RENAMED in  -> NOT applied automatically (PostgreSQL can't drop an
--     enum_defs                       enum value). Rename with
--                                       ALTER TYPE support.<type> RENAME VALUE 'old' TO 'new';
--                                     (existing rows follow automatically). Removing a value
--                                     requires updating any rows that use it, then rebuilding
--                                     the type -- do this by hand, deliberately.
--   * ADD VALUE can run inside this DO block on PostgreSQL 12+, but a newly added value
--     can't be USED until this script's transaction commits (not an issue here).

DO $$
DECLARE
	-- type name -> ordered list of values ('' is appended automatically; see above).
	-- Values must not contain single quotes (they'd break this JSON string literal).
	enum_defs CONSTANT jsonb := '{
		"tt_structure_type": [
			"Dam", "Stream crossing - OBS", "Stream crossing - CBS", "Stream crossing - Ford",
			"Other", "None"
		],
		"tt_structure_list_status_type": [
			"Excluded structure", "Data-deficient barrier", "Non-actionable barrier",
			"Priority barrier", "Rehabilitated barrier"
		],
		"tt_passability_asmt_type": [
			"Informal assessment", "Rapid assessment", "Full assessment"
		],
		"tt_assessment_step_type": [
			"Informal assessment", "Passability assessment", "Habitat confirmation",
			"Detailed habitat investigation", "Engineering design", "Rehabilitated",
			"Post-rehabilitation monitoring", "Other"
		],
		"tt_excl_reason_type": [
			"Passable", "No structure", "No key upstream habitat",
			"No structure and key upstream habitat"
		],
		"tt_excl_method_type": [
			"Imagery review", "Informal assessment", "Field assessment", "Local knowledge"
		],
		"tt_partial_passability_type": [
			"Yes", "No", "Unknown"
		],
		"tt_partial_passability_notes_type": [
			"Proportion of individuals", "Proportion of time"
		],
		"tt_upstr_hab_quality_type": [
			"High", "Medium", "Low", "N/A or unassessed"
		],
		"tt_constructability_type": [
			"Difficult", "Moderate", "Easy"
		],
		"tt_priority_type": [
			"High", "Medium", "Low"
		],
		"tt_rehab_type": [
			"Removal/decommissioned", "Replacement - OBS", "Replacement - CBS", "Retrofit"
		],
		"tt_next_steps_type": [
			"Barrier assessment", "Barrier reassessment", "In-depth passage assessment",
			"Habitat confirmation", "In-depth habitat investigation", "Identify barrier owner",
			"Engage with barrier owner", "Engage with partners", "Engage in public consultation",
			"Bring barrier to regulator", "Commission engineering designs", "Fundraise",
			"Rehabilitation", "Post-rehabilitation monitoring", "Correct deficiencies",
			"Leave until end of lifecycle", "Non-actionable", "N/A - project complete"
		]
	}';
	type_name text;
	type_values jsonb;
	enum_value text;
BEGIN
	FOR type_name, type_values IN SELECT key, value FROM jsonb_each(enum_defs) LOOP
		IF NOT EXISTS (
			SELECT 1
			FROM pg_type t
			JOIN pg_namespace n ON n.oid = t.typnamespace
			WHERE n.nspname = 'support' AND t.typname = type_name
		) THEN
			EXECUTE format(
				'CREATE TYPE support.%I AS ENUM (%s, %L)',
				type_name,
				(SELECT string_agg(format('%L', v), ', ' ORDER BY ord)
				 FROM jsonb_array_elements_text(type_values) WITH ORDINALITY AS e(v, ord)),
				''
			);
			RAISE NOTICE 'Created enum support.%', type_name;
		ELSE
			-- Make sure the blank value exists (appended last) before positioning new
			-- values ahead of it.
			EXECUTE format('ALTER TYPE support.%I ADD VALUE IF NOT EXISTS %L', type_name, '');
			FOR enum_value IN
				SELECT v FROM jsonb_array_elements_text(type_values) WITH ORDINALITY AS e(v, ord)
				ORDER BY ord
			LOOP
				EXECUTE format(
					'ALTER TYPE support.%I ADD VALUE IF NOT EXISTS %L BEFORE %L',
					type_name, enum_value, ''
				);
			END LOOP;
		END IF;
	END LOOP;
END
$$;

-- =================================================================================
--  blank2null() trigger function
-- =================================================================================
-- Generic, enum-only, self-maintaining trigger function: on every INSERT/UPDATE it finds
-- the ENUM columns of whichever table it fired on and turns any '' into NULL (see "THE
-- BLANK ('') VALUE" above). All other columns are untouched. Lives in the support schema
-- alongside the tt_* enums it services, so creating it needs no CREATE on public.
--
-- Attached per tracking table by create_wcrp_tracking_table.py:
--   create trigger blank2null_trg before insert or update on <code>_wcrp.tracking_table_<code>
--   for each row execute function support.blank2null();

CREATE OR REPLACE FUNCTION support.blank2null()
	RETURNS trigger
	LANGUAGE plpgsql AS
$func$
DECLARE
	patch jsonb := '{}'::jsonb;
	col   text;
BEGIN
	-- Only the ENUM columns of whichever table this trigger fired on.
	FOR col IN
		SELECT a.attname
		FROM pg_attribute a
		JOIN pg_type t ON t.oid = a.atttypid
		WHERE a.attrelid = TG_RELID
		  AND a.attnum > 0
		  AND NOT a.attisdropped
		  AND t.typtype = 'e'            -- 'e' = enum only
	LOOP
		IF (to_jsonb(NEW) ->> col) = '' THEN
			patch := jsonb_set(patch, ARRAY[col], 'null'::jsonb);
		END IF;
	END LOOP;

	-- Override ONLY the blank enum columns; all other fields are untouched.
	IF patch <> '{}'::jsonb THEN
		NEW := jsonb_populate_record(NEW, patch);
	END IF;

	RETURN NEW;
END
$func$;
