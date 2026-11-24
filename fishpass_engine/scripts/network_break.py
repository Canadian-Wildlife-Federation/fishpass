"""Break the network at all barrier and habitat points.

Design: structure/habitat snapping already
inserted a real vertex into <output_schema>.streams' geometry at every barrier/habitat location
that needed one -- so breaking never has to locate an arbitrary point along a line, only find
which *existing* vertex of an edge a marker sits on, and split the edge's row there.

A marker that lands on an edge's own first vertex is treated as sitting at the *start* of that
edge (rather than the end of whatever edge(s) feed into it): a marker at a nexus is placed at
the upstream point of the downstream edge. That edge is unique (one edge leaves a nexus, where
several can enter at a confluence), and it means an edge never needs splitting at its own last
vertex (which would produce a zero-length tail segment): any marker coinciding with edge E's last vertex is,
by construction, the same physical nexus as the first vertex of whatever edge continues
downstream from E, and is explicitly reassigned to that downstream edge's id (found via a
from_nexus_id lookup on E's to_nexus_id) rather than left on any segment of E itself.

That rule covers barriers and habitat "top" points: the point of a "downstream" update and the
upstream point of a "between" update, which mark the upstream end of the habitat. The habitat
starts with the segment below them.

Habitat "bottom" points -- the point of an "upstream" update and the downstream point of a
"between" update -- mark the downstream end of the habitat, so they go the other way: a bottom
point is attached to the segment *ending* at it. The edge is still split at the point, but the
point is recorded on the segment above the split, and a bottom point on an edge's last vertex
stays on that edge's last segment (it is never moved to the downstream edge, which at a
confluence can belong to a different river). habitat_access then walks up from, and including,
that segment. A bottom point never sits on an edge's first vertex: Process Habitat
(load_habitat.resolve_bottom_point_at_nexus) has already moved any such point to the last vertex
of the edge above it, so one found here stops the run.

At the last vertex of a network outlet edge (no downstream continuation) there is no downstream
edge to move a marker to:
- A habitat top point there is not moved onto a sink edge: several outlet edges can share one
  outlet nexus, and the edge a point was snapped to is what says which river (mainstem_id) the
  habitat update follows. A "downstream" update's point gets a NULL downstream_snapped_edge_id,
  since nothing is downstream of an outlet; habitat_access ignores it, so it applies to no edges.
  Its downstream_snapped_point is deliberately kept, to show where the point was placed.
  A "between" update's upstream point stays on the outlet edge's *last* segment (if the edge was
  split elsewhere, its original id now names only the first segment).
- A barrier marker there is a "sink barrier". A zero-length "sink edge" is added below the outlet
  edge (one per outlet nexus, shared by every barrier and outlet edge there): it starts at the outlet edge's
  to_nexus_id, ends at a new outlet nexus, and is flagged streams.is_sink_barrier_edge. The
  barrier's downstream_edge_id is set to it, so the barrier sits at the start of an edge like every
  other barrier, and the outlet edge is upstream of it. Sink edges are kept in streams but left out
  of every view. A sink edge copies its other attributes from the first outlet edge processed
  with a barrier there, except mainstem_id, stream_name_en and rivernameid1, which are NULL since
  a sink edge can sit below more than one river. If barriers at the same outlet nexus were snapped via outlet
  edges with different graph_ids, the run stops. (Outlet edges at that nexus with no barrier of
  their own aren't checked.)

Only edges with at least one marker on them are touched; the (large majority of) edges with none
are left as single, unbroken segments.

Each barrier marker's all_barriers.downstream_edge_id ends up as the segment starting at the
marker's vertex, continuing toward to_nexus_id (a sink edge, for a sink barrier). It is the same whichever edge the barrier was
snapped to: a barrier at the shared nexus of edges A -> B is moved to B whether it was snapped to
A's last vertex or B's first. Whatever is upstream of a barrier is found from the edges flowing
into the start of downstream_edge_id (see length_stats.compute_barrier_upstream_downstream_stats),
so no upstream edge reference is stored.

Edge lengths are not recomputed here -- compute_statistics.compute_effective_length_and_gradient
recomputes every edge's length from its final geometry, split or not.
"""

import logging
import sys
import uuid

from db import quote_ident
from network_snap import edge_vertices, linestring_zm_wkb
from utils import is_bottom_point

logger = logging.getLogger(__name__)

VERTEX_MATCH_TOLERANCE = 1e-9  # degrees -- exact float match expected, no reprojection occurs
BATCH_SIZE = 5000

STREAM_FIELDS = (
	"id",
	"aoi_id",
	"ef_type",
	"ef_subtype",
	"rank",
	"from_nexus_id",
	"to_nexus_id",
	"ecatchment_id",
	"mainstem_id",
	"graph_id",
	"is_isolated",
	"strahler_order",
	"rivernameid1",
	"stream_name_en",
)


def get_break_points(cursor, output_schema):
	"""Return (by_edge, bottom_markers). by_edge is {edge_id: [(x, y, kind, ref_id), ...]} for
	every streams edge with at least one barrier or habitat point snapped onto it. kind is
	'barrier', 'habitat_upstream', or 'habitat_downstream'; ref_id is the all_barriers.id or
	habitat_updates.id, needed so their edge-id reference can be corrected if the marker ends up in
	a non-first segment. bottom_markers is the set of (kind, ref_id) for habitat bottom points --
	an "upstream" update's point and a "between" update's downstream point -- which attach to the
	segment ending at them (see module docstring)."""

	schema_ident = quote_ident(output_schema)
	by_edge = {}
	bottom_markers = set()

	cursor.execute(f"""
		SELECT downstream_edge_id, network_vertex_x, network_vertex_y, id
		FROM {schema_ident}.all_barriers
		WHERE downstream_edge_id IS NOT NULL
	""")
	for edge_id, x, y, structure_id in cursor.fetchall():
		by_edge.setdefault(edge_id, []).append((x, y, "barrier", structure_id))

	cursor.execute(f"""
		SELECT upstream_snapped_edge_id, ST_X(upstream_snapped_point), ST_Y(upstream_snapped_point), id,
			location_type
		FROM {schema_ident}.habitat_updates
		WHERE upstream_snapped_edge_id IS NOT NULL
	""")
	for edge_id, x, y, habitat_id, location_type in cursor.fetchall():
		by_edge.setdefault(edge_id, []).append((x, y, "habitat_upstream", habitat_id))
		if is_bottom_point(location_type, "upstream"):
			bottom_markers.add(("habitat_upstream", habitat_id))

	cursor.execute(f"""
		SELECT downstream_snapped_edge_id, ST_X(downstream_snapped_point), ST_Y(downstream_snapped_point), id,
			location_type
		FROM {schema_ident}.habitat_updates
		WHERE downstream_snapped_edge_id IS NOT NULL
	""")
	for edge_id, x, y, habitat_id, location_type in cursor.fetchall():
		by_edge.setdefault(edge_id, []).append((x, y, "habitat_downstream", habitat_id))
		if is_bottom_point(location_type, "downstream"):
			bottom_markers.add(("habitat_downstream", habitat_id))

	return by_edge, bottom_markers


def match_vertex_index(vertices, x, y):
	for i, (vx, vy, _z, _m) in enumerate(vertices):
		if abs(vx - x) < VERTEX_MATCH_TOLERANCE and abs(vy - y) < VERTEX_MATCH_TOLERANCE:
			return i
	return None


def break_edge(edge, points, bottom_markers=frozenset(), new_id_factory=lambda: str(uuid.uuid4())):
	"""Split one edge's vertex list into consecutive segments at every internal vertex a marker
	lands on. `edge` is a dict with at least vertices/from_nexus_id/to_nexus_id. `points` is
	this edge's marker list from get_break_points, and `bottom_markers` its set of (kind, ref_id)
	habitat bottom points. Returns (segments, end_markers):

	segments is a list of segment dicts {"vertices", "from_nexus_id", "to_nexus_id",
	"start_markers", "bottom_markers"} in edge order -- the first segment always starts at the
	original from_nexus_id, the last always ends at the original to_nexus_id, and every internal
	split point becomes a freshly generated nexus id shared by the segment ending there and the
	segment starting there. start_markers are the barriers and habitat top points on the
	segment's first vertex; bottom_markers are the habitat bottom points on its last vertex.

	end_markers is the (kind, ref_id) list for barriers and habitat top points matched at the
	edge's own last vertex -- per this module's marker-attachment convention these belong at the
	*start* of whatever edge continues downstream, never on a segment of this edge, so the caller
	must resolve them against the downstream edge instead of folding them into `segments`.

	Stops the run if a marker doesn't match any of the edge's vertices, or if a habitat bottom
	point sits on the edge's first vertex (see below).
	"""
	vertices = edge["vertices"]
	n = len(vertices)

	markers_by_index = {}
	bottom_by_index = {}
	end_markers = []
	for x, y, kind, ref_id in points:
		idx = match_vertex_index(vertices, x, y)
		if idx is None:
			# Snapping put a vertex at every point it recorded, and nothing since then moves or
			# removes vertices, so this means an earlier step broke that assumption.
			sys.exit(
				f"{kind} point {ref_id} at ({x}, {y}) doesn't match any vertex of its streams edge "
				f"{edge.get('id')}; the edge geometry changed after snapping.\n***STOPPING***"
			)
		if (kind, ref_id) in bottom_markers:
			if idx == 0:
				# Process Habitat moves a bottom point on an edge's first vertex to the last vertex
				# of the edge above it (or clears it), so it should never reach here.
				sys.exit(
					f"{kind} point {ref_id} at ({x}, {y}) marks the downstream end of its habitat but "
					f"sits on the first vertex of its streams edge {edge.get('id')}; Process Habitat "
					f"should have moved it to the edge above.\n***STOPPING***"
				)
			bottom_by_index.setdefault(idx, []).append((kind, ref_id))
			continue
		if idx == n - 1:
			end_markers.append((kind, ref_id))
			continue
		markers_by_index.setdefault(idx, []).append((kind, ref_id))

	split_indices = sorted(i for i in set(markers_by_index) | set(bottom_by_index) if 0 < i < n - 1)
	boundaries = [0] + split_indices + [n - 1]
	new_nexus_id = {i: new_id_factory() for i in split_indices}

	segments = []
	for start, end in zip(boundaries, boundaries[1:]):
		segments.append(
			{
				"vertices": vertices[start : end + 1],
				"from_nexus_id": edge["from_nexus_id"] if start == 0 else new_nexus_id[start],
				"to_nexus_id": edge["to_nexus_id"] if end == n - 1 else new_nexus_id[end],
				"start_markers": markers_by_index.get(start, []),
				"bottom_markers": bottom_by_index.get(end, []),
			}
		)
	return segments, end_markers


def build_segment_writes(edge_row, srid, segments):
	"""Compute (no DB access) the writes `segments` (from break_edge) require against
	<output_schema>.streams: the first segment updates edge_row's own id in place, later
	segments become new rows sharing edge_row's non-geometric attributes. Returns
	(update_row, insert_rows, reassignments, last_segment_id):

	update_row is the single UPDATE params tuple for segment 0 (always present). insert_rows
	is the list of INSERT params tuples for segments 1..n. reassignments is
	{(kind, ref_id): new_segment_id} for every marker that ended up in a non-first segment, so
	the caller can fix up the referencing all_barriers/habitat_updates row -- a "barrier" marker
	produces a "barrier_downstream" entry (the segment it starts), and a habitat bottom point
	gets the segment it ends. last_segment_id is the id of the edge's final segment (edge_row's
	own id when it isn't split), where a "between" update's upstream point on an outlet edge's
	last vertex goes."""

	edge_id = edge_row["id"]
	update_row = None
	insert_rows = []
	reassignments = {}

	for seg_num, seg in enumerate(segments):
		if seg_num == 0:
			seg_id = edge_id
			wkb = linestring_zm_wkb(seg["vertices"])
			update_row = (seg["from_nexus_id"], seg["to_nexus_id"], wkb, srid, seg_id)
		else:
			seg_id = str(uuid.uuid4())
			add_edge(insert_rows, edge_row, seg_id, seg["from_nexus_id"], seg["to_nexus_id"], seg["vertices"], srid)
			for kind, ref_id in seg["start_markers"]:
				if kind == "barrier":
					reassignments[("barrier_downstream", ref_id)] = seg_id
				else:
					reassignments[(kind, ref_id)] = seg_id
			for kind, ref_id in seg["bottom_markers"]:
				reassignments[(kind, ref_id)] = seg_id

	return update_row, insert_rows, reassignments, seg_id


def add_edge(insert_rows, source_edge, edge_id, from_nexus_id, to_nexus_id, vertices, srid, is_sink_barrier_edge=False):
	"""Append the INSERT params for one new streams row to insert_rows (see flush_edge_inserts).
	The new edge gets the given id, nexus ids, geometry (vertices, a list of [x, y, z, m]) and
	is_sink_barrier_edge flag, and copies every other STREAM_FIELDS value from source_edge -- the
	edge it was split from, or the outlet edge a sink edge sits below."""

	values = {field: source_edge[field] for field in STREAM_FIELDS}
	values.update(id=edge_id, from_nexus_id=from_nexus_id, to_nexus_id=to_nexus_id)
	insert_rows.append(
		(*(values[field] for field in STREAM_FIELDS), linestring_zm_wkb(vertices), srid, is_sink_barrier_edge)
	)


def flush_segment_updates(cursor, output_schema, rows):
	if not rows:
		return
	schema_ident = quote_ident(output_schema)
	cursor.executemany(
		f"""
		UPDATE {schema_ident}.streams
		SET from_nexus_id = %s, to_nexus_id = %s,
			geometry = ST_SetSRID(ST_GeomFromWKB(%s), %s)
		WHERE id = %s
		""",
		rows,
	)


def flush_edge_inserts(cursor, output_schema, rows):
	"""Insert the new streams rows (split segments and sink edges) built by add_edge."""
	if not rows:
		return
	schema_ident = quote_ident(output_schema)
	cols = ", ".join(STREAM_FIELDS)
	placeholders = ", ".join(["%s"] * len(STREAM_FIELDS))
	cursor.executemany(
		f"""
		INSERT INTO {schema_ident}.streams
			({cols}, geometry, is_sink_barrier_edge)
		VALUES ({placeholders}, ST_SetSRID(ST_GeomFromWKB(%s), %s), %s)
		""",
		rows,
	)


def apply_edge_id_reassignments(cursor, output_schema, reassignments):
	schema_ident = quote_ident(output_schema)
	tables_and_columns = {
		"barrier_downstream": (f"{schema_ident}.all_barriers", "downstream_edge_id"),
		"habitat_upstream": (f"{schema_ident}.habitat_updates", "upstream_snapped_edge_id"),
		"habitat_downstream": (f"{schema_ident}.habitat_updates", "downstream_snapped_edge_id"),
	}

	grouped = {}
	for (kind, ref_id), new_edge_id in reassignments.items():
		grouped.setdefault(kind, []).append((new_edge_id, ref_id))

	total = len(reassignments)
	done = 0
	for kind, rows in grouped.items():
		table, column = tables_and_columns[kind]
		for i in range(0, len(rows), BATCH_SIZE):
			chunk = rows[i : i + BATCH_SIZE]
			cursor.executemany(f"UPDATE {table} SET {column} = %s WHERE id = %s", chunk)
			done += len(chunk)
			logger.info("  reassigned %d/%d edge id(s)", done, total)


def find_downstream_edge_ids(cursor, output_schema, to_nexus_ids):
	"""Return {to_nexus_id: downstream_edge_id} for every to_nexus_id that has an edge starting
	there (i.e. from_nexus_id matches) in <output_schema>.streams. A to_nexus_id with no entry is
	a network outlet -- nothing continues downstream of it. Normally one edge starts at a nexus.
	Where more than one does (a braid), the smallest id is picked so the result doesn't depend on
	row order; braids aren't supported, and graph_stats.build_graph stops the run at one later in
	Compute Statistics."""

	schema_ident = quote_ident(output_schema)
	cursor.execute(
		f"SELECT from_nexus_id, id FROM {schema_ident}.streams WHERE from_nexus_id = ANY(%s)",
		(list(to_nexus_ids),),
	)
	edges_at_nexus = {}
	for nexus_id, edge_id in cursor.fetchall():
		edges_at_nexus.setdefault(nexus_id, []).append(edge_id)
	return {nexus_id: min(edge_ids) for nexus_id, edge_ids in edges_at_nexus.items()}


def fetch_edges_with_markers(cursor, output_schema, edge_ids):
	schema_ident = quote_ident(output_schema)
	cols = ", ".join(STREAM_FIELDS)
	cursor.execute(
		f"SELECT {cols}, ST_AsBinary(geometry) FROM {schema_ident}.streams WHERE id = ANY(%s)",
		(edge_ids,),
	)
	rows = []
	for row in cursor.fetchall():
		edge_row = dict(zip(STREAM_FIELDS, row[:-1]))
		edge_row["vertices"] = edge_vertices(bytes(row[-1]))
		rows.append(edge_row)
	return rows


def break_network(conn, cursor, plan, srid):
	"""Run Compute Statistics step 2. Returns the number of new streams rows created -- split
	segments plus sink edges (i.e. the net increase in <output_schema>.streams row count)."""

	output_schema = plan["output_schema"]
	logger.info("  getting break points")
	by_edge, bottom_markers = get_break_points(cursor, output_schema)
	if not by_edge:
		logger.info("No barrier/habitat points to break the network at.")
		return 0

	logger.info("  fetch edges with markers")
	edges = fetch_edges_with_markers(cursor, output_schema, list(by_edge.keys()))
	nexus_to_downstream_edge = find_downstream_edge_ids(
		cursor, output_schema, [edge_row["to_nexus_id"] for edge_row in edges]
	)

	all_reassignments = {}
	new_segment_count = 0
	pending_updates = []
	pending_inserts = []  # split segments and sink edges, both from add_edge
	sink_edge_by_nexus = {}  # outlet to_nexus_id -> (sink edge id, its graph_id)
	outlet_habitat_cleared = 0
	edges_done = 0
	logger.info("  walk edges")
	for edge_row in edges:
		# end_markers holds barriers and habitat top points only; habitat bottom points are
		# already on a segment of this edge.
		segments, end_markers = break_edge(edge_row, by_edge[edge_row["id"]], bottom_markers)
		update_row, insert_rows, reassignments, last_segment_id = build_segment_writes(edge_row, srid, segments)
		pending_updates.append(update_row)
		pending_inserts.extend(insert_rows)
		all_reassignments.update(reassignments)
		new_segment_count += len(segments) - 1
		edges_done += 1

		downstream_edge_id = nexus_to_downstream_edge.get(edge_row["to_nexus_id"])
		if downstream_edge_id is not None:
			for kind, ref_id in end_markers:
				if kind == "barrier":
					all_reassignments[("barrier_downstream", ref_id)] = downstream_edge_id
				else:
					all_reassignments[(kind, ref_id)] = downstream_edge_id
		else:
			# Outlet edge: barriers on its last vertex go on a new zero-length sink edge below it
			# (one per outlet nexus, shared), so they sit at the start of an edge like every other
			# barrier. The habitat points here are top points: a "downstream" update's point is
			# cleared (nothing is downstream of an outlet), and a "between" update's upstream
			# point stays on this edge's last segment.
			for kind, ref_id in end_markers:
				if kind == "barrier":
					continue
				if kind == "habitat_downstream":
					all_reassignments[(kind, ref_id)] = None
					outlet_habitat_cleared += 1
				else:
					all_reassignments[(kind, ref_id)] = last_segment_id
			sink_barrier_ids = [ref_id for kind, ref_id in end_markers if kind == "barrier"]
			if sink_barrier_ids:
				# One sink edge per outlet nexus: outlet edges ending at the same point share it,
				# so it has every one of them as a predecessor.
				outlet_nexus_id = edge_row["to_nexus_id"]
				sink_edge_id, sink_graph_id = sink_edge_by_nexus.get(outlet_nexus_id, (None, None))
				if sink_edge_id is not None and edge_row["graph_id"] != sink_graph_id:
					sys.exit(
						f"Outlet nexus {outlet_nexus_id} has barriers snapped via outlet edges with different "
						f"graph_ids ({sink_graph_id}, {edge_row['graph_id']}); they can't share one sink edge."
					)
				if sink_edge_id is None:
					# Zero length: the outlet edge's last vertex twice (Z and M kept), from the
					# outlet nexus to a new one. Copies the outlet edge's other attributes, so it
					# lands in the same component, except the mainstem id, stream name and river
					# name id: the sink edge can sit below more than one river, so it has none.
					sink_edge_id = str(uuid.uuid4())
					last_vertex = edge_row["vertices"][-1]
					add_edge(
						pending_inserts,
						{**edge_row, "mainstem_id": None, "stream_name_en": None, "rivernameid1": None},
						sink_edge_id,
						outlet_nexus_id,
						str(uuid.uuid4()),
						[last_vertex, last_vertex],
						srid,
						is_sink_barrier_edge=True,
					)
					sink_edge_by_nexus[outlet_nexus_id] = (sink_edge_id, edge_row["graph_id"])
				for ref_id in sink_barrier_ids:
					all_reassignments[("barrier_downstream", ref_id)] = sink_edge_id

		if len(pending_updates) + len(pending_inserts) >= BATCH_SIZE:
			flush_segment_updates(cursor, output_schema, pending_updates)
			flush_edge_inserts(cursor, output_schema, pending_inserts)
			logger.info("    wrote segments for %d/%d edges", edges_done, len(edges))
			pending_updates.clear()
			pending_inserts.clear()

	# Every new edge is written before the reassignments, so the ids they point to exist.
	flush_segment_updates(cursor, output_schema, pending_updates)
	flush_edge_inserts(cursor, output_schema, pending_inserts)
	logger.info("    wrote segments for %d/%d edges", edges_done, len(edges))

	apply_edge_id_reassignments(cursor, output_schema, all_reassignments)
	conn.commit()

	sink_edge_count = len(sink_edge_by_nexus)
	logger.info(
		"Broke %d edge(s) into %d segment(s); added %d sink edge(s).",
		len(edges),
		len(edges) + new_segment_count,
		sink_edge_count,
	)
	if outlet_habitat_cleared:
		logger.info(
			"Cleared %d 'downstream' habitat update point(s) on a network outlet (nothing downstream).",
			outlet_habitat_cleared,
		)
	return new_segment_count + sink_edge_count
