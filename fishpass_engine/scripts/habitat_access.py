"""Compute Statistics step 8 (fishpass/requirements/requirements.md): override species/lifecycle
habitat flags from <output_schema>.habitat_updates.

Per your decision recorded in requirements.md, "along the mainstem" is literal: flagging walks
only the single mainstem_id chain through the habitat update's snapped point(s), not the full
upstream/downstream network graph_stats.py's engine otherwise uses -- a tributary joining
partway along a flagged stretch is not flagged even though it's hydrologically upstream.

habitat_updates has no update_type/precedence field (unlike structure_updates' authoritative vs.
local_override), so overlapping rows are applied in update_date ascending order -- a later row's
flag simply overwrites an earlier one's for any segment/species/lifecycle they both touch.
"""

import sys

LIFECYCLES = ("rear", "spawn")


def parse_species_lifestage(entry):
	"""Parse one habitat_updates.species_lifestage array element into
	(species_code, [lifecycle, ...], flag). Format: [not_]<species>[_<spawn|rear>]. Omitting the
	lifecycle suffix means both spawn and rear. A not_ prefix means this entry clears (False)
	rather than sets (True) the flag."""

	flag = True
	rest = entry
	if rest.startswith("not_"):
		flag = False
		rest = rest[len("not_"):]

	for lifecycle in LIFECYCLES:
		suffix = f"_{lifecycle}"
		if rest.endswith(suffix):
			return rest[:-len(suffix)], [lifecycle], flag

	return rest, list(LIFECYCLES), flag


def mainstem_segments_upstream(edges_by_id, predecessors, start_edge_id):
	"""Every edge id reachable upstream from start_edge_id (inclusive), staying on
	edges_by_id[start_edge_id]'s mainstem_id."""

	mainstem_id = edges_by_id[start_edge_id]["mainstem_id"]
	result = []
	seen = set()
	stack = [start_edge_id]
	while stack:
		eid = stack.pop()
		if eid in seen:
			continue
		seen.add(eid)
		result.append(eid)
		for p in predecessors.get(eid, []):
			if edges_by_id[p]["mainstem_id"] == mainstem_id:
				stack.append(p)
	return result


def mainstem_segments_downstream(edges_by_id, successor, start_edge_id):
	"""Every edge id reachable downstream from start_edge_id (inclusive), staying on the same
	mainstem_id -- stops at the first edge with a different mainstem_id, or no successor."""

	mainstem_id = edges_by_id[start_edge_id]["mainstem_id"]
	result = [start_edge_id]
	current = start_edge_id
	while True:
		succ = successor.get(current)
		if succ is None or edges_by_id[succ]["mainstem_id"] != mainstem_id:
			return result
		result.append(succ)
		current = succ


def mainstem_segments_between(edges_by_id, predecessors, downstream_edge_id, upstream_edge_id):
	"""Every edge id from downstream_edge_id up to and including upstream_edge_id, walking the
	single mainstem_id chain. Raises ValueError if the two points aren't on the same mainstem_id,
	or if upstream_edge_id can't be reached walking upstream from downstream_edge_id along it --
	both indicate the habitat update's two points aren't actually a valid single-mainstem span."""

	mainstem_id = edges_by_id[downstream_edge_id]["mainstem_id"]
	if edges_by_id[upstream_edge_id]["mainstem_id"] != mainstem_id:
		raise ValueError(
			f"upstream point (mainstem_id={edges_by_id[upstream_edge_id]['mainstem_id']!r}) "
			f"and downstream point (mainstem_id={mainstem_id!r}) are on different mainstems"
		)

	result = [downstream_edge_id]
	current = downstream_edge_id
	while current != upstream_edge_id:
		candidates = [p for p in predecessors.get(current, []) if edges_by_id[p]["mainstem_id"] == mainstem_id]
		if not candidates:
			raise ValueError(
				f"could not walk from {downstream_edge_id} to {upstream_edge_id} along mainstem_id {mainstem_id!r}"
			)
		current = candidates[0]  # normally exactly one same-mainstem predecessor
		result.append(current)
	return result


def resolve_segments(row, edges_by_id, predecessors, successor):
	"""Return the list of edge ids a habitat_updates row's location_type/snapped edges resolve
	to, or None if the point(s) needed weren't snapped (Process Habitat step 2 left them
	unresolved -- per requirements.md, such a point is simply ignored, not an error here)."""

	location_type = row["location_type"]
	up_id = row["upstream_snapped_edge_id"]
	down_id = row["downstream_snapped_edge_id"]

	if location_type == "upstream":
		if up_id is None or up_id not in edges_by_id:
			return None
		return mainstem_segments_upstream(edges_by_id, predecessors, up_id)
	if location_type == "downstream":
		if down_id is None or down_id not in edges_by_id:
			return None
		return mainstem_segments_downstream(edges_by_id, successor, down_id)
	if location_type == "between":
		if up_id is None or down_id is None or up_id not in edges_by_id or down_id not in edges_by_id:
			return None
		try:
			return mainstem_segments_between(edges_by_id, predecessors, down_id, up_id)
		except ValueError as e:
			sys.exit(f"habitat_updates row {row.get('id')}: {e}")
	return None


def apply_habitat_access_overrides(habitat, edges_by_id, predecessors, successor, habitat_update_rows):
	"""Mutates `habitat` (species -> lifecycle ("rear"/"spawn") -> {edge_id: bool}) in place.
	habitat_update_rows must already be sorted by update_date ascending. Rows referencing a
	species not present in `habitat` (i.e. not in this run's target_species) are skipped."""

	for row in habitat_update_rows:
		segment_ids = resolve_segments(row, edges_by_id, predecessors, successor)
		if not segment_ids:
			continue

		for entry in row["species_lifestage"]:
			species, lifecycles, flag = parse_species_lifestage(entry)
			if species not in habitat:
				continue
			for lifecycle in lifecycles:
				for eid in segment_ids:
					habitat[species][lifecycle][eid] = flag


def derive_spawnrear_habitat(habitat):
	"""Add habitat[species]["spawnrear"] = rear OR spawn for every species, per requirements.md's
	documented spawnrear = union(rear, spawn) resolution. Call this after all step 7/8
	rear/spawn values (including overrides) are final."""

	for species, lifecycles in habitat.items():
		rear, spawn = lifecycles["rear"], lifecycles["spawn"]
		lifecycles["spawnrear"] = {eid: rear.get(eid, False) or spawn.get(eid, False) for eid in set(rear) | set(spawn)}
