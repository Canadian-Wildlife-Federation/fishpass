"""Tests for fishpass_engine/scripts/network_snap.py -- pure geometry logic (segment location,
vertex snap-or-insert) shared by structure and habitat point snapping. Uses real shapely (WKB
round-tripping), no database.

Run with: python -m unittest fishpass_engine.tests.test_network_snap
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

import shapely  # noqa: E402

import network_snap as ns  # noqa: E402

make_edge_wkb = ns.linestring_zm_wkb  # vertices: list of [x, y, z, m]


def point_zm_wkb(x, y, z, m):
	return shapely.to_wkb(shapely.from_wkt(f"POINT ZM ({x} {y} {z} {m})"))


def project_onto_vertices(vertices, x, y):
	"""Simulate what the caller's SQL (ST_LineLocatePoint/ST_LineInterpolatePoint) would hand
	snap_points_to_edge: the closest point on the polyline to (x, y), with z/m interpolated.
	Test-fixture helper only -- production code gets this from PostGIS, not this formula."""
	best = None
	for i in range(len(vertices) - 1):
		x1, y1, z1, m1 = vertices[i]
		x2, y2, z2, m2 = vertices[i + 1]
		seg_len_sq = (x2 - x1) ** 2 + (y2 - y1) ** 2
		t = 0.0 if seg_len_sq == 0 else max(0.0, min(1.0, ((x - x1) * (x2 - x1) + (y - y1) * (y2 - y1)) / seg_len_sq))
		px, py = x1 + t * (x2 - x1), y1 + t * (y2 - y1)
		pz, pm = z1 + t * (z2 - z1), m1 + t * (m2 - m1)
		dist = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
		if best is None or dist < best[0]:
			best = (dist, px, py, pz, pm)
	return best[1], best[2], best[3], best[4]


class PointXyzmTests(unittest.TestCase):
	def test_decodes_xyzm(self):
		wkb = point_zm_wkb(1.5, 0.0, 0.0, 15.0)
		x, y, z, m = ns.point_xyzm(wkb)
		self.assertAlmostEqual(x, 1.5)
		self.assertAlmostEqual(y, 0.0)
		self.assertAlmostEqual(m, 15.0)


class LocateSegmentTests(unittest.TestCase):
	def test_point_on_middle_segment(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 10.0], [2.0, 0.0, 0.0, 20.0]]
		self.assertEqual(ns.locate_segment(vertices, (1.5, 0.0)), 1)

	def test_point_on_first_segment(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 10.0], [2.0, 0.0, 0.0, 20.0]]
		self.assertEqual(ns.locate_segment(vertices, (0.5, 0.0)), 0)

	def test_point_at_shared_interior_vertex(self):
		# lands exactly on the vertex shared by segments 0 and 1 -- either index is a correct
		# answer since both are zero-distance; locate_segment should not raise or pick randomly.
		vertices = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 10.0], [2.0, 0.0, 0.0, 20.0]]
		seg_index = ns.locate_segment(vertices, (1.0, 0.0))
		self.assertIn(seg_index, (0, 1))


class SnapPointsToEdgeTests(unittest.TestCase):
	def test_snaps_to_existing_vertex_within_tolerance(self):
		# vertices ~0.001 deg apart (~111m at the equator); point very close to the second vertex
		vertices = [[0.0, 0.0, 0.0, 0.0], [0.001, 0.0, 0.0, 10.0], [0.002, 0.0, 0.0, 20.0]]
		wkb = make_edge_wkb(vertices)
		points = [("s1", *project_onto_vertices(vertices, 0.001001, 0.0))]
		new_vertices, results, changed = ns.snap_points_to_edge(wkb, points, vertex_distance_m=50)
		self.assertFalse(changed)
		self.assertEqual(len(new_vertices), 3)  # unchanged
		item_id, x, y, z, m = results[0]
		self.assertAlmostEqual(x, 0.001)
		self.assertAlmostEqual(m, 10.0)

	def test_inserts_new_vertex_when_no_vertex_in_tolerance(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]]  # ~1.1km apart
		wkb = make_edge_wkb(vertices)
		points = [("s1", *project_onto_vertices(vertices, 0.005, 0.0))]  # midpoint, far from either existing vertex
		new_vertices, results, changed = ns.snap_points_to_edge(wkb, points, vertex_distance_m=50)
		self.assertTrue(changed)
		self.assertEqual(len(new_vertices), 3)
		item_id, x, y, z, m = results[0]
		self.assertAlmostEqual(x, 0.005, places=5)
		self.assertAlmostEqual(m, 50.0, places=3)  # interpolated halfway

	def test_multiple_points_on_same_edge_both_inserted(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]]
		wkb = make_edge_wkb(vertices)
		points = [
			("s1", *project_onto_vertices(vertices, 0.003, 0.0)),
			("s2", *project_onto_vertices(vertices, 0.007, 0.0)),
		]
		new_vertices, results, changed = ns.snap_points_to_edge(wkb, points, vertex_distance_m=50)
		self.assertTrue(changed)
		self.assertEqual(len(new_vertices), 4)
		self.assertEqual(len(results), 2)
		xs = sorted(r[1] for r in results)
		self.assertAlmostEqual(xs[0], 0.003, places=5)
		self.assertAlmostEqual(xs[1], 0.007, places=5)


class HaversineTests(unittest.TestCase):
	def test_zero_distance(self):
		self.assertAlmostEqual(ns.haversine_m(-63.0, 45.0, -63.0, 45.0), 0.0)

	def test_known_short_distance(self):
		# ~0.001 degree longitude at the equator is ~111m
		d = ns.haversine_m(0.0, 0.0, 0.001, 0.0)
		self.assertAlmostEqual(d, 111.19, delta=1.0)


if __name__ == "__main__":
	unittest.main()
