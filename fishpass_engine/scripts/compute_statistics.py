"""Compute Statistics phase (fishpass/docs/fishpass_docs.md), orchestrator.

See network_break.py's module docstring for the two-pass, graph_id-
partitioned design behind steps 5-9, and graph_component.py's module docstring for the current
AOI-boundary caveat.
"""

import logging

from barrier_tables import (
	create_and_populate_gradient_barriers_cache,
	write_barrier_stat_tables,
)
from db import quote_ident
from graph_component import (
	build_graph_id_bundles,
	fetch_bundle_barriers,
	fetch_bundle_edges,
	fetch_bundle_habitat_updates,
	fetch_graph_id_counts,
	flush_stats_writes,
	iter_stats_write_rows,
	process_component,
)
from load_stream_network import (
	create_streams_bulk_write_indexes,
	drop_streams_bulk_write_indexes,
)
from network_break import break_network
from species_params import load_species_params

logger = logging.getLogger(__name__)

NO_DATA = -9999  # sentinel used in chyf_raw for a missing smoothed-elevation (M ordinate) value
BUNDLE_EDGE_BUDGET = 100_000  # max total edge count packed into one bulk-fetch bundle of graph_ids
WRITE_BATCH_SIZE = 5000  # streams.species_stats write-back chunk size (rows)
# Cap on the same chunk in serialised JSON characters: an edge low on a large river carries the id
# of every barrier upstream of it, so WRITE_BATCH_SIZE such rows can exceed Postgres's 1GB message limit.
WRITE_BATCH_BYTES = 100_000_000


def compute_effective_length_and_gradient(cursor, output_schema):
	"""Compute Statistics steps 3-4, combined into a single UPDATE so each streams row is
	rewritten only once.

	Step 3 (effective_length): for each ecatchment_id, the mainstem_id with the greatest total
	length gets effective_length = length on its own edges; every other edge in that
	ecatchment_id (including edges on other mainstems, or with no mainstem_id at all) gets
	effective_length = 0. Edges with no ecatchment_id/mainstem_id keep their own length (nothing
	to compare them against).

	Step 4 (segment_gradient): (upstream_elevation - downstream_elevation) / length, where
	"elevation" is the smoothed elevation stored in each vertex's M ordinate (same convention as
	gradient_barriers), read from the segment's own first (upstream) and last (downstream)
	vertex. NULL if length is 0 or either endpoint's M is missing (the NO_DATA sentinel or NaN).
	Each endpoint is read once (NULLIF turns NO_DATA into NULL, which then propagates through the
	subtraction) -- every extra reference to the geometry is another detoast per row."""

	schema_ident = quote_ident(output_schema)

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
		),
		best_mainstem_by_row AS (
			SELECT st.id, bm.mainstem_id AS best_mainstem_id
			FROM {schema_ident}.streams st
			LEFT JOIN best_mainstem bm ON st.ecatchment_id = bm.ecatchment_id
		)
		UPDATE {schema_ident}.streams s
		SET
			effective_length = CASE
				WHEN s.ecatchment_id IS NULL OR s.mainstem_id IS NULL THEN s.length
				WHEN s.mainstem_id = r.best_mainstem_id THEN s.length
				ELSE 0
			END,
			segment_gradient = CASE
				WHEN s.length > 0
				THEN (
					NULLIF(ST_M(ST_StartPoint(s.geometry)), {NO_DATA})
					- NULLIF(ST_M(ST_EndPoint(s.geometry)), {NO_DATA})
				) / s.length
				ELSE NULL
			END
		FROM best_mainstem_by_row r
		WHERE r.id = s.id
	""")


def run_component_statistics(cursor, output_schema, plan, species_params_by_code):
	"""Steps 5-9, run independently per graph_id component (see graph_component.py), fetched and
	written in bundles of up to BUNDLE_EDGE_BUDGET total edges to cut down on per-component
	round trips without loading the whole network into memory at once -- components are bundled
	largest-first (fetch_graph_id_counts), so large components end up alone in their own bundle
	and small components pack together. Each component's edge stats are serialised and flushed
	to streams as they are produced, and each bundle's barrier stats are written to all_barriers
	before the next bundle is fetched, so neither accumulates across the run. Returns the number
	of barriers written."""

	graph_id_counts = fetch_graph_id_counts(cursor, output_schema)
	bundles = build_graph_id_bundles(graph_id_counts, BUNDLE_EDGE_BUDGET)
	total_components = len(graph_id_counts)
	logger.info("Step 5-9: processing %d connected component(s) in %d bundle(s)...", total_components, len(bundles))

	pending_write_rows = []
	pending_write_bytes = 0
	components_done = 0
	barriers_done = 0

	for bundle_num, graph_ids in enumerate(bundles, start=1):
		edges_by_graph = fetch_bundle_edges(cursor, output_schema, graph_ids)
		barriers_by_graph = fetch_bundle_barriers(cursor, output_schema, graph_ids)
		habitat_by_graph = fetch_bundle_habitat_updates(cursor, output_schema, graph_ids)
		bundle_barrier_rows = []

		for graph_id in graph_ids:
			# Popped (and edges dropped below) so a component's fetched rows are released once
			# it has been processed, rather than held while its stats are written out.
			edges = edges_by_graph.pop(graph_id, [])
			components_done += 1
			if not edges:
				continue

			edge_stats, barrier_rows, route_measures = process_component(
				graph_id,
				edges,
				barriers_by_graph.pop(graph_id, []),
				habitat_by_graph.pop(graph_id, []),
				plan,
				species_params_by_code,
			)
			del edges
			bundle_barrier_rows.extend(barrier_rows)

			# Flushed as edge_stats is consumed rather than once per component: one component
			# can produce millions of rows, and holding all of their JSON at once is the largest
			# single allocation in this phase.
			for row in iter_stats_write_rows(edge_stats, route_measures):
				pending_write_rows.append(row)
				pending_write_bytes += len(row[0])
				if len(pending_write_rows) >= WRITE_BATCH_SIZE or pending_write_bytes >= WRITE_BATCH_BYTES:
					flush_stats_writes(cursor, output_schema, pending_write_rows)
					pending_write_rows.clear()
					pending_write_bytes = 0

			if components_done % 100 == 0 or components_done == total_components:
				logger.info(
					"  Processed %d/%d connected component(s) (bundle %d/%d).",
					components_done,
					total_components,
					bundle_num,
					len(bundles),
				)

		write_barrier_stat_tables(cursor, output_schema, bundle_barrier_rows)
		barriers_done += len(bundle_barrier_rows)

	flush_stats_writes(cursor, output_schema, pending_write_rows)
	return barriers_done


def compute_statistics(conn, cursor, plan, srid):
	"""Run Compute Statistics steps 1-9 and populate the remaining output objects (Outputs
	section): the cached cabd_<feature_type> tables, and gradient_barriers (if included in the
	plan). See postprocess_views.create_barrier_views for the reporting views, run as its own
	phase after this one."""

	output_schema = plan["output_schema"]

	# Step 1 (Load the stream network) -- <output_schema>.streams already holds the working
	# network from Load Stream Network/Load Structures/Process Habitat; nothing to do here.

	# Steps 2-9 rewrite every streams row (twice); see drop_streams_bulk_write_indexes.
	drop_streams_bulk_write_indexes(cursor, output_schema)
	conn.commit()

	logger.info("Break Network")
	new_segments = break_network(conn, cursor, plan, srid)  # step 2
	logger.info("Break Network - done: %d new segment(s).", new_segments)

	logger.info("Compute Effective Length and Segment Gradient")
	compute_effective_length_and_gradient(cursor, output_schema)  # steps 3-4
	conn.commit()
	logger.info("Compute Effective Length and Segment Gradient - done.")

	logger.info("Compute Edge Statistics")
	species_params_by_code = load_species_params()
	# steps 5-9; also writes each barrier's stats to all_barriers.species_stats as it goes
	barriers_done = run_component_statistics(cursor, output_schema, plan, species_params_by_code)
	conn.commit()
	logger.info("Compute Edge Statistics - done: (%d barrier(s) processed).", barriers_done)

	logger.info("Rebuilding streams indexes")
	create_streams_bulk_write_indexes(cursor, output_schema)
	conn.commit()
	logger.info("Rebuilding streams indexes - done.")

	if plan["include_gradient_barriers"]:
		create_and_populate_gradient_barriers_cache(cursor, output_schema, srid)
	conn.commit()
	logger.info("Gradient barriers cache table: done.")
