"""Load Structures step 5 (fishpass/requirements/requirements.md): snap
<output_schema>.all_structures to the <output_schema>.streams network.

Candidate edge search (within structure_snap_edge_distance_m) uses PostGIS geography distance
in SQL -- structure counts are small enough (thousands, not the 10M-edge network) that this bulk
join is cheap, unlike the network-wide walks gradient_barriers deliberately keeps out of SQL.
The rest (locating the exact snap point, inserting a vertex if needed) is shared with habitat
point snapping -- see network_snap.py.
"""

import psycopg

from db import quote_ident
from network_snap import linestring_zm_wkb, point_xyzm, snap_points_to_edge

SNAPPED_GEOMETRY_SRID = 4617
BATCH_SIZE = 5000  # rows per bulk write; bounds memory, not a correctness requirement


def fetch_candidate_matches(cursor, output_schema, edge_distance_m):
	"""Return (structure_id, edge_id, edge_wkb, closest_point_wkb) for every unsnapped structure
	that has a streams edge within edge_distance_m, matched to its single nearest such edge, with
	closest_point_wkb being the closest point on that edge to the structure. Structures with no
	edge in range are simply absent from the result."""

	schema_ident = quote_ident(output_schema)
	cursor.execute(
		f"""
		SELECT
    		s.id,
			e.id,
			ST_AsBinary(e.geometry),
			ST_AsBinary(ST_LineInterpolatePoint(e.geometry, ST_LineLocatePoint(e.geometry, s.geometry)))
		FROM {schema_ident}.all_structures s
    	LEFT JOIN LATERAL (
    		SELECT id, geometry
    		FROM {schema_ident}.streams e
			WHERE ST_DWithin(s.geometry::geography, e.geometry::geography, %s)
    		ORDER BY s.geometry::geography <-> e.geometry::geography
    		LIMIT 1
		) e ON true
		WHERE e.id IS NOT NULL
		  AND s.snapped_geometry IS NULL;
		""",
		(edge_distance_m,),
	)
	return cursor.fetchall()


def group_by_edge(matches):
	by_edge = {}
	for structure_id, edge_id, edge_wkb, closest_point_wkb in matches:
		group = by_edge.setdefault(edge_id, {"wkb": bytes(edge_wkb), "structures": []})
		x, y, z, m = point_xyzm(bytes(closest_point_wkb))
		group["structures"].append((structure_id, x, y, z, m))
	return by_edge


def write_snapped_geometries(cursor, output_schema, srid, all_results):
	"""all_results: list of (structure_id, edge_id, x, y, z, m) across one or more edges.
	snapped_geometry (for reporting) is stored in SNAPPED_GEOMETRY_SRID (4617, per
	requirements.md), reprojected from the streams SRID. network_vertex_x/y store the same
	location in the streams table's native SRID (no reprojection), so Compute Statistics'
	network-breaking step can match it against streams vertices exactly. snapped_edge_id
	records which streams edge the structure landed on."""

	schema_ident = quote_ident(output_schema)
	rows = [(x, y, edge_id, structure_id) for structure_id, edge_id, x, y, _z, _m in all_results]

	cursor.executemany(
		f"""
		UPDATE {schema_ident}.all_structures AS s
		SET snapped_geometry = ST_Transform(ST_SetSRID(ST_MakePoint(v.x, v.y), {srid}), {SNAPPED_GEOMETRY_SRID}),
			network_vertex_x = v.x,
			network_vertex_y = v.y,
			snapped_edge_id = v.edge_id
		FROM (VALUES (%s, %s, %s, %s)) AS v(x, y, edge_id, structure_id)
		WHERE s.id = v.structure_id
		""",
		rows,
	)


def write_edge_geometries(cursor, output_schema, srid, changed_edges):
	"""changed_edges: list of (edge_id, vertices) for edges where snap_points_to_edge inserted
	a new vertex."""

	schema_ident = quote_ident(output_schema)
	rows = [(linestring_zm_wkb(vertices), srid, edge_id) for edge_id, vertices in changed_edges]

	cursor.executemany(
		f"UPDATE {schema_ident}.streams SET geometry = ST_SetSRID(ST_GeomFromWKB(%s), %s) WHERE id = %s",
		rows,
	)


def snap_structures(conn, cursor, plan, srid):
	"""Run Load Structures step 5 for every all_structures row that isn't already snapped.
	Returns (snapped_count, unmatched_count)."""

	output_schema = plan["output_schema"]
	edge_distance_m = plan["structure_snap_edge_distance_m"]
	vertex_distance_m = plan["structure_snap_vertex_distance_m"]

    # create necessary indexes to improve performance
	#do this here after the data is loaded so data loading isn't affected
	cursor.execute(f"CREATE INDEX all_structures_geometry_idx ON {quote_ident(output_schema)}.all_structures USING gist (geometry);")
	cursor.execute(f"CREATE INDEX all_structures_geometry_geog_idx ON {quote_ident(output_schema)}.all_structures USING gist((geometry::geography))")
	cursor.execute(f"CREATE INDEX streams_geometry_geog_idx ON {quote_ident(output_schema)}.streams USING gist((geometry::geography))")

	conn.commit()

	cursor.execute(f"SELECT count(*) FROM {quote_ident(output_schema)}.all_structures WHERE snapped_geometry IS NULL")
	total_unsnapped = cursor.fetchone()[0]

	print(f"Finding candidate edges for {total_unsnapped} unsnapped structure(s)...")
	matches = fetch_candidate_matches(cursor, output_schema, edge_distance_m)
	by_edge = group_by_edge(matches)

	snapped_count = 0
	results_buffer = []  # (structure_id, edge_id, x, y, z, m)
	edges_buffer = []  # (edge_id, vertices), only where changed=True

	for edge_id, group in by_edge.items():
		vertices, results, changed = snap_points_to_edge(group["wkb"], group["structures"], vertex_distance_m)
		if changed:
			edges_buffer.append((edge_id, vertices))
		results_buffer.extend((structure_id, edge_id, x, y, z, m) for structure_id, x, y, z, m in results)
		snapped_count += len(results)

		if len(results_buffer) >= BATCH_SIZE:
			write_snapped_geometries(cursor, output_schema, srid, results_buffer)
			results_buffer = []
		if len(edges_buffer) >= BATCH_SIZE:
			write_edge_geometries(cursor, output_schema, srid, edges_buffer)
			edges_buffer = []

		if snapped_count // BATCH_SIZE > (snapped_count - len(results)) // BATCH_SIZE:
			print(f"Snapped {snapped_count}/{total_unsnapped} structure(s)...")

	if results_buffer:
		write_snapped_geometries(cursor, output_schema, srid, results_buffer)
	if edges_buffer:
		write_edge_geometries(cursor, output_schema, srid, edges_buffer)

	unmatched_count = total_unsnapped - snapped_count

	# create after the data is loaded so data loading isn't affected
	cursor.execute(f"CREATE INDEX all_structures_snapped_edge_id_idx ON {quote_ident(output_schema)}.all_structures (snapped_edge_id);")

	conn.commit()

	print(f"Snapped {snapped_count} structure(s); {unmatched_count} had no edge within {edge_distance_m}m.")
	return snapped_count, unmatched_count
