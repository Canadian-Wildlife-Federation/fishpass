#!/usr/bin/env python3
"""Compute gradient barriers from the cached CHyF network and write them to
support.gradient_barriers.

Database connection details come from environment variables only (see
README.md) -- never from a config file and never logged.
"""

import argparse
import configparser
import json
import math
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import os
import psycopg
import shapely
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPECIES_PARAMS_FILE = REPO_ROOT / "config" / "fish_species_parameters.yaml"
DEFAULT_CONFIG_FILE = REPO_ROOT / "config" / "gradient_barriers.ini"

SHORT_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")

UPSTREAM_DISTANCE_M = 100.0  # the distance to travel upstream to determine gradient
EARTH_RADIUS_M = 6_371_008.8  # mean Earth radius (IUGG); chyf_raw geometries are geographic (deg)
NO_DATA = -9999  # sentinel used in chyf_raw for a missing smoothed-elevation (M ordinate) value

EDGE_FETCH_BATCH_SIZE = 10_000  # number of edges to fetch at once
BARRIER_CACHE_SIZE = 5_000  # number of cached barrier rows before flushing an INSERT
PROGRESS_LOG_INTERVAL_MAINSTEMS = 1_000  # print a progress line every N mainstems walked

REQUIRED_ENV_VARS = [
	"FISHPASS_HOST",
	"FISHPASS_PORT",
	"FISHPASS_DBNAME",
	"FISHPASS_USER",
	"FISHPASS_PASSWORD",
]

# optional params path for the species parameters file
def parse_args():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--species_params", type=Path, default=DEFAULT_SPECIES_PARAMS_FILE)
	parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_FILE)
	return parser.parse_args()


def load_aoi_config(config_path):
	"""Return the list of chyf_raw.aoi.short_name values to scope this run to, from the
	[aoi] short_names setting in config_path. Returns [] (meaning: recompute the entire network) 
	if config_path doesn't exist or	short_names is blank."""

	if not config_path.is_file():
		return []

	parser = configparser.ConfigParser()
	parser.read(config_path)
	short_names = [
		s.strip()
		for s in parser.get("aoi", "short_names", fallback="").split(",")
		if s.strip()
	]

	invalid = [s for s in short_names if not SHORT_NAME_RE.match(s)]
	if invalid:
		sys.exit(f"Invalid short_name(s) in [aoi] short_names: {', '.join(invalid)}")

	return short_names


def require_env():
	missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
	if missing:
		sys.exit(f"Missing required environment variable(s): {', '.join(missing)}")


def db_connect():
	return psycopg.connect(
		host=os.environ["FISHPASS_HOST"],
		port=os.environ["FISHPASS_PORT"],
		dbname=os.environ["FISHPASS_DBNAME"],
		user=os.environ["FISHPASS_USER"],
		password=os.environ["FISHPASS_PASSWORD"],
	)


def _validate_threshold(value, field_name, code, params_path):
	"""Exit with a clear message unless value is None or a fractional gradient in [0, 1]."""
	if value is None:
		return
	if isinstance(value, bool) or not isinstance(value, (int, float)) or not (0 <= value <= 1):
		sys.exit(
			f"Invalid {field_name} for species '{code}' in {params_path}: "
			f"expected a number between 0 and 1 (or blank), got {value!r}"
		)


def load_species_parameters(params_path):
	"""Return [{code, spawning_max, rearing_max}, ...] from the fish species parameter file."""
	if not params_path.is_file():
		sys.exit(f"Species parameter file not found: {params_path}")

	with open(params_path) as f:
		data = yaml.safe_load(f)

	species = []
	for entry in data.get("species", []):
		code = entry["code"]
		spawning_max = entry.get("accessibility_gradient_spawning_max")
		rearing_max = entry.get("accessibility_gradient_rearing_max")
		_validate_threshold(spawning_max, "accessibility_gradient_spawning_max", code, params_path)
		_validate_threshold(rearing_max, "accessibility_gradient_rearing_max", code, params_path)
		species.append({
			"code": code,
			"spawning_max": spawning_max,
			"rearing_max": rearing_max,
		})
	if not species:
		sys.exit(f"No species entries found in {params_path}")
	return species


def resolve_aois(cursor, short_names):
	"""Resolve chyf_raw.aoi.short_name values to {id, short_name} rows, Exits if any short_name 
	doesn't resolve (typo protection)."""

	cursor.execute(
		"SELECT id, short_name FROM chyf_raw.aoi WHERE short_name = ANY(%s)",
		(short_names,),
	)
	rows = cursor.fetchall()
	if len(rows) != len(short_names):
		found = {short_name for _id, short_name in rows}
		missing = [s for s in short_names if s not in found]
		sys.exit(f"Could not resolve AOI short_name(s) in chyf_raw.aoi: {', '.join(missing)}")

	return [{"id": aoi_id, "short_name": short_name} for aoi_id, short_name in rows]


def get_source_srid(cursor):
	cursor.execute(
		"SELECT ST_SRID(geometry) FROM chyf_raw.flowpath WHERE geometry IS NOT NULL LIMIT 1"
	)
	row = cursor.fetchone()
	if row is None or row[0] is None:
		sys.exit("Could not determine SRID from chyf_raw.flowpath -- is the table empty?")
	return row[0]


def table_exists(cursor, table_name):
	cursor.execute(
		"SELECT 1 FROM information_schema.tables "
		"WHERE table_schema = 'support' AND table_name = %s",
		(table_name,),
	)
	return cursor.fetchone() is not None


def next_archive_name_postfix(cursor, prefix):
	"""Return f"{prefix}_<today's yyyymmdd>_<seq>", where seq is one past the highest
	existing sequence number for today's date under this prefix (so re-runs on the same day
	don't collide)."""
	date_str = datetime.now(tz=timezone.utc).date().strftime("%Y%m%d")
	cursor.execute(
		"SELECT table_name FROM information_schema.tables "
		"WHERE table_schema = 'support' AND table_name LIKE %s",
		(f"{prefix}_{date_str}_%",),
	)
	existing_seqs = [
		int(name.rsplit("_", 1)[-1])
		for (name,) in cursor.fetchall()
		if name.rsplit("_", 1)[-1].isdigit()
	]
	next_seq = max(existing_seqs, default=0) + 1
	return f"{date_str}_{next_seq}"


def create_tables(cursor, srid):
	"""Ensure the support schema exists, and creates the gradient_barriers and gradient_barriers_metadata tables"""

	cursor.execute("CREATE SCHEMA IF NOT EXISTS support;")

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

	cursor.execute("""
		CREATE TABLE support.gradient_barriers_metadata (
			id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
			aoi varchar[] NOT NULL,
			species_params jsonb NOT NULL,
			run_at timestamptz NOT NULL DEFAULT now()
		);
	""")

def prepare_tables(cursor, srid):
	"""Ensure the support schema exists, archive any existing gradient_barriers table, and
	create a fresh one."""

	cursor.execute("CREATE SCHEMA IF NOT EXISTS support;")

	archive_postfix = next_archive_name_postfix(cursor, "gradient_barriers_archive")

	if table_exists(cursor, "gradient_barriers"):
		# rename existing table with current date
		# to ensure we keep a copy of it and don't lose
		# any manual updates
		archive_name = f"gradient_barriers_archive_{archive_postfix}";
		cursor.execute(f"ALTER TABLE support.gradient_barriers RENAME TO {archive_name};")
		cursor.execute(
			f"ALTER INDEX support.gradient_barriers_geometry_idx RENAME TO {archive_name}_geometry_idx;"
		)
		print(f"Archived existing table to support.{archive_name}")

	if table_exists(cursor, "gradient_barriers_metadata"):
		# rename existing table with current date
		# to ensure we keep a copy of it and don't lose
		# any manual updates
		archive_name = f"gradient_barriers_metadata_{archive_postfix}";
		cursor.execute(f"ALTER TABLE support.gradient_barriers_metadata RENAME TO {archive_name};")		
		print(f"Archived existing table to support.{archive_name}")

	create_tables(cursor, srid);

def insert_metadata_record(cursor, aoi, species_params):
	"""Append a row to support.gradient_barriers_metadata recording this run's AOI scope
	({'all'} for a full run, or the reprocessed short_names for a partial run) and the fish
	species parameters that were in effect."""

	cursor.execute(
		"INSERT INTO support.gradient_barriers_metadata (aoi, species_params) VALUES (%s, %s::jsonb)",
		(aoi, json.dumps(species_params)),
	)


def backup_and_clear_aoi_rows(cursor, srid, short_names):
	"""Back up the existing support.gradient_barriers rows for short_names to a timestamped
	audit table, then delete them from the live table, ahead of a scoped recompute."""

	if not table_exists(cursor, "gradient_barriers"):
		print("support.gradient_barriers does not exist yet -- creating it.")
		create_tables(cursor, srid)
		return

	backup_name = next_archive_name_postfix(cursor, "gradient_barriers_aoi_backup")
	backup_name = f"gradient_barriers_aoi_backup_{backup_name}"

	cursor.execute(
		f"CREATE TABLE support.{backup_name} AS "
		f"SELECT *, now() AS archived_at FROM support.gradient_barriers WHERE workunit && %s::varchar[]",
		(short_names,),
	)
	cursor.execute(
		"DELETE FROM support.gradient_barriers WHERE workunit && %s::varchar[]",
		(short_names,),
	)
	print(f"Archived {cursor.rowcount} existing row(s) for {', '.join(short_names)} to support.{backup_name}")


def fetch_edges(conn, aoi_ids=None):
	"""Stream (edge_id, mainstem_id, mainstem_seq, aoi_id, wkb) for every flowpath edge that
	belongs to a mainstem, ordered so mainstems are grouped together with their most downstream
	edge first.

	If aoi_ids is given, edges are restricted to every mainstem that has at least one edge in
	those AOI(s) -- but *all* of each such mainstem's edges are included, even the portions
	that fall in neighboring AOIs, so the 100m upstream gradient walk stays correct across an
	AOI boundary. 

	Uses a named (server-side) cursor so the full ~tens-of-millions-of-row result set is
	fetched from Postgres in batches rather than loaded into client memory all at once.
	Caller must close() the returned cursor when done iterating.

	withhold=True keeps this server-side cursor open across conn.commit() calls -- compute_barriers
	commits after every barrier-cache flush while this cursor is still being iterated, and a
	named cursor without WITH HOLD is dropped at the end of its transaction.
	"""
	edge_cursor = conn.cursor(name="gradient_barriers_edges", withhold=True)
	edge_cursor.itersize = EDGE_FETCH_BATCH_SIZE
	aoi_filter = (
		"AND mainstem_id = ANY(SELECT DISTINCT mainstem_id FROM chyf_raw.flowpath "
		"WHERE aoi_id = ANY(%(aoi_ids)s) AND mainstem_id IS NOT NULL)"
		if aoi_ids is not None
		else ""
	)
	edge_cursor.execute(
		f"""
		SELECT id, mainstem_id, mainstem_seq, aoi_id, ST_AsBinary(geometry)
		FROM chyf_raw.flowpath
		WHERE mainstem_id IS NOT NULL AND mainstem_seq IS NOT NULL
		{aoi_filter}
		ORDER BY mainstem_id, mainstem_seq ASC
		""",
		{"aoi_ids": aoi_ids},
	)
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
	return coords.tolist()


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


def resolve_vertex(window, prev, cum_dist, elevation, lon, lat, species_params, in_scope):
	"""Append a newly-walked vertex to `window` (mutated in place), then pop and resolve any
	vertices whose point >= UPSTREAM_DISTANCE_M upstream has now been reached.

	Each resolved vertex's upstream reference elevation is linearly interpolated at exactly
	UPSTREAM_DISTANCE_M, between `prev` (the vertex immediately before this one in the walk) and
	this vertex -- the two endpoints of the single segment containing that point -- rather than
	read from whichever vertex happens to be first at or beyond it, which would average the
	gradient over however far past the mark that vertex actually is.

	`prev` is None only for the very first vertex of a mainstem, when `window` can't yet contain
	anything old enough to resolve, so it's never dereferenced in that case.

	in_scope records, for this vertex specifically, whether its own edge is eligible to produce a
	barrier (see compute_barriers) -- carried alongside it in `window` since a vertex is only
	resolved once its 100m-upstream point is reached, potentially several edges later. An
	out-of-scope vertex is still popped and its window slot freed (so window doesn't grow
	unbounded through a long out-of-scope stretch of a mainstem), it just never produces a
	barrier.

	Returns the list of (lon, lat, gradient, computed_species) barrier tuples produced by this
	vertex (usually 0 or 1, but more than one queued vertex can resolve off the same newly-
	arrived vertex if they're closely spaced).
	"""
	window.append((cum_dist, elevation, lon, lat, in_scope))

	barriers = []
	while window and cum_dist - window[0][0] >= UPSTREAM_DISTANCE_M:
		i_dist, i_elev, i_lon, i_lat, i_in_scope = window.popleft()
		target = i_dist + UPSTREAM_DISTANCE_M
		prev_dist, prev_elev = prev
		frac = (target - prev_dist) / (cum_dist - prev_dist)
		interp_elev = prev_elev + frac * (elevation - prev_elev)
		gradient = (interp_elev - i_elev) / UPSTREAM_DISTANCE_M
		if not i_in_scope:
			continue
		computed_species = flag_species(gradient, species_params)
		if computed_species:
			barriers.append((i_lon, i_lat, gradient, computed_species))
	return barriers


def compute_barriers(conn, cursor, srid, species_params, aoi_ids=None):
	"""Walk every mainstem in chyf_raw.flowpath in a single pass -- for each vertex (in
	downstream -> upstream order), compute its gradient as soon as its upstream point comes
	into view, check it against every species/lifestage threshold immediately, and cache it
	flushing the cache and writing ot he database after it reaches a specified size.

	Returns the total number of barrier rows inserted.

	Edges arrive pre-sorted by (mainstem_id, mainstem_seq ASC) from fetch_edges, so a mainstem's
	vertices are walked in that same streaming pass. A vertex is only held onto (in `window`)
	for as long as it's still waiting for a	point >= UPSTREAM_DISTANCE_M upstream of it; 
	see resolve_vertex for how each one is resolved.

	Edges outside the aoid_ids (if given) and walked through (in case it exists and re-enters) but never
	contribute barriers.

	Vertices whose smoothed elevation is missing (the NO_DATA sentinel, or NaN) are skipped: they
	never become a barrier and never become the interpolation reference (`prev`), but distance
	still accumulates through them and the last valid vertex remains the interpolation reference
	until another valid vertex is walked -- see resolve_vertex.
	"""
	aoi_id_set = set(aoi_ids) if aoi_ids is not None else None
	edges = fetch_edges(conn, aoi_ids=aoi_ids)

	barriers = []
	total = 0
	invalid_elevation_count = 0
	mainstem_count = 0
	current_mainstem = None
	running_total = 0.0
	prev = None  # (cum_dist, elevation) of the vertex immediately before the one just walked
	window = deque()  # (cum_dist, elevation, lon, lat, in_scope) vertices awaiting an upstream match

	try:
		for _edge_id, mainstem_id, mainstem_seq, edge_aoi_id, wkb in edges:
			if mainstem_id != current_mainstem:
				window.clear()  # vertices left waiting had no upstream point on this mainstem
				running_total = 0.0
				prev = None
				current_mainstem = mainstem_id
				mainstem_count += 1
				if mainstem_count % PROGRESS_LOG_INTERVAL_MAINSTEMS == 0:
					print(f"Processed {mainstem_count} mainstem(s), {total + len(barriers)} barrier(s) found so far.")

			in_scope = aoi_id_set is None or edge_aoi_id in aoi_id_set

			# Edge vertices are stored upstream -> downstream; reverse to walk downstream ->
			# upstream so cum_dist increases monotonically as we move up the mainstem.
			vertices = edge_vertices(bytes(wkb))[::-1]
			for i, (lon, lat, m) in enumerate(vertices):
				if i == 0 and mainstem_seq != 1:
					# Shared boundary vertex with the previous edge's last (topologically
					# connected) vertex -- already resolved/tracked via `prev`, so skip it here
					# rather than re-walking the same physical point a second time.
					continue
				if i > 0:
					prev_lon, prev_lat, _ = vertices[i - 1]
					running_total += haversine_m(prev_lon, prev_lat, lon, lat)
				cum_dist = running_total
				if math.isnan(m) or m == NO_DATA:
					invalid_elevation_count += 1
					continue
				barriers.extend(resolve_vertex(window, prev, cum_dist, m, lon, lat, species_params, in_scope))
				prev = (cum_dist, m)
				if len(barriers) >= BARRIER_CACHE_SIZE:
					insert_barriers(cursor, srid, barriers)
					conn.commit()
					total += len(barriers)
					barriers = []
	finally:
		edges.close()

	if barriers:
		insert_barriers(cursor, srid, barriers)
		conn.commit()
		total += len(barriers)

	if invalid_elevation_count:
		print(f"Skipped {invalid_elevation_count} vertex/vertices with invalid elevation (NaN or {NO_DATA}).")

	return total


def insert_barriers(cursor, srid, barriers):
	rows = [
		(lon, lat, srid, gradient, computed_species, computed_species)
		for lon, lat, gradient, computed_species in barriers
	]

	cursor.executemany(
		"""
		INSERT INTO support.gradient_barriers
			(geometry, gradient, computed_species, actual_species)
		VALUES (ST_SetSRID(ST_MakePoint(%s, %s), %s), %s, %s, %s)
		""",
		rows
	)


def assign_workunits(cursor):
	"""Spatially assign `workunit` to every row that doesn't have one yet."""
	
	cursor.execute("""
		UPDATE support.gradient_barriers b
		SET workunit = matched.short_names
		FROM (
			SELECT b.id, array_agg(DISTINCT a.short_name) AS short_names
			FROM support.gradient_barriers b
			JOIN chyf_raw.aoi a ON ST_Intersects(b.geometry, a.geometry)
			WHERE b.workunit IS NULL
			GROUP BY b.id
		) AS matched
		WHERE b.id = matched.id;
	""")


def format_elapsed(seconds):
	minutes, secs = divmod(int(seconds), 60)
	return f"{minutes}m {secs:02d}s"


def main():
	start_time = time.monotonic()

	args = parse_args()
	require_env()
	species_params = load_species_parameters(args.species_params)
	short_names = load_aoi_config(args.config)

	conn = db_connect()
	try:
		with conn.cursor() as cursor:
			srid = get_source_srid(cursor)

			if short_names:
				print(f"Reprocessing AOI(s): {', '.join(short_names)}")
				aois = resolve_aois(cursor, short_names)
				backup_and_clear_aoi_rows(cursor, srid, short_names)
				conn.commit()
				count = compute_barriers(conn, cursor, srid, species_params, aoi_ids=[a["id"] for a in aois])
				if not table_exists(cursor, "gradient_barriers_metadata"):
					print("gradient_barriers_metadata table does not exist - metadata will not be updated")
				else:
					insert_metadata_record(cursor, short_names, species_params)
			else:
				prepare_tables(cursor, srid)
				conn.commit()
				count = compute_barriers(conn, cursor, srid, species_params)
				insert_metadata_record(cursor, ["all"], species_params)

			print(f"Computed and inserted {count} barrier point(s).")
			if count:
				assign_workunits(cursor)
			conn.commit()
	except Exception:
		conn.rollback()
		raise
	finally:
		conn.close()

	elapsed = format_elapsed(time.monotonic() - start_time)
	print(f"Gradient barrier computation complete in {elapsed}.")


if __name__ == "__main__":
	main()
