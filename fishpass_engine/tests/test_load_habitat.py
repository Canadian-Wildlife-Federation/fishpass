"""Tests for fishpass_engine/scripts/load_habitat.py.

Run with: python -m unittest fishpass_engine.tests.test_load_habitat
"""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

for _module_name in ("psycopg",):
	if _module_name not in sys.modules:
		try:
			__import__(_module_name)
		except ImportError:
			sys.modules[_module_name] = types.ModuleType(_module_name)

import shapely  # noqa: E402 -- must be real; these tests build/parse actual WKB fixtures

import load_habitat as lh  # noqa: E402
from network_snap import linestring_zm_wkb  # noqa: E402


class FakeCursor:
	def __init__(self, fetch_results=None, fetchone_results=None):
		self.executed = []
		self.executemany_calls = []
		self._fetch_results = list(fetch_results or [])
		self._fetchone_results = list(fetchone_results or [])
		self.rowcount = 0

	def execute(self, sql, params=None):
		self.executed.append((" ".join(sql.split()), params))
		self.rowcount = 3

	def executemany(self, sql, params_seq=None):
		self.executemany_calls.append((" ".join(sql.split()), list(params_seq or [])))

	def fetchall(self):
		return self._fetch_results.pop(0) if self._fetch_results else []

	def fetchone(self):
		return self._fetchone_results.pop(0) if self._fetchone_results else None


def multipoint_wkb(coords):
	geom = shapely.MultiPoint(coords)
	return shapely.to_wkb(geom)


def point_zm_wkb(x, y, z, m):
	return shapely.to_wkb(shapely.from_wkt(f"POINT ZM ({x} {y} {z} {m})"))


class PointsForRoleTests(unittest.TestCase):
	def test_upstream_requires_one_point(self):
		result = lh.points_for_role("upstream", [(1.0, 2.0)], "h1")
		self.assertEqual(result, [("upstream", (1.0, 2.0))])

	def test_downstream_requires_one_point(self):
		result = lh.points_for_role("downstream", [(1.0, 2.0)], "h1")
		self.assertEqual(result, [("downstream", (1.0, 2.0))])

	def test_between_requires_two_points_in_order(self):
		result = lh.points_for_role("between", [(1.0, 2.0), (3.0, 4.0)], "h1")
		self.assertEqual(result, [("upstream", (1.0, 2.0)), ("downstream", (3.0, 4.0))])

	def test_upstream_with_wrong_point_count_exits(self):
		with self.assertRaises(SystemExit):
			lh.points_for_role("upstream", [(1.0, 2.0), (3.0, 4.0)], "h1")

	def test_between_with_wrong_point_count_exits(self):
		with self.assertRaises(SystemExit):
			lh.points_for_role("between", [(1.0, 2.0)], "h1")

	def test_unknown_location_type_exits(self):
		with self.assertRaises(SystemExit):
			lh.points_for_role("sideways", [(1.0, 2.0)], "h1")


class ResolvePointSpecificEdgeTests(unittest.TestCase):
	def test_snaps_to_specific_edge_upstream_endpoint(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]]
		wkb = linestring_zm_wkb(vertices)
		closest = point_zm_wkb(0.0, 0.0, 0.0, 0.0)
		cursor = FakeCursor(fetchone_results=[(wkb, closest, 1.0)])
		edge_cache = {}
		result = lh.resolve_point(
			cursor, "model_test", edge_cache, 4617, (0.00001, 0.0), 100, 50, "edge-1", "upstream", "h1"
		)
		self.assertEqual(result, ("edge-1", (0.0, 0.0, 0.0, 0.0)))

	def test_snaps_to_specific_edge_downstream_endpoint(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]]
		wkb = linestring_zm_wkb(vertices)
		closest = point_zm_wkb(0.01, 0.0, 0.0, 100.0)
		cursor = FakeCursor(fetchone_results=[(wkb, closest, 1.0)])
		edge_cache = {}
		result = lh.resolve_point(
			cursor, "model_test", edge_cache, 4617, (0.0100001, 0.0), 100, 50, "edge-1", "downstream", "h1"
		)
		self.assertEqual(result, ("edge-1", (0.01, 0.0, 0.0, 100.0)))

	def test_missing_specific_edge_exits(self):
		cursor = FakeCursor(fetchone_results=[None])
		edge_cache = {}
		with self.assertRaises(SystemExit):
			lh.resolve_point(cursor, "model_test", edge_cache, 4617, (0.0, 0.0), 100, 50, "missing-edge", "upstream", "h1")

	def test_point_too_far_from_specific_edge_exits(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]]
		wkb = linestring_zm_wkb(vertices)
		closest = point_zm_wkb(0.005, 0.0, 0.0, 50.0)
		cursor = FakeCursor(fetchone_results=[(wkb, closest, 1_200.0)])
		edge_cache = {}
		with self.assertRaises(SystemExit):
			# 1200m from the edge itself, well past a 100m tolerance
			lh.resolve_point(cursor, "model_test", edge_cache, 4617, (0.01, 0.02), 100, 50, "edge-1", "upstream", "h1")

	def test_snaps_mid_edge_when_far_from_endpoints_but_close_to_edge(self):
		# Regression test: a point far from either endpoint vertex but close to the middle of the
		# edge must still succeed (and insert a mid-edge vertex), not be checked against a single
		# endpoint's distance.
		vertices = [[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]]
		wkb = linestring_zm_wkb(vertices)
		closest = point_zm_wkb(0.005, 0.0, 0.0, 50.0)
		cursor = FakeCursor(fetchone_results=[(wkb, closest, 5.0)])
		edge_cache = {}
		edge_id, xyzm = lh.resolve_point(
			cursor, "model_test", edge_cache, 4617, (0.005, 0.00001), 100, 50, "edge-1", "upstream", "h1"
		)
		self.assertEqual(edge_id, "edge-1")
		self.assertAlmostEqual(xyzm[0], 0.005, places=5)
		self.assertTrue(edge_cache["edge-1"]["changed"])

	def test_reuses_cached_edge_for_second_point_on_specific_edge(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]]
		wkb = linestring_zm_wkb(vertices)
		closest1 = point_zm_wkb(0.003, 0.0, 0.0, 30.0)
		closest2 = point_zm_wkb(0.007, 0.0, 0.0, 70.0)
		cursor = FakeCursor(fetchone_results=[(wkb, closest1, 1.0), (wkb, closest2, 1.0)])
		edge_cache = {}
		lh.resolve_point(cursor, "model_test", edge_cache, 4617, (0.003, 0.0), 100, 50, "edge-1", "upstream", "h1")
		lh.resolve_point(cursor, "model_test", edge_cache, 4617, (0.007, 0.0), 100, 50, "edge-1", "downstream", "h1")
		# both points inserted into the same edge's vertex list
		self.assertEqual(len(edge_cache["edge-1"]["vertices"]), 4)


class ResolvePointNearestEdgeTests(unittest.TestCase):
	def test_no_edge_in_range_returns_none(self):
		cursor = FakeCursor(fetchone_results=[None])
		edge_cache = {}
		result = lh.resolve_point(cursor, "model_test", edge_cache, 4617, (5.0, 5.0), 100, 50, None, "upstream", "h1")
		self.assertIsNone(result)

	def test_inserts_vertex_on_nearest_edge(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]]
		wkb = linestring_zm_wkb(vertices)
		closest = point_zm_wkb(0.005, 0.0, 0.0, 50.0)
		cursor = FakeCursor(fetchone_results=[("edge-1", wkb, closest)])
		edge_cache = {}
		edge_id, xyzm = lh.resolve_point(cursor, "model_test", edge_cache, 4617, (0.005, 0.0), 100, 50, None, "upstream", "h1")
		self.assertEqual(edge_id, "edge-1")
		self.assertAlmostEqual(xyzm[0], 0.005, places=5)
		self.assertTrue(edge_cache["edge-1"]["changed"])

	def test_reuses_cached_edge_for_second_point(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]]
		wkb = linestring_zm_wkb(vertices)
		closest1 = point_zm_wkb(0.003, 0.0, 0.0, 30.0)
		closest2 = point_zm_wkb(0.007, 0.0, 0.0, 70.0)
		cursor = FakeCursor(fetchone_results=[("edge-1", wkb, closest1), ("edge-1", wkb, closest2)])
		edge_cache = {}
		lh.resolve_point(cursor, "model_test", edge_cache, 4617, (0.003, 0.0), 100, 50, None, "upstream", "h1")
		lh.resolve_point(cursor, "model_test", edge_cache, 4617, (0.007, 0.0), 100, 50, None, "downstream", "h1")
		# both points inserted into the same edge's vertex list
		self.assertEqual(len(edge_cache["edge-1"]["vertices"]), 4)


class ProcessHabitatRowTests(unittest.TestCase):
	def test_upstream_row(self):
		wkb = multipoint_wkb([(0.0, 0.0)])
		vertices = [[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]]
		edge_wkb = linestring_zm_wkb(vertices)
		closest = point_zm_wkb(0.0, 0.0, 0.0, 0.0)
		cursor = FakeCursor(fetchone_results=[(edge_wkb, closest, 1.0)])
		edge_cache = {}
		row = ("h1", "upstream", wkb, "edge-1", None)
		habitat_id, up, down = lh.process_habitat_row(cursor, "model_test", edge_cache, 4617, row, 100, 50)
		self.assertEqual(habitat_id, "h1")
		self.assertIsNotNone(up)
		self.assertIsNone(down)

	def test_between_row_resolves_both_ends(self):
		wkb = multipoint_wkb([(0.0, 0.0), (0.01, 0.0)])
		vertices = [[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]]
		edge_wkb = linestring_zm_wkb(vertices)
		closest_up = point_zm_wkb(0.0, 0.0, 0.0, 0.0)
		closest_down = point_zm_wkb(0.01, 0.0, 0.0, 100.0)
		cursor = FakeCursor(fetchone_results=[(edge_wkb, closest_up, 1.0), (edge_wkb, closest_down, 1.0)])
		edge_cache = {}
		row = ("h1", "between", wkb, "edge-1", "edge-1")
		habitat_id, up, down = lh.process_habitat_row(cursor, "model_test", edge_cache, 4617, row, 100, 50)
		self.assertIsNotNone(up)
		self.assertIsNotNone(down)


class LoadHabitatUpdatesRowsTests(unittest.TestCase):
	def test_query_filters_by_update_scope_and_distance(self):
		cursor = FakeCursor()
		count = lh.load_habitat_updates_rows(cursor, "model_test", {
			"habitat_update_table": "support.habitat_updates",
			"update_scope": "plan1",
			"habitat_point_snap_edge_distance_m": 100,
		})
		self.assertEqual(count, 3)
		sql, params = cursor.executed[0]
		self.assertIn("src.update_scope = 'all' OR src.update_scope = %s", sql)
		self.assertIn("ST_DWithin(e.geometry::geography, src.points::geography, %s)", sql)
		self.assertEqual(params, ("plan1", 100))


class WriteHabitatSnapResultsTests(unittest.TestCase):
	def test_null_handling_for_unresolved_point(self):
		cursor = FakeCursor()
		lh.write_habitat_snap_results(cursor, "model_test", 4617, [
			("h1", ("edge-1", (1.0, 2.0, 0.0, 5.0)), None),
		])
		_, rows = cursor.executemany_calls[0]
		self.assertEqual(rows, [("edge-1", 1.0, 2.0, None, None, None, "h1")])


if __name__ == "__main__":
	unittest.main()
