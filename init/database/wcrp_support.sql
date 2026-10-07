-- Database-wide WCRP support objects:
--   * support.blank2null() trigger function attached to every WCRP tracking table
--
-- The WCRP tracking-table enum types live in config/fishpass.yaml and are synced into the
-- database at the start of each model run via sync_wcrp_tracking_enums(). This SQL file is
-- intended to be run once per database to create the support function.

CREATE SCHEMA IF NOT EXISTS support;

-- Generic, enum-only, self-maintaining trigger function: on every INSERT/UPDATE it finds
-- the ENUM columns of whichever table it fired on and turns any '' into NULL. All other
-- columns are untouched.
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
	FOR col IN
		SELECT a.attname
		FROM pg_attribute a
		JOIN pg_type t ON t.oid = a.atttypid
		WHERE a.attrelid = TG_RELID
		  AND a.attnum > 0
		  AND NOT a.attisdropped
		  AND t.typtype = 'e'
	LOOP
		IF (to_jsonb(NEW) ->> col) = '' THEN
			patch := jsonb_set(patch, ARRAY[col], 'null'::jsonb);
		END IF;
	END LOOP;

	IF patch <> '{}'::jsonb THEN
		NEW := jsonb_populate_record(NEW, patch);
	END IF;

	RETURN NEW;
END
$func$;
