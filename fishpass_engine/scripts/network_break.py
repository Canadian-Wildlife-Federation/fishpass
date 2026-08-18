"""Compute Statistics step 2 (fishpass/requirements/requirements.md): break the network at all
barrier and habitat points.

Design: structure/habitat snapping (Load Structures step 5, Process Habitat step 2) already
inserted a real vertex into <output_schema>.streams' geometry at every barrier/habitat location
that needed one -- so breaking never has to locate an arbitrary point along a line, only find
which *existing* vertex of an edge a marker sits on, and split the edge's row there.

A marker that lands on an edge's own first vertex is treated as sitting at the *start* of that
edge (rather than the end of whatever edge(s) feed into it) -- this is an arbitrary but
consistent convention that avoids ever needing to split an edge at its own last vertex (which
would produce a zero-length tail segment): any marker coinciding with edge E's last vertex is,
by construction, the same physical nexus as the first vertex of whatever edge continues
downstream from E, and is explicitly reassigned to that downstream edge's id (found via a
from_nexus_id lookup on E's to_nexus_id) rather than left on any segment of E itself. A marker at
the last vertex of a network outlet edge (no downstream continuation) is simply not attached to
anything -- a rare, harmless edge case since there is nothing downstream of an outlet to affect.

Only edges with at least one marker on them are touched; the (large majority of) edges with none
are left as single, unbroken segments.
"""

import uuid

from db import quote_ident
from network_snap import edge_vertices, linestring_zm_wkb

VERTEX_MATCH_TOLERANCE = 1e-9  # degrees -- exact float match expected, no reprojection occurs

STREAM_FIELDS = (
	"id", "aoi_id", "ef_type", "ef_subtype", "rank", "from_nexus_id", "to_nexus_id",
	"ecatchment_id", "mainstem_id", "graph_id", "is_isolated", "strahler_order",
)


def get_break_points(cursor, output_schema):
	"""Return {edge_id: [(x, y, kind, ref_id), ...]} for every streams edge with at least one
	barrier or habitat point snapped onto it. kind is 'barrier', 'habitat_upstream', or
	'habitat_downstream'; ref_id is the all_structures.id or habitat_updates.id, needed so their
	edge-id reference can be corrected if the marker ends up in a non-first segment."""

	schema_ident = quote_ident(output_schema)
	by_edge = {}

	cursor.execute(f"""
		SELECT snapped_edge_id, network_vertex_x, network_vertex_y, id
		FROM {schema_ident}.all_structures
		WHERE snapped_edge_id IS NOT NULL
	""")
	for edge_id, x, y, structure_id in cursor.fetchall():
		by_edge.setdefault(edge_id, []).append((x, y, "barrier", structure_id))

	cursor.execute(f"""
		SELECT upstream_snapped_edge_id, ST_X(upstream_snapped_point), ST_Y(upstream_snapped_point), id
		FROM {schema_ident}.habitat_updates
		WHERE upstream_snapped_edge_id IS NOT NULL
	""")
	for edge_id, x, y, habitat_id in cursor.fetchall():
		by_edge.setdefault(edge_id, []).append((x, y, "habitat_upstream", habitat_id))

	cursor.execute(f"""
		SELECT downstream_snapped_edge_id, ST_X(downstream_snapped_point), ST_Y(downstream_snapped_point), id
		FROM {schema_ident}.habitat_updates
		WHERE downstream_snapped_edge_id IS NOT NULL
	""")
	for edge_id, x, y, habitat_id in cursor.fetchall():
		by_edge.setdefault(edge_id, []).append((x, y, "habitat_downstream", habitat_id))

	return by_edge


def match_vertex_index(vertices, x, y):
	for i, (vx, vy, _z, _m) in enumerate(vertices):
		if abs(vx - x) < VERTEX_MATCH_TOLERANCE and abs(vy - y) < VERTEX_MATCH_TOLERANCE:
			return i
	return None


def break_edge(edge, points, new_id_factory=lambda: str(uuid.uuid4())):
	"""Split one edge's vertex list into consecutive segments at every internal vertex a marker
	lands on. `edge` is a dict with at least vertices/from_nexus_id/to_nexus_id. `points` is
	this edge's marker list from get_break_points. Returns (segments, end_markers):

	segments is a list of segment dicts {"vertices", "from_nexus_id", "to_nexus_id",
	"start_markers"} in edge order -- the first segment always starts at the original
	from_nexus_id, the last always ends at the original to_nexus_id, and every internal split
	point becomes a freshly generated nexus id shared by the segment ending there and the segment
	starting there.

	end_markers is the (kind, ref_id) list for markers matched at the edge's own last vertex --
	per this module's marker-attachment convention these belong at the *start* of whatever edge
	continues downstream, never on a segment of this edge, so the caller must resolve them
	against the downstream edge instead of folding them into `segments`.
	"""
	vertices = edge["vertices"]
	n = len(vertices)

	markers_by_index = {}
	end_markers = []
	for x, y, kind, ref_id in points:
		idx = match_vertex_index(vertices, x, y)
		if idx is None:
			continue  # unmatched
		if idx == n - 1:
			end_markers.append((kind, ref_id))
			continue
		markers_by_index.setdefault(idx, []).append((kind, ref_id))

	split_indices = sorted(i for i in markers_by_index if 0 < i < n - 1)
	boundaries = [0] + split_indices + [n - 1]
	new_nexus_id = {i: new_id_factory() for i in split_indices}

	segments = []
	for start, end in zip(boundaries, boundaries[1:]):
		segments.append({
			"vertices": vertices[start:end + 1],
			"from_nexus_id": edge["from_nexus_id"] if start == 0 else new_nexus_id[start],
			"to_nexus_id": edge["to_nexus_id"] if end == n - 1 else new_nexus_id[end],
			"start_markers": markers_by_index.get(start, []),
		})
	return segments, end_markers


def write_segments(cursor, output_schema, srid, edge_row, segments):
	"""Write `segments` (from break_edge) back to <output_schema>.streams: the first segment
	updates edge_row's own id in place, later segments are inserted as new rows sharing
	edge_row's non-geometric attributes. Returns {(kind, ref_id): new_segment_id} for every
	marker that ended up in a non-first segment, so the caller can fix up the referencing
	all_structures/habitat_updates row."""

	schema_ident = quote_ident(output_schema)
	edge_id = edge_row["id"]
	reassignments = {}

	for seg_num, seg in enumerate(segments):
		seg_id = edge_id if seg_num == 0 else str(uuid.uuid4())
		wkb = linestring_zm_wkb(seg["vertices"])

		if seg_num == 0:
			cursor.execute(
				f"""
				UPDATE {schema_ident}.streams
				SET from_nexus_id = %s, to_nexus_id = %s,
					geometry = ST_SetSRID(ST_GeomFromWKB(%s), %s),
					length = ST_Length(ST_SetSRID(ST_GeomFromWKB(%s), %s)::geography)
				WHERE id = %s
				""",
				(seg["from_nexus_id"], seg["to_nexus_id"], wkb, srid, wkb, srid, seg_id),
			)
		else:
			cols = ", ".join(STREAM_FIELDS)
			cursor.execute(
				f"""
				INSERT INTO {schema_ident}.streams
					({cols}, geometry, length)
				VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
						ST_SetSRID(ST_GeomFromWKB(%s), %s), ST_Length(ST_SetSRID(ST_GeomFromWKB(%s), %s)::geography))
				""",
				(
					seg_id, edge_row["aoi_id"], edge_row["ef_type"], edge_row["ef_subtype"], edge_row["rank"],
					seg["from_nexus_id"], seg["to_nexus_id"], edge_row["ecatchment_id"], edge_row["mainstem_id"],
					edge_row["graph_id"], edge_row["is_isolated"], edge_row["strahler_order"],
					wkb, srid, wkb, srid,
				),
			)

		if seg_num != 0:
			for kind, ref_id in seg["start_markers"]:
				reassignments[(kind, ref_id)] = seg_id

	return reassignments


def apply_edge_id_reassignments(cursor, output_schema, reassignments):
	schema_ident = quote_ident(output_schema)
	tables_and_columns = {
		"barrier": (f"{schema_ident}.all_structures", "snapped_edge_id"),
		"habitat_upstream": (f"{schema_ident}.habitat_updates", "upstream_snapped_edge_id"),
		"habitat_downstream": (f"{schema_ident}.habitat_updates", "downstream_snapped_edge_id"),
	}
	for (kind, ref_id), new_edge_id in reassignments.items():
		table, column = tables_and_columns[kind]
		cursor.execute(f"UPDATE {table} SET {column} = %s WHERE id = %s", (new_edge_id, ref_id))


def find_downstream_edge_ids(cursor, output_schema, to_nexus_ids):
	"""Return {to_nexus_id: downstream_edge_id} for every to_nexus_id that has an edge starting
	there (i.e. from_nexus_id matches) in <output_schema>.streams. A to_nexus_id with no entry is
	a network outlet -- nothing continues downstream of it."""

	schema_ident = quote_ident(output_schema)
	cursor.execute(
		f"SELECT from_nexus_id, id FROM {schema_ident}.streams WHERE from_nexus_id = ANY(%s)",
		(list(to_nexus_ids),),
	)
	return dict(cursor.fetchall())


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
	"""Run Compute Statistics step 2. Returns the number of new segments created (i.e. the net
	increase in <output_schema>.streams row count)."""

	output_schema = plan["output_schema"]
	by_edge = get_break_points(cursor, output_schema)
	if not by_edge:
		print("No barrier/habitat points to break the network at.")
		return 0

	edges = fetch_edges_with_markers(cursor, output_schema, list(by_edge.keys()))
	nexus_to_downstream_edge = find_downstream_edge_ids(
		cursor, output_schema, [edge_row["to_nexus_id"] for edge_row in edges]
	)

	all_reassignments = {}
	new_segment_count = 0
	for edge_row in edges:
		segments, end_markers = break_edge(edge_row, by_edge[edge_row["id"]])
		reassignments = write_segments(cursor, output_schema, srid, edge_row, segments)
		all_reassignments.update(reassignments)
		new_segment_count += len(segments) - 1

		downstream_edge_id = nexus_to_downstream_edge.get(edge_row["to_nexus_id"])
		if downstream_edge_id is not None:
			for kind, ref_id in end_markers:
				all_reassignments[(kind, ref_id)] = downstream_edge_id

	apply_edge_id_reassignments(cursor, output_schema, all_reassignments)
	conn.commit()

	print(f"Broke {len(edges)} edge(s) into {len(edges) + new_segment_count} segment(s).")
	return new_segment_count
