"""Process Habitat phase (fishpass/docs/fishpass_docs.md Process Habitat, steps 1-2).

Step 2's per-point resolution has two cases -- a specific chyf_<end>_edge_id, or no edge id (use
the nearest streams edge instead) -- that otherwise share the same resolution logic: project the
point onto the edge, and either snap to an existing vertex within
habitat_point_snap_vertex_distance_m of that projected point or insert a new one, reusing the same
network_snap machinery as structure snapping. The two cases differ only in which edge is used and
what happens when the point is farther than habitat_point_snap_edge_distance_m from it: the
specific-edge-id case stops the whole run with an error, while the nearest-edge case just ignores
that point.

A habitat "bottom" point -- the point of an "upstream" update, or the downstream point of a
"between" update -- marks the downstream end of the habitat, so Compute Statistics attaches it to
the stream segment ending at it (see network_break). When such a point snaps onto a nexus (the
first or last vertex of an edge), which edge it was snapped to decides which river the habitat
follows, so resolve_bottom_point_at_nexus picks the edge above the nexus it belongs to and
records the point as that edge's last vertex. The case table is in fishpass_docs.md (Process
Habitat).

A per-run in-memory cache of edge_id -> vertex list is threaded through snap_habitat_points so
that multiple habitat points landing on the same edge in one run see each other's inserted
vertices, and each edge's geometry is only written back to the database once.
"""

import logging
import sys

import shapely

from db import quote_ident, quote_qualified_ident
from utils import is_bottom_point
from network_snap import (
	edge_vertices,
	linestring_zm_wkb,
	point_xyzm,
	snap_points_to_edge,
	write_edge_geometry,
)

LOCATION_TYPES = {"upstream", "downstream", "between"}

# A habitat point whose distances to two edges at a nexus differ by no more than this is equally
# close to both (in practice: it sits exactly on the nexus).
TIE_TOLERANCE_M = 0.000001

logger = logging.getLogger(__name__)


def create_habitat_updates_table(cursor, output_schema, srid):
	schema_ident = quote_ident(output_schema)
	cursor.execute(f"""
		CREATE TABLE {schema_ident}.habitat_updates (
			id uuid PRIMARY KEY,
			species_lifestage varchar[] NOT NULL,
			update_scope varchar NOT NULL,
			points geometry(multipoint, {srid}) NOT NULL,
			location_type varchar NOT NULL CHECK (location_type IN ('upstream', 'downstream', 'between')),
			chyf_upstream_edge_id uuid,
			chyf_downstream_edge_id uuid,
			update_source varchar,
			update_date date,
			notes varchar,
			upstream_snapped_edge_id uuid,
			upstream_snapped_point geometry(point, {srid}),
			downstream_snapped_edge_id uuid,
			downstream_snapped_point geometry(point, {srid})
		);
	""")
	cursor.execute(f"CREATE INDEX habitat_updates_points_idx ON {schema_ident}.habitat_updates USING gist (points);")


def load_habitat_updates_rows(cursor, output_schema, plan):
	"""Process Habitat step 1. Returns the number of rows inserted."""

	table_ident = quote_qualified_ident(plan["habitat_update_table"])
	schema_ident = quote_ident(output_schema)
	cursor.execute(
		f"""
		INSERT INTO {schema_ident}.habitat_updates
			(id, species_lifestage, update_scope, points, location_type,
			 chyf_upstream_edge_id, chyf_downstream_edge_id, update_source, update_date, notes)
		SELECT id, species_lifestage, update_scope, points, location_type,
			   chyf_upstream_edge_id, chyf_downstream_edge_id, update_source, update_date, notes
		FROM {table_ident} src
		WHERE (src.update_scope = 'all' OR src.update_scope = %s)
		  AND EXISTS (
			  SELECT 1 FROM {schema_ident}.streams e
			  WHERE ST_DWithin(e.geometry::geography, src.points::geography, %s)
		  )
		""",
		(plan["update_scope"], plan["habitat_point_snap_edge_distance_m"]),
	)
	return cursor.rowcount


def find_nearest_edge(cursor, output_schema, xy, srid, edge_distance_m):
	"""Return (edge_id, edge_wkb, closest_point_wkb) for the streams edge nearest to xy within
	edge_distance_m, with closest_point_wkb being the closest point on that edge to xy, or None
	if none is in range."""

	schema_ident = quote_ident(output_schema)
	cursor.execute(
		f"""
		SELECT
			id, ST_AsBinary(geometry),
			ST_AsBinary(ST_LineInterpolatePoint(
				geometry,
				ST_LineLocatePoint(geometry, ST_SetSRID(ST_MakePoint(%s, %s), %s))
			))
		FROM {schema_ident}.streams
		WHERE ST_DWithin(geometry::geography, ST_SetSRID(ST_MakePoint(%s, %s), %s)::geography, %s)
		ORDER BY ST_Distance(geometry::geography, ST_SetSRID(ST_MakePoint(%s, %s), %s)::geography), id
		LIMIT 1
		""",
		(xy[0], xy[1], srid, xy[0], xy[1], srid, edge_distance_m, xy[0], xy[1], srid),
	)
	row = cursor.fetchone()
	if row is None:
		return None
	return row[0], bytes(row[1]), bytes(row[2])


def get_cached_edge(cursor, output_schema, edge_cache, edge_id, edge_wkb=None):
	"""Return edge_cache[edge_id] (a {"vertices", "changed"} dict), fetching and populating it
	from the database first if this is the first time this edge has been touched this run.
	Returns None if edge_id doesn't exist in <output_schema>.streams."""

	if edge_id not in edge_cache:
		if edge_wkb is None:
			cursor.execute(
				f"SELECT ST_AsBinary(geometry) FROM {quote_ident(output_schema)}.streams WHERE id = %s",
				(edge_id,),
			)
			row = cursor.fetchone()
			if row is None:
				return None
			edge_wkb = bytes(row[0])
		edge_cache[edge_id] = {"vertices": edge_vertices(edge_wkb), "changed": False}
	return edge_cache[edge_id]


def find_edge_by_id(cursor, output_schema, edge_id, xy, srid):
	"""Return (edge_wkb, closest_point_wkb, distance_m) for the streams edge with id=edge_id,
	with closest_point_wkb being the closest point on that edge to xy and distance_m the distance
	from xy to that closest point. Returns None if edge_id doesn't exist in
	<output_schema>.streams."""

	schema_ident = quote_ident(output_schema)
	cursor.execute(
		f"""
		SELECT
			ST_AsBinary(geometry),
			ST_AsBinary(ST_LineInterpolatePoint(
				geometry,
				ST_LineLocatePoint(geometry, ST_SetSRID(ST_MakePoint(%s, %s), %s))
			)),
			ST_Distance(geometry::geography, ST_SetSRID(ST_MakePoint(%s, %s), %s)::geography)
		FROM {schema_ident}.streams
		WHERE id = %s
		""",
		(xy[0], xy[1], srid, xy[0], xy[1], srid, edge_id),
	)
	row = cursor.fetchone()
	if row is None:
		return None
	return bytes(row[0]), bytes(row[1]), row[2]


def snap_to_edge(cursor, output_schema, edge_cache, edge_id, edge_wkb, closest_point_wkb, vertex_distance_m):
	"""Snap the already-known closest_point_wkb (a point on edge_id, per the caller's SQL
	projection) onto edge_id's vertices -- an existing vertex within vertex_distance_m, or a newly
	inserted one -- updating edge_cache in place. Returns (edge_id, (x, y, z, m))."""

	entry = get_cached_edge(cursor, output_schema, edge_cache, edge_id, edge_wkb=edge_wkb)
	current_wkb = linestring_zm_wkb(entry["vertices"])
	px, py, pz, pm = point_xyzm(closest_point_wkb)
	new_vertices, results, changed = snap_points_to_edge(current_wkb, [("pt", px, py, pz, pm)], vertex_distance_m)
	entry["vertices"] = new_vertices
	entry["changed"] = entry["changed"] or changed
	_, x, y, z, m = results[0]
	return edge_id, (x, y, z, m)


def fetch_nexus_edges(cursor, output_schema, edge_id, at_last_vertex, xy, srid):
	"""Return every streams edge touching the nexus at edge_id's last vertex (at_last_vertex) or
	first vertex, as a list of {"id", "mainstem_id", "is_input", "distance_m", "wkb"} ordered by
	id. is_input is True for an edge that ends at the nexus (flows into it) and False for the edge
	that starts there. distance_m is from the edge to xy, the habitat point as it was entered.
	Edges are matched on nexus id only (indexed; see load_stream_network.create_indexes)."""

	schema_ident = quote_ident(output_schema)
	nexus_column = "to_nexus_id" if at_last_vertex else "from_nexus_id"
	cursor.execute(
		f"""
		SELECT e.id, e.mainstem_id, e.to_nexus_id = n.nexus_id,
			ST_Distance(e.geometry::geography, ST_SetSRID(ST_MakePoint(%s, %s), %s)::geography),
			ST_AsBinary(e.geometry)
		FROM {schema_ident}.streams e
		JOIN (SELECT {nexus_column} AS nexus_id FROM {schema_ident}.streams WHERE id = %s) n
			ON e.to_nexus_id = n.nexus_id OR e.from_nexus_id = n.nexus_id
		ORDER BY e.id
		""",
		(xy[0], xy[1], srid, edge_id),
	)
	return [
		{"id": row[0], "mainstem_id": row[1], "is_input": row[2], "distance_m": row[3], "wkb": bytes(row[4])}
		for row in cursor.fetchall()
	]


def same_mainstem_input(inputs, below_edge, end, habitat_id):
	"""Return the edge in `inputs` (the edges flowing into a nexus) with the same mainstem_id as
	below_edge, the edge leaving the nexus. A NULL mainstem_id matches nothing, not even another
	NULL. Exits if there is no match: a mainstem that has edges above it always continues up one
	of them, and an edge with no mainstem_id can't say which river to follow."""

	mainstem_id = below_edge["mainstem_id"]
	if mainstem_id is not None:
		for edge in inputs:
			if edge["mainstem_id"] == mainstem_id:
				return edge
	reason = "has no mainstem_id" if mainstem_id is None else f"has mainstem_id {mainstem_id}, which none of them share"
	sys.exit(
		f"Habitat {end} point {habitat_id} is at the start of streams edge {below_edge['id']}, so it belongs "
		f"to one of the edges flowing into it ({', '.join(str(e['id']) for e in inputs)}), but which one "
		f"can't be determined: edge {below_edge['id']} {reason}. Set chyf_{end}_edge_id on the habitat "
		f"update to one of those edges.\n***STOPPING***"
	)


def fetch_mainstem_id(cursor, output_schema, edge_id):
	"""Return the mainstem_id of streams edge edge_id (None if it has none, or isn't found).

	Used only for a "between" row's downstream point on an ambiguous nexus, to look up the edge
	its upstream point snapped to. That edge's row was already read when the upstream point
	snapped (find_edge_by_id / find_nearest_edge), so this query could be avoided. It runs rarely,
	so it was left as is. Options if it ever matters:
	- Select mainstem_id in those two queries and keep it in the edge cache next to the vertices
	  (smaller change; every path that adds to the cache would need to supply it).
	- Return mainstem_id from snap_to_edge / resolve_point alongside (edge_id, (x, y, z, m))
	  (touches every caller and many tests).
	"""

	cursor.execute(f"SELECT mainstem_id FROM {quote_ident(output_schema)}.streams WHERE id = %s", (edge_id,))
	row = cursor.fetchone()
	return row[0] if row else None


def choose_input_edge(nexus_edges, specific_edge_id, end, habitat_id, top_mainstem_id=None):
	"""Pick which of the edges flowing into a nexus a habitat bottom point sitting on that nexus
	belongs to. nexus_edges is fetch_nexus_edges' list. specific_edge_id is the row's
	chyf_<end>_edge_id, given only when the point is on that edge's first vertex. top_mainstem_id
	is, for a "between" row, the mainstem_id of the edge its upstream point snapped to. Returns
	the chosen edge's dict, or None if no edge flows into the nexus (a headwater: nothing is
	upstream, so the point applies to nothing).

	- specific_edge_id given: the input edge with that edge's mainstem_id (error if none).
	- One input edge: that edge.
	- Several input edges, and one of them is clearly closest to the point as entered: that edge.
	- Several input edges otherwise (the outflow edge is clearly closest, or two or more edges
	  are equally close, within TIE_TOLERANCE_M): the point alone doesn't say which input edge it
	  is on.
	  - If an input edge has top_mainstem_id, that edge: the row's upstream point is on that river.
	  - Otherwise the input edge with the outflow edge's mainstem_id, with a warning if it was a
	    tie. If the nexus is a network outlet there is no outflow edge to go by, so the run stops.
	"""

	inputs = [edge for edge in nexus_edges if edge["is_input"]]
	outflows = [edge for edge in nexus_edges if not edge["is_input"]]
	if not inputs:
		return None

	if specific_edge_id is not None:
		named_edge = next(edge for edge in outflows if edge["id"] == specific_edge_id)
		return same_mainstem_input(inputs, named_edge, end, habitat_id)

	if len(inputs) == 1:
		return inputs[0]

	ranked = sorted(nexus_edges, key=lambda edge: edge["distance_m"])
	closest = [edge for edge in ranked if edge["distance_m"] - ranked[0]["distance_m"] <= TIE_TOLERANCE_M]
	tied = len(closest) > 1
	if not tied and closest[0]["is_input"]:
		return closest[0]

	if top_mainstem_id is not None:
		for edge in inputs:
			if edge["mainstem_id"] == top_mainstem_id:
				return edge

	tied_ids = ", ".join(str(edge["id"]) for edge in closest)
	if not outflows:
		# Only reached on a tie: with no outflow edge, a single closest edge is an input edge.
		sys.exit(
			f"Habitat {end} point {habitat_id} is equally close to streams edges {tied_ids}, which end at "
			f"the same network outlet, so the edge it belongs to can't be determined. Set "
			f"chyf_{end}_edge_id on the habitat update.\n***STOPPING***"
		)

	# More than one outflow edge is a braid, which stops the run in Compute Statistics.
	chosen = same_mainstem_input(inputs, outflows[0], end, habitat_id)
	if tied:
		logger.warning(
			"Habitat %s point %s is equally close to streams edges %s at a confluence; using edge %s, "
			"which is on the same mainstem as the edge below the confluence (%s). "
			"Set chyf_%s_edge_id on the habitat update to choose the edge.",
			end,
			habitat_id,
			tied_ids,
			chosen["id"],
			outflows[0]["id"],
			end,
		)
	return chosen


def resolve_bottom_point_at_nexus(
	cursor, output_schema, edge_cache, srid, xy, snapped, specific_edge_id, end, habitat_id, top_edge_id=None
):
	"""Settle which edge a snapped habitat bottom point belongs to. `snapped` is snap_to_edge's
	(edge_id, (x, y, z, m)). Returns the same shape, or None if the point applies to nothing.

	A bottom point is attached to the segment ending at it, so it has to be on the edge above it.
	In the middle of an edge, or on the last vertex of the edge named by chyf_<end>_edge_id, it
	already is, and `snapped` is returned unchanged. Otherwise it is on a nexus, and is moved to
	the last vertex of the input edge choose_input_edge picks (no vertex is inserted).
	top_edge_id is, for a "between" row, the edge its upstream point snapped to; where the point
	alone doesn't settle the choice, that edge's river does."""

	edge_id, (x, y, _z, _m) = snapped
	vertices = edge_cache[edge_id]["vertices"]
	at_last_vertex = (x, y) == tuple(vertices[-1][:2])
	at_first_vertex = (x, y) == tuple(vertices[0][:2])
	if not (at_last_vertex or at_first_vertex):
		return snapped
	if at_last_vertex and specific_edge_id is not None:
		return snapped

	nexus_edges = fetch_nexus_edges(cursor, output_schema, edge_id, at_last_vertex, xy, srid)
	top_mainstem_id = None
	if top_edge_id is not None and specific_edge_id is None:
		top_mainstem_id = fetch_mainstem_id(cursor, output_schema, top_edge_id)
	chosen = choose_input_edge(nexus_edges, specific_edge_id, end, habitat_id, top_mainstem_id)
	if chosen is None:
		logger.info(
			"Habitat %s point %s is at the top of the stream network (no edge flows into it); "
			"nothing is upstream of it, so it is ignored.",
			end,
			habitat_id,
		)
		return None

	entry = get_cached_edge(cursor, output_schema, edge_cache, chosen["id"], edge_wkb=chosen["wkb"])
	return chosen["id"], tuple(entry["vertices"][-1])


def resolve_point(
	cursor,
	output_schema,
	edge_cache,
	srid,
	xy,
	edge_distance_m,
	vertex_distance_m,
	specific_edge_id,
	end,
	habitat_id,
	is_bottom=False,
	top_edge_id=None,
):
	"""Resolve one habitat point (upstream or downstream role, `end`) to (edge_id, (x, y, z, m)).

	If specific_edge_id is given, the point is projected onto that edge and must be within
	edge_distance_m of the edge itself (not a specific vertex). Exits if the
	edge doesn't exist or the point is too far from it.

	Otherwise, searches for the nearest streams edge within edge_distance_m. Returns None
	(meaning: this point is ignored) if no edge is in range.

	Either way, once an edge and a projected point on it are known, the point snaps to an
	existing vertex on that edge (within vertex_distance_m) or a new vertex is inserted.

	A bottom point (is_bottom, see is_bottom_point) that lands on a nexus is then moved to the
	edge above the nexus that it belongs to, or ignored (None) if there is none -- see
	resolve_bottom_point_at_nexus, which also uses top_edge_id (the edge a "between" row's
	upstream point snapped to).
	"""

	if specific_edge_id is not None:
		found = find_edge_by_id(cursor, output_schema, specific_edge_id, xy, srid)
		if found is None:
			sys.exit(f"chyf_{end}_edge_id {specific_edge_id} not found in {output_schema}.streams\n***STOPPING***")
		edge_wkb, closest_point_wkb, dist = found
		if dist > edge_distance_m:
			sys.exit(
				f"Habitat {end} point {habitat_id} is {dist:.1f}m from chyf_{end}_edge_id {specific_edge_id}, "
				f"exceeding habitat_point_snap_edge_distance_m ({edge_distance_m}m)."
				f"\n***STOPPING***"
			)
		snapped = snap_to_edge(
			cursor, output_schema, edge_cache, specific_edge_id, edge_wkb, closest_point_wkb, vertex_distance_m
		)
	else:
		nearest = find_nearest_edge(cursor, output_schema, xy, srid, edge_distance_m)
		if nearest is None:
			return None

		edge_id, edge_wkb, closest_point_wkb = nearest
		snapped = snap_to_edge(
			cursor, output_schema, edge_cache, edge_id, edge_wkb, closest_point_wkb, vertex_distance_m
		)

	if is_bottom:
		return resolve_bottom_point_at_nexus(
			cursor, output_schema, edge_cache, srid, xy, snapped, specific_edge_id, end, habitat_id, top_edge_id
		)
	return snapped


def points_for_role(location_type, coords, habitat_id):
	"""Return [(end, xy), ...] to resolve for this row, per the per-location_type
	point-count rules (points[0] is always the upstream point, points[1] the downstream point,
	for a 'between' row)."""

	if location_type == "upstream":
		if len(coords) != 1:
			sys.exit(f"habitat_updates row {habitat_id}: location_type=upstream requires exactly 1 point")
		return [("upstream", coords[0])]
	if location_type == "downstream":
		if len(coords) != 1:
			sys.exit(f"habitat_updates row {habitat_id}: location_type=downstream requires exactly 1 point")
		return [("downstream", coords[0])]
	if location_type == "between":
		if len(coords) != 2:
			sys.exit(f"habitat_updates row {habitat_id}: location_type=between requires exactly 2 points")
		return [("upstream", coords[0]), ("downstream", coords[1])]
	sys.exit(f"habitat_updates row {habitat_id}: unknown location_type {location_type!r}")


def process_habitat_row(cursor, output_schema, edge_cache, srid, row, edge_distance_m, vertex_distance_m):
	"""Resolve one habitat_updates row's point(s). Returns (habitat_id, upstream, downstream),
	each point as resolve_point's (edge_id, (x, y, z, m)) or None.

	A "between" row whose two points resolve to the same place has no stream between them, so
	both are returned as None (the row then applies to no edges) and a warning is logged."""

	habitat_id, location_type, points_wkb, up_edge_id, down_edge_id = row
	geom = shapely.from_wkb(bytes(points_wkb))
	coords = [(p.x, p.y) for p in geom.geoms]

	specific_edge_id = {"upstream": up_edge_id, "downstream": down_edge_id}
	result = {}
	for end, xy in points_for_role(location_type, coords, habitat_id):
		# A "between" row's upstream point is resolved first, so its edge is known here.
		top = result.get("upstream") if location_type == "between" and end == "downstream" else None
		result[end] = resolve_point(
			cursor,
			output_schema,
			edge_cache,
			srid,
			xy,
			edge_distance_m,
			vertex_distance_m,
			specific_edge_id[end],
			end,
			habitat_id,
			is_bottom=is_bottom_point(location_type, end),
			top_edge_id=top[0] if top else None,
		)

	up, down = result.get("upstream"), result.get("downstream")
	if location_type == "between" and up is not None and down is not None and up[1][:2] == down[1][:2]:
		logger.warning(
			"Habitat update %s: its upstream and downstream points both snap to (%s, %s) on the stream "
			"network, so there is no stream between them and the update is ignored. Points within "
			"habitat_point_snap_vertex_distance_m of the same vertex snap to that vertex.",
			habitat_id,
			up[1][0],
			up[1][1],
		)
		up = down = None
	return habitat_id, up, down


def write_habitat_snap_results(cursor, output_schema, srid, snap_results):
	schema_ident = quote_ident(output_schema)
	rows = []
	for habitat_id, up, down in snap_results:
		up_edge_id, up_xy = (up[0], up[1][:2]) if up else (None, None)
		down_edge_id, down_xy = (down[0], down[1][:2]) if down else (None, None)
		rows.append(
			(
				up_edge_id,
				up_xy[0] if up_xy else None,
				up_xy[1] if up_xy else None,
				down_edge_id,
				down_xy[0] if down_xy else None,
				down_xy[1] if down_xy else None,
				habitat_id,
			)
		)

	cursor.executemany(
		f"""
		UPDATE {schema_ident}.habitat_updates AS h
		SET upstream_snapped_edge_id = v.up_edge_id,
			upstream_snapped_point = CASE WHEN v.up_x IS NULL THEN NULL
				ELSE ST_SetSRID(ST_MakePoint(v.up_x, v.up_y), {srid}) END,
			downstream_snapped_edge_id = v.down_edge_id,
			downstream_snapped_point = CASE WHEN v.down_x IS NULL THEN NULL
				ELSE ST_SetSRID(ST_MakePoint(v.down_x, v.down_y), {srid}) END
		FROM (VALUES (%s::uuid, %s::double precision, %s::double precision,
				 %s::uuid, %s::double precision, %s::double precision, %s::uuid)) AS v(up_edge_id, up_x, up_y, down_edge_id, down_x, down_y, habitat_id)
		WHERE h.id = v.habitat_id
		""",
		rows,
	)


def snap_habitat_points(conn, cursor, plan, srid):
	"""Process Habitat steps 2-3. Returns (total_rows, unapplied_rows), where unapplied_rows is
	the number of rows left without a point they need -- it didn't snap, or was ignored (a bottom
	point at the top of the network, or a "between" row with no stream between its points) -- and
	which therefore apply to no edges."""

	output_schema = plan["output_schema"]
	edge_distance_m = plan["habitat_point_snap_edge_distance_m"]
	vertex_distance_m = plan["habitat_point_snap_vertex_distance_m"]

	cursor.execute(f"""
		SELECT id, location_type, ST_AsBinary(points), chyf_upstream_edge_id, chyf_downstream_edge_id
		FROM {quote_ident(output_schema)}.habitat_updates
	""")
	rows = cursor.fetchall()

	edge_cache = {}
	snap_results = []
	unapplied_count = 0
	for row in rows:
		habitat_id, up, down = process_habitat_row(
			cursor, output_schema, edge_cache, srid, row, edge_distance_m, vertex_distance_m
		)
		location_type = row[1]
		if (up is None and location_type in ("upstream", "between")) or (
			down is None and location_type in ("downstream", "between")
		):
			unapplied_count += 1
		snap_results.append((habitat_id, up, down))

	for edge_id, entry in edge_cache.items():
		if entry["changed"]:
			write_edge_geometry(cursor, output_schema, edge_id, entry["vertices"], srid)

	if snap_results:
		write_habitat_snap_results(cursor, output_schema, srid, snap_results)

	# create after the data is loaded so data loading isn't affected
	cursor.execute(
		f"CREATE INDEX habitat_updates_upstream_snapped_edge_id_idx ON {quote_ident(output_schema)}.habitat_updates (upstream_snapped_edge_id);"
	)
	cursor.execute(
		f"CREATE INDEX habitat_updates_downstream_snapped_edge_id_idx ON {quote_ident(output_schema)}.habitat_updates (downstream_snapped_edge_id);"
	)

	conn.commit()
	logger.info(
		"Snapped %d/%d habitat update row(s); %d have a point that didn't snap or was ignored, "
		"so they apply to no edges.",
		len(rows) - unapplied_count,
		len(rows),
		unapplied_count,
	)
	return len(rows), unapplied_count


def load_habitat(conn, cursor, plan, srid):
	"""Run the full Process Habitat phase for `plan`. Must be called after
	load_stream_network (needs <output_schema>.streams)."""

	output_schema = plan["output_schema"]
	create_habitat_updates_table(cursor, output_schema, srid)

	count = load_habitat_updates_rows(cursor, output_schema, plan)
	conn.commit()
	logger.info("Loaded %d habitat update(s) into %s.", count, output_schema)

	snap_habitat_points(conn, cursor, plan, srid)
