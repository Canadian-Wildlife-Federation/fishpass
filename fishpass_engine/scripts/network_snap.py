"""Shared geometry helpers for snapping points onto <output_schema>.streams edges, used by both
snap_structures.py and load_habitat.py.

Each caller's own SQL finds the candidate edge *and* the closest point on it (via
ST_LineLocatePoint/ST_LineInterpolatePoint), since the two callers query different source tables
and PostGIS already does that projection math natively. What's left here in Python is locating
that already-known closest point against the *current* vertex list -- which may have vertices
inserted earlier in the same run, by an earlier point snapping to the same edge -- and deciding
whether to snap to an existing vertex or insert a new one. That part is inherently sequential
across a batch (each point depends on prior insertions) and can't move into one set-based SQL
statement.
"""

import math

import shapely

from db import quote_ident

EARTH_RADIUS_M = 6_371_008.8  # mean Earth radius (IUGG); geometries are geographic (deg)


def haversine_m(lon1, lat1, lon2, lat2):
	"""Great-circle distance in metres between two lon/lat points (degrees)."""
	phi1, phi2 = math.radians(lat1), math.radians(lat2)
	dphi = math.radians(lat2 - lat1)
	dlambda = math.radians(lon2 - lon1)
	a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
	return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def edge_vertices(wkb_bytes):
	"""Return an (N, 4) list of [lon, lat, z, m] for a LineString edge, preserving both Z and M
	so a rewritten edge (see write_edge_geometry) doesn't lose elevation data. Requires
	shapely>=2.1 / GEOS>=3.12, same as gradient_barriers. Vertex order matches the geometry's
	native (upstream -> downstream, per chyf_loader's digitizing convention) order."""
	geom = shapely.from_wkb(wkb_bytes)
	coords = shapely.get_coordinates(geom, include_z=True, include_m=True)
	return coords.tolist()


def point_xyzm(wkb_bytes):
	"""Decode a single POINT ZM's WKB (as returned by a caller's closest-point-on-edge SQL) into
	(x, y, z, m)."""
	geom = shapely.from_wkb(wkb_bytes)
	x, y, z, m = shapely.get_coordinates(geom, include_z=True, include_m=True)[0]
	return x, y, z, m


def locate_segment(vertices, point):
	"""Return the index of the segment (vertices[i] -> vertices[i+1]) that `point` (x, y) --
	already known to lie on the polyline, per SQL's ST_LineInterpolatePoint against the edge's
	original geometry -- falls on in the *current* `vertices` list, which may have vertices
	inserted earlier in this run (insertions never change the line's path, so `point` still lies
	exactly on it). Picks the segment with the smallest perpendicular distance to `point`, to stay
	robust to floating-point noise rather than requiring an exact match.
	"""
	best = None
	for i in range(len(vertices) - 1):
		x1, y1, _z1, _m1 = vertices[i]
		x2, y2, _z2, _m2 = vertices[i + 1]
		seg_len_sq = (x2 - x1) ** 2 + (y2 - y1) ** 2
		if seg_len_sq == 0:
			t = 0.0
		else:
			t = ((point[0] - x1) * (x2 - x1) + (point[1] - y1) * (y2 - y1)) / seg_len_sq
			t = max(0.0, min(1.0, t))
		px, py = x1 + t * (x2 - x1), y1 + t * (y2 - y1)
		dist = math.hypot(px - point[0], py - point[1])
		if best is None or dist < best[0]:
			best = (dist, i)
	return best[1]


def snap_points_to_edge(edge_wkb, points, vertex_distance_m):
	"""Snap every (item_id, px, py, pz, pm) in `points` -- each already the closest point on this
	edge, per the caller's SQL -- onto that edge's vertices, inserting new vertices as needed.

	Each point is resolved against the *current* vertex list (including any vertices already
	inserted earlier in this same call, for a prior point on this edge), so multiple points
	snapping to one edge in a single run are handled consistently.

	Returns (vertices, results, changed) -- the edge's (possibly extended) vertex list, a list
	of (item_id, snap_x, snap_y, snap_z, snap_m), and whether any vertex was inserted (i.e.
	whether the caller needs to write the edge geometry back).
	"""
	vertices = edge_vertices(edge_wkb)
	changed = False
	results = []

	for item_id, px, py, pz, pm in points:
		seg_index = locate_segment(vertices, (px, py))
		v1, v2 = vertices[seg_index], vertices[seg_index + 1]
		d1 = haversine_m(px, py, v1[0], v1[1])
		d2 = haversine_m(px, py, v2[0], v2[1])
		nearest_v, nearest_d = (v1, d1) if d1 <= d2 else (v2, d2)

		if nearest_d <= vertex_distance_m:
			results.append((item_id, nearest_v[0], nearest_v[1], nearest_v[2], nearest_v[3]))
		else:
			vertices = vertices[:seg_index + 1] + [[px, py, pz, pm]] + vertices[seg_index + 1:]
			changed = True
			results.append((item_id, px, py, pz, pm))

	return vertices, results, changed


def linestring_zm_wkb(vertices):
	"""Build WKB for a LINESTRING ZM from a list of [x, y, z, m]. shapely's array-based
	LineString constructor only accepts 2 or 3 ordinates (no direct XYZM support as of
	shapely 2.1), so this goes through WKT text, which GEOS parses as true 4D ZM."""
	coords_str = ", ".join(f"{x} {y} {z} {m}" for x, y, z, m in vertices)
	geom = shapely.from_wkt(f"LINESTRING ZM ({coords_str})")
	return shapely.to_wkb(geom)


def write_edge_geometry(cursor, output_schema, edge_id, vertices, srid):
	"""Rewrite <output_schema>.streams' geometry for edge_id to `vertices` (a list of
	[x, y, z, m]), e.g. after snap_points_to_edge reports changed=True."""
	wkb = linestring_zm_wkb(vertices)
	cursor.execute(
		f"UPDATE {quote_ident(output_schema)}.streams SET geometry = ST_SetSRID(ST_GeomFromWKB(%s), %s) WHERE id = %s",
		(wkb, srid, edge_id),
	)
