"""Tests for gradient_barriers/scripts/compute_barriers.py.

These drive the whole compute_barriers() function -- including fetch_edges and the shapely
WKB/M-ordinate parsing in edge_vertices() -- against a stubbed psycopg2 connection/cursor, so no
real database is needed. shapely must be genuinely installed (not stubbed) since these tests build
and parse real WKB fixtures.

Run with: python -m unittest gradient_barriers.tests.test_compute_barriers
"""

import math
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# compute_barriers.py imports psycopg2/psycopg2.extras/yaml at module level for its DB-connection
# and species-file-loading code paths, neither of which compute_barriers() itself touches (it
# only calls conn.cursor(...), which FakeConnection below provides). Stub them out when
# unavailable so these tests don't require the full production dependency set to be installed.
for _module_name in ("psycopg2", "psycopg2.extras", "yaml"):
	try:
		__import__(_module_name)
	except ImportError:
		sys.modules[_module_name] = types.ModuleType(_module_name)

import shapely  # noqa: E402 -- must be real; these tests build/parse actual WKB fixtures

import compute_barriers as cb  # noqa: E402


# ---------------------------------------------------------------------------
# Stubbed DB layer -- compute_barriers(conn, ...) only ever calls fetch_edges(conn), which calls
# conn.cursor(name=...), sets .itersize, .execute(...), iterates the cursor, then closes it.
# ---------------------------------------------------------------------------


class FakeNamedCursor:
	def __init__(self, rows):
		self._rows = rows
		self.itersize = None

	def execute(self, sql):
		pass  # SQL text isn't inspected; the fixture rows are already prepared

	def __iter__(self):
		return iter(self._rows)

	def close(self):
		pass


class FakeConnection:
	def __init__(self, rows):
		self._rows = rows

	def cursor(self, name=None):
		return FakeNamedCursor(self._rows)


# ---------------------------------------------------------------------------
# WKB fixture helpers
# ---------------------------------------------------------------------------

BASE_LON = -64.0
BASE_LAT = 45.0


def lat_offset_deg(distance_m):
	"""Degrees of latitude corresponding to distance_m north, at BASE_LAT.

	Holding longitude constant makes haversine_m (which the production code uses) reduce to
	distance ~= EARTH_RADIUS_M * delta_lat_radians for these small distances -- essentially
	exact at this scale, so fixtures built this way have real haversine distances matching
	their intended cumulative distances.
	"""
	return distance_m / cb.EARTH_RADIUS_M * 180.0 / math.pi


def point_at(distance_m, elevation, lon=BASE_LON, lat=BASE_LAT):
	"""A point distance_m upstream (north) of (lon, lat), at the given elevation."""
	return (lon, lat + lat_offset_deg(distance_m), elevation)


def make_wkb(vertices):
	"""vertices: [(lon, lat, elevation), ...] in storage (upstream -> downstream) order.

	Built via WKT ("LINESTRING M (...)") since that's the most portable way to express a
	measured (M-only, no Z) LineString, and mirrors what ST_AsBinary on a real LINESTRING M
	flowpath geometry would produce.
	"""
	points = ", ".join(f"{lon} {lat} {m}" for lon, lat, m in vertices)
	return shapely.to_wkb(shapely.from_wkt(f"LINESTRING M ({points})"))


def edge_row(edge_id, mainstem_id, mainstem_seq, vertices):
	return (edge_id, mainstem_id, mainstem_seq, make_wkb(vertices))


# Thresholds picked low enough that both worked-example gradients (5.25% and 0.5%) clear
# rearing_max and register as barriers -- not meant to reflect any real species' parameters.
LOW_THRESHOLDS = [{"code": "chn", "spawning_max": 1.0, "rearing_max": 0.003}]

# Thresholds set above both worked-example gradients, so neither should register as a barrier.
HIGH_THRESHOLDS = [{"code": "chn", "spawning_max": 1.0, "rearing_max": 1.0}]

# Thresholds set for multiple species
MULTI_THRESHOLDS = [{"code": "chn", "spawning_max": 0.001, "rearing_max": 0.002}, {"code": "as", "spawning_max": 0.0015, "rearing_max": 0.0018}]

class FlagSpeciesTests(unittest.TestCase):

	def test_blank_threshold_is_never_a_barrier(self):
		species_params = [{"code": "chn", "spawning_max": None, "rearing_max": None}]
		self.assertEqual(cb.flag_species(1000.0, species_params), [])

	def test_blank_threshold_does_not_suppress_other_lifestage(self):
		species_params = [{"code": "chn", "spawning_max": None, "rearing_max": 0.01}]
		self.assertEqual(cb.flag_species(0.5, species_params), ["chn_rear"])


class ComputeBarriersTests(unittest.TestCase):

	"""The i / A / B worked example from requirements.md's "Design Decisions" section:
	i: 0m, elevation 0; A: 50m, elevation 5 (10% grade); B: 150m, elevation 5.5 (0.5% grade for
	the 100m beyond A). Interpolating between A and B for i's 100m mark gives 5.25%; A's own
	100m mark lands exactly on B, giving 0.5%.
	"""
	def test_worked_example_single_edge(self):
		i = point_at(0.0, 0.0)
		a = point_at(50.0, 5.0)
		b = point_at(200.0, 5.75)
		rows =  [edge_row("worked-e1", "1", 1, [b, a, i])]
	
		barriers = cb.compute_barriers(FakeConnection(rows), LOW_THRESHOLDS)

		self.assertEqual(len(barriers), 2)

		i_lon, i_lat, i_gradient, i_species = barriers[0]
		self.assertAlmostEqual(i_lon, i[0], places=9)
		self.assertAlmostEqual(i_lat, i[1], places=9)
		self.assertAlmostEqual(i_gradient, 0.0525)
		self.assertEqual(i_species, ["chn_rear"])

		a_lon, a_lat, a_gradient, a_species = barriers[1]
		self.assertAlmostEqual(a_lon, a[0], places=9)
		self.assertAlmostEqual(a_lat, a[1], places=9)
		self.assertAlmostEqual(a_gradient, 0.005)
		self.assertEqual(a_species, ["chn_rear"])

		barriers = cb.compute_barriers(FakeConnection(rows), HIGH_THRESHOLDS)
		self.assertEqual(barriers, [])

	def test_nearest_vertex_would_have_given_a_different_answer(self):
		"""Documents *why* interpolation matters: using vertex B's elevation directly (the old
		nearest-vertex-past-100m behavior) instead of interpolating would average the gradient
		over the full 150m from i to B, not the 100m the spec asks about. Pure arithmetic --
		no fixture needed."""
		nearest_vertex_gradient = (5.5 - 0.0) / 150.0
		interpolated_elevation = 5.0 + (100.0 - 50.0) / (150.0 - 50.0) * (5.5 - 5.0)
		interpolated_gradient = (interpolated_elevation - 0.0) / 100.0

		self.assertAlmostEqual(nearest_vertex_gradient, 0.036666, places=5)
		self.assertAlmostEqual(interpolated_gradient, 0.0525)
		self.assertNotAlmostEqual(nearest_vertex_gradient, interpolated_gradient, places=3)

	def test_no_gradient_when_upstream_mainstem_too_short(self):
		# A single 60m edge -- well under UPSTREAM_DISTANCE_M, so the downstream vertex never
		# reaches a point 100m upstream and no barrier can be produced.
		start = point_at(0.0, 0.0)
		end = point_at(60.0, 3.0)
		rows = [edge_row("short-e1", "short", 1, [end, start])]

		barriers = cb.compute_barriers(FakeConnection(rows), LOW_THRESHOLDS)
		self.assertEqual(barriers, [])

	def test_multi_edge_mainstem_crosses_edge_boundary(self):
		# Two edges on the same mainstem: edge1 (mainstem_seq=1, most downstream) covers 0-60m,
		# edge2 (mainstem_seq=2, upstream of edge1) continues 60-105m. The shared boundary
		# vertex is the same point object in both edges' vertex lists, as real topologically
		# connected edges would share exact coordinates.
		i2 = point_at(0.0, 0.0)
		boundary = point_at(60.0, 6.0)
		up = point_at(105.0, 7.0)
		rows = [
			edge_row("multi-e1", "multi", 1, [boundary, i2]),
			edge_row("multi-e2", "multi", 2, [up, boundary]),
		]

		barriers = cb.compute_barriers(FakeConnection(rows), LOW_THRESHOLDS)

		# Only i2 resolves: its 100m mark (falls between boundary@60m and up@105m) is reached
		# once `up` is walked. boundary's own 100m mark (target 160m) is never reached -- the
		# mainstem only reaches 105m -- so it's left in the window and discarded.
		self.assertEqual(len(barriers), 1)
		lon, lat, gradient, species = barriers[0]
		self.assertAlmostEqual(lon, i2[0], places=9)
		self.assertAlmostEqual(lat, i2[1], places=9)
		expected_interp_elev = 6.0 + (100.0 - 60.0) / (105.0 - 60.0) * (7.0 - 6.0)
		expected_gradient = (expected_interp_elev - 0.0) / 100.0
		self.assertAlmostEqual(gradient, expected_gradient)
		self.assertEqual(species, ["chn_rear"])

	def test_multiple_mainstems_do_not_leak_into_each_other(self):
		# A short (no-barrier) mainstem followed by the worked-example mainstem in the same
		# fetch. Confirms running_total/window/prev reset cleanly on the mainstem_id boundary --
		# the short mainstem's leftover unresolved vertex must not carry into the next one.
		short_start = point_at(0.0, 0.0)
		short_end = point_at(60.0, 3.0)
		short_rows = [edge_row("short-e1", "short", 1, [short_end, short_start])]

		i = point_at(0.0, 0.0)
		a = point_at(50.0, 5.0)
		b = point_at(200.0, 5.75)
		worked_rows =  [edge_row("worked-e1", "2", 1, [b, a, i])]
		
		barriers = cb.compute_barriers(FakeConnection(short_rows + worked_rows), LOW_THRESHOLDS)

		self.assertEqual(len(barriers), 2)
		self.assertAlmostEqual(barriers[0][0], i[0], places=9)
		self.assertAlmostEqual(barriers[0][2], 0.0525)
		self.assertAlmostEqual(barriers[1][0], a[0], places=9)
		self.assertAlmostEqual(barriers[1][2], 0.005)


if __name__ == "__main__":
	unittest.main()
