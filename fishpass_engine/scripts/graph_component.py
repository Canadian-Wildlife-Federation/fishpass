"""DB I/O for Compute Statistics steps 5-9: fetch one graph_id connected component's data from
<output_schema>.streams/all_structures/habitat_updates, run it through the pure engine
(graph_stats.py, habitat_access.py, length_stats.py), and assemble the per-edge JSON to write
back to streams.species_stats/lifecycle_stats.

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
	compute_habitat_assignment,
	downstream_order,
	upstream_order,
)
from habitat_access import apply_habitat_access_overrides, derive_general_habitat
from length_stats import (
	compute_barrier_upstream_downstream_stats,
	compute_lifecycle_rollups,
	compute_species_length_stats,
)

STREAM_STAT_FIELDS = (
	"id", "from_nexus_id", "to_nexus_id", "mainstem_id",
	"effective_length", "segment_gradient", "strahler_order",
)


def fetch_graph_ids(cursor, output_schema):
	"""Every distinct graph_id among non-isolated streams edges -- the unit of work for Compute
	Statistics steps 5-9 (requirements.md: graph_id groups with is_isolated edges are skipped
	entirely)."""

	cursor.execute(f"""
		SELECT DISTINCT graph_id FROM {quote_ident(output_schema)}.streams
		WHERE graph_id IS NOT NULL AND (is_isolated IS NULL OR is_isolated = false)
	""")
	return [row[0] for row in cursor.fetchall()]


def fetch_component_edges(cursor, output_schema, graph_id):
	cols = ", ".join(STREAM_STAT_FIELDS)
	cursor.execute(
		f"SELECT {cols} FROM {quote_ident(output_schema)}.streams WHERE graph_id = %s",
		(graph_id,),
	)
	return [dict(zip(STREAM_STAT_FIELDS, row)) for row in cursor.fetchall()]


def fetch_component_barriers(cursor, output_schema, edge_ids):
	cursor.execute(
		f"""
		SELECT id, snapped_edge_id, species_passability_value, structure_type
		FROM {quote_ident(output_schema)}.all_structures
		WHERE snapped_edge_id = ANY(%s)
		""",
		(edge_ids,),
	)
	return [
		{"id": row[0], "edge_id": row[1], "species_passability_value": row[2], "structure_type": row[3]}
		for row in cursor.fetchall()
	]


def fetch_component_habitat_updates(cursor, output_schema, edge_ids):
	"""Ordered by update_date ascending -- habitat_access.apply_habitat_access_overrides
	requires this so later rows win on overlap (see its module docstring)."""

	cursor.execute(
		f"""
		SELECT id, species_lifestage, location_type, upstream_snapped_edge_id,
			downstream_snapped_edge_id, update_date
		FROM {quote_ident(output_schema)}.habitat_updates
		WHERE upstream_snapped_edge_id = ANY(%s) OR downstream_snapped_edge_id = ANY(%s)
		ORDER BY update_date ASC NULLS FIRST
		""",
		(edge_ids, edge_ids),
	)
	return [
		{
			"id": row[0], "species_lifestage": row[1], "location_type": row[2],
			"upstream_snapped_edge_id": row[3], "downstream_snapped_edge_id": row[4],
		}
		for row in cursor.fetchall()
	]


def assemble_edge_json(edge_ids, reporting_species_lifecycles, accessibility, barrier_stats, habitat, species_length_stats, lifecycle_rollups):
	"""Returns (species_stats, lifecycle_stats), each {edge_id: {...}} ready for json.dumps,
	matching requirements.md's Outputs section fields for <output_schema>.streams."""

	species_lifecycles = {}
	for sp, lc in reporting_species_lifecycles:
		species_lifecycles.setdefault(sp, set()).add(lc)

	species_stats = {}
	for eid in edge_ids:
		entry = {}
		for species, lifecycles in species_lifecycles.items():
			s = {
				"accessibility": accessibility[species][eid],
				"upstream_anthro_count": barrier_stats[species]["upstream_anthro_count"][eid],
				"downstream_anthro_count": barrier_stats[species]["downstream_anthro_count"][eid],
				"upstream_natural_count": barrier_stats[species]["upstream_natural_count"][eid],
				"downstream_natural_count": barrier_stats[species]["downstream_natural_count"][eid],
				"upstream_anthro_ids": barrier_stats[species]["upstream_anthro_ids"][eid],
				"downstream_anthro_ids": barrier_stats[species]["downstream_anthro_ids"][eid],
				"rear_habitat": habitat[species]["rear"][eid],
				"spawn_habitat": habitat[species]["spawn"][eid],
				"general_habitat": habitat[species]["general"][eid],
				"upstream_accessible_length": species_length_stats[species]["upstream_accessible_length"][eid],
			}
			for lc in lifecycles:
				for field in (
					"upstream_length", "functional_upstream_length",
					"weighted_upstream_length", "functional_weighted_upstream_length",
				):
					s[f"{lc}_{field}"] = species_length_stats[species][f"{lc}_{field}"][eid]
			entry[species] = s
		species_stats[eid] = entry

	lifecycle_stats = {}
	for eid in edge_ids:
		entry = {}
		for lc, data in lifecycle_rollups.items():
			for field in ("upstream_length", "functional_upstream_length", "weighted_upstream_length"):
				entry[f"{lc}_{field}"] = data[field][eid]
		lifecycle_stats[eid] = entry

	return species_stats, lifecycle_stats


def write_streams_stats(cursor, output_schema, species_stats, lifecycle_stats):
	schema_ident = quote_ident(output_schema)
	rows = [
		(json.dumps(species_stats[eid], default=str), json.dumps(lifecycle_stats[eid], default=str), eid)
		for eid in species_stats
	]
	
	cursor.executemany(
		f"""
		UPDATE {schema_ident}.streams AS s
		SET species_stats = v.species_stats::jsonb, lifecycle_stats = v.lifecycle_stats::jsonb
		FROM (VALUES (%s::jsonb, %s::jsonb, %s::uuid)) AS v(species_stats, lifecycle_stats, id)
		WHERE s.id = v.id
		""",
		rows
	)


def process_component(cursor, output_schema, graph_id, plan, species_params_by_code):
	"""Run Compute Statistics steps 5-9 for one graph_id component and write results back to
	<output_schema>.streams. Returns the number of barriers processed (for
	write_barrier_tables to use as the working set for the natural_barriers/
	anthropogenic_barriers output tables)."""

	edges = fetch_component_edges(cursor, output_schema, graph_id)
	if not edges:
		return []
	edges_by_id = {e["id"]: e for e in edges}
	edge_ids = list(edges_by_id.keys())

	successor, predecessors, roots = build_graph(edges)
	order_up = upstream_order(predecessors, roots)
	order_down = downstream_order(order_up)

	barriers = fetch_component_barriers(cursor, output_schema, edge_ids)
	reporting_species_lifecycles = plan["reporting_species_lifecycles"]
	species_list = sorted({sp for sp, _lc in reporting_species_lifecycles})
	impassable_threshold = plan["impassable_threshold"]

	barrier_here = compute_barrier_here(edge_ids, barriers, species_list, impassable_threshold)
	barrier_stats = compute_barrier_stats(order_up, order_down, predecessors, successor, barrier_here)
	accessibility = compute_accessibility(edge_ids, barrier_stats)

	edge_gradient = {eid: edges_by_id[eid]["segment_gradient"] for eid in edge_ids}
	edge_strahler = {eid: edges_by_id[eid]["strahler_order"] for eid in edge_ids}
	habitat = compute_habitat_assignment(edge_ids, species_list, accessibility, edge_gradient, edge_strahler, species_params_by_code)

	habitat_rows = fetch_component_habitat_updates(cursor, output_schema, edge_ids)
	apply_habitat_access_overrides(habitat, edges_by_id, predecessors, successor, habitat_rows)
	derive_general_habitat(habitat)

	effective_length = {eid: edges_by_id[eid]["effective_length"] for eid in edge_ids}
	species_length_stats = compute_species_length_stats(
		order_up, predecessors, edge_ids, effective_length, edge_strahler,
		accessibility, habitat, barrier_here, species_params_by_code,
		reporting_species_lifecycles,
	)
	lifecycle_rollups = compute_lifecycle_rollups(
		order_up, predecessors, edge_ids, effective_length, edge_strahler,
		habitat, barrier_here, species_params_by_code, reporting_species_lifecycles,
	)

	species_stats_json, lifecycle_stats_json = assemble_edge_json(
		edge_ids, reporting_species_lifecycles, accessibility, barrier_stats,
		habitat, species_length_stats, lifecycle_rollups,
	)
	write_streams_stats(cursor, output_schema, species_stats_json, lifecycle_stats_json)

	barrier_stats_by_id = compute_barrier_upstream_downstream_stats(barriers, barrier_stats, barrier_here)
	return [{**b, "stats": barrier_stats_by_id[b["id"]]} for b in barriers]
