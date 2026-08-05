#!/usr/bin/env python3
"""Compute gradient barriers from the cached CHyF network and write them to
support.gradient_barriers.

Database connection details come from environment variables only (see
README.md) -- never from a config file and never logged.
"""

import argparse
import math
import sys
from collections import deque
from datetime import date
from pathlib import Path

import os
import psycopg2
import psycopg2.extras
import shapely
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PARAMS_FILE = REPO_ROOT / "config" / "fish_species_parameters.yaml"

UPSTREAM_DISTANCE_M = 100.0
EARTH_RADIUS_M = 6_371_008.8  # mean Earth radius (IUGG); chyf_raw geometries are geographic (deg)

EDGE_FETCH_BATCH_SIZE = 10_000

REQUIRED_ENV_VARS = [
	"FISHPASS_HOST",
	"FISHPASS_PORT",
	"FISHPASS_DBNAME",
	"FISHPASS_USER",
	"FISHPASS_PASSWORD",
]


def parse_args():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--params", type=Path, default=DEFAULT_PARAMS_FILE)
	return parser.parse_args()


def require_env():
	missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
	if missing:
		sys.exit(f"Missing required environment variable(s): {', '.join(missing)}")


def db_connect():
	return psycopg2.connect(
		host=os.environ["FISHPASS_HOST"],
		port=os.environ["FISHPASS_PORT"],
		dbname=os.environ["FISHPASS_DBNAME"],
		user=os.environ["FISHPASS_USER"],
		password=os.environ["FISHPASS_PASSWORD"],
	)


def load_species_parameters(params_path):
	"""Return [{code, spawning_max, rearing_max}, ...] from the fish species parameter file."""
	if not params_path.is_file():
		sys.exit(f"Species parameter file not found: {params_path}")

	with open(params_path) as f:
		data = yaml.safe_load(f)

	species = []
	for entry in data.get("species", []):
		species.append({
			"code": entry["code"],
			"spawning_max": entry.get("accessibility_gradient_spawning_max"),
			"rearing_max": entry.get("accessibility_gradient_rearing_max"),
		})
	if not species:
		sys.exit(f"No species entries found in {params_path}")
	return species


def get_source_srid(cursor):
	cursor.execute(
		"SELECT ST_SRID(geometry) FROM chyf_raw.flowpath WHERE geometry IS NOT NULL LIMIT 1"
	)
	row = cursor.fetchone()
	if row is None or row[0] is None:
		sys.exit("Could not determine SRID from chyf_raw.flowpath -- is the table empty?")
	return row[0]


def prepare_table(cursor, srid):
	"""Ensure the support schema exists, archive any existing gradient_barriers table, and
	create a fresh one."""
	cursor.execute("CREATE SCHEMA IF NOT EXISTS support;")

	cursor.execute(
		"SELECT 1 FROM information_schema.tables "
		"WHERE table_schema = 'support' AND table_name = 'gradient_barriers'"
	)

	if cursor.fetchone():
		# rename existing table with current date
		# to ensure we keep a copy of it and don't lose
		# any manual updates
		date_str = date.today().strftime("%Y%m%d")
		cursor.execute(
			"SELECT table_name FROM information_schema.tables "
			"WHERE table_schema = 'support' AND table_name LIKE %s",
			(f"gradient_barriers_archive_{date_str}_%",),
		)
		existing_seqs = [
			int(name.rsplit("_", 1)[-1])
			for (name,) in cursor.fetchall()
			if name.rsplit("_", 1)[-1].isdigit()
		]
		next_seq = max(existing_seqs, default=0) + 1
		archive_name = f"gradient_barriers_archive_{date_str}_{next_seq}"
		cursor.execute(f"ALTER TABLE support.gradient_barriers RENAME TO {archive_name};")
		print(f"Archived existing table to support.{archive_name}")

	cursor.execute(f"""
		CREATE TABLE support.gradient_barriers (
			id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
			geometry geometry(point, {srid}) NOT NULL,
			workunit varchar[],
			gradient double precision NOT NULL,
			computed_species varchar[] NOT NULL,
			actual_species varchar[] NOT NULL,
			comments varchar
		);
	""")
	cursor.execute(
		"CREATE INDEX gradient_barriers_geometry_idx ON support.gradient_barriers USING gist (geometry);"
	)


def fetch_edges(conn):
	"""Stream (edge_id, mainstem_id, mainstem_seq, wkb) for every flowpath edge that belongs
	to a mainstem, ordered so mainstems are grouped together with their most downstream edge
	first.

	Uses a named (server-side) cursor so the full ~tens-of-millions-of-row result set is
	fetched from Postgres in batches rather than loaded into client memory all at once.
	Caller must close() the returned cursor when done iterating.
	"""
	edge_cursor = conn.cursor(name="gradient_barriers_edges")
	edge_cursor.itersize = EDGE_FETCH_BATCH_SIZE
	edge_cursor.execute("""
		SELECT id, mainstem_id, mainstem_seq, ST_AsBinary(geometry)
		FROM chyf_raw.flowpath
		WHERE mainstem_id IS NOT NULL AND mainstem_seq IS NOT NULL
		ORDER BY mainstem_id, mainstem_seq ASC
	""")
	return edge_cursor


def haversine_m(lon1, lat1, lon2, lat2):
	"""Great-circle distance in metres between two lon/lat points (degrees)."""
	phi1, phi2 = math.radians(lat1), math.radians(lat2)
	dphi = math.radians(lat2 - lat1)
	dlambda = math.radians(lon2 - lon1)
	a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
	return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def edge_vertices(wkb_bytes):
	"""Return an (N, 3) array of (lon, lat, m) for a flowpath edge's LineString, in the
	geometry's native (upstream -> downstream) vertex order.

	Requires shapely>=2.1 / GEOS>=3.12 for M-ordinate support -- see requirements.md's
	"Design Decisions" section.
	"""
	geom = shapely.from_wkb(wkb_bytes)
	coords = shapely.get_coordinates(geom, include_m=True)
	return coords


def flag_species(gradient, species_params):
	"""Return the list of <species>_<lifestage> codes this gradient is a barrier for.

	A blank threshold (None) means that lifestage has no gradient limit, so it's
	never flagged as a barrier."""
	return [
		f"{sp['code']}_{lifestage}"
		for sp in species_params
		for lifestage, max_key in (("spawn", "spawning_max"), ("rear", "rearing_max"))
		if sp[max_key] is not None and gradient > sp[max_key]
	]


def resolve_vertex(window, prev, cum_dist, elevation, lon, lat, species_params):
	"""Append a newly-walked vertex to `window` (mutated in place), then pop and resolve any
	vertices whose point >= UPSTREAM_DISTANCE_M upstream has now been reached.

	Each resolved vertex's upstream reference elevation is linearly interpolated at exactly
	UPSTREAM_DISTANCE_M, between `prev` (the vertex immediately before this one in the walk) and
	this vertex -- the two endpoints of the single segment containing that point -- rather than
	read from whichever vertex happens to be first at or beyond it, which would average the
	gradient over however far past the mark that vertex actually is.

	`prev` is None only for the very first vertex of a mainstem, when `window` can't yet contain
	anything old enough to resolve, so it's never dereferenced in that case.

	Returns the list of (lon, lat, gradient, computed_species) barrier tuples produced by this
	vertex (usually 0 or 1, but more than one queued vertex can resolve off the same newly-
	arrived vertex if they're closely spaced).
	"""
	window.append((cum_dist, elevation, lon, lat))

	barriers = []
	while window and cum_dist - window[0][0] >= UPSTREAM_DISTANCE_M:
		i_dist, i_elev, i_lon, i_lat = window.popleft()
		target = i_dist + UPSTREAM_DISTANCE_M
		prev_dist, prev_elev = prev
		frac = (target - prev_dist) / (cum_dist - prev_dist)
		interp_elev = prev_elev + frac * (elevation - prev_elev)
		gradient = (interp_elev - i_elev) / UPSTREAM_DISTANCE_M
		computed_species = flag_species(gradient, species_params)
		if computed_species:
			barriers.append((i_lon, i_lat, gradient, computed_species))
	return barriers


def compute_barriers(conn, species_params):
	"""Walk every mainstem in chyf_raw.flowpath in a single pass -- for each vertex (in
	downstream -> upstream order), compute its gradient as soon as its upstream point comes
	into view, check it against every species/lifestage threshold immediately, and move on.

	Edges arrive pre-sorted by (mainstem_id, mainstem_seq ASC) from fetch_edges, so a mainstem's
	vertices are walked in that same streaming pass -- no per-mainstem arrays are built up
	first. A vertex is only held onto (in `window`) for as long as it's still waiting for a
	point >= UPSTREAM_DISTANCE_M upstream of it; see resolve_vertex for how each one is resolved.
	"""
	edges = fetch_edges(conn)

	barriers = []
	current_mainstem = None
	running_total = 0.0
	prev = None  # (cum_dist, elevation) of the vertex immediately before the one just walked
	window = deque()  # (cum_dist, elevation, lon, lat) vertices still waiting for an upstream match

	for _edge_id, mainstem_id, _mainstem_seq, wkb in edges:
		if mainstem_id != current_mainstem:
			window.clear()  # vertices left waiting had no upstream point on this mainstem
			running_total = 0.0
			prev = None
			current_mainstem = mainstem_id

		# Edge vertices are stored upstream -> downstream; reverse to walk downstream ->
		# upstream so cum_dist increases monotonically as we move up the mainstem.
		vertices = edge_vertices(bytes(wkb))[::-1]
		for i, (lon, lat, m) in enumerate(vertices):
			if i > 0:
				prev_lon, prev_lat, _ = vertices[i - 1]
				running_total += haversine_m(prev_lon, prev_lat, lon, lat)
			cum_dist = running_total
			barriers.extend(resolve_vertex(window, prev, cum_dist, m, lon, lat, species_params))
			prev = (cum_dist, m)

	edges.close()
	return barriers


def insert_barriers(cursor, srid, barriers):
	rows = [
		(lon, lat, srid, gradient, computed_species, computed_species)
		for lon, lat, gradient, computed_species in barriers
	]
	psycopg2.extras.execute_values(
		cursor,
		"""
		INSERT INTO support.gradient_barriers
			(geometry, gradient, computed_species, actual_species)
		VALUES %s
		""",
		rows,
		template="(ST_SetSRID(ST_MakePoint(%s, %s), %s), %s, %s, %s)",
	)


def assign_workunits(cursor):
	cursor.execute("""
		UPDATE support.gradient_barriers b
		SET workunit = matched.short_names
		FROM (
			SELECT b.id, array_agg(DISTINCT a.short_name) AS short_names
			FROM support.gradient_barriers b
			JOIN chyf_raw.aoi a ON ST_Intersects(b.geometry, a.geometry)
			GROUP BY b.id
		) AS matched
		WHERE b.id = matched.id;
	""")


def main():
	args = parse_args()
	require_env()
	species_params = load_species_parameters(args.params)

	conn = db_connect()
	try:
		with conn.cursor() as cursor:
			srid = get_source_srid(cursor)
			prepare_table(cursor, srid)
			barriers = compute_barriers(conn, species_params)
			print(f"Computed {len(barriers)} barrier point(s).")
			if barriers:
				insert_barriers(cursor, srid, barriers)
				assign_workunits(cursor)
		conn.commit()
	except Exception:
		conn.rollback()
		raise
	finally:
		conn.close()

	print("Gradient barrier computation complete.")


if __name__ == "__main__":
	main()
