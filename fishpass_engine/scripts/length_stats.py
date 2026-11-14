"""Compute Statistics step 9 (fishpass/docs/fishpass_docs.md): per-species upstream length
aggregates and per-barrier upstream/downstream counts (the latter now also carrying each barrier's
upstream length figures).

Every barrier upstream length is read as the sum of the running totals of every edge flowing into
the barrier's downstream_edge_id (its predecessors): one inflowing edge gives that edge's total, a
confluence gives the sum of all its branches, and a headwater (none) gives 0.0.

All length computations use effective_length, except two whose length is set by
UPSTREAM_STREAM_LENGTH_COLUMN (raw length or effective_length; raw length for now):
`upstream_stream_length` (every upstream edge, no filter, one value per barrier rather than per
species) and `<lc>_upstream_below_gradient_threshold_length` (upstream edges whose
segment_gradient is within that species' accessibility gradient threshold for spawn/rear). Both
are plain sums to the headwaters (no barrier stops them).

The per-edge `<lc>_weighted_length` value (and the raw stream-order-weight formula behind it) is
only computed for the "rear" and "spawn" lifecycles -- the species parameter file has no
stream-order-weight fields for "spawnrear" (which is itself a union of rear/spawn habitat, not its
own habitat purpose).

`<lc>_weighted_length` is the *base* value -- effective_length * stream-order weight, zeroed out
for an edge that is not <lc> habitat or not naturally accessible for that same lifecycle (that
edge's own <lc>-specific accessibility -- spawn_accessibility for spawn, rear_accessibility for
rear; see graph_stats.compute_accessibility). It carries no barrier degradation. Two more per-edge
fields split that base value by the passability of the nearest ("first") downstream anthropogenic
barrier that is not fully passable (graph_stats.compute_downstream_first_anthropogenic_barrier_
passability -- natural barriers, and fully-passable anthropogenic barriers, are skipped over when
searching downstream, and this is not a product across every downstream barrier):
`<lc>_weighted_connected_length` = base * that passability, `<lc>_weighted_disconnected_length` =
base * (1 - that passability).

The barrier-level upstream aggregates (`<lc>_weighted_connected_upstream_length`,
`<lc>_weighted_disconnected_upstream_length`, `<lc>_functional_weighted_connected_upstream_length`,
`<lc>_functional_weighted_disconnected_upstream_length`) sum the *base* (undegraded) per-edge
weighted length upstream of a barrier -- plain sum, and reset-at-first-non-passable-anthropogenic-
barrier sum, same two propagations as the unweighted upstream-length pair -- and then multiply that
sum by the barrier's *own* raw passability for that species/lifestage (and its complement),
computed in compute_barrier_upstream_downstream_stats. This is a different passability value than
the downstream-chain one used for the per-edge connected/disconnected split above: it's specific to
this one barrier, not the nearest-downstream-barrier lookup. For "spawnrear", both the per-edge
base value (`<lc>_weighted_length` is not itself produced for spawnrear, but the summed base used
here) and the upstream aggregates sum, per edge, the maximum of that edge's rear/spawn base weighted
length, and the barrier's own passability is min(spawn_passability, rear_passability) -- matching
graph_stats.is_impassable's combined "impassable if either lifestage fails" rule.
"""

from graph_stats import ACCESSIBILITY_ACCESSIBLE, propagate_upstream_multi, propagate_upstream_with_reset_multi
from species_params import accessibility_gradient_ok, stream_order_weight
from utils import LIFESTAGES, LIFESTAGES_ALL, REAR, SPAWN, SPAWNREAR

UPSTREAM_STREAM_LENGTH = "upstream_stream_length"
BELOW_GRADIENT_THRESHOLD_SUFFIX = "_upstream_below_gradient_threshold_length"

# Streams length summed for ONLY these two barrier stats:
#   - upstream_stream_length (all_barriers column)
#   - <lc>_upstream_below_gradient_threshold_length (species_stats)
# "length" uses the raw stream length (the raw_stream_length parameter of compute_length_stats);
# any other value uses effective_length instead.
# It is NOT used by any other length statistic -- habitat, accessible, functional and
# weighted lengths always use effective_length, and are not affected by this setting.
UPSTREAM_STREAM_LENGTH_COLUMN = "length"


def masked_local_value(edge_ids, effective_length, flag):
	return {eid: effective_length[eid] if flag.get(eid, False) else 0.0 for eid in edge_ids}


def base_weighted_length(
	edge_ids, effective_length, strahler_order, species_params, lifecycle, habitat_flag, accessible
):
	"""{edge_id: effective_length * stream_order_weight} -- the base weighted-length formula (step
	9), with no barrier degradation applied. This is a per-edge value, not an upstream aggregate.
	The result is 0.0 for an edge that is not <lifecycle> habitat (habitat_flag) or whose
	accessibility for that same lifecycle is not naturally accessible (accessible) -- otherwise the
	formula above applies. lifecycle must be "rear" or "spawn" -- there is no stream-order weight
	(and so no weighted length) for "spawnrear"."""

	return {
		eid: (
			effective_length[eid] * stream_order_weight(species_params, lifecycle, strahler_order.get(eid))
			if habitat_flag.get(eid, False) and accessible.get(eid, False)
			else 0.0
		)
		for eid in edge_ids
	}


def sum_inflowing(predecessors, edge_id, totals_by_edge):
	"""Everything upstream of a barrier: the sum of the running totals (totals_by_edge,
	{edge_id: float}) of every edge flowing into the start of edge_id, the barrier's
	downstream_edge_id. One inflowing edge gives that edge's total, a confluence adds every
	branch, and a headwater (none) gives 0.0. The single rule for every barrier upstream value --
	the per-species lengths here and upstream_stream_length in graph_component."""
	return sum((totals_by_edge[p] for p in predecessors.get(edge_id, [])), 0.0)


def connected_disconnected_weighted_length(base_weighted, downstream_first_barrier_passability):
	"""Splits a per-edge base_weighted_length dict into (connected, disconnected) dicts using each
	edge's nearest ("first") downstream anthropogenic barrier that is not fully passable
	(graph_stats.compute_downstream_first_anthropogenic_barrier_passability) -- natural barriers,
	and fully-passable anthropogenic barriers, are skipped over when searching downstream.
	connected[eid] = base_weighted[eid] * downstream_first_barrier_passability[eid];
	disconnected[eid] = base_weighted[eid] * (1 - downstream_first_barrier_passability[eid])."""

	connected = {eid: base_weighted[eid] * downstream_first_barrier_passability[eid] for eid in base_weighted}
	disconnected = {eid: base_weighted[eid] * (1 - downstream_first_barrier_passability[eid]) for eid in base_weighted}
	return connected, disconnected


def compute_length_stats(
	order_up,
	predecessors,
	edge_ids,
	effective_length,
	strahler_order,
	accessibility,
	habitat,
	barrier_here_by_species,
	species_params_by_code,
	reporting_species_lifecycles,
	downstream_first_barrier_passability,
	raw_stream_length,
	segment_gradient,
):
	"""Returns (species_result, upstream_stream_length_by_edge).

	raw_stream_length: {edge_id: length}. Each edge's own raw stream length. Used only for
	upstream_stream_length and <lc>_upstream_below_gradient_threshold_length, and only while
	UPSTREAM_STREAM_LENGTH_COLUMN is "length"; otherwise those two use effective_length. Every
	other length field here always uses effective_length.

	segment_gradient: {edge_id: gradient or None}. Each edge's gradient, used to decide whether the
	edge counts toward <lc>_upstream_below_gradient_threshold_length.

	upstream_stream_length_by_edge is {edge_id: float}: that edge's length plus the length of every
	edge above it, with no filter. It is kept outside species_result because it is not per species.

	species_result is {species: {"spawn_upstream_accessible_length": {edge_id: float},
	"rear_upstream_accessible_length": {edge_id: float},
	"spawn_upstream_below_gradient_threshold_length",
	"rear_upstream_below_gradient_threshold_length": {edge_id: float} (running totals, including
	the edge itself, of the length of edges whose segment_gradient passes
	species_params.accessibility_gradient_ok -- computed for every species, for both spawn and
	rear, like the accessible lengths),
	"<lc>_upstream_length", "<lc>_functional_upstream_length": {edge_id: float},
	"<lc>_weighted_length", "<lc>_weighted_connected_length", "<lc>_weighted_disconnected_length":
	{edge_id: float} (rear/spawn only),
	"<lc>_weighted_upstream_length_base", "<lc>_functional_weighted_upstream_length_base":
	{edge_id: float} (intermediate, base-weighted-length upstream sums -- consumed by
	compute_barrier_upstream_downstream_stats to derive the connected/disconnected upstream-length
	barrier fields; not a public output field)}} for every (species, lifecycle) in
	reporting_species_lifecycles (lifecycle in "rear"/"spawn"/"spawnrear" -- "spawnrear" reuses
	habitat[species]["spawnrear"], already derived as rear OR spawn by habitat_access.py). No raw
	"<lc>_weighted_length"/"<lc>_weighted_connected_length"/"<lc>_weighted_disconnected_length"
	field is produced for "spawnrear" (there is no spawnrear stream-order weight), but the
	"_base" upstream aggregates ARE, from the rear/spawn per-edge maximum -- see below.

	accessibility is graph_stats.compute_accessibility's output, {species: {"spawn"/"rear":
	{edge_id: accessibility_string}}} -- spawn_upstream_accessible_length and rear/spawn weighted
	length masking each use their own lifecycle's accessibility value, independently of the other.

	"<lc>_weighted_length" is a per-edge value (not an upstream aggregate, unlike the other
	fields here): effective_length * stream-order weight (see base_weighted_length), zeroed out
	for an edge that is not <lc> habitat or not naturally accessible for that same lifecycle. It
	carries no barrier degradation. "<lc>_weighted_connected_length"/"<lc>_weighted_disconnected_length"
	split that base value by that edge's nearest downstream, not-fully-passable anthropogenic
	barrier's passability (see connected_disconnected_weighted_length and graph_stats.
	compute_downstream_first_anthropogenic_barrier_passability). downstream_first_barrier_passability
	is that function's output, {species: {"rear"/"spawn": {edge_id: float}}}.

	For "rear"/"spawn", "<lc>_weighted_upstream_length_base" and
	"<lc>_functional_weighted_upstream_length_base" are upstream sums/resets of the *base*
	(undegraded) per-edge weighted length. For "spawnrear", they are upstream sums/resets of, per
	edge, max(rear's base weighted length, spawn's base weighted length) -- rear and spawn's
	per-edge base weighted lengths are computed for this purpose even when "rear"/"spawn"
	themselves aren't in reporting_species_lifecycles for this species."""

	species_list = sorted({sp for sp, _lc in reporting_species_lifecycles})

	plain_zeros, plain_local = {}, {eid: {} for eid in edge_ids}
	reset_zeros, reset_local, reset_is_reset = {}, {eid: {} for eid in edge_ids}, {eid: {} for eid in edge_ids}
	lifecycles_by_species = {}
	weighted_length_by_species_lc = {}
	connected_by_species_lc = {}
	disconnected_by_species_lc = {}

	# Length used for upstream_stream_length and the gradient-threshold lengths only.
	if UPSTREAM_STREAM_LENGTH_COLUMN == "length":
		upstream_stream_lengths = raw_stream_length
	else:
		upstream_stream_lengths = effective_length

	# Not per species: every edge, no filter.
	plain_zeros[UPSTREAM_STREAM_LENGTH] = 0.0
	for eid in edge_ids:
		plain_local[eid][UPSTREAM_STREAM_LENGTH] = upstream_stream_lengths[eid]

	for species in species_list:
		accessible_by_lc = {
			lc: {eid: accessibility[species][lc][eid] == ACCESSIBILITY_ACCESSIBLE for eid in edge_ids}
			for lc in LIFESTAGES
		}
		for lc_key, accessible_bool in accessible_by_lc.items():
			accessible_local = {eid: effective_length[eid] if accessible_bool[eid] else 0.0 for eid in edge_ids}
			accessible_field = f"{species}:{lc_key}_upstream_accessible_length"
			plain_zeros[accessible_field] = 0.0
			for eid in edge_ids:
				plain_local[eid][accessible_field] = accessible_local[eid]

		params = species_params_by_code[species]
		for lc_key in LIFESTAGES:
			gradient_field = f"{species}:{lc_key}{BELOW_GRADIENT_THRESHOLD_SUFFIX}"
			plain_zeros[gradient_field] = 0.0
			for eid in edge_ids:
				if accessibility_gradient_ok(params, lc_key, segment_gradient.get(eid)):
					plain_local[eid][gradient_field] = upstream_stream_lengths[eid]
				else:
					plain_local[eid][gradient_field] = 0.0

		is_barrier = {eid: bool(barrier_here_by_species[species]["anthro"].get(eid)) for eid in edge_ids}
		lifecycles = {lc for sp, lc in reporting_species_lifecycles if sp == species}
		lifecycles_by_species[species] = lifecycles

		rear_spawn_base_weighted = {}
		if lifecycles & set(LIFESTAGES_ALL):
			for base_lc in LIFESTAGES:
				rear_spawn_base_weighted[base_lc] = base_weighted_length(
					edge_ids,
					effective_length,
					strahler_order,
					params,
					base_lc,
					habitat[species][base_lc],
					accessible_by_lc[base_lc],
				)

		for lc in lifecycles:
			habitat_flag = habitat[species][lc]
			local = masked_local_value(edge_ids, effective_length, habitat_flag)
			if lc in LIFESTAGES:
				weighted_length = rear_spawn_base_weighted[lc]
				weighted_length_by_species_lc[(species, lc)] = weighted_length
				connected, disconnected = connected_disconnected_weighted_length(
					weighted_length,
					downstream_first_barrier_passability[species][lc],
				)
				connected_by_species_lc[(species, lc)] = connected
				disconnected_by_species_lc[(species, lc)] = disconnected
			else:
				weighted_length = {
					eid: max(rear_spawn_base_weighted[REAR][eid], rear_spawn_base_weighted[SPAWN][eid])
					for eid in edge_ids
				}
			weighted_local = weighted_length

			up_field = f"{species}:{lc}_upstream_length"
			func_field = f"{species}:{lc}_functional_upstream_length"

			plain_zeros[up_field] = 0.0
			reset_zeros[func_field] = 0.0

			for eid in edge_ids:
				plain_local[eid][up_field] = local[eid]
				reset_local[eid][func_field] = local[eid]
				reset_is_reset[eid][func_field] = is_barrier[eid]

			weighted_field = f"{species}:{lc}_weighted_upstream_length_base"
			func_weighted_field = f"{species}:{lc}_functional_weighted_upstream_length_base"

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
			f"{lc}_upstream_accessible_length": {
				eid: v[f"{species}:{lc}_upstream_accessible_length"] for eid, v in plain_acc.items()
			}
			for lc in LIFESTAGES
		}
		for lc in LIFESTAGES:
			species_result[f"{lc}{BELOW_GRADIENT_THRESHOLD_SUFFIX}"] = {
				eid: v[f"{species}:{lc}{BELOW_GRADIENT_THRESHOLD_SUFFIX}"] for eid, v in plain_acc.items()
			}
		for lc in lifecycles_by_species[species]:
			species_result[f"{lc}_upstream_length"] = {
				eid: v[f"{species}:{lc}_upstream_length"] for eid, v in plain_acc.items()
			}
			species_result[f"{lc}_functional_upstream_length"] = {
				eid: v[f"{species}:{lc}_functional_upstream_length"] for eid, v in reset_acc.items()
			}
			species_result[f"{lc}_weighted_upstream_length_base"] = {
				eid: v[f"{species}:{lc}_weighted_upstream_length_base"] for eid, v in plain_acc.items()
			}
			species_result[f"{lc}_functional_weighted_upstream_length_base"] = {
				eid: v[f"{species}:{lc}_functional_weighted_upstream_length_base"] for eid, v in reset_acc.items()
			}
			if lc != SPAWNREAR:
				species_result[f"{lc}_weighted_length"] = weighted_length_by_species_lc[(species, lc)]
				species_result[f"{lc}_weighted_connected_length"] = connected_by_species_lc[(species, lc)]
				species_result[f"{lc}_weighted_disconnected_length"] = disconnected_by_species_lc[(species, lc)]
		result[species] = species_result
	upstream_stream_length_by_edge = {eid: v[UPSTREAM_STREAM_LENGTH] for eid, v in plain_acc.items()}
	return result, upstream_stream_length_by_edge


def compute_barrier_upstream_downstream_stats(
	barriers, barrier_stats, barrier_here_by_species, species_length_stats, predecessors
):
	"""Step 9's third bullet group: for each barrier and each species,
	upstream/downstream natural/anthropogenic counts (downstream ids of both types, plus upstream
	anthro ids -- natural_barriers_<species>/anthropogenic_barriers_<species> want upstream anthro
	ids too, alongside the downstream ids of both types -- each split into per-lifestage
	spawn/rear id lists, no combined "spawnrear" id list), plus that species' upstream length
	figures at the barrier. A barrier's own position is its downstream_edge_id; "upstream of the
	barrier" excludes the barrier's own contribution to that edge's upstream count (subtracting
	barrier_here_by_species for that specific species, which is 1 only if this position is
	actually impassable for that species -- not simply "this barrier's structure_type", since the
	same position may be passable for one species and not another) -- upstream_anthro_spawn_ids/
	upstream_anthro_rear_ids are filtered the same way, dropping this barrier's own feature_id from each
	list rather than subtracting a count. "downstream of the barrier" is that edge's downstream
	count/ids as-is (already excludes the barrier's own position -- see graph_stats.py). The length
	fields are not read at downstream_edge_id -- that edge starts right at the barrier and so isn't
	upstream of it. Instead each is the sum of the running totals (from species_length_stats) of
	every edge flowing into downstream_edge_id (its predecessors): one edge normally, every branch
	at a confluence, and none (0.0) at a headwater. So the values don't depend on which edge the
	barrier was snapped to. predecessors is graph_stats.build_graph's.

	Returns {internal_barrier_id: {species: {upstream_natural_spawnrear_count, upstream_anthro_spawnrear_count,
	downstream_natural_spawnrear_count, downstream_anthro_spawnrear_count, downstream_natural_spawn_ids,
	downstream_natural_rear_ids, downstream_anthro_spawn_ids, downstream_anthro_rear_ids,
	upstream_anthro_spawn_ids, upstream_anthro_rear_ids, and the same upstream_/downstream_ counts
	split by structure type (natural/anthro) x lifestage (spawn/rear/spawnrear) -- 12 count fields
	total, plus spawn_upstream_accessible_length/rear_upstream_accessible_length and, for each
	lifecycle that species reports, <lc>_upstream_length/<lc>_functional_upstream_length/
	<lc>_weighted_connected_upstream_length/<lc>_weighted_disconnected_upstream_length/
	<lc>_functional_weighted_connected_upstream_length/<lc>_functional_weighted_disconnected_upstream_length}}.
	barrier_stats is compute_barrier_stats' output, barrier_here_by_species is
	compute_barrier_here's output, species_length_stats is compute_length_stats' output;
	barriers is the same list passed to compute_barrier_here (needs internal "id", stable
	"feature_id", "edge_id", and "species_passability_value").

	The four weighted upstream-length fields are this barrier's own raw passability for that
	species/lifestage (b["species_passability_value"], not the downstream-chain passability used
	for the per-edge connected/disconnected split) times the base-weighted-length upstream sum,
	summed over the same inflowing edges (<lc>_weighted_upstream_length_base for the plain
	pair, <lc>_functional_weighted_upstream_length_base for the functional/reset pair): connected =
	sum * passability, disconnected = sum * (1 - passability). For "spawnrear", passability is
	min(spawn_passability, rear_passability) -- matching graph_stats.is_impassable's combined
	"impassable if either lifestage fails" rule. A missing species_lifestage key on this barrier is
	treated as 0 (full barrier), consistent with is_impassable's convention.
	"""

	count_keys = [
		(struct if lc == SPAWNREAR else f"{struct}_{lc}", f"{struct}_{lc}")
		for struct in ("natural", "anthro")
		for lc in LIFESTAGES_ALL
	]

	result = {}
	for b in barriers:
		barrier_row_id, feature_id = b["id"], b["feature_id"]
		edge_id = b["edge_id"]
		passability_value = b.get("species_passability_value") or {}
		per_species = {}
		for species, stats in barrier_stats.items():
			here = barrier_here_by_species[species]
			species_stats = {}
			for lc in LIFESTAGES:
				for key in (f"downstream_natural_{lc}_ids", f"downstream_anthro_{lc}_ids"):
					species_stats[key] = stats[key].get(edge_id, [])
				# This barrier's own feature_id is dropped from its upstream id list.
				key = f"upstream_anthro_{lc}_ids"
				species_stats[key] = [bid for bid in stats.get(key, {}).get(edge_id, []) if bid != feature_id]
			for data_key, count_key in count_keys:
				species_stats[f"upstream_{count_key}_count"] = stats.get(f"upstream_{count_key}_count", {}).get(
					edge_id, 0
				) - here.get(data_key, {}).get(edge_id, 0)
				species_stats[f"downstream_{count_key}_count"] = stats.get(f"downstream_{count_key}_count", {}).get(
					edge_id, 0
				)

			length_stats = species_length_stats.get(species, {})
			base_suffix = "_weighted_upstream_length_base"
			func_base_suffix = "_functional_weighted_upstream_length_base"
			lifecycles = [
				field[: -len(base_suffix)]
				for field in length_stats
				if field.endswith(base_suffix) and not field.endswith(func_base_suffix)
			]
			for field, values in length_stats.items():
				if field.endswith(
					base_suffix
				):  # covers func_base_suffix too, since it's a longer suffix ending the same way
					continue
				species_stats[field] = sum_inflowing(predecessors, edge_id, values)

			for lc in lifecycles:
				if lc == SPAWNREAR:
					passability = min(passability_value.get(f"{species}_{stage}", 0) for stage in LIFESTAGES)
				else:
					passability = passability_value.get(f"{species}_{lc}", 0)

				plain_base = sum_inflowing(predecessors, edge_id, length_stats[f"{lc}{base_suffix}"])
				func_base = sum_inflowing(predecessors, edge_id, length_stats[f"{lc}{func_base_suffix}"])

				species_stats[f"{lc}_weighted_connected_upstream_length"] = plain_base * passability
				species_stats[f"{lc}_weighted_disconnected_upstream_length"] = plain_base * (1 - passability)
				species_stats[f"{lc}_functional_weighted_connected_upstream_length"] = func_base * passability
				species_stats[f"{lc}_functional_weighted_disconnected_upstream_length"] = func_base * (1 - passability)

			per_species[species] = species_stats
		result[barrier_row_id] = per_species
	return result
