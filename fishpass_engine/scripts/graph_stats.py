"""Compute Statistics steps 5-7 (fishpass/requirements/requirements.md): the graph traversal
engine and the per-species barrier-count/accessibility/habitat-assignment statistics built on
top of it.

Everything here operates on a single connected component (one graph_id group, already excluding
is_isolated edges) passed in as a plain list of edge dicts -- the DB I/O (fetching one graph_id's
post-break edges, writing results back) lives in compute_statistics.py, kept separate so this
module's logic is fully unit-testable on small synthetic graphs with no database.

Known gap: requirements.md's `supports_species` output field is described as being "based on
the fish species model aoi", a concept not defined anywhere in the provided requirements docs
(no species-range/AOI dataset is documented). supports_species_fn defaults to "always True" for
every edge/species here -- pluggable, but effectively unimplemented pending that missing data
source.
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

	Used for "functional" length aggregates, where a barrier resets the upstream accumulation
	(requirements.md Compute Statistics step 9: "A barrier 'resets' the upstream length
	calculation"). is_reset[E] means "a barrier sits at E's own start" (network_break.py's
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


def is_impassable(species_passability_value, species, impassable_threshold):
	"""requirements.md Compute Statistics step 5: impassable for `species` if either lifestage's
	value is below impassable_threshold. A missing species_lifestage key is treated as 0
	(impassable), consistent with structure_updates/new_structures' "missing = full barrier"
	convention (see load_structures.explode_passability)."""

	rear = species_passability_value.get(f"{species}_rear", 0)
	spawn = species_passability_value.get(f"{species}_spawn", 0)
	return min(rear, spawn) < impassable_threshold


def compute_barrier_here(edge_ids, barriers, species_list, impassable_threshold):
	"""barriers: list of dicts {"edge_id", "species_passability_value", "structure_type", "id"}
	for barriers snapped onto edges in this component (structure_type is 'natural' or
	'anthropogenic', per Load Structures step 7).

	Returns {species: {"natural": {edge_id: 0/1}, "anthro": {edge_id: 0/1},
	"natural_ids": {edge_id: [id,...]}, "anthro_ids": {edge_id: [id,...]}}} -- "here" meaning "at
	this edge's own start", per network_break.py's marker-attachment convention.
	"""

	result = {}
	for species in species_list:
		natural = {eid: 0 for eid in edge_ids}
		anthro = {eid: 0 for eid in edge_ids}
		natural_ids = {eid: [] for eid in edge_ids}
		anthro_ids = {eid: [] for eid in edge_ids}

		for b in barriers:
			eid = b["edge_id"]
			if eid not in natural:
				continue
			if not is_impassable(b["species_passability_value"], species, impassable_threshold):
				continue
			if b["structure_type"] == "natural":
				natural[eid] = 1
				natural_ids[eid].append(b["id"])
			else:
				anthro[eid] = 1
				anthro_ids[eid].append(b["id"])

		result[species] = {
			"natural": natural, "anthro": anthro,
			"natural_ids": natural_ids, "anthro_ids": anthro_ids,
		}
	return result


def compute_barrier_stats(order_up, order_down, predecessors, successor, barrier_here_by_species):
	"""Returns {species: {upstream_natural_count, downstream_natural_count,
	upstream_anthro_count, downstream_anthro_count, upstream_anthro_ids, downstream_anthro_ids,
	downstream_natural_ids}} (requirements.md's streams output only wants anthropogenic id
	lists; the natural_barriers/anthropogenic_barriers output tables want downstream ids of
	both types -- see the Outputs section -- hence downstream_natural_ids is included here too,
	but not upstream_natural_ids, which nothing needs)."""

	up_zeros = {}
	up_local = {}
	down_zeros = {}
	down_local = {}

	for species, data in barrier_here_by_species.items():
		up_zeros[f"{species}:upstream_natural_count"] = 0
		up_zeros[f"{species}:upstream_anthro_count"] = 0
		up_zeros[f"{species}:upstream_anthro_ids"] = []
		down_zeros[f"{species}:downstream_natural_count"] = 0
		down_zeros[f"{species}:downstream_anthro_count"] = 0
		down_zeros[f"{species}:downstream_anthro_ids"] = []
		down_zeros[f"{species}:downstream_natural_ids"] = []

		for eid, v in data["natural"].items():
			up_local.setdefault(eid, {})[f"{species}:upstream_natural_count"] = v
			down_local.setdefault(eid, {})[f"{species}:downstream_natural_count"] = v
		for eid, v in data["anthro"].items():
			up_local.setdefault(eid, {})[f"{species}:upstream_anthro_count"] = v
			down_local.setdefault(eid, {})[f"{species}:downstream_anthro_count"] = v
		for eid, v in data["anthro_ids"].items():
			up_local.setdefault(eid, {})[f"{species}:upstream_anthro_ids"] = v
			down_local.setdefault(eid, {})[f"{species}:downstream_anthro_ids"] = v
		for eid, v in data["natural_ids"].items():
			down_local.setdefault(eid, {})[f"{species}:downstream_natural_ids"] = v

	up_acc = propagate_upstream_multi(order_up, predecessors, up_local, up_zeros)
	down_acc = propagate_downstream_multi(order_down, successor, down_local, down_zeros)

	stats = {}
	for species in barrier_here_by_species:
		stats[species] = {
			"upstream_natural_count": {eid: v[f"{species}:upstream_natural_count"] for eid, v in up_acc.items()},
			"downstream_natural_count": {eid: v[f"{species}:downstream_natural_count"] for eid, v in down_acc.items()},
			"upstream_anthro_count": {eid: v[f"{species}:upstream_anthro_count"] for eid, v in up_acc.items()},
			"downstream_anthro_count": {eid: v[f"{species}:downstream_anthro_count"] for eid, v in down_acc.items()},
			"upstream_anthro_ids": {eid: v[f"{species}:upstream_anthro_ids"] for eid, v in up_acc.items()},
			"downstream_anthro_ids": {eid: v[f"{species}:downstream_anthro_ids"] for eid, v in down_acc.items()},
			"downstream_natural_ids": {eid: v[f"{species}:downstream_natural_ids"] for eid, v in down_acc.items()},
		}
	return stats


ACCESSIBILITY_CONNECTED = "connected_naturally_accessible"
ACCESSIBILITY_DISCONNECTED = "disconnected_naturally_accessible"
ACCESSIBILITY_INACCESSIBLE = "naturally_inaccessible"


def compute_accessibility(edge_ids, barrier_stats, supports_species_fn=None):
	"""requirements.md Compute Statistics step 6. supports_species_fn(species, edge_id) -> bool
	defaults to always True (see module docstring's "Known gap" note).

	Returns {species: {edge_id: accessibility_string}}."""

	supports_species_fn = supports_species_fn or (lambda species, edge_id: True)

	result = {}
	for species, stats in barrier_stats.items():
		accessibility = {}
		for eid in edge_ids:
			if not supports_species_fn(species, eid):
				accessibility[eid] = ACCESSIBILITY_INACCESSIBLE
				continue
			nat = stats["downstream_natural_count"][eid]
			anthro = stats["downstream_anthro_count"][eid]
			if nat == 0 and anthro == 0:
				accessibility[eid] = ACCESSIBILITY_CONNECTED
			elif nat == 0 and anthro > 0:
				accessibility[eid] = ACCESSIBILITY_DISCONNECTED
			else:
				accessibility[eid] = ACCESSIBILITY_INACCESSIBLE
		result[species] = accessibility
	return result


ACCESSIBLE_STATES = (ACCESSIBILITY_CONNECTED, ACCESSIBILITY_DISCONNECTED)


def compute_habitat_assignment(edge_ids, species_list, accessibility, edge_gradient, edge_strahler, species_params_by_code):
	"""requirements.md Compute Statistics step 7. edge_gradient/edge_strahler:
	{edge_id: value or None}. Returns {species: {"rear": {edge_id: bool}, "spawn": {edge_id: bool}}}
	-- "general" is derived by the caller as the union of rear/spawn (see requirements.md's
	documented general = rear OR spawn resolution)."""

	result = {}
	for species in species_list:
		params = species_params_by_code[species]
		rear = {}
		spawn = {}
		for eid in edge_ids:
			accessible = accessibility[species][eid] in ACCESSIBLE_STATES
			gradient = edge_gradient.get(eid)
			strahler = edge_strahler.get(eid)
			rear[eid] = accessible and sp_mod.habitat_gradient_ok(params, "rear", gradient) and \
				sp_mod.habitat_strahler_ok(params, "rear", strahler)
			spawn[eid] = accessible and sp_mod.habitat_gradient_ok(params, "spawn", gradient) and \
				sp_mod.habitat_strahler_ok(params, "spawn", strahler)
		result[species] = {"rear": rear, "spawn": spawn}
	return result
