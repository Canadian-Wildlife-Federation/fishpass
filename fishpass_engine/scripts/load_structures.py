"""Load Structures phase (fishpass/docs/fishpass_docs.md Load Structures, steps 1-4,
6-7). Step 5 (snap to the CHyF network) is implemented separately in snap_structures.py.

Design note / deviation from the literal requirements.md wording: requirements.md describes
all_barriers as potentially having multiple rows per feature_id, merging/splitting rows as
species share or diverge in passability value. This module instead keeps exactly one
all_barriers row per feature_id, with species_passability_value holding the complete
<species>_<lifestage> -> value map for that feature. This is query-equivalent (every downstream
lookup is by species_lifestage jsonb key, which works identically either way) and much simpler
to build/update correctly; flagged here in case the row-per-species-group shape turns out to
matter for a reason not captured in requirements.md.
"""

import json
import sys
from pathlib import Path

import psycopg
import yaml

from cabd_client import fetch_feature_type, map_passability
from db import quote_ident, quote_qualified_ident
from model_plan import IDENTIFIER_RE

STRUCTURE_LIFESTAGES = ("rear", "spawn")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CLASSIFICATION_CONFIG = REPO_ROOT / "config" / "fishpass.yaml"


def load_natural_feature_types(config_path=DEFAULT_CLASSIFICATION_CONFIG):
	"""requirements.md Classify Structures. Returns the set of feature_type values classified as
	'natural' -- any feature_type not in this set is classified as 'anthropogenic' (see
	classify_structures). See config/fishpass.yaml."""

	if not Path(config_path).is_file():
		sys.exit(f"FishPass config file not found: {config_path}")
	with open(config_path) as f:
		data = yaml.safe_load(f) or {}
	raw = (data.get("structure_classification") or {}).get("natural_feature_types") or []
	return set(raw)

def create_cabd_table(cursor, output_schema, feature_type, srid):
	"""Create <output_schema>.cabd_<feature_type>, one per plan structure_types entry (the caller
	is responsible for excluding 'gradients', which gets its own dedicated table/function below),
	holding the raw, unmodified rows as returned by the CABD API -- populated before any
	structure_updates/new_structures/gradient/classification/snapping logic runs (see
	load_structures.populate_from_cabd / populate_cabd_table)."""

	if not IDENTIFIER_RE.match(feature_type):
		sys.exit(f"Invalid structure_types entry (must be a safe table-name identifier): {feature_type!r}")
	schema_ident = quote_ident(output_schema)
	table_ident = quote_ident(f"cabd_{feature_type}")
	cursor.execute(f"""
		CREATE TABLE {schema_ident}.{table_ident} (
			cabd_id uuid NOT NULL primary key,
			species_passability_value jsonb NOT NULL,
			passability_status_code integer,
			geometry geometry(point, {srid}) NOT NULL
		);
	""")


def create_structures_table(cursor, output_schema, srid):
	schema_ident = quote_ident(output_schema)
	cursor.execute(f"""
		CREATE TABLE {schema_ident}.all_barriers (
			id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
			feature_id uuid NOT NULL,
			feature_type varchar NOT NULL,
			species_passability_value jsonb NOT NULL DEFAULT '{{}}'::jsonb,
			source varchar NOT NULL CHECK (source IN ('cabd', 'new_structure', 'gradient_barriers')),
			structure_type varchar CHECK (structure_type IN ('natural', 'anthropogenic')),
			geometry geometry(point, {srid}) NOT NULL,
			snapped_geometry geometry(point, 4617),
			upstream_edge_id uuid,
			downstream_edge_id uuid,
			network_vertex_x double precision,
			network_vertex_y double precision,
			species_stats jsonb
		);
	""")
	cursor.execute(
		f"CREATE UNIQUE INDEX all_barriers_feature_id_idx ON {schema_ident}.all_barriers (feature_id);"
	)



def _require_passability_object(status, lifestage, row_id):
	"""passability_status_rear/_spawn must be a JSON object per structure_new_dataset.md /
	structure_updates_dataset.md (e.g. {"es": 0.25}). Raise a clear, actionable error instead of
	an AttributeError deep in .items() if a row has malformed data (e.g. a JSON string/number)."""
	if not isinstance(status, dict):
		raise ValueError(
			f"passability_status_{lifestage} for row {row_id!r} must be a JSON object "
			f"(e.g. {{\"es\": 0.25}}), got {status!r}"
		)


def explode_new_structure_passability(passability_status_rear, passability_status_spawn, target_species, row_id=None):
	"""Build a <species>_<lifestage> -> value map from support.new_structures' split
	passability_status_rear/_spawn columns, for species in target_species only. A species with
	no entry in the relevant column defaults to impassable (0) -- unlike
	explode_structure_update, there is no pre-existing all_barriers row for a brand-new
	structure to fall back to; this call builds the initial insert."""
	result = {f"{sp}_{lc}": 0 for sp in target_species for lc in STRUCTURE_LIFESTAGES}
	for lifestage, status in (("rear", passability_status_rear), ("spawn", passability_status_spawn)):
		if not status:
			continue
		_require_passability_object(status, lifestage, row_id)
		for sp, value in status.items():
			if sp in target_species:
				result[f"{sp}_{lifestage}"] = value
	return result


def explode_structure_update(passability_status_rear, passability_status_spawn, target_species, row_id=None):
	"""Build a <species>_<lifestage> -> value map from support.structure_updates' split
	passability_status_rear/_spawn columns, for species in target_species only. Species with no
	entry in the relevant column are omitted (not defaulted to impassable) so the jsonb `||` merge
	in apply_structure_updates leaves the structure's existing default (from CABD or
	support.new_structures) untouched."""
	result = {}
	for lifestage, status in (("rear", passability_status_rear), ("spawn", passability_status_spawn)):
		if not status:
			continue
		_require_passability_object(status, lifestage, row_id)
		for sp, value in status.items():
			if sp in target_species:
				result[f"{sp}_{lifestage}"] = value
	return result


def get_aoi_short_names(cursor, output_schema):
	cursor.execute(f"SELECT short_name FROM {quote_ident(output_schema)}.aoi WHERE short_name IS NOT NULL")
	return [row[0] for row in cursor.fetchall()]


def build_cabd_row(feature, target_species):
	"""Return (cabd_id, species_passability_value_json, passability_status_code, lon, lat) for one
	CABD GeoJSON feature, per requirements.md Load Structures step 2's field mapping."""

	props = feature["properties"]
	cabd_id = props["cabd_id"]
	passability_status_code = props.get("passability_status_code")
	lon, lat = feature["geometry"]["coordinates"][:2]
	value = map_passability(passability_status_code)
	species_passability_value = {
		f"{sp}_{lc}": value for sp in target_species for lc in STRUCTURE_LIFESTAGES
	}
	return (cabd_id, json.dumps(species_passability_value), passability_status_code, lon, lat)


def populate_cabd_table(cursor, output_schema, feature_type, short_names, target_species, srid):
	"""Fetch one feature_type from CABD and insert it into <output_schema>.cabd_<feature_type>,
	one feature at a time -- no full features list or rows list is ever held. Returns the number
	of features fetched."""

	if not short_names:
		return 0

	schema_ident = quote_ident(output_schema)
	table_ident = quote_ident(f"cabd_{feature_type}")

	count = 0

	def rows():
		nonlocal count
		for feature in fetch_feature_type(feature_type, short_names):
			count += 1
			yield build_cabd_row(feature, target_species)

	cursor.executemany(
		f"""
		INSERT INTO {schema_ident}.{table_ident}
			(cabd_id, species_passability_value, passability_status_code, geometry)
		VALUES (%s, %s::jsonb, %s, ST_SetSRID(ST_MakePoint(%s, %s), {srid}))
		""",
		rows(),
	)
	return count


def populate_all_barriers_from_cabd(cursor, output_schema, cabd_feature_types):
	"""Populate all_barriers by reading back from the already-populated
	<output_schema>.cabd_<feature_type> tables (see populate_cabd_table) rather than from
	Python-held rows. Returns the number of rows inserted."""

	schema_ident = quote_ident(output_schema)
	selects = [
		f"SELECT cabd_id, %s::varchar AS feature_type, species_passability_value, 'cabd' AS source, geometry "
		f"FROM {schema_ident}.{quote_ident(f'cabd_{ft}')}"
		for ft in cabd_feature_types
	]
	cursor.execute(
		f"""
		INSERT INTO {schema_ident}.all_barriers
			(feature_id, feature_type, species_passability_value, source, geometry)
		{" UNION ALL ".join(selects)}
		ON CONFLICT (feature_id) DO NOTHING
		""",
		cabd_feature_types,
	)
	return cursor.rowcount


def populate_from_cabd(cursor, conn, output_schema, plan, srid):
	"""Load Structures step 2. Returns the number of rows inserted into all_barriers.

	For each feature_type: creates <output_schema>.cabd_<feature_type>, fetches that type from
	CABD and streams it straight into the table, then commits -- before any
	structure_updates/new_structures/gradient/classification/snapping logic that mutates
	all_barriers later in the pipeline. Once every feature_type's cache table is populated,
	all_barriers is populated by reading back from those cache tables."""

	cabd_feature_types = [ft for ft in plan["structure_types"] if ft != "gradients"]
	if not cabd_feature_types:
		return 0

	short_names = get_aoi_short_names(cursor, output_schema)

	for feature_type in cabd_feature_types:
		create_cabd_table(cursor, output_schema, feature_type, srid)
		count = populate_cabd_table(cursor, output_schema, feature_type, short_names, plan["target_species"], srid)
		conn.commit()
		print(f"  CABD {feature_type}: {count} feature(s)")

	return populate_all_barriers_from_cabd(cursor, output_schema, cabd_feature_types)


def load_new_structures(cursor, output_schema, plan, srid):
	"""Load Structures step 3. Returns the number of rows inserted."""

	table_ident = quote_qualified_ident(plan["structure_new_table"])
	cursor.execute(
		f"""
		SELECT new_structure_id, feature_type, passability_status_rear, passability_status_spawn, ST_AsBinary(point)
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
	for new_structure_id, feature_type, passability_status_rear, passability_status_spawn, point_wkb in rows:
		species_map = explode_new_structure_passability(
			passability_status_rear, passability_status_spawn, plan["target_species"], row_id=new_structure_id
		)
		insert_rows.append((new_structure_id, feature_type, json.dumps(species_map), bytes(point_wkb)))

	schema_ident = quote_ident(output_schema)
	
	cursor.executemany(
		f"""
		INSERT INTO {schema_ident}.all_barriers
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

	Species with no entry in a given row's passability_status_rear/_spawn are omitted from that
	row's exploded map (see explode_structure_update), so the species_passability_value || %s::jsonb
	merge below leaves that structure's existing value (from CABD or new_structures) untouched
	rather than forcing it to impassable.
	"""

	table_ident = quote_qualified_ident(plan["structure_update_table"])
	cursor.execute(
		f"""
		SELECT barrier_id, passability_status_rear, passability_status_spawn
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
	for barrier_id, passability_status_rear, passability_status_spawn in rows:
		exploded = explode_structure_update(
			passability_status_rear, passability_status_spawn, plan["target_species"], row_id=barrier_id
		)
		updates_by_feature.setdefault(barrier_id, {}).update(exploded)

	schema_ident = quote_ident(output_schema)
	updated = 0
	for feature_id, update_map in updates_by_feature.items():
		cursor.execute(
			f"""
			UPDATE {schema_ident}.all_barriers
			SET species_passability_value = species_passability_value || %s::jsonb
			WHERE feature_id = %s
			""",
			(json.dumps(update_map), feature_id),
		)
		updated += cursor.rowcount
	return updated


def add_gradient_barriers(cursor, output_schema, plan, srid):
	"""Load Structures step 6. Only call this when plan['include_gradient_barriers'] is True.
	Returns the number of rows inserted."""

	source_table = plan["gradient_barriers_table"]
	table_ident = quote_qualified_ident(source_table)

	cursor.execute("SELECT to_regclass(%s)", (source_table,))
	if cursor.fetchone()[0] is None:
		print(f"  {source_table} does not exist -- skipping.")
		return 0

	short_names = get_aoi_short_names(cursor, output_schema)
	if not short_names:
		return 0

	cursor.execute(
    	f"SELECT id, actual_species, ST_AsBinary(geometry) FROM {table_ident} WHERE workunit && %s::varchar[]",
    	(short_names,),
	)
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
		INSERT INTO {schema_ident}.all_barriers
			(feature_id, feature_type, species_passability_value, source, geometry)
		VALUES (%s, 'gradients', %s::jsonb, 'gradient_barriers', ST_SetSRID(ST_GeomFromWKB(%s), {srid}))
		ON CONFLICT (feature_id) DO NOTHING
		""",
		insert_rows
	)
	return len(insert_rows)


def classify_structures(cursor, output_schema, natural_feature_types):
	"""Load Structures step 7. Any feature_type not in natural_feature_types is classified as
	'anthropogenic'. Returns the number of rows updated."""

	schema_ident = quote_ident(output_schema)
	cursor.execute(
		f"""
		UPDATE {schema_ident}.all_barriers
		SET structure_type = CASE WHEN feature_type = ANY(%s) THEN 'natural' ELSE 'anthropogenic' END
		""",
		(list(natural_feature_types),),
	)
	return cursor.rowcount


def load_structures(conn, cursor, plan, srid):
	"""Run Load Structures steps 1-4, 6, 7 (step 5 -- snapping -- is run separately, see
	snap_structures.py). srid must match <output_schema>.streams' geometry SRID."""

	output_schema = plan["output_schema"]

	create_structures_table(cursor, output_schema, srid)

	cabd_count = populate_from_cabd(cursor, conn, output_schema, plan, srid)
	print(f"Loaded {cabd_count} structure(s) from CABD.")

	new_count = load_new_structures(cursor, output_schema, plan, srid)
	print(f"Loaded {new_count} new structure(s).")

	updated_count = apply_structure_updates(cursor, output_schema, plan)
	print(f"Applied structure updates to {updated_count} structure(s).")

	if plan["include_gradient_barriers"]:
		gb_count = add_gradient_barriers(cursor, output_schema, plan, srid)
		print(f"Added {gb_count} gradient barrier(s).")

	natural_feature_types = plan["natural_feature_types_override"]
	if natural_feature_types is None:
		natural_feature_types = load_natural_feature_types()
	classify_structures(cursor, output_schema, natural_feature_types)

	conn.commit()
