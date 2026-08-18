"""Populate the remaining Compute Statistics output tables (requirements.md's Outputs section):
<output_schema>.natural_barriers / anthropogenic_barriers (views over all_structures), the cached
<output_schema>.cabd_<feature_type> tables (one per plan structure_types entry), and
<output_schema>.gradient_barriers.
"""

import json
import sys

import psycopg

from db import quote_ident
from model_plan import IDENTIFIER_RE


def write_barrier_stat_tables(cursor, output_schema, barrier_rows):
	"""barrier_rows: the list returned by graph_component.process_component -- barrier dicts
	with "id" and "stats" ({species: {...}}, from length_stats.compute_barrier_upstream_downstream_stats).
	Writes each barrier's stats into all_structures.species_stats -- only structures that
	successfully snapped onto a processed edge appear here, so species_stats stays NULL for
	every other row (see create_barrier_views, which relies on that to reproduce the previous
	natural_barriers/anthropogenic_barriers row set)."""

	if not barrier_rows:
		return

	schema_ident = quote_ident(output_schema)
	rows = [(json.dumps(b["stats"], default=str), b["id"]) for b in barrier_rows]

	cursor.executemany(
		f"""
		UPDATE {schema_ident}.all_structures
		SET species_stats = v.species_stats::jsonb
		FROM (VALUES (%s::text, %s::uuid)) AS v(species_stats, id)
		WHERE {schema_ident}.all_structures.id = v.id
		""",
		rows
	)


def create_barrier_views(cursor, output_schema):
	"""natural_barriers/anthropogenic_barriers as views over all_structures, filtered to the
	structure_type and to rows write_barrier_stat_tables actually populated (species_stats
	IS NOT NULL) -- i.e. structures that snapped onto a processed edge, matching the row set the
	old natural_barriers/anthropogenic_barriers tables held."""

	schema_ident = quote_ident(output_schema)
	for table, structure_type in (("natural_barriers", "natural"), ("anthropogenic_barriers", "anthropogenic")):
		cursor.execute(f"""
			CREATE VIEW {schema_ident}.{table} AS
			SELECT id, feature_id, feature_type, species_passability_value, geometry, snapped_geometry, species_stats
			FROM {schema_ident}.all_structures
			WHERE structure_type = '{structure_type}' AND species_stats IS NOT NULL
		""")


def create_and_populate_feature_type_tables(cursor, output_schema, structure_types, srid):
	"""One <output_schema>.cabd_<feature_type> table per plan structure_types entry (excluding
	'gradients', which gets its own dedicated table/function below), cached from
	all_structures with all modelling updates already applied."""

	schema_ident = quote_ident(output_schema)
	for feature_type in structure_types:
		if feature_type == "gradients":
			continue
		if not IDENTIFIER_RE.match(feature_type):
			sys.exit(f"Invalid structure_types entry (must be a safe table-name identifier): {feature_type!r}")
		table_ident = quote_ident(f"cabd_{feature_type}")
		cursor.execute(f"""
			CREATE TABLE {schema_ident}.{table_ident} (
				id uuid PRIMARY KEY,
				feature_id uuid NOT NULL,
				species_passability_value jsonb NOT NULL,
				source varchar NOT NULL,
				structure_type varchar,
				geometry geometry(point, {srid}) NOT NULL,
				snapped_geometry geometry(point, 4617)
			);
		""")
		cursor.execute(
			f"""
			INSERT INTO {schema_ident}.{table_ident}
				(id, feature_id, species_passability_value, source, structure_type, geometry, snapped_geometry)
			SELECT id, feature_id, species_passability_value, source, structure_type, geometry, snapped_geometry
			FROM {schema_ident}.all_structures WHERE feature_type = %s
			""",
			(feature_type,),
		)


def create_and_populate_gradient_barriers_cache(cursor, output_schema, srid):
	"""<output_schema>.gradient_barriers -- only called when the plan includes gradient_barriers
	(see plan['include_gradient_barriers'])."""

	schema_ident = quote_ident(output_schema)
	cursor.execute(f"""
		CREATE TABLE {schema_ident}.gradient_barriers (
			id uuid PRIMARY KEY,
			feature_id uuid NOT NULL,
			species_passability_value jsonb NOT NULL,
			geometry geometry(point, {srid}) NOT NULL,
			snapped_geometry geometry(point, 4617)
		);
	""")
	cursor.execute(f"""
		INSERT INTO {schema_ident}.gradient_barriers (id, feature_id, species_passability_value, geometry, snapped_geometry)
		SELECT id, feature_id, species_passability_value, geometry, snapped_geometry
		FROM {schema_ident}.all_structures WHERE source = 'gradient_barriers'
	""")
