"""Compute Statistics step 9 (fishpass/requirements/requirements.md): upstream length
aggregates and per-barrier upstream/downstream counts.

All length computations use effective_length, per requirements.md. Two sub-parts of this step
have no single-species anchor to resolve against (the per-lifecycle rollups aggregate "habitat
for at least one reporting species", independent of which species) -- see the module-level
"Outstanding Decisions" additions in requirements.md for the assumptions made here:
  * functional reset for the per-lifecycle rollup: an edge resets accumulation only if it is
    impassable for *every* reporting species (if even one species can still pass, the reach
    beyond it still counts toward "habitat for at least one species").
  * weighted length for the per-lifecycle rollup: averaged across reporting species' own
    stream_order_weight, since the weighting is inherently species-specific and there is no
    single species to anchor a shared value to.
"""

from graph_stats import propagate_upstream_multi, propagate_upstream_with_reset_multi
from species_params import stream_order_weight


def weighted_local_value(edge_ids, effective_length, strahler_order, habitat_flag, species_params):
	"""{edge_id: effective_length * stream_order_weight if habitat_flag else 0}."""
	return {
		eid: effective_length[eid] * stream_order_weight(species_params, strahler_order.get(eid))
		if habitat_flag.get(eid, False) else 0.0
		for eid in edge_ids
	}


def masked_local_value(edge_ids, effective_length, flag):
	return {eid: effective_length[eid] if flag.get(eid, False) else 0.0 for eid in edge_ids}


def compute_species_length_stats(
	order_up, predecessors, edge_ids, effective_length, strahler_order,
	accessibility, habitat, barrier_here_by_species, species_params_by_code,
	reporting_species_lifecycles,
):
	"""Returns {species: {"upstream_accessible_length": {edge_id: float},
	"<lc>_upstream_length", "<lc>_functional_upstream_length", "<lc>_weighted_upstream_length",
	"<lc>_functional_weighted_upstream_length": {edge_id: float}}} for every (species, lifecycle)
	in reporting_species_lifecycles (lifecycle in "rear"/"spawn"/"general" -- "general" reuses
	habitat[species]["general"], already derived as rear OR spawn by habitat_access.py)."""

	species_list = sorted({sp for sp, _lc in reporting_species_lifecycles})

	plain_zeros, plain_local = {}, {eid: {} for eid in edge_ids}
	reset_zeros, reset_local, reset_is_reset = {}, {eid: {} for eid in edge_ids}, {eid: {} for eid in edge_ids}
	lifecycles_by_species = {}

	for species in species_list:
		accessible_local = {
			eid: effective_length[eid] if accessibility[species][eid] in ("connected_naturally_accessible", "disconnected_naturally_accessible") else 0.0
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
			weighted_local = weighted_local_value(edge_ids, effective_length, strahler_order, habitat_flag, params)

			up_field = f"{species}:{lc}_upstream_length"
			weighted_field = f"{species}:{lc}_weighted_upstream_length"
			func_field = f"{species}:{lc}_functional_upstream_length"
			func_weighted_field = f"{species}:{lc}_functional_weighted_upstream_length"

			plain_zeros[up_field] = 0.0
			plain_zeros[weighted_field] = 0.0
			reset_zeros[func_field] = 0.0
			reset_zeros[func_weighted_field] = 0.0

			for eid in edge_ids:
				plain_local[eid][up_field] = local[eid]
				plain_local[eid][weighted_field] = weighted_local[eid]
				reset_local[eid][func_field] = local[eid]
				reset_local[eid][func_weighted_field] = weighted_local[eid]
				reset_is_reset[eid][func_field] = is_barrier[eid]
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
			species_result[f"{lc}_weighted_upstream_length"] = {eid: v[f"{species}:{lc}_weighted_upstream_length"] for eid, v in plain_acc.items()}
			species_result[f"{lc}_functional_upstream_length"] = {eid: v[f"{species}:{lc}_functional_upstream_length"] for eid, v in reset_acc.items()}
			species_result[f"{lc}_functional_weighted_upstream_length"] = {eid: v[f"{species}:{lc}_functional_weighted_upstream_length"] for eid, v in reset_acc.items()}
		result[species] = species_result
	return result


def compute_lifecycle_rollups(
	order_up, predecessors, edge_ids, effective_length, strahler_order,
	habitat, barrier_here_by_species, species_params_by_code, reporting_species_lifecycles,
):
	"""Returns {lifecycle: {"upstream_length", "functional_upstream_length",
	"weighted_upstream_length": {edge_id: float}}} for rear/spawn/general, aggregated across
	every reporting species that requested that lifecycle -- "habitat for at least one species"."""

	species_by_lifecycle = {}
	for sp, lc in reporting_species_lifecycles:
		species_by_lifecycle.setdefault(lc, set()).add(sp)

	plain_zeros, plain_local = {}, {eid: {} for eid in edge_ids}
	reset_zeros, reset_local, reset_is_reset = {}, {eid: {} for eid in edge_ids}, {eid: {} for eid in edge_ids}

	for lc, species_set in species_by_lifecycle.items():
		any_habitat = {
			eid: any(habitat[sp][lc].get(eid, False) for sp in species_set)
			for eid in edge_ids
		}
		local = masked_local_value(edge_ids, effective_length, any_habitat)

		# an edge only resets accumulation for this rollup if it's impassable for *every*
		# reporting species that wanted this lifecycle (see module docstring)
		is_barrier_for_all = {
			eid: all(
				barrier_here_by_species[sp]["natural"].get(eid) or barrier_here_by_species[sp]["anthro"].get(eid)
				for sp in species_set
			)
			for eid in edge_ids
		}

		avg_weighted_local = {
			eid: (
				effective_length[eid] * sum(
					stream_order_weight(species_params_by_code[sp], strahler_order.get(eid)) for sp in species_set
				) / len(species_set)
				if any_habitat.get(eid, False) else 0.0
			)
			for eid in edge_ids
		}

		up_field = f"{lc}:upstream_length"
		weighted_field = f"{lc}:weighted_upstream_length"
		func_field = f"{lc}:functional_upstream_length"
		plain_zeros[up_field] = 0.0
		plain_zeros[weighted_field] = 0.0
		reset_zeros[func_field] = 0.0
		for eid in edge_ids:
			plain_local[eid][up_field] = local[eid]
			plain_local[eid][weighted_field] = avg_weighted_local[eid]
			reset_local[eid][func_field] = local[eid]
			reset_is_reset[eid][func_field] = is_barrier_for_all[eid]

	plain_acc = propagate_upstream_multi(order_up, predecessors, plain_local, plain_zeros)
	reset_acc = propagate_upstream_with_reset_multi(order_up, predecessors, reset_local, reset_is_reset, reset_zeros)

	result = {}
	for lc in species_by_lifecycle:
		result[lc] = {
			"upstream_length": {eid: v[f"{lc}:upstream_length"] for eid, v in plain_acc.items()},
			"functional_upstream_length": {eid: v[f"{lc}:functional_upstream_length"] for eid, v in reset_acc.items()},
			"weighted_upstream_length": {eid: v[f"{lc}:weighted_upstream_length"] for eid, v in plain_acc.items()},
		}
	return result


def compute_barrier_upstream_downstream_stats(barriers, barrier_stats, barrier_here_by_species):
	"""requirements.md step 9's third bullet group: for each barrier and each species,
	upstream/downstream natural/anthropogenic counts (and downstream ids, per the doc -- only
	downstream ids are listed there, not upstream). A barrier's own position is its
	snapped_edge_id; "upstream of the barrier" excludes the barrier's own contribution to that
	edge's upstream count (subtracting barrier_here_by_species for that specific species, which
	is 1 only if this position is actually impassable for that species -- not simply "this
	barrier's structure_type", since the same position may be passable for one species and not
	another). "downstream of the barrier" is that edge's downstream count as-is (already
	excludes the barrier's own position -- see graph_stats.py).

	Returns {barrier_id: {species: {upstream_natural_count, upstream_anthro_count,
	downstream_natural_count, downstream_anthro_count, downstream_natural_ids,
	downstream_anthro_ids}}}. barrier_stats is compute_barrier_stats' output,
	barrier_here_by_species is compute_barrier_here's output; barriers is the same list passed
	to compute_barrier_here (needs "id" and "edge_id").
	"""

	result = {}
	for b in barriers:
		barrier_id, edge_id = b["id"], b["edge_id"]
		per_species = {}
		for species, stats in barrier_stats.items():
			here = barrier_here_by_species[species]
			per_species[species] = {
				"upstream_natural_count": stats["upstream_natural_count"].get(edge_id, 0) - here["natural"].get(edge_id, 0),
				"upstream_anthro_count": stats["upstream_anthro_count"].get(edge_id, 0) - here["anthro"].get(edge_id, 0),
				"downstream_natural_count": stats["downstream_natural_count"].get(edge_id, 0),
				"downstream_anthro_count": stats["downstream_anthro_count"].get(edge_id, 0),
				"downstream_natural_ids": stats["downstream_natural_ids"].get(edge_id, []),
				"downstream_anthro_ids": stats["downstream_anthro_ids"].get(edge_id, []),
			}
		result[barrier_id] = per_species
	return result
