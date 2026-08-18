"""Compute Statistics phase (fishpass/requirements/requirements.md), orchestrator.

See network_break.py's module docstring and requirements.md for the two-pass, graph_id-
partitioned design behind steps 5-9, and graph_component.py's module docstring for the current
AOI-boundary caveat.
"""

from barrier_tables import (
	create_and_populate_feature_type_tables,
	create_and_populate_gradient_barriers_cache,
	create_barrier_views,
	write_barrier_stat_tables,
)
from db import quote_ident
from graph_component import fetch_graph_ids, process_component
from network_break import break_network
from species_params import load_species_params

NO_DATA = -9999  # sentinel used in chyf_raw for a missing smoothed-elevation (M ordinate) value


def compute_effective_length(cursor, output_schema):
	"""Compute Statistics step 3: for each ecatchment_id, the mainstem_id with the greatest
	total length gets effective_length = length on its own edges; every other edge in that
	ecatchment_id (including edges on other mainstems, or with no mainstem_id at all) gets
	effective_length = 0. Edges with no ecatchment_id/mainstem_id keep their own length (nothing
	to compare them against)."""

	schema_ident = quote_ident(output_schema)

	cursor.execute(f"""
		UPDATE {schema_ident}.streams
		SET effective_length = length
		WHERE ecatchment_id IS NULL OR mainstem_id IS NULL
	""")

	cursor.execute(f"""
		UPDATE {schema_ident}.streams
		SET effective_length = 0
		WHERE ecatchment_id IS NOT NULL AND mainstem_id IS NOT NULL
	""")

	cursor.execute(f"""
		WITH mainstem_lengths AS (
			SELECT ecatchment_id, mainstem_id, SUM(length) AS total_length
			FROM {schema_ident}.streams
			WHERE ecatchment_id IS NOT NULL AND mainstem_id IS NOT NULL
			GROUP BY ecatchment_id, mainstem_id
		),
		best_mainstem AS (
			SELECT DISTINCT ON (ecatchment_id) ecatchment_id, mainstem_id
			FROM mainstem_lengths
			ORDER BY ecatchment_id, total_length DESC, mainstem_id
		)
		UPDATE {schema_ident}.streams s
		SET effective_length = s.length
		FROM best_mainstem bm
		WHERE s.ecatchment_id = bm.ecatchment_id AND s.mainstem_id = bm.mainstem_id
	""")


def compute_segment_gradient(cursor, output_schema):
	"""Compute Statistics step 4: (upstream_elevation - downstream_elevation) / length, where
	"elevation" is the smoothed elevation stored in each vertex's M ordinate (same convention as
	gradient_barriers), read from the segment's own first (upstream) and last (downstream)
	vertex. NULL if length is 0 or either endpoint's M is missing (the NO_DATA sentinel or NaN)."""

	schema_ident = quote_ident(output_schema)
	cursor.execute(f"""
		UPDATE {schema_ident}.streams
		SET segment_gradient = CASE
			WHEN length > 0
				AND ST_M(ST_PointN(geometry, 1)) IS NOT NULL
				AND ST_M(ST_PointN(geometry, 1)) != {NO_DATA}
				AND ST_M(ST_PointN(geometry, ST_NPoints(geometry))) IS NOT NULL
				AND ST_M(ST_PointN(geometry, ST_NPoints(geometry))) != {NO_DATA}
			THEN (ST_M(ST_PointN(geometry, 1)) - ST_M(ST_PointN(geometry, ST_NPoints(geometry)))) / length
			ELSE NULL
		END
	""")


def run_component_statistics(cursor, output_schema, plan, species_params_by_code):
	"""Steps 5-9, run independently per graph_id component (see graph_component.py). Returns
	the full list of barrier stat rows (across every component) for write_barrier_stat_tables."""

	graph_ids = fetch_graph_ids(cursor, output_schema)
	all_barrier_rows = []
	for i, graph_id in enumerate(graph_ids, start=1):
		barrier_rows = process_component(cursor, output_schema, graph_id, plan, species_params_by_code)
		all_barrier_rows.extend(barrier_rows)
		if i % 100 == 0 or i == len(graph_ids):
			print(f"  Processed {i}/{len(graph_ids)} connected component(s).")
	return all_barrier_rows


def compute_statistics(conn, cursor, plan, srid):
	"""Run Compute Statistics steps 1-9 and populate the remaining output objects (Outputs
	section): the natural_barriers/anthropogenic_barriers views, the cached cabd_<feature_type>
	tables, and gradient_barriers (if included in the plan)."""

	output_schema = plan["output_schema"]

	# Step 1 (Load the stream network) -- <output_schema>.streams already holds the working
	# network from Load Stream Network/Load Structures/Process Habitat; nothing to do here.

	new_segments = break_network(conn, cursor, plan, srid)  # step 2
	print(f"Step 2 (break network): {new_segments} new segment(s).")

	compute_effective_length(cursor, output_schema)  # step 3
	conn.commit()
	print("Step 3 (effective_length): done.")

	compute_segment_gradient(cursor, output_schema)  # step 4
	conn.commit()
	print("Step 4 (segment_gradient): done.")

	species_params_by_code = load_species_params()
	barrier_rows = run_component_statistics(cursor, output_schema, plan, species_params_by_code)  # steps 5-9
	conn.commit()
	print(f"Steps 5-9 (per-component statistics): done ({len(barrier_rows)} barrier(s) processed).")

	write_barrier_stat_tables(cursor, output_schema, barrier_rows)
	conn.commit()
	create_barrier_views(cursor, output_schema)
	conn.commit()
	print("natural_barriers/anthropogenic_barriers: done.")

	create_and_populate_feature_type_tables(cursor, output_schema, plan["structure_types"], srid)
	if plan["include_gradient_barriers"]:
		create_and_populate_gradient_barriers_cache(cursor, output_schema, srid)
	conn.commit()
	print("Cached feature-type tables: done.")
