"""Compute Statistics step 9 (fishpass/requirements/requirements.md): per-species upstream length
aggregates and per-barrier upstream/downstream counts (the latter now also carrying each barrier's
upstream length figures, read from the per-species length aggregates at the barrier's snapped
edge).

All length computations use effective_length, per requirements.md.

Weighted length (and functional weighted length) is only computed for the "rear" and "spawn"
lifecycles -- the species parameter file has no stream-order-weight fields for "spawnrear" (which
is itself a union of rear/spawn habitat, not its own habitat purpose), so no
weighted_upstream_length is produced for it.

The per-edge weighted length (<lc>_weighted_length) is degraded by the passability of the nearest
("first") downstream barrier of any type (graph_stats.compute_downstream_first_barrier_passability)
-- not a product across every downstream barrier. The weighted upstream aggregates
(<lc>_weighted_upstream_length, <lc>_functional_weighted_upstream_length) sum this same degraded,
habitat-masked per-edge value, rather than an undegraded one.
"""

from graph_stats import ACCESSIBILITY_ACCESSIBLE, propagate_upstream_multi, propagate_upstream_with_reset_multi
from species_params import stream_order_weight


def masked_local_value(edge_ids, effective_length, flag):
	return {eid: effective_length[eid] if flag.get(eid, False) else 0.0 for eid in edge_ids}


def downstream_degraded_weighted_length(edge_ids, effective_length, strahler_order, species_params, lifecycle, downstream_first_barrier_passability):
	"""{edge_id: effective_length * stream_order_weight * downstream_first_barrier_passability} --
	the base weighted-length formula (requirements.md step 9), degraded by each edge's nearest
	("first") downstream barrier of any type (graph_stats.
	compute_downstream_first_barrier_passability), independent of habitat_flag: this is a per-edge
	value, not an upstream aggregate, and is not masked to habitat edges. lifecycle must be "rear"
	or "spawn" -- there is no stream-order weight (and so no weighted length) for "spawnrear"."""

	return {
		eid: effective_length[eid] * stream_order_weight(species_params, lifecycle, strahler_order.get(eid)) * downstream_first_barrier_passability[eid]
		for eid in edge_ids
	}


def compute_species_length_stats(
	order_up, predecessors, edge_ids, effective_length, strahler_order,
	accessibility, habitat, barrier_here_by_species, species_params_by_code,
	reporting_species_lifecycles, downstream_first_barrier_passability,
):
	"""Returns {species: {"upstream_accessible_length": {edge_id: float},
	"<lc>_upstream_length", "<lc>_functional_upstream_length": {edge_id: float},
	"<lc>_weighted_upstream_length", "<lc>_functional_weighted_upstream_length",
	"<lc>_weighted_length": {edge_id: float} (rear/spawn only)}} for every (species, lifecycle) in
	reporting_species_lifecycles (lifecycle in "rear"/"spawn"/"spawnrear" -- "spawnrear" reuses
	habitat[species]["spawnrear"], already derived as rear OR spawn by habitat_access.py). No
	weighted-length fields are produced for "spawnrear".

	"<lc>_weighted_length" is a per-edge value (not an upstream aggregate, unlike the other
	fields here): effective_length * stream-order weight * that edge's nearest downstream barrier's
	passability (see downstream_degraded_weighted_length and graph_stats.
	compute_downstream_first_barrier_passability). downstream_first_barrier_passability is that
	function's output, {species: {"rear"/"spawn": {edge_id: float}}}.

	"<lc>_weighted_upstream_length" and "<lc>_functional_weighted_upstream_length" are upstream
	sums/resets of this same degraded per-edge weighted length, masked to habitat edges (i.e. they
	no longer sum an undegraded effective_length * stream-order-weight value -- each edge's
	contribution now also reflects its own nearest-downstream-barrier degradation)."""

	species_list = sorted({sp for sp, _lc in reporting_species_lifecycles})

	plain_zeros, plain_local = {}, {eid: {} for eid in edge_ids}
	reset_zeros, reset_local, reset_is_reset = {}, {eid: {} for eid in edge_ids}, {eid: {} for eid in edge_ids}
	lifecycles_by_species = {}
	weighted_length_by_species_lc = {}

	for species in species_list:
		accessible_local = {
			eid: effective_length[eid] if accessibility[species][eid] == ACCESSIBILITY_ACCESSIBLE else 0.0
			for eid in edge_ids
		}
		accessible_field = f"{species}:upstream_accessible_length"
		plain_zeros[accessible_field] = 0.0
		for eid in edge_ids:
			plain_local[eid][accessible_field] = accessible_local[eid]

		is_barrier = {
			eid: bool(barrier_here_by_species[species]["natural"].get(eid) or barrier_here_by_species[species]["anthro"].get(eid))
			for eid in edge_ids
		}
		lifecycles = {lc for sp, lc in reporting_species_lifecycles if sp == species}
		lifecycles_by_species[species] = lifecycles
		params = species_params_by_code[species]

		for lc in lifecycles:
			habitat_flag = habitat[species][lc]
			local = masked_local_value(edge_ids, effective_length, habitat_flag)
			has_weight = lc != "spawnrear"
			if has_weight:
				weighted_length = downstream_degraded_weighted_length(
					edge_ids, effective_length, strahler_order, params, lc,
					downstream_first_barrier_passability[species][lc],
				)
				weighted_length_by_species_lc[(species, lc)] = weighted_length
				weighted_local = {eid: weighted_length[eid] if habitat_flag.get(eid, False) else 0.0 for eid in edge_ids}

			up_field = f"{species}:{lc}_upstream_length"
			func_field = f"{species}:{lc}_functional_upstream_length"

			plain_zeros[up_field] = 0.0
			reset_zeros[func_field] = 0.0

			for eid in edge_ids:
				plain_local[eid][up_field] = local[eid]
				reset_local[eid][func_field] = local[eid]
				reset_is_reset[eid][func_field] = is_barrier[eid]

			if has_weight:
				weighted_field = f"{species}:{lc}_weighted_upstream_length"
				func_weighted_field = f"{species}:{lc}_functional_weighted_upstream_length"

				plain_zeros[weighted_field] = 0.0
				reset_zeros[func_weighted_field] = 0.0

				for eid in edge_ids:
					plain_local[eid][weighted_field] = weighted_local[eid]
					reset_local[eid][func_weighted_field] = weighted_local[eid]
					reset_is_reset[eid][func_weighted_field] = is_barrier[eid]

	plain_acc = propagate_upstream_multi(order_up, predecessors, plain_local, plain_zeros)
	reset_acc = propagate_upstream_with_reset_multi(order_up, predecessors, reset_local, reset_is_reset, reset_zeros)

	result = {}
	for species in species_list:
		species_result = {
			"upstream_accessible_length": {eid: v[f"{species}:upstream_accessible_length"] for eid, v in plain_acc.items()},
		}
		for lc in lifecycles_by_species[species]:
			species_result[f"{lc}_upstream_length"] = {eid: v[f"{species}:{lc}_upstream_length"] for eid, v in plain_acc.items()}
			species_result[f"{lc}_functional_upstream_length"] = {eid: v[f"{species}:{lc}_functional_upstream_length"] for eid, v in reset_acc.items()}
			if lc != "spawnrear":
				species_result[f"{lc}_weighted_upstream_length"] = {eid: v[f"{species}:{lc}_weighted_upstream_length"] for eid, v in plain_acc.items()}
				species_result[f"{lc}_functional_weighted_upstream_length"] = {eid: v[f"{species}:{lc}_functional_weighted_upstream_length"] for eid, v in reset_acc.items()}
				species_result[f"{lc}_weighted_length"] = weighted_length_by_species_lc[(species, lc)]
		result[species] = species_result
	return result


def compute_barrier_upstream_downstream_stats(barriers, barrier_stats, barrier_here_by_species, species_length_stats):
	"""requirements.md step 9's third bullet group: for each barrier and each species,
	upstream/downstream natural/anthropogenic counts (and downstream ids, per the doc -- only
	downstream ids are listed there, not upstream), plus that species' upstream length figures at
	the barrier. A barrier's own position is its downstream_edge_id; "upstream of the barrier"
	excludes the barrier's own contribution to that edge's upstream count (subtracting
	barrier_here_by_species for that specific species, which is 1 only if this position is
	actually impassable for that species -- not simply "this barrier's structure_type", since the
	same position may be passable for one species and not another). "downstream of the barrier" is
	that edge's downstream count as-is (already excludes the barrier's own position -- see
	graph_stats.py). The length fields have no analogous subtraction -- they're taken as-is from
	species_length_stats at the barrier's edge_id (already "upstream of and including this edge").

	Returns {barrier_id: {species: {upstream_natural_spawnrear_count, upstream_anthro_spawnrear_count,
	downstream_natural_spawnrear_count, downstream_anthro_spawnrear_count, downstream_natural_ids,
	downstream_anthro_ids, and the same upstream_/downstream_ counts split by structure type
	(natural/anthro) x lifestage (spawn/rear/spawnrear) -- 12 count fields total, plus
	upstream_accessible_length and, for each lifecycle that species reports,
	<lc>_upstream_length/<lc>_functional_upstream_length (+ weighted variants for rear/spawn)}}.
	barrier_stats is compute_barrier_stats' output, barrier_here_by_species is
	compute_barrier_here's output, species_length_stats is compute_species_length_stats' output;
	barriers is the same list passed to compute_barrier_here (needs "id" and "edge_id").
	"""

	count_keys = [
		(struct if lc == "spawnrear" else f"{struct}_{lc}", f"{struct}_{lc}")
		for struct in ("natural", "anthro")
		for lc in ("spawn", "rear", "spawnrear")
	]

	result = {}
	for b in barriers:
		barrier_id, edge_id = b["id"], b["edge_id"]
		per_species = {}
		for species, stats in barrier_stats.items():
			here = barrier_here_by_species[species]
			species_stats = {
				"downstream_natural_ids": stats["downstream_natural_ids"].get(edge_id, []),
				"downstream_anthro_ids": stats["downstream_anthro_ids"].get(edge_id, []),
			}
			for data_key, count_key in count_keys:
				species_stats[f"upstream_{count_key}_count"] = (
					stats.get(f"upstream_{count_key}_count", {}).get(edge_id, 0) - here.get(data_key, {}).get(edge_id, 0)
				)
				species_stats[f"downstream_{count_key}_count"] = stats.get(f"downstream_{count_key}_count", {}).get(edge_id, 0)

			length_stats = species_length_stats.get(species, {})
			for field, values in length_stats.items():
				species_stats[field] = values[edge_id]

			per_species[species] = species_stats
		result[barrier_id] = per_species
	return result
