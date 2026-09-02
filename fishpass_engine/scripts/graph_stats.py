"""Compute statistics (fishpass/docs/fishpass_docs.md): the graph traversal
engine and the per-species barrier-count/accessibility/habitat-assignment statistics built on
top of it.

Everything here operates on a single connected component (one graph_id group, already excluding
is_isolated edges) passed in as a plain list of edge dicts -- the DB I/O (fetching one graph_id's
post-break edges, writing results back) lives in compute_statistics.py, kept separate so this
module's logic is fully unit-testable on small synthetic graphs with no database.

"""

import species_params as sp_mod


def build_graph(edges):
	"""edges: iterable of dicts with at least "id", "from_nexus_id", "to_nexus_id".

	Returns (successor, predecessors, roots):
	  successor[edge_id] -> the edge_id immediately downstream of it, or None if it's a local
	    outlet (nothing in this edge set starts where it ends).
	  predecessors[edge_id] -> list of edge_ids immediately upstream of it (i.e. every edge
	    ending at this edge's from_nexus_id).
	  roots -> every edge_id with no successor.

	Assumes the normal dendritic case: at most one edge starts at any given nexus. If two edges
	share a from_nexus_id (a distributary/braid), one is picked arbitrarily as that nexus's
	"successor" edge and a warning-worthy edge case is silently resolved rather than erroring --
	true distributaries are not expected in this network.
	"""
	edge_at_from_nexus = {}
	for e in edges:
		edge_at_from_nexus.setdefault(e["from_nexus_id"], e["id"])

	predecessors = {e["id"]: [] for e in edges}
	successor = {}
	for e in edges:
		succ_id = edge_at_from_nexus.get(e["to_nexus_id"])
		successor[e["id"]] = succ_id
		if succ_id is not None:
			predecessors[succ_id].append(e["id"])

	roots = [e["id"] for e in edges if successor[e["id"]] is None]
	return successor, predecessors, roots


def upstream_order(predecessors, roots):
	"""Post-order traversal: every edge appears only after all of its predecessors. Correct
	processing order for an upward (headwaters -> outlet) accumulating pass."""

	order = []
	visited = set()
	stack = [(r, False) for r in roots]
	while stack:
		node, expanded = stack.pop()
		if expanded:
			order.append(node)
			continue
		if node in visited:
			continue
		visited.add(node)
		stack.append((node, True))
		for p in predecessors.get(node, []):
			stack.append((p, False))
	return order


def upstream_closure(predecessors, seed_ids):
	"""Return the set of edge_ids consisting of every id in seed_ids plus all of their upstream
	ancestors (transitively, via predecessors) -- every edge that drains into a seed, directly or
	indirectly, including the seeds themselves."""

	closure = set()
	stack = list(seed_ids)
	while stack:
		eid = stack.pop()
		if eid in closure:
			continue
		closure.add(eid)
		stack.extend(predecessors.get(eid, []))
	return closure


def downstream_order(order_up):
	"""Reverse of upstream_order: every edge appears only after its successor. Correct
	processing order for a downward (outlet -> headwaters) propagating pass."""
	return list(reversed(order_up))


def propagate_upstream(order_up, predecessors, local_value, combine, zero):
	"""accumulated[E] = local_value[E] combined with accumulated[P] for every predecessor P of E.
	combine(a, b) must be associative (predecessors are folded in an arbitrary order)."""

	acc = {}
	for edge_id in order_up:
		value = local_value.get(edge_id, zero)
		for p in predecessors.get(edge_id, []):
			value = combine(value, acc[p])
		acc[edge_id] = value
	return acc


def propagate_downstream(order_down, successor, local_value, combine, zero):
	"""accumulated[E] = accumulated[successor(E)] combined with local_value[successor(E)],
	or `zero` if E has no successor."""

	acc = {}
	for edge_id in order_down:
		succ = successor.get(edge_id)
		acc[edge_id] = zero if succ is None else combine(acc[succ], local_value.get(succ, zero))
	return acc


def propagate_upstream_with_reset(order_up, predecessors, local_value, is_reset, zero=0.0):
	"""acc[E] = local_value[E] + (zero if is_reset[E] else sum(acc[P] for P in predecessors[E])).

	Used for "functional" length aggregates, where a barrier resets the upstream accumulation.
	is_reset[E] means "a barrier sits at E's own start" (network_break.py's
	marker-attachment convention) -- i.e. between E and E's own predecessors. That barrier
	blocks E from reaching *its own* predecessors, so E's predecessors' accumulated totals are
	excluded when is_reset[E] is true -- but E's own local_value is always included regardless,
	since E itself sits below that barrier and is reachable without crossing it. (A barrier at a
	*predecessor* P's start affects whether P reaches beyond itself -- already correctly baked
	into acc[P] by the time E sums it in -- not whether E can reach P itself.)
	"""

	acc = {}
	for edge_id in order_up:
		if is_reset.get(edge_id, False):
			acc[edge_id] = local_value.get(edge_id, zero)
		else:
			value = local_value.get(edge_id, zero)
			for p in predecessors.get(edge_id, []):
				value = value + acc[p]
			acc[edge_id] = value
	return acc


def propagate_upstream_multi(order_up, predecessors, local_values, zeros):
	"""Vector-valued propagate_upstream: local_values[edge_id] is a {field: value} dict (any
	fields missing for an edge fall back to zeros[field]), and every field is accumulated in the
	same single traversal of order_up rather than one traversal per field. `+` is used to combine
	values, which does the right thing for both int fields (addition) and list fields
	(concatenation) without special-casing either.

	Returns {edge_id: {field: accumulated_value}}.
	"""

	acc = {}
	for edge_id in order_up:
		values = local_values.get(edge_id, {})
		combined = {field: values.get(field, zero) for field, zero in zeros.items()}
		for p in predecessors.get(edge_id, []):
			p_acc = acc[p]
			for field in zeros:
				combined[field] = combined[field] + p_acc[field]
		acc[edge_id] = combined
	return acc


def propagate_downstream_multi(order_down, successor, local_values, zeros):
	"""Vector-valued propagate_downstream -- see propagate_upstream_multi. Returns
	{edge_id: {field: accumulated_value}}."""

	acc = {}
	for edge_id in order_down:
		succ = successor.get(edge_id)
		if succ is None:
			acc[edge_id] = dict(zeros)
			continue
		succ_acc = acc[succ]
		succ_values = local_values.get(succ, {})
		acc[edge_id] = {
			field: succ_acc[field] + succ_values.get(field, zero)
			for field, zero in zeros.items()
		}
	return acc


def propagate_upstream_with_reset_multi(order_up, predecessors, local_values, is_reset, zeros):
	"""Vector-valued propagate_upstream_with_reset -- see propagate_upstream_multi and
	propagate_upstream_with_reset. is_reset[edge_id] is now a {field: bool} dict instead of a
	single bool, so each field independently resets or accumulates from predecessors within the
	same traversal -- e.g. two species with barriers at different positions can share one pass
	even though they reset at different edges."""

	acc = {}
	for edge_id in order_up:
		values = local_values.get(edge_id, {})
		resets = is_reset.get(edge_id, {})
		combined = {}
		for field, zero in zeros.items():
			local = values.get(field, zero)
			if resets.get(field, False):
				combined[field] = local
			else:
				for p in predecessors.get(edge_id, []):
					local = local + acc[p][field]
				combined[field] = local
		acc[edge_id] = combined
	return acc


def compute_route_measures(edges_by_id, predecessors, successor):
	"""Per-mainstem linear-referencing measure: {edge_id: (downstream_route_measure,
	upstream_route_measure)}, in `length` units, local to each mainstem_id chain -- 0 at the
	chain's own mouth (an edge with no successor, i.e. a network outlet, or whose successor is on
	a *different* mainstem_id, i.e. it joins a larger mainstem), increasing upstream to the
	chain's headwater. downstream_route_measure is the distance from the chain's mouth to this
	edge's downstream end; upstream_route_measure = downstream_route_measure + this edge's own
	length (its distance to the edge's upstream end).

	Edges with mainstem_id is None are excluded from the result (caller should treat missing
	entries as NULL/unmeasured -- there's no chain to measure them along).

	Walks upstream one mainstem_id chain at a time, picking the single same-mainstem_id
	predecessor at each step -- same "at most one same-mainstem predecessor" assumption as
	habitat_access.mainstem_segments_between."""

	result = {}
	mouths = [
		eid for eid, e in edges_by_id.items()
		if e["mainstem_id"] is not None and (
			successor.get(eid) is None
			or edges_by_id[successor[eid]]["mainstem_id"] != e["mainstem_id"]
		)
	]
	for mouth in mouths:
		mainstem_id = edges_by_id[mouth]["mainstem_id"]
		measure = 0.0
		current = mouth
		while True:
			length = edges_by_id[current]["length"]
			result[current] = (measure, measure + length)
			measure += length
			candidates = [p for p in predecessors.get(current, []) if edges_by_id[p]["mainstem_id"] == mainstem_id]
			if not candidates:
				break
			current = candidates[0]  # normally exactly one same-mainstem predecessor
	return result


LIFESTAGES = ("spawn", "rear")


def is_impassable(species_passability_value, species, impassable_threshold, lifestage=None):
	"""Compute statistics impassable for `species` if either lifestage's
	value is below impassable_threshold. A missing species_lifestage key is treated as 0
	(impassable), consistent with new_structures' "missing = full barrier" convention (see
	load_structures.explode_new_structure_passability).

	If `lifestage` ("spawn" or "rear") is given, only that lifestage's value is checked instead of
	the combined either-lifestage rule -- used to derive lifestage-specific barrier counts (e.g.
	"impassable for spawn regardless of rear") on top of the same threshold/missing-value rules."""

	if lifestage is not None:
		value = species_passability_value.get(f"{species}_{lifestage}", 0)
		return value < impassable_threshold

	rear = species_passability_value.get(f"{species}_rear", 0)
	spawn = species_passability_value.get(f"{species}_spawn", 0)
	return min(rear, spawn) < impassable_threshold


def compute_barrier_here(edge_ids, barriers, species_list, impassable_threshold):
	"""barriers: list of dicts {"edge_id", "species_passability_value", "structure_type", "id"}
	for barriers snapped onto edges in this component (structure_type is 'natural' or
	'anthropogenic', per Load Structures step 7).

	Returns {species: {"natural": {edge_id: 0/1}, "anthro": {edge_id: 0/1},
	"natural_spawn_ids"/"natural_rear_ids"/"anthro_spawn_ids"/"anthro_rear_ids": {edge_id: [id,...]},
	"natural_spawn"/"natural_rear"/"anthro_spawn"/"anthro_rear": {edge_id: 0/1}}} -- "here" meaning
	"at this edge's own start", per network_break.py's marker-attachment convention. A barrier's id
	lands in a lifestage's id list iff it's impassable for that lifestage specifically -- a barrier
	blocking both lifestages appears in both lists (no combined "spawnrear" id list is produced).

	"natural"/"anthro" are impassable-for-either-lifestage (step 5's combined
	rule), equal to the OR of that type's own "_spawn"/"_rear" flags. "anthro" drives
	length_stats.py's functional-reset logic (only non-passable anthropogenic barriers reset
	functional upstream length); "natural" is used for barrier-count/id outputs only.
	"""

	result = {}
	for species in species_list:
		lifestage_flags = {
			f"{struct}_{lc}": {eid: 0 for eid in edge_ids}
			for struct in ("natural", "anthro") for lc in LIFESTAGES
		}
		lifestage_ids = {
			f"{struct}_{lc}_ids": {eid: [] for eid in edge_ids}
			for struct in ("natural", "anthro") for lc in LIFESTAGES
		}

		for b in barriers:
			eid = b["edge_id"]
			if eid not in lifestage_flags["natural_spawn"]:
				continue
			struct = "natural" if b["structure_type"] == "natural" else "anthro"
			for lc in LIFESTAGES:
				if is_impassable(b["species_passability_value"], species, impassable_threshold, lifestage=lc):
					lifestage_flags[f"{struct}_{lc}"][eid] = 1
					lifestage_ids[f"{struct}_{lc}_ids"][eid].append(b["id"])

		natural = {eid: int(bool(lifestage_flags["natural_spawn"][eid] or lifestage_flags["natural_rear"][eid])) for eid in edge_ids}
		anthro = {eid: int(bool(lifestage_flags["anthro_spawn"][eid] or lifestage_flags["anthro_rear"][eid])) for eid in edge_ids}

		result[species] = {
			"natural": natural, "anthro": anthro,
			**lifestage_flags, **lifestage_ids,
		}
	return result


def compute_barrier_stats(order_up, order_down, predecessors, successor, barrier_here_by_species):
	"""Returns {species: {upstream_natural_spawnrear_count, downstream_natural_spawnrear_count,
	upstream_anthro_spawnrear_count, downstream_anthro_spawnrear_count, and the same
	upstream_/downstream_ counts split by structure type (natural/anthro) x lifestage
	(spawn/rear/spawnrear) -- 12 count fields total, all following
	<direction>_<type>_<lifestage>_count}}, plus per-lifestage (spawn/rear, no combined
	"spawnrear") id lists: upstream_anthro_spawn_ids, upstream_anthro_rear_ids,
	downstream_anthro_spawn_ids, downstream_anthro_rear_ids, downstream_natural_spawn_ids,
	downstream_natural_rear_ids (the streams output only wants anthropogenic id
	lists; the natural_barriers/anthropogenic_barriers output tables want downstream ids of both
	types plus upstream anthro ids -- see the Outputs section -- hence downstream_natural_*_ids is
	included here too, but not upstream_natural_*_ids, which nothing needs).

	"_spawnrear_count" fields mean "impassable for either lifestage" (step 5's
	combined rule); "_spawn_count"/"_rear_count" mean impassable for that lifestage specifically,
	independent of the other. The id-list fields have no "_spawnrear_" variant -- a barrier's id
	lands in a lifestage's list iff it's impassable for that lifestage specifically, so a barrier
	blocking both lifestages appears in both lists."""

	up_zeros = {}
	up_local = {}
	down_zeros = {}
	down_local = {}

	count_keys = [
		(struct if lc == "spawnrear" else f"{struct}_{lc}", f"{struct}_{lc}")
		for struct in ("natural", "anthro")
		for lc in (*LIFESTAGES, "spawnrear")
	]

	for species, data in barrier_here_by_species.items():
		for data_key, count_key in count_keys:
			up_zeros[f"{species}:upstream_{count_key}_count"] = 0
			down_zeros[f"{species}:downstream_{count_key}_count"] = 0
			for eid, v in data[data_key].items():
				up_local.setdefault(eid, {})[f"{species}:upstream_{count_key}_count"] = v
				down_local.setdefault(eid, {})[f"{species}:downstream_{count_key}_count"] = v

		for lc in LIFESTAGES:
			up_zeros[f"{species}:upstream_anthro_{lc}_ids"] = []
			down_zeros[f"{species}:downstream_anthro_{lc}_ids"] = []
			down_zeros[f"{species}:downstream_natural_{lc}_ids"] = []

			for eid, v in data[f"anthro_{lc}_ids"].items():
				up_local.setdefault(eid, {})[f"{species}:upstream_anthro_{lc}_ids"] = v
				down_local.setdefault(eid, {})[f"{species}:downstream_anthro_{lc}_ids"] = v
			for eid, v in data[f"natural_{lc}_ids"].items():
				down_local.setdefault(eid, {})[f"{species}:downstream_natural_{lc}_ids"] = v

	up_acc = propagate_upstream_multi(order_up, predecessors, up_local, up_zeros)
	down_acc = propagate_downstream_multi(order_down, successor, down_local, down_zeros)

	stats = {}
	for species in barrier_here_by_species:
		species_stats = {}
		for lc in LIFESTAGES:
			species_stats[f"upstream_anthro_{lc}_ids"] = {eid: v[f"{species}:upstream_anthro_{lc}_ids"] for eid, v in up_acc.items()}
			species_stats[f"downstream_anthro_{lc}_ids"] = {eid: v[f"{species}:downstream_anthro_{lc}_ids"] for eid, v in down_acc.items()}
			species_stats[f"downstream_natural_{lc}_ids"] = {eid: v[f"{species}:downstream_natural_{lc}_ids"] for eid, v in down_acc.items()}
		for _data_key, count_key in count_keys:
			species_stats[f"upstream_{count_key}_count"] = {eid: v[f"{species}:upstream_{count_key}_count"] for eid, v in up_acc.items()}
			species_stats[f"downstream_{count_key}_count"] = {eid: v[f"{species}:downstream_{count_key}_count"] for eid, v in down_acc.items()}
		stats[species] = species_stats
	return stats


def compute_downstream_first_anthropogenic_barrier_passability(edge_ids, order_down, successor, barriers, species_list):
	"""For each species and lifestage (spawn, rear), {edge_id: that lifestage's raw
	species_passability_value at the nearest ("first") downstream anthropogenic barrier that is not
	fully passable (value < 1) for that species/lifestage}. Natural barriers are skipped entirely
	(never considered), and a fully-passable anthropogenic barrier (value == 1.0) is also skipped
	when searching downstream -- walking continues past it looking for a further, degrading
	anthropogenic barrier. A missing species_lifestage key on a barrier is treated as 0 (full
	barrier), consistent with is_impassable's "missing = full barrier" convention -- it always
	qualifies as < 1. An edge with no qualifying downstream barrier at all gets 1.0 (no
	degradation). If multiple anthropogenic, value < 1 barriers are snapped to the same nearest
	qualifying location, their raw values combine by product for that location (natural barriers
	and any fully-passable anthropogenic barrier at that same location are excluded from the
	product).

	Returns {species: {"spawn": {edge_id: float}, "rear": {edge_id: float}}}."""

	keys = [(species, lifestage) for species in species_list for lifestage in LIFESTAGES]
	ones = {key: 1.0 for key in keys}

	local_by_edge = {eid: dict(ones) for eid in edge_ids}
	qualifies = {eid: {key: False for key in keys} for eid in edge_ids}
	for b in barriers:
		if b["structure_type"] == "natural":
			continue
		eid = b["edge_id"]
		if eid not in local_by_edge:
			continue
		for species, lifestage in keys:
			value = b["species_passability_value"].get(f"{species}_{lifestage}", 0)
			if value < 1:
				local_by_edge[eid][(species, lifestage)] *= value
				qualifies[eid][(species, lifestage)] = True

	acc = {}
	for eid in order_down:
		succ = successor.get(eid)
		if succ is None:
			acc[eid] = dict(ones)
		else:
			acc[eid] = {
				key: local_by_edge[succ][key] if qualifies[succ][key] else acc[succ][key]
				for key in keys
			}

	result = {species: {lifestage: {} for lifestage in LIFESTAGES} for species in species_list}
	for eid, values in acc.items():
		for (species, lifestage), v in values.items():
			result[species][lifestage][eid] = v
	return result


ACCESSIBILITY_ACCESSIBLE = "naturally_accessible"
ACCESSIBILITY_INACCESSIBLE = "naturally_inaccessible"


def compute_accessibility(edge_ids, barrier_stats):
	"""Compute Statistics step 6: an edge is naturally accessible for a species/
	lifestage if it has 0 downstream natural barriers impassable for that lifestage --
	anthropogenic barriers never factor in, and spawn/rear are computed independently of each
	other (a barrier impassable only for rear does not affect spawn_accessibility, and vice versa).

	Returns {species: {"spawn": {edge_id: accessibility_string}, "rear": {edge_id: accessibility_string}}}."""

	result = {}
	for species, stats in barrier_stats.items():
		spawn = {}
		rear = {}
		for eid in edge_ids:
			spawn[eid] = ACCESSIBILITY_ACCESSIBLE if stats["downstream_natural_spawn_count"][eid] == 0 else ACCESSIBILITY_INACCESSIBLE
			rear[eid] = ACCESSIBILITY_ACCESSIBLE if stats["downstream_natural_rear_count"][eid] == 0 else ACCESSIBILITY_INACCESSIBLE
		result[species] = {"spawn": spawn, "rear": rear}
	return result


def compute_habitat_assignment(edge_ids, species_list, accessibility, edge_gradient, edge_strahler, species_params_by_code):
	"""Compute Statistics step 7. edge_gradient/edge_strahler:
	{edge_id: value or None}. accessibility is compute_accessibility's output -- rear habitat gates
	on accessibility[species]["rear"], spawn habitat gates on accessibility[species]["spawn"],
	independently of each other. Returns {species: {"rear": {edge_id: bool}, "spawn": {edge_id:
	bool}}} -- "spawnrear" is derived by the caller as the union of rear/spawn (see the
	documented spawnrear = rear OR spawn resolution)."""

	result = {}
	for species in species_list:
		params = species_params_by_code[species]
		rear = {}
		spawn = {}
		for eid in edge_ids:
			rear_accessible = accessibility[species]["rear"][eid] == ACCESSIBILITY_ACCESSIBLE
			spawn_accessible = accessibility[species]["spawn"][eid] == ACCESSIBILITY_ACCESSIBLE
			gradient = edge_gradient.get(eid)
			strahler = edge_strahler.get(eid)
			rear[eid] = rear_accessible and sp_mod.habitat_gradient_ok(params, "rear", gradient) and \
				sp_mod.habitat_strahler_ok(params, "rear", strahler)
			spawn[eid] = spawn_accessible and sp_mod.habitat_gradient_ok(params, "spawn", gradient) and \
				sp_mod.habitat_strahler_ok(params, "spawn", strahler)
		result[species] = {"rear": rear, "spawn": spawn}
	return result
