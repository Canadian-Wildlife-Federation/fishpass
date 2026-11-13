"""Writes the computed barriers statistic to the all_barriers table. Creates the cached
version of the gradient barrers (if required).
"""

import json


from db import quote_ident
from utils import SNAPPED_GEOMETRY_SRID


def write_barrier_stat_tables(cursor, output_schema, barrier_rows):
	"""Writes each barrier's stats into all_barriers.species_stats and
	all_barriers.upstream_stream_length -- only structures that successfully snapped onto a
	processed edge appear here, so both stay NULL for the other rows"""

	if not barrier_rows:
		return

	schema_ident = quote_ident(output_schema)
	rows = [(json.dumps(b["stats"], default=str), b["upstream_stream_length"], b["id"]) for b in barrier_rows]

	cursor.executemany(
		f"""
		UPDATE {schema_ident}.all_barriers
		SET species_stats = v.species_stats::jsonb,
			upstream_stream_length = v.upstream_stream_length
		FROM (VALUES (%s::text, %s::double precision, %s::uuid)) AS v(species_stats, upstream_stream_length, id)
		WHERE {schema_ident}.all_barriers.id = v.id
		""",
		rows,
	)


def create_and_populate_gradient_barriers_cache(cursor, output_schema, srid):
	"""<output_schema>.gradient_barriers -- only called when the plan includes gradient_barriers
	(see plan['include_gradient_barriers'])."""

	schema_ident = quote_ident(output_schema)
	cursor.execute(f"""
		CREATE TABLE {schema_ident}.gradient_barriers (
			feature_id uuid PRIMARY KEY,
			species_passability_value jsonb NOT NULL,
			geometry geometry(point, {srid}) NOT NULL,
			snapped_geometry geometry(point, {SNAPPED_GEOMETRY_SRID})
		);
	""")
	cursor.execute(f"""
		INSERT INTO {schema_ident}.gradient_barriers (feature_id, species_passability_value, geometry, snapped_geometry)
		 SELECT feature_id, species_passability_value, geometry, snapped_geometry
		FROM {schema_ident}.all_barriers WHERE source = 'gradient_barriers'
	""")
