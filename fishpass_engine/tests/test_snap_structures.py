"""Tests for fishpass_engine/scripts/snap_structures.py's grouping/SQL-shape logic (the pure
geometry math it delegates to is tested in test_network_snap.py).

Run with: python -m unittest fishpass_engine.tests.test_snap_structures
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

import snap_structures as ss  # noqa: E402


def point_zm_wkb(x, y, z, m):
	return shapely.to_wkb(shapely.from_wkt(f"POINT ZM ({x} {y} {z} {m})"))


class FakeCursor:
	def __init__(self, fetch_results=None, fetchone_results=None):
		self.executed = []
		self.executemany_calls = []
		self._fetch_results = list(fetch_results or [])
		self._fetchone_results = list(fetchone_results or [])
		self.rowcount = 0

	def execute(self, sql, params=None):
		self.executed.append((" ".join(sql.split()), params))
		self.rowcount = 1

	def executemany(self, sql, params_seq=None):
		self.executemany_calls.append((" ".join(sql.split()), list(params_seq or [])))

	def fetchall(self):
		return self._fetch_results.pop(0) if self._fetch_results else []

	def fetchone(self):
		return self._fetchone_results.pop(0) if self._fetchone_results else None


class GroupByEdgeTests(unittest.TestCase):
	def test_groups_multiple_structures_on_same_edge(self):
		matches = [
			("s1", "e1", b"wkb1", point_zm_wkb(0.0, 0.0, 0.0, 0.0)),
			("s2", "e1", b"wkb1", point_zm_wkb(0.1, 0.1, 0.0, 1.0)),
			("s3", "e2", b"wkb2", point_zm_wkb(1.0, 1.0, 0.0, 2.0)),
		]
		by_edge = ss.group_by_edge(matches)
		self.assertEqual(set(by_edge.keys()), {"e1", "e2"})
		self.assertEqual(len(by_edge["e1"]["structures"]), 2)
		self.assertEqual(len(by_edge["e2"]["structures"]), 1)
		self.assertEqual(by_edge["e1"]["wkb"], b"wkb1")
		structure_id, x, y, z, m = by_edge["e1"]["structures"][0]
		self.assertEqual(structure_id, "s1")
		self.assertAlmostEqual(x, 0.0)
		self.assertAlmostEqual(m, 0.0)


class FetchCandidateMatchesTests(unittest.TestCase):
	def test_query_shape(self):
		cursor = FakeCursor(fetch_results=[[]])
		ss.fetch_candidate_matches(cursor, "model_test", 100)
		sql, params = cursor.executed[0]
		self.assertIn("ST_DWithin", sql)
		self.assertIn("ST_LineLocatePoint", sql)
		self.assertIn("ST_LineInterpolatePoint", sql)
		self.assertIn("s.snapped_geometry IS NULL", sql)
		self.assertEqual(params, (100,))


class WriteSnappedGeometriesTests(unittest.TestCase):
	def test_reprojects_to_snapped_srid(self):
		cursor = FakeCursor()
		ss.write_snapped_geometries(cursor, "model_test", 4617, [("s1", "edge-1", -63.0, 45.0, 0.0, 10.0)])
		sql, rows = cursor.executemany_calls[0]
		self.assertIn(f"ST_Transform(ST_SetSRID(ST_MakePoint(v.x, v.y), 4617), {ss.SNAPPED_GEOMETRY_SRID})", sql)
		self.assertIn("VALUES (%s, %s, %s, %s)) AS v(x, y, edge_id, structure_id)", sql)
		self.assertEqual(rows, [(-63.0, 45.0, "edge-1", "s1")])

	def test_sets_downstream_edge_id_in_same_statement(self):
		cursor = FakeCursor()
		ss.write_snapped_geometries(cursor, "model_test", 4617, [("s1", "edge-1", -63.0, 45.0, 0.0, 10.0)])
		sql, _rows = cursor.executemany_calls[0]
		self.assertIn("downstream_edge_id = v.edge_id", sql)
		self.assertEqual(cursor.executed, [])


class WriteEdgeGeometriesTests(unittest.TestCase):
	def test_writes_geometry_per_changed_edge(self):
		cursor = FakeCursor()
		vertices = [[0.0, 0.0, 0.0, 0.0], [1.0, 1.0, 0.0, 1.0]]
		ss.write_edge_geometries(cursor, "model_test", 4617, [("edge-1", vertices)])
		sql, rows = cursor.executemany_calls[0]
		self.assertIn("UPDATE", sql)
		self.assertIn("streams SET geometry = ST_SetSRID(ST_GeomFromWKB(%s), %s) WHERE id = %s", sql)
		self.assertEqual(len(rows), 1)
		wkb, srid, edge_id = rows[0]
		self.assertEqual(srid, 4617)
		self.assertEqual(edge_id, "edge-1")
		geom = shapely.from_wkb(bytes(wkb))
		self.assertEqual(
			shapely.get_coordinates(geom, include_z=True, include_m=True).tolist(),
			vertices,
		)


if __name__ == "__main__":
	unittest.main()
