"""Populate the remaining Compute Statistics output tables (requirements.md's Outputs section):
the cached <output_schema>.cabd_<feature_type> tables (one per plan structure_types entry), and
<output_schema>.gradient_barriers.
"""

import json
import sys

import psycopg

from db import quote_ident
from model_plan import IDENTIFIER_RE


def write_barrier_stat_tables(cursor, output_schema, barrier_rows):
	"""barrier_rows: the list returned by graph_component.process_component -- barrier dicts
	with "id" and "stats" ({species: {...}}, from length_stats.compute_barrier_upstream_downstream_stats,
	including each species' upstream length figures at the barrier). Writes each barrier's stats
	into all_barriers.species_stats -- only structures that
	successfully snapped onto a processed edge appear here, so species_stats stays NULL for
	every other row (see postprocess_views.create_natural_anthropogenic_views, which relies on
	that to reproduce the previous natural_barriers/anthropogenic_barriers row set)."""

	if not barrier_rows:
		return

	schema_ident = quote_ident(output_schema)
	rows = [(json.dumps(b["stats"], default=str), b["id"]) for b in barrier_rows]

	cursor.executemany(
		f"""
		UPDATE {schema_ident}.all_barriers
		SET species_stats = v.species_stats::jsonb
		FROM (VALUES (%s::text, %s::uuid)) AS v(species_stats, id)
		WHERE {schema_ident}.all_barriers.id = v.id
		""",
		rows
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
		FROM {schema_ident}.all_barriers WHERE source = 'gradient_barriers'
	""")
