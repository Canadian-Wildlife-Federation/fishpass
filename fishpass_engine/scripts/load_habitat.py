"""Process Habitat phase (fishpass/requirements/requirements.md Process Habitat, steps 1-2).

Step 2's per-point resolution has two cases -- a specific chyf_<end>_edge_id, or no edge id (use
the nearest streams edge instead) -- that otherwise share the same resolution logic: project the
point onto the edge, and either snap to an existing vertex within
habitat_point_snap_vertex_distance_m of that projected point or insert a new one, reusing the same
network_snap machinery as structure snapping. The two cases differ only in which edge is used and
what happens when the point is farther than habitat_point_snap_edge_distance_m from it: the
specific-edge-id case stops the whole run with an error, while the nearest-edge case just ignores
that point.

A per-run in-memory cache of edge_id -> vertex list is threaded through snap_habitat_points so
that multiple habitat points landing on the same edge in one run see each other's inserted
vertices, and each edge's geometry is only written back to the database once.
"""

import sys

import psycopg
import shapely

from db import quote_ident, quote_qualified_ident
from network_snap import (
	edge_vertices,
	linestring_zm_wkb,
	point_xyzm,
	snap_points_to_edge,
	write_edge_geometry,
)

LOCATION_TYPES = {"upstream", "downstream", "between"}


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
	cursor.execute(
		f"CREATE INDEX habitat_updates_points_idx ON {schema_ident}.habitat_updates USING gist (points);"
	)


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
		ORDER BY ST_Distance(geometry::geography, ST_SetSRID(ST_MakePoint(%s, %s), %s)::geography)
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


def resolve_point(cursor, output_schema, edge_cache, srid, xy, edge_distance_m, vertex_distance_m, specific_edge_id, end, habitat_id):
	"""Resolve one habitat point (upstream or downstream role, `end`) to (edge_id, (x, y, z, m)).

	If specific_edge_id is given, the point is projected onto that edge and must be within
	edge_distance_m of the edge itself (not a specific vertex). Exits (per requirements.md) if the
	edge doesn't exist or the point is too far from it.

	Otherwise, searches for the nearest streams edge within edge_distance_m. Returns None
	(meaning: this point is ignored, per requirements.md) if no edge is in range.

	Either way, once an edge and a projected point on it are known, the point snaps to an
	existing vertex on that edge (within vertex_distance_m) or a new vertex is inserted.
	"""

	if specific_edge_id is not None:
		found = find_edge_by_id(cursor, output_schema, specific_edge_id, xy, srid)
		if found is None:
			sys.exit(f"chyf_{end}_edge_id {specific_edge_id} not found in {output_schema}.streams"
				f"\n***STOPPING***"
			)
		edge_wkb, closest_point_wkb, dist = found
		if dist > edge_distance_m:
			sys.exit(				
				f"Habitat {end} point {habitat_id} is {dist:.1f}m from chyf_{end}_edge_id {specific_edge_id}, "
				f"exceeding habitat_point_snap_edge_distance_m ({edge_distance_m}m)."
				f"\n***STOPPING***"
			)
		return snap_to_edge(cursor, output_schema, edge_cache, specific_edge_id, edge_wkb, closest_point_wkb, vertex_distance_m)

	nearest = find_nearest_edge(cursor, output_schema, xy, srid, edge_distance_m)
	if nearest is None:
		return None

	edge_id, edge_wkb, closest_point_wkb = nearest
	return snap_to_edge(cursor, output_schema, edge_cache, edge_id, edge_wkb, closest_point_wkb, vertex_distance_m)


def points_for_role(location_type, coords, habitat_id):
	"""Return [(end, xy), ...] to resolve for this row, per requirements.md's per-location_type
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
	habitat_id, location_type, points_wkb, up_edge_id, down_edge_id = row
	geom = shapely.from_wkb(bytes(points_wkb))
	coords = [(p.x, p.y) for p in geom.geoms]

	specific_edge_id = {"upstream": up_edge_id, "downstream": down_edge_id}
	result = {}
	for end, xy in points_for_role(location_type, coords, habitat_id):
		result[end] = resolve_point(
			cursor, output_schema, edge_cache, srid, xy,
			edge_distance_m, vertex_distance_m, specific_edge_id[end], end, habitat_id,
		)
	return habitat_id, result.get("upstream"), result.get("downstream")


def write_habitat_snap_results(cursor, output_schema, srid, snap_results):
	schema_ident = quote_ident(output_schema)
	rows = []
	for habitat_id, up, down in snap_results:
		up_edge_id, up_xy = (up[0], up[1][:2]) if up else (None, None)
		down_edge_id, down_xy = (down[0], down[1][:2]) if down else (None, None)
		rows.append((
			up_edge_id, up_xy[0] if up_xy else None, up_xy[1] if up_xy else None,
			down_edge_id, down_xy[0] if down_xy else None, down_xy[1] if down_xy else None,
			habitat_id,
		))

	
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
		rows
	)


def snap_habitat_points(conn, cursor, plan, srid):
	"""Process Habitat step 2. Returns (total_rows, rows_with_an_ignored_point)."""

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
	ignored_count = 0
	for row in rows:
		habitat_id, up, down = process_habitat_row(
			cursor, output_schema, edge_cache, srid, row, edge_distance_m, vertex_distance_m
		)
		if (row[3] is None and up is None and row[1] in ("upstream", "between")) or \
		   (row[4] is None and down is None and row[1] in ("downstream", "between")):
			ignored_count += 1
		snap_results.append((habitat_id, up, down))

	for edge_id, entry in edge_cache.items():
		if entry["changed"]:
			write_edge_geometry(cursor, output_schema, edge_id, entry["vertices"], srid)

	if snap_results:
		write_habitat_snap_results(cursor, output_schema, srid, snap_results)

	conn.commit()
	print(
		f"Snapped {len(rows) - ignored_count}/{len(rows)} habitat update row(s) "
		f"({ignored_count} had an unresolvable point and were left partially unsnapped)."
	)
	return len(rows), ignored_count


def load_habitat(conn, cursor, plan, srid):
	"""Run the full Process Habitat phase for `plan`. Must be called after
	load_stream_network (needs <output_schema>.streams)."""

	output_schema = plan["output_schema"]
	create_habitat_updates_table(cursor, output_schema, srid)

	count = load_habitat_updates_rows(cursor, output_schema, plan)
	conn.commit()
	print(f"Loaded {count} habitat update(s) into {output_schema}.")

	snap_habitat_points(conn, cursor, plan, srid)
