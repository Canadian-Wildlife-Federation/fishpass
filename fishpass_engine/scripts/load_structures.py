"""Load Structures phase (fishpass/requirements/requirements.md Load Structures, steps 1-4,
6-7). Step 5 (snap to the CHyF network) is implemented separately in snap_structures.py.

Design note / deviation from the literal requirements.md wording: requirements.md describes
all_structures as potentially having multiple rows per feature_id, merging/splitting rows as
species share or diverge in passability value. This module instead keeps exactly one
all_structures row per feature_id, with species_passability_value holding the complete
<species>_<lifestage> -> value map for that feature. This is query-equivalent (every downstream
lookup is by species_lifestage jsonb key, which works identically either way) and much simpler
to build/update correctly; flagged here in case the row-per-species-group shape turns out to
matter for a reason not captured in requirements.md.
"""

import json
import sys

import psycopg

from cabd_client import fetch_feature_type, map_passability
from db import quote_ident, quote_qualified_ident

STRUCTURE_LIFESTAGES = ("rear", "spawn")

# requirements.md Load Structures step 7
NATURAL_FEATURE_TYPES = {"waterfalls", "gradient"}

def create_structures_table(cursor, output_schema, srid):
	schema_ident = quote_ident(output_schema)
	cursor.execute(f"""
		CREATE TABLE {schema_ident}.all_structures (
			id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
			feature_id uuid NOT NULL,
			feature_type varchar NOT NULL,
			species_passability_value jsonb NOT NULL DEFAULT '{{}}'::jsonb,
			source varchar NOT NULL CHECK (source IN ('cabd', 'new_structure', 'gradient_barriers')),
			structure_type varchar CHECK (structure_type IN ('natural', 'anthropogenic')),
			geometry geometry(point, {srid}) NOT NULL,
			snapped_geometry geometry(point, 4617),
			snapped_edge_id uuid,
			network_vertex_x double precision,
			network_vertex_y double precision,
			species_stats jsonb
		);
	""")
	cursor.execute(
		f"CREATE UNIQUE INDEX all_structures_feature_id_idx ON {schema_ident}.all_structures (feature_id);"
	)



def explode_passability(passability_status, target_species=None):
	"""Explode a passability_status jsonb value (from new_structures/structure_updates) into
	full <species>_<lifestage> keys.

	A key without an explicit _rear/_spawn suffix applies to both lifestages (requirements.md).

	A null passability_status means "full barrier" for every species/lifestage in
	target_species -- the "full barrier (impassable)" case documented on the passability_status
	column in structure_new_dataset.md / structure_updates_dataset.md. target_species is
	required in that case (callers omit it only when they don't need null-handling, e.g. tests).

	A non-null passability_status only produces keys for the species it explicitly lists --
	species it doesn't mention are left untouched by the caller's merge, rather than being
	defaulted to impassable, so a partial update/new-structure record can't accidentally blank
	out unrelated species. (This is the one place requirements.md's "or the specific species
	doesn't exist in the JSON string" wording is not applied literally -- flagged for
	confirmation.)
	"""
	if passability_status is None:
		if target_species is None:
			return {}
		return {f"{sp}_{lc}": 0 for sp in target_species for lc in STRUCTURE_LIFESTAGES}

	result = {}
	for key, value in passability_status.items():
		if key.endswith("_rear") or key.endswith("_spawn"):
			result[key] = value
		else:
			for lc in STRUCTURE_LIFESTAGES:
				result[f"{key}_{lc}"] = value
	return result


def get_aoi_short_names(cursor, output_schema):
	cursor.execute(f"SELECT short_name FROM {quote_ident(output_schema)}.aoi WHERE short_name IS NOT NULL")
	return [row[0] for row in cursor.fetchall()]


def build_cabd_row(feature, target_species):
	"""Return (cabd_id, feature_type, species_passability_value_json, lon, lat) for one CABD
	GeoJSON feature, per requirements.md Load Structures step 2's field mapping."""

	props = feature["properties"]
	cabd_id = props["cabd_id"]
	feature_type = props["feature_type"]
	lon, lat = feature["geometry"]["coordinates"][:2]
	value = map_passability(props.get("passability_status_code"))
	species_passability_value = {
		f"{sp}_{lc}": value for sp in target_species for lc in STRUCTURE_LIFESTAGES
	}
	return (cabd_id, feature_type, json.dumps(species_passability_value), lon, lat)


def populate_from_cabd(cursor, output_schema, plan, srid):
	"""Load Structures step 2. Returns the number of rows inserted."""

	cabd_feature_types = [ft for ft in plan["structure_types"] if ft != "gradients"]
	if not cabd_feature_types:
		return 0

	short_names = get_aoi_short_names(cursor, output_schema)
	if not short_names:
		return 0

	rows = []
	for feature_type in cabd_feature_types:
		features = fetch_feature_type(feature_type, short_names)
		rows.extend(build_cabd_row(f, plan["target_species"]) for f in features)
		print(f"  CABD {feature_type}: {len(features)} feature(s)")

	if not rows:
		return 0

	schema_ident = quote_ident(output_schema)
	
	cursor.executemany(
		f"""
		INSERT INTO {schema_ident}.all_structures
			(feature_id, feature_type, species_passability_value, source, geometry)
		VALUES (%s, %s, %s::jsonb, 'cabd', ST_SetSRID(ST_MakePoint(%s, %s), {srid}))
		ON CONFLICT (feature_id) DO NOTHING
		""",
		rows
	)
	return len(rows)


def load_new_structures(cursor, output_schema, plan, srid):
	"""Load Structures step 3. Returns the number of rows inserted."""

	table_ident = quote_qualified_ident(plan["structure_new_table"])
	cursor.execute(
		f"""
		SELECT new_structure_id, feature_type, passability_status, ST_AsBinary(point)
		FROM {table_ident}
		WHERE feature_type = ANY(%s)
		  AND ('all' = ANY(update_scope) OR %s = ANY(update_scope))
		""",
		(plan["structure_types"], plan["update_scope"]),
	)
	rows = cursor.fetchall()
	if not rows:
		return 0

	insert_rows = []
	for new_structure_id, feature_type, passability_status, point_wkb in rows:
		species_map = explode_passability(passability_status, plan["target_species"])
		insert_rows.append((new_structure_id, feature_type, json.dumps(species_map), bytes(point_wkb)))

	schema_ident = quote_ident(output_schema)
	
	cursor.executemany(
		f"""
		INSERT INTO {schema_ident}.all_structures
			(feature_id, feature_type, species_passability_value, source, geometry)
		VALUES (%s, %s, %s::jsonb, 'new_structure', ST_SetSRID(ST_GeomFromWKB(%s), {srid}))
		ON CONFLICT (feature_id) DO NOTHING
		""",
		insert_rows
	)
	return len(insert_rows)


def apply_structure_updates(cursor, output_schema, plan):
	"""Load Structures step 4. Returns the number of features updated.

	Updates are fetched pre-ordered (authoritative asc update_date, then local_override asc
	update_date) and folded into one map per feature_id in that order, so a later dict.update()
	naturally wins over an earlier one for any species/lifestage key both touch -- giving
	exactly the authoritative-then-local_override, earlier-then-later precedence
	requirements.md specifies.
	"""

	table_ident = quote_qualified_ident(plan["structure_update_table"])
	cursor.execute(
		f"""
		SELECT barrier_id, passability_status
		FROM {table_ident}
		WHERE ('all' = ANY(update_scope) OR %s = ANY(update_scope))
		ORDER BY (update_type = 'local_override'), update_date ASC
		""",
		(plan["update_scope"],),
	)
	rows = cursor.fetchall()
	if not rows:
		return 0

	updates_by_feature = {}
	for barrier_id, passability_status in rows:
		exploded = explode_passability(passability_status, plan["target_species"])
		updates_by_feature.setdefault(barrier_id, {}).update(exploded)

	schema_ident = quote_ident(output_schema)
	updated = 0
	for feature_id, update_map in updates_by_feature.items():
		cursor.execute(
			f"""
			UPDATE {schema_ident}.all_structures
			SET species_passability_value = species_passability_value || %s::jsonb
			WHERE feature_id = %s
			""",
			(json.dumps(update_map), feature_id),
		)
		updated += cursor.rowcount
	return updated


def add_gradient_barriers(cursor, output_schema, srid):
	"""Load Structures step 6. Only call this when plan['include_gradient_barriers'] is True.
	Returns the number of rows inserted."""

	cursor.execute("SELECT to_regclass('support.gradient_barriers')")
	if cursor.fetchone()[0] is None:
		print("  support.gradient_barriers does not exist -- skipping.")
		return 0

	cursor.execute("SELECT id, actual_species, ST_AsBinary(geometry) FROM support.gradient_barriers")
	rows = cursor.fetchall()
	if not rows:
		return 0

	insert_rows = [
		(gb_id, json.dumps({sp: 0 for sp in actual_species}), bytes(geom_wkb))
		for gb_id, actual_species, geom_wkb in rows
	]

	schema_ident = quote_ident(output_schema)
	
	cursor.executemany(
		f"""
		INSERT INTO {schema_ident}.all_structures
			(feature_id, feature_type, species_passability_value, source, geometry)
		VALUES (%s, 'gradient', %s::jsonb, 'gradient_barriers', ST_SetSRID(ST_GeomFromWKB(%s), {srid}))
		ON CONFLICT (feature_id) DO NOTHING
		""",
		insert_rows
	)
	return len(insert_rows)


def classify_structures(cursor, output_schema):
	"""Load Structures step 7. Returns the number of rows updated."""

	schema_ident = quote_ident(output_schema)
	cursor.execute(
		f"""
		UPDATE {schema_ident}.all_structures
		SET structure_type = CASE WHEN feature_type = ANY(%s) THEN 'natural' ELSE 'anthropogenic' END
		""",
		(list(NATURAL_FEATURE_TYPES),),
	)
	return cursor.rowcount


def load_structures(conn, cursor, plan, srid):
	"""Run Load Structures steps 1-4, 6, 7 (step 5 -- snapping -- is run separately, see
	snap_structures.py). srid must match <output_schema>.streams' geometry SRID."""

	output_schema = plan["output_schema"]

	create_structures_table(cursor, output_schema, srid)

	cabd_count = populate_from_cabd(cursor, output_schema, plan, srid)
	print(f"Loaded {cabd_count} structure(s) from CABD.")

	new_count = load_new_structures(cursor, output_schema, plan, srid)
	print(f"Loaded {new_count} new structure(s).")

	updated_count = apply_structure_updates(cursor, output_schema, plan)
	print(f"Applied structure updates to {updated_count} structure(s).")

	if plan["include_gradient_barriers"]:
		gb_count = add_gradient_barriers(cursor, output_schema, srid)
		print(f"Added {gb_count} gradient barrier(s).")

	classify_structures(cursor, output_schema)

	conn.commit()
