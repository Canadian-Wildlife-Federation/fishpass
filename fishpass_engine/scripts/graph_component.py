"""DB I/O for Compute Statistics steps 5-9: fetch one graph_id connected component's data from
<output_schema>.streams/all_barriers/habitat_updates, run it through the pure engine
(graph_stats.py, habitat_access.py, length_stats.py), and assemble the per-edge JSON to write
back to streams.species_stats and the per-barrier JSON to write back to all_barriers.species_stats.

Caveat (see requirements.md's Outstanding Decisions): for an AOI-scoped run, a graph_id's edges
here are only whatever fell inside the requested AOI(s) during Load Stream Network -- this
module does not reach across into chyf_raw for the rest of a component that crosses an AOI
boundary. See requirements.md for the accuracy implication and recommendation.
"""

import json

import psycopg

from db import quote_ident
from graph_stats import (
	build_graph,
	compute_accessibility,
	compute_barrier_here,
	compute_barrier_stats,
	compute_downstream_first_barrier_passability,
	compute_habitat_assignment,
	compute_route_measures,
	downstream_order,
	upstream_order,
)
from habitat_access import apply_habitat_access_overrides, derive_spawnrear_habitat
from length_stats import (
	compute_barrier_upstream_downstream_stats,
	compute_species_length_stats,
)

STREAM_STAT_FIELDS = (
	"id", "from_nexus_id", "to_nexus_id", "mainstem_id",
	"length", "effective_length", "segment_gradient", "strahler_order",
)


def fetch_graph_id_counts(cursor, output_schema):
	"""(graph_id, edge_count) for every graph_id among non-isolated streams edges, largest first --
	the unit of work for Compute Statistics steps 5-9 (requirements.md: graph_id groups with
	is_isolated edges are skipped entirely). Ordering largest-first lets build_graph_id_bundles
	isolate big components into their own bundle before packing small ones together."""

	cursor.execute(f"""
		SELECT graph_id, COUNT(*) FROM {quote_ident(output_schema)}.streams
		WHERE graph_id IS NOT NULL AND (is_isolated IS NULL OR is_isolated = false)
		GROUP BY graph_id
		ORDER BY COUNT(*) DESC
	""")
	return [(row[0], row[1]) for row in cursor.fetchall()]


def build_graph_id_bundles(graph_id_counts, max_bundle_edges):
	"""Greedily pack (graph_id, edge_count) pairs (as returned by fetch_graph_id_counts, largest
	first) into bundles whose total edge_count stays within max_bundle_edges. A component whose
	own count already meets or exceeds the budget always starts (and, since nothing more can be
	added, ends) its own bundle rather than blocking forever. Returns list[list[graph_id]]."""

	bundles = []
	current_bundle = []
	current_total = 0

	for graph_id, count in graph_id_counts:
		if current_bundle and current_total + count > max_bundle_edges:
			bundles.append(current_bundle)
			current_bundle = []
			current_total = 0
		current_bundle.append(graph_id)
		current_total += count

	if current_bundle:
		bundles.append(current_bundle)

	return bundles


def fetch_bundle_edges(cursor, output_schema, graph_ids):
	"""{graph_id: [edge dicts]} for every edge in any of graph_ids -- bundle-scoped bulk version
	of fetching one component's edges at a time."""

	cols = ", ".join(STREAM_STAT_FIELDS)
	cursor.execute(
		f"SELECT graph_id, {cols} FROM {quote_ident(output_schema)}.streams WHERE graph_id = ANY(%s)",
		(list(graph_ids),),
	)
	by_graph = {}
	for row in cursor.fetchall():
		graph_id, edge_row = row[0], row[1:]
		by_graph.setdefault(graph_id, []).append(dict(zip(STREAM_STAT_FIELDS, edge_row)))
	return by_graph


def fetch_bundle_barriers(cursor, output_schema, graph_ids):
	"""{graph_id: [barrier dicts]} for every barrier snapped onto an edge in any of graph_ids."""

	schema_ident = quote_ident(output_schema)
	cursor.execute(
		f"""
		SELECT e.graph_id, s.id, s.downstream_edge_id, s.species_passability_value, s.structure_type
		FROM {schema_ident}.all_barriers s
		JOIN {schema_ident}.streams e ON e.id = s.downstream_edge_id
		WHERE e.graph_id = ANY(%s)
		""",
		(list(graph_ids),),
	)
	by_graph = {}
	for graph_id, *row in cursor.fetchall():
		by_graph.setdefault(graph_id, []).append(
			{"id": row[0], "edge_id": row[1], "species_passability_value": row[2], "structure_type": row[3]}
		)
	return by_graph


def fetch_bundle_habitat_updates(cursor, output_schema, graph_ids):
	"""{graph_id: [habitat update dicts]} for every habitat_updates row whose upstream or
	downstream snapped edge falls in any of graph_ids. Each graph_id's list stays ordered by
	update_date ascending -- habitat_access.apply_habitat_access_overrides requires this so later
	rows win on overlap (see its module docstring) -- because the single query below is ordered
	that way and grouping preserves fetchall()'s row order within each graph_id bucket. A row
	whose two endpoints fall in different components is attached under both graph_ids."""

	schema_ident = quote_ident(output_schema)
	graph_id_list = list(graph_ids)
	cursor.execute(
		f"""
		SELECT su.graph_id, sd.graph_id, hu.id, hu.species_lifestage, hu.location_type,
			hu.upstream_snapped_edge_id, hu.downstream_snapped_edge_id, hu.update_date
		FROM {schema_ident}.habitat_updates hu
		LEFT JOIN {schema_ident}.streams su ON su.id = hu.upstream_snapped_edge_id
		LEFT JOIN {schema_ident}.streams sd ON sd.id = hu.downstream_snapped_edge_id
		WHERE su.graph_id = ANY(%s) OR sd.graph_id = ANY(%s)
		ORDER BY hu.update_date ASC NULLS FIRST
		""",
		(graph_id_list, graph_id_list),
	)
	by_graph = {}
	for up_graph_id, down_graph_id, *row in cursor.fetchall():
		update = {
			"id": row[0], "species_lifestage": row[1], "location_type": row[2],
			"upstream_snapped_edge_id": row[3], "downstream_snapped_edge_id": row[4],
		}
		for graph_id in {up_graph_id, down_graph_id} - {None}:
			by_graph.setdefault(graph_id, []).append(update)
	return by_graph


def assemble_edge_json(edge_ids, reporting_species_lifecycles, accessibility, barrier_stats, habitat, species_length_stats):
	"""Returns species_stats, {edge_id: {...}} ready for json.dumps, matching requirements.md's
	Outputs section fields for <output_schema>.streams. Upstream length fields (accessible
	length, and per-lifecycle upstream/functional upstream/weighted upstream/functional weighted
	upstream length) live on barriers instead (see
	length_stats.compute_barrier_upstream_downstream_stats) -- but the per-edge, non-aggregate
	"<lc>_weighted_length" (rear/spawn only) IS written here too, since unlike the aggregates it's
	already a per-edge quantity (see length_stats.compute_species_length_stats)."""

	species_lifecycles = {}
	for sp, lc in reporting_species_lifecycles:
		species_lifecycles.setdefault(sp, set()).add(lc)

	species_stats = {}
	for eid in edge_ids:
		entry = {}
		for species, lifecycles in species_lifecycles.items():
			s = {
				"accessibility": accessibility[species][eid],
				"upstream_anthro_spawnrear_count": barrier_stats[species]["upstream_anthro_spawnrear_count"][eid],
				"upstream_anthro_spawn_count": barrier_stats[species]["upstream_anthro_spawn_count"][eid],
				"upstream_anthro_rear_count": barrier_stats[species]["upstream_anthro_rear_count"][eid],
				"downstream_anthro_spawnrear_count": barrier_stats[species]["downstream_anthro_spawnrear_count"][eid],
				"downstream_anthro_spawn_count": barrier_stats[species]["downstream_anthro_spawn_count"][eid],
				"downstream_anthro_rear_count": barrier_stats[species]["downstream_anthro_rear_count"][eid],
				"upstream_natural_spawnrear_count": barrier_stats[species]["upstream_natural_spawnrear_count"][eid],
				"upstream_natural_spawn_count": barrier_stats[species]["upstream_natural_spawn_count"][eid],
				"upstream_natural_rear_count": barrier_stats[species]["upstream_natural_rear_count"][eid],
				"downstream_natural_spawnrear_count": barrier_stats[species]["downstream_natural_spawnrear_count"][eid],
				"downstream_natural_spawn_count": barrier_stats[species]["downstream_natural_spawn_count"][eid],
				"downstream_natural_rear_count": barrier_stats[species]["downstream_natural_rear_count"][eid],
				"upstream_anthro_ids": barrier_stats[species]["upstream_anthro_ids"][eid],
				"downstream_anthro_ids": barrier_stats[species]["downstream_anthro_ids"][eid],
				"rear_habitat": habitat[species]["rear"][eid],
				"spawn_habitat": habitat[species]["spawn"][eid],
				"spawnrear_habitat": habitat[species]["spawnrear"][eid],
			}
			for lc in ("rear", "spawn"):
				if lc in lifecycles:
					s[f"{lc}_weighted_length"] = species_length_stats[species][f"{lc}_weighted_length"][eid]
			entry[species] = s
		species_stats[eid] = entry

	return species_stats


def build_stats_write_rows(species_stats, route_measures):
	"""(species_json, downstream_route_measure, upstream_route_measure, edge_id) tuples for
	flush_stats_writes, one per edge in species_stats (as returned by assemble_edge_json).
	route_measures is compute_route_measures' {edge_id: (downstream, upstream)} result -- an edge
	missing from it (no mainstem_id) writes NULL for both measure columns."""

	return [
		(json.dumps(species_stats[eid], default=str), *route_measures.get(eid, (None, None)), eid)
		for eid in species_stats
	]


def flush_stats_writes(cursor, output_schema, rows):
	"""One UPDATE statement for the whole batch via UNNEST over parallel arrays, rather than one
	statement per row -- lets Postgres plan and execute a single join against streams instead of
	up to WRITE_BATCH_SIZE separate single-row UPDATEs."""

	if not rows:
		return
	schema_ident = quote_ident(output_schema)
	species_json, downstream_measures, upstream_measures, edge_ids = zip(*rows)
	cursor.execute(
		f"""
		UPDATE {schema_ident}.streams AS s
		SET species_stats = v.species_stats::jsonb,
			downstream_route_measure = v.downstream_route_measure,
			upstream_route_measure = v.upstream_route_measure
		FROM (
			SELECT * FROM UNNEST(%s::jsonb[], %s::double precision[], %s::double precision[], %s::uuid[])
				AS v(species_stats, downstream_route_measure, upstream_route_measure, id)
		) AS v
		WHERE s.id = v.id
		""",
		(list(species_json), list(downstream_measures), list(upstream_measures), list(edge_ids)),
	)


def process_component(graph_id, edges, barriers, habitat_rows, plan, species_params_by_code):
	"""Run Compute Statistics steps 5-9 for one graph_id component's already-fetched data (edges,
	barriers, habitat_rows -- see fetch_bundle_edges/fetch_bundle_barriers/
	fetch_bundle_habitat_updates). Returns (species_stats, barrier_rows, route_measures):
	species_stats and route_measures are both ready for build_stats_write_rows, and barrier_rows
	is barriers annotated with "stats" (for write_barrier_tables to use as the working set for the
	natural_barriers/anthropogenic_barriers output tables -- including each barrier's upstream
	length figures)."""

	edges_by_id = {e["id"]: e for e in edges}
	edge_ids = list(edges_by_id.keys())

	successor, predecessors, roots = build_graph(edges)
	order_up = upstream_order(predecessors, roots)
	order_down = downstream_order(order_up)
	route_measures = compute_route_measures(edges_by_id, predecessors, successor)

	reporting_species_lifecycles = plan["reporting_species_lifecycles"]
	species_list = sorted({sp for sp, _lc in reporting_species_lifecycles})
	impassable_threshold = plan["impassable_threshold"]

	barrier_here = compute_barrier_here(edge_ids, barriers, species_list, impassable_threshold)
	barrier_stats = compute_barrier_stats(order_up, order_down, predecessors, successor, barrier_here)
	accessibility = compute_accessibility(edge_ids, barrier_stats)

	edge_gradient = {eid: edges_by_id[eid]["segment_gradient"] for eid in edge_ids}
	edge_strahler = {eid: edges_by_id[eid]["strahler_order"] for eid in edge_ids}
	habitat = compute_habitat_assignment(edge_ids, species_list, accessibility, edge_gradient, edge_strahler, species_params_by_code)

	apply_habitat_access_overrides(habitat, edges_by_id, predecessors, successor, habitat_rows)
	derive_spawnrear_habitat(habitat)

	effective_length = {eid: edges_by_id[eid]["effective_length"] for eid in edge_ids}
	downstream_first_barrier_passability = compute_downstream_first_barrier_passability(
		edge_ids, order_down, successor, barriers, species_list,
	)
	species_length_stats = compute_species_length_stats(
		order_up, predecessors, edge_ids, effective_length, edge_strahler,
		accessibility, habitat, barrier_here, species_params_by_code,
		reporting_species_lifecycles, downstream_first_barrier_passability,
	)
	species_stats_json = assemble_edge_json(
		edge_ids, reporting_species_lifecycles, accessibility, barrier_stats, habitat, species_length_stats,
	)

	barrier_stats_by_id = compute_barrier_upstream_downstream_stats(barriers, barrier_stats, barrier_here, species_length_stats)
	barrier_rows = [{**b, "stats": barrier_stats_by_id[b["id"]]} for b in barriers]
	return species_stats_json, barrier_rows, route_measures
