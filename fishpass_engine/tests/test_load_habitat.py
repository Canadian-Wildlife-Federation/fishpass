"""Tests for fishpass_engine/scripts/load_habitat.py.

Run with: python -m unittest fishpass_engine.tests.test_load_habitat
"""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

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
			lh.resolve_point(
				cursor, "model_test", edge_cache, 4617, (0.0, 0.0), 100, 50, "missing-edge", "upstream", "h1"
			)

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
		edge_id, xyzm = lh.resolve_point(
			cursor, "model_test", edge_cache, 4617, (0.005, 0.0), 100, 50, None, "upstream", "h1"
		)
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


def nexus_edge(edge_id, is_input, distance_m, mainstem_id="ms-main"):
	return {"id": edge_id, "mainstem_id": mainstem_id, "is_input": is_input, "distance_m": distance_m, "wkb": b""}


class ChooseInputEdgeTests(unittest.TestCase):
	"""The nexus cases of the Process Habitat table in fishpass_docs.md. The confluence here is
	tributary "trib" (mainstem ms-trib) and "main-up" (ms-main) flowing into "main-down" (ms-main)."""

	def confluence(self, trib_m, main_up_m, main_down_m):
		return [
			nexus_edge("main-down", False, main_down_m),
			nexus_edge("main-up", True, main_up_m),
			nexus_edge("trib", True, trib_m, mainstem_id="ms-trib"),
		]

	def choose(self, nexus_edges, specific_edge_id=None, top_mainstem_id=None):
		return lh.choose_input_edge(nexus_edges, specific_edge_id, "upstream", "h1", top_mainstem_id)

	def test_case_4_no_edge_id_single_input_edge_uses_it(self):
		edges = [nexus_edge("below", False, 0.0), nexus_edge("above", True, 0.0)]
		self.assertEqual(self.choose(edges)["id"], "above")

	def test_case_5a_edge_id_on_first_vertex_uses_same_mainstem_input(self):
		edges = self.confluence(trib_m=0.0, main_up_m=5.0, main_down_m=0.0)
		self.assertEqual(self.choose(edges, specific_edge_id="main-down")["id"], "main-up")

	def test_case_5b_edge_id_on_headwater_first_vertex_is_cleared(self):
		self.assertIsNone(self.choose([nexus_edge("headwater", False, 0.0)], specific_edge_id="headwater"))

	def test_case_5c_edge_id_with_no_same_mainstem_input_exits(self):
		edges = [
			nexus_edge("main-down", False, 0.0),
			nexus_edge("other-1", True, 0.0, mainstem_id="ms-other-1"),
			nexus_edge("other-2", True, 0.0, mainstem_id="ms-other-2"),
		]
		with self.assertRaises(SystemExit) as ctx:
			self.choose(edges, specific_edge_id="main-down")
		self.assertIn("h1", str(ctx.exception))
		self.assertIn("main-down", str(ctx.exception))

	def test_case_6a_i_closest_input_edge_is_used(self):
		edges = self.confluence(trib_m=1.0, main_up_m=4.0, main_down_m=3.0)
		with self.assertNoLogs(lh.logger, level="WARNING"):
			self.assertEqual(self.choose(edges)["id"], "trib")

	def test_case_6a_ii_closest_outflow_edge_uses_its_same_mainstem_input(self):
		edges = self.confluence(trib_m=3.0, main_up_m=4.0, main_down_m=1.0)
		with self.assertNoLogs(lh.logger, level="WARNING"):
			self.assertEqual(self.choose(edges)["id"], "main-up")

	def test_case_6a_ii_closest_outflow_edge_with_no_same_mainstem_input_exits(self):
		edges = [
			nexus_edge("main-down", False, 1.0),
			nexus_edge("other-1", True, 3.0, mainstem_id="ms-other-1"),
			nexus_edge("other-2", True, 4.0, mainstem_id="ms-other-2"),
		]
		with self.assertRaises(SystemExit):
			self.choose(edges)

	def test_case_6b_tie_uses_outflow_mainstem_input_and_warns(self):
		edges = self.confluence(trib_m=0.0, main_up_m=0.0, main_down_m=0.0)
		with self.assertLogs(lh.logger, level="WARNING") as logs:
			self.assertEqual(self.choose(edges)["id"], "main-up")
		message = logs.output[0]
		for expected in ("h1", "trib", "main-up", "main-down", "chyf_upstream_edge_id"):
			self.assertIn(expected, message)

	def test_case_6b_tie_between_tributary_and_outflow_edge_is_still_a_tie(self):
		# Any tie for closest is ambiguous, whichever edges are in it.
		edges = self.confluence(trib_m=1.0, main_up_m=5.0, main_down_m=1.0)
		with self.assertLogs(lh.logger, level="WARNING"):
			self.assertEqual(self.choose(edges)["id"], "main-up")

	def test_distances_within_the_tolerance_are_a_tie(self):
		edges = self.confluence(trib_m=1.0, main_up_m=1.0 + lh.TIE_TOLERANCE_M / 2, main_down_m=9.0)
		with self.assertLogs(lh.logger, level="WARNING"):
			self.assertEqual(self.choose(edges)["id"], "main-up")

	def test_distances_beyond_the_tolerance_are_not_a_tie(self):
		edges = self.confluence(trib_m=1.0, main_up_m=1.0 + lh.TIE_TOLERANCE_M * 2, main_down_m=9.0)
		with self.assertNoLogs(lh.logger, level="WARNING"):
			self.assertEqual(self.choose(edges)["id"], "trib")

	def test_tie_among_farther_edges_does_not_matter(self):
		edges = self.confluence(trib_m=1.0, main_up_m=4.0, main_down_m=4.0)
		with self.assertNoLogs(lh.logger, level="WARNING"):
			self.assertEqual(self.choose(edges)["id"], "trib")

	def test_case_7_no_edge_id_on_headwater_first_vertex_is_cleared(self):
		self.assertIsNone(self.choose([nexus_edge("headwater", False, 0.0)]))

	def test_case_8_tie_at_shared_outlet_exits(self):
		edges = [nexus_edge("out-1", True, 0.0), nexus_edge("out-2", True, 0.0, mainstem_id="ms-2")]
		with self.assertRaises(SystemExit) as ctx:
			self.choose(edges)
		for expected in ("h1", "out-1", "out-2", "chyf_upstream_edge_id"):
			self.assertIn(expected, str(ctx.exception))

	def test_shared_outlet_with_one_closest_edge_uses_it(self):
		edges = [nexus_edge("out-1", True, 2.0), nexus_edge("out-2", True, 1.0, mainstem_id="ms-2")]
		self.assertEqual(self.choose(edges)["id"], "out-2")

	# A "between" row: where the downstream point alone doesn't settle the edge, the river its
	# upstream point is on (top_mainstem_id) does.

	def test_between_tie_follows_the_upstream_points_river_without_warning(self):
		edges = self.confluence(trib_m=0.0, main_up_m=0.0, main_down_m=0.0)
		with self.assertNoLogs(lh.logger, level="WARNING"):
			self.assertEqual(self.choose(edges, top_mainstem_id="ms-trib")["id"], "trib")

	def test_between_closest_outflow_edge_follows_the_upstream_points_river(self):
		edges = self.confluence(trib_m=3.0, main_up_m=4.0, main_down_m=1.0)
		self.assertEqual(self.choose(edges, top_mainstem_id="ms-trib")["id"], "trib")

	def test_between_clearly_closest_input_edge_still_wins(self):
		edges = self.confluence(trib_m=4.0, main_up_m=1.0, main_down_m=3.0)
		self.assertEqual(self.choose(edges, top_mainstem_id="ms-trib")["id"], "main-up")

	def test_between_tie_at_shared_outlet_follows_the_upstream_points_river(self):
		edges = [nexus_edge("out-1", True, 0.0), nexus_edge("out-2", True, 0.0, mainstem_id="ms-2")]
		self.assertEqual(self.choose(edges, top_mainstem_id="ms-2")["id"], "out-2")

	def test_between_upstream_point_on_another_river_falls_back_to_the_tie_rule(self):
		edges = self.confluence(trib_m=0.0, main_up_m=0.0, main_down_m=0.0)
		with self.assertLogs(lh.logger, level="WARNING"):
			self.assertEqual(self.choose(edges, top_mainstem_id="ms-elsewhere")["id"], "main-up")

	def test_between_edge_id_given_ignores_the_upstream_points_river(self):
		edges = self.confluence(trib_m=0.0, main_up_m=5.0, main_down_m=0.0)
		chosen = self.choose(edges, specific_edge_id="main-down", top_mainstem_id="ms-trib")
		self.assertEqual(chosen["id"], "main-up")


class ResolveBottomPointTests(unittest.TestCase):
	"""resolve_point for a bottom point. "edge-1" runs from (0, 0) to (0.01, 0); "above" ends at
	(0, 0), edge-1's first vertex."""

	EDGE_1 = [[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]]
	ABOVE = [[-0.01, 0.0, 5.0, 0.0], [0.0, 0.0, 0.0, 100.0]]

	def resolve(self, cursor, edge_cache, xy, specific_edge_id):
		return lh.resolve_point(
			cursor, "model_test", edge_cache, 4617, xy, 100, 50, specific_edge_id, "upstream", "h1", is_bottom=True
		)

	def nexus_rows(self):
		# fetch_nexus_edges rows: id, mainstem_id, is_input, distance_m, wkb -- ordered by id.
		return [
			("above", "ms", True, 0.0, linestring_zm_wkb(self.ABOVE)),
			("edge-1", "ms", False, 0.0, linestring_zm_wkb(self.EDGE_1)),
		]

	def test_cases_1_and_2_mid_edge_point_keeps_its_edge(self):
		wkb = linestring_zm_wkb(self.EDGE_1)
		closest = point_zm_wkb(0.005, 0.0, 0.0, 50.0)
		for specific_edge_id, fetchone in (("edge-1", (wkb, closest, 1.0)), (None, ("edge-1", wkb, closest))):
			cursor = FakeCursor(fetchone_results=[fetchone])
			edge_id, xyzm = self.resolve(cursor, {}, (0.005, 0.0), specific_edge_id)
			self.assertEqual(edge_id, "edge-1")
			self.assertAlmostEqual(xyzm[0], 0.005, places=5)
			# No nexus lookup.
			self.assertEqual(len(cursor.executed), 1)

	def test_case_3_edge_id_on_last_vertex_keeps_its_edge(self):
		wkb = linestring_zm_wkb(self.EDGE_1)
		closest = point_zm_wkb(0.01, 0.0, 0.0, 100.0)
		cursor = FakeCursor(fetchone_results=[(wkb, closest, 1.0)])
		result = self.resolve(cursor, {}, (0.01, 0.0), "edge-1")
		self.assertEqual(result, ("edge-1", (0.01, 0.0, 0.0, 100.0)))
		self.assertEqual(len(cursor.executed), 1)

	def test_case_5a_edge_id_on_first_vertex_moves_to_last_vertex_of_edge_above(self):
		wkb = linestring_zm_wkb(self.EDGE_1)
		closest = point_zm_wkb(0.0, 0.0, 0.0, 0.0)
		cursor = FakeCursor(fetchone_results=[(wkb, closest, 1.0)], fetch_results=[self.nexus_rows()])
		edge_cache = {}
		result = self.resolve(cursor, edge_cache, (0.00001, 0.0), "edge-1")
		# The edge above, at its own last vertex (its Z and M, not edge-1's).
		self.assertEqual(result, ("above", (0.0, 0.0, 0.0, 100.0)))
		# Nothing was inserted into either edge.
		self.assertFalse(edge_cache["edge-1"]["changed"])
		self.assertFalse(edge_cache["above"]["changed"])

	def test_nexus_lookup_uses_first_vertex_nexus_and_the_point_as_entered(self):
		wkb = linestring_zm_wkb(self.EDGE_1)
		closest = point_zm_wkb(0.0, 0.0, 0.0, 0.0)
		cursor = FakeCursor(fetchone_results=[(wkb, closest, 1.0)], fetch_results=[self.nexus_rows()])
		self.resolve(cursor, {}, (0.00001, 0.0), "edge-1")
		sql, params = cursor.executed[-1]
		self.assertIn("SELECT from_nexus_id AS nexus_id", sql)
		# Edges are matched on nexus id alone; distances are measured to the point as entered.
		self.assertNotIn("ST_DWithin", sql)
		self.assertEqual(params, (0.00001, 0.0, 4617, "edge-1"))

	def test_case_4_no_edge_id_on_last_vertex_looks_up_the_to_nexus(self):
		# Snapped to the last vertex of "above"; it is the only edge flowing into that nexus.
		wkb = linestring_zm_wkb(self.ABOVE)
		closest = point_zm_wkb(0.0, 0.0, 0.0, 100.0)
		cursor = FakeCursor(fetchone_results=[("above", wkb, closest)], fetch_results=[self.nexus_rows()])
		result = self.resolve(cursor, {}, (0.0, 0.0), None)
		self.assertEqual(result, ("above", (0.0, 0.0, 0.0, 100.0)))
		self.assertIn("SELECT to_nexus_id AS nexus_id", cursor.executed[-1][0])

	def test_cases_5b_and_7_headwater_point_is_ignored(self):
		wkb = linestring_zm_wkb(self.EDGE_1)
		closest = point_zm_wkb(0.0, 0.0, 0.0, 0.0)
		headwater_rows = [("edge-1", "ms", False, 0.0, wkb)]
		for specific_edge_id, fetchone in (("edge-1", (wkb, closest, 1.0)), (None, ("edge-1", wkb, closest))):
			cursor = FakeCursor(fetchone_results=[fetchone], fetch_results=[headwater_rows])
			self.assertIsNone(self.resolve(cursor, {}, (0.0, 0.0), specific_edge_id))

	def test_between_downstream_point_at_confluence_looks_up_the_upstream_points_river(self):
		# No edge id; the point sits exactly on the confluence of "main-up" and "trib" above edge-1.
		wkb = linestring_zm_wkb(self.EDGE_1)
		trib = [[-0.01, 0.01, 7.0, 0.0], [0.0, 0.0, 0.0, 140.0]]
		rows = [
			("edge-1", "ms-main", False, 0.0, wkb),
			("main-up", "ms-main", True, 0.0, linestring_zm_wkb(self.ABOVE)),
			("trib", "ms-trib", True, 0.0, linestring_zm_wkb(trib)),
		]
		closest = point_zm_wkb(0.0, 0.0, 0.0, 0.0)
		# fetchone: find_nearest_edge, then the mainstem_id of the upstream point's edge.
		cursor = FakeCursor(fetchone_results=[("edge-1", wkb, closest), ("ms-trib",)], fetch_results=[rows])
		result = lh.resolve_point(
			cursor,
			"model_test",
			{},
			4617,
			(0.0, 0.0),
			100,
			50,
			None,
			"downstream",
			"h1",
			is_bottom=True,
			top_edge_id="top-edge",
		)
		self.assertEqual(result, ("trib", (0.0, 0.0, 0.0, 140.0)))
		sql, params = cursor.executed[-1]
		self.assertIn("SELECT mainstem_id", sql)
		self.assertEqual(params, ("top-edge",))

	def test_top_point_on_first_vertex_is_left_alone(self):
		wkb = linestring_zm_wkb(self.EDGE_1)
		closest = point_zm_wkb(0.0, 0.0, 0.0, 0.0)
		cursor = FakeCursor(fetchone_results=[(wkb, closest, 1.0)])
		result = lh.resolve_point(
			cursor, "model_test", {}, 4617, (0.0, 0.0), 100, 50, "edge-1", "downstream", "h1", is_bottom=False
		)
		self.assertEqual(result, ("edge-1", (0.0, 0.0, 0.0, 0.0)))
		self.assertEqual(len(cursor.executed), 1)


class ProcessHabitatRowTests(unittest.TestCase):
	def test_upstream_row(self):
		# An "upstream" row's point is a bottom point. It is on edge-1's last vertex, so it stays.
		wkb = multipoint_wkb([(0.01, 0.0)])
		vertices = [[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]]
		edge_wkb = linestring_zm_wkb(vertices)
		closest = point_zm_wkb(0.01, 0.0, 0.0, 100.0)
		cursor = FakeCursor(fetchone_results=[(edge_wkb, closest, 1.0)])
		edge_cache = {}
		row = ("h1", "upstream", wkb, "edge-1", None)
		habitat_id, up, down = lh.process_habitat_row(cursor, "model_test", edge_cache, 4617, row, 100, 50)
		self.assertEqual(habitat_id, "h1")
		self.assertEqual(up, ("edge-1", (0.01, 0.0, 0.0, 100.0)))
		self.assertIsNone(down)

	def test_upstream_row_on_first_vertex_moves_to_edge_above(self):
		wkb = multipoint_wkb([(0.0, 0.0)])
		edge_wkb = linestring_zm_wkb([[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]])
		above_wkb = linestring_zm_wkb([[-0.01, 0.0, 5.0, 0.0], [0.0, 0.0, 0.0, 100.0]])
		closest = point_zm_wkb(0.0, 0.0, 0.0, 0.0)
		cursor = FakeCursor(
			fetchone_results=[(edge_wkb, closest, 1.0)],
			fetch_results=[[("above", "ms", True, 0.0, above_wkb), ("edge-1", "ms", False, 0.0, edge_wkb)]],
		)
		row = ("h1", "upstream", wkb, "edge-1", None)
		_, up, _ = lh.process_habitat_row(cursor, "model_test", {}, 4617, row, 100, 50)
		self.assertEqual(up, ("above", (0.0, 0.0, 0.0, 100.0)))

	def test_downstream_row_on_first_vertex_is_left_alone(self):
		# A "downstream" row's point is a top point: no nexus lookup.
		wkb = multipoint_wkb([(0.0, 0.0)])
		edge_wkb = linestring_zm_wkb([[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]])
		closest = point_zm_wkb(0.0, 0.0, 0.0, 0.0)
		cursor = FakeCursor(fetchone_results=[(edge_wkb, closest, 1.0)])
		row = ("h1", "downstream", wkb, None, "edge-1")
		_, up, down = lh.process_habitat_row(cursor, "model_test", {}, 4617, row, 100, 50)
		self.assertIsNone(up)
		self.assertEqual(down, ("edge-1", (0.0, 0.0, 0.0, 0.0)))
		self.assertEqual(len(cursor.executed), 1)

	def test_between_row_passes_the_upstream_points_edge_to_the_downstream_point(self):
		wkb = multipoint_wkb([(0.0, 0.0), (0.01, 0.0)])
		resolved = [("top-edge", (0.0, 0.0, 0.0, 0.0)), ("bottom-edge", (0.01, 0.0, 0.0, 100.0))]
		with mock.patch.object(lh, "resolve_point", side_effect=resolved) as resolve_point:
			lh.process_habitat_row(mock.Mock(), "model_test", {}, 4617, ("h1", "between", wkb, None, None), 100, 50)
		up_call, down_call = resolve_point.call_args_list
		self.assertIsNone(up_call.kwargs["top_edge_id"])
		self.assertFalse(up_call.kwargs["is_bottom"])
		self.assertEqual(down_call.kwargs["top_edge_id"], "top-edge")
		self.assertTrue(down_call.kwargs["is_bottom"])

	def test_between_row_with_both_points_on_one_vertex_is_ignored(self):
		# Both points snap to edge-1's last vertex: there is no stream between them.
		wkb = multipoint_wkb([(0.0099, 0.0), (0.01, 0.0)])
		edge_wkb = linestring_zm_wkb([[0.0, 0.0, 0.0, 0.0], [0.01, 0.0, 0.0, 100.0]])
		closest = point_zm_wkb(0.01, 0.0, 0.0, 100.0)
		cursor = FakeCursor(fetchone_results=[(edge_wkb, closest, 1.0), (edge_wkb, closest, 1.0)])
		row = ("h1", "between", wkb, "edge-1", "edge-1")
		with self.assertLogs(lh.logger, level="WARNING") as logs:
			habitat_id, up, down = lh.process_habitat_row(cursor, "model_test", {}, 4617, row, 100, 50)
		self.assertEqual((habitat_id, up, down), ("h1", None, None))
		self.assertIn("h1", logs.output[0])

	def test_between_row_with_points_on_different_edges_at_one_nexus_is_ignored(self):
		# The upstream point is on the edge below the nexus and the downstream point on the edge
		# above it, at the same place.
		wkb = multipoint_wkb([(0.0, 0.0), (0.0, 0.0)])
		resolved = [("below", (0.0, 0.0, 0.0, 0.0)), ("above", (0.0, 0.0, 0.0, 100.0))]
		with mock.patch.object(lh, "resolve_point", side_effect=resolved), self.assertLogs(lh.logger, level="WARNING"):
			_, up, down = lh.process_habitat_row(
				mock.Mock(), "model_test", {}, 4617, ("h1", "between", wkb, None, None), 100, 50
			)
		self.assertIsNone(up)
		self.assertIsNone(down)


class SnapHabitatPointsTests(unittest.TestCase):
	def test_counts_every_row_left_without_a_point_it_needs(self):
		snapped = ("edge-1", (0.0, 0.0, 0.0, 0.0))
		# (row, process_habitat_row's upstream and downstream results)
		cases = [
			(("h1", "upstream", b"", None, None), snapped, None),  # applies
			(("h2", "upstream", b"", "edge-1", None), None, None),  # edge id given, point ignored
			(("h3", "upstream", b"", None, None), None, None),  # point didn't snap
			(("h4", "downstream", b"", None, None), None, snapped),  # applies
			(("h5", "between", b"", None, None), snapped, None),  # one point missing
			(("h6", "between", b"", None, None), snapped, snapped),  # applies
		]
		cursor = FakeCursor(fetch_results=[[row for row, _, _ in cases]])
		plan = {
			"output_schema": "model_test",
			"habitat_point_snap_edge_distance_m": 100,
			"habitat_point_snap_vertex_distance_m": 50,
		}
		results = [(row[0], up, down) for row, up, down in cases]
		with mock.patch.object(lh, "process_habitat_row", side_effect=results):
			counts = lh.snap_habitat_points(mock.Mock(), cursor, plan, 4617)
		self.assertEqual(counts, (6, 3))

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
		count = lh.load_habitat_updates_rows(
			cursor,
			"model_test",
			{
				"habitat_update_table": "support.habitat_updates",
				"update_scope": "plan1",
				"habitat_point_snap_edge_distance_m": 100,
			},
		)
		self.assertEqual(count, 3)
		sql, params = cursor.executed[0]
		self.assertIn("src.update_scope = 'all' OR src.update_scope = %s", sql)
		self.assertIn("ST_DWithin(e.geometry::geography, src.points::geography, %s)", sql)
		self.assertEqual(params, ("plan1", 100))


class WriteHabitatSnapResultsTests(unittest.TestCase):
	def test_null_handling_for_unresolved_point(self):
		cursor = FakeCursor()
		lh.write_habitat_snap_results(
			cursor,
			"model_test",
			4617,
			[
				("h1", ("edge-1", (1.0, 2.0, 0.0, 5.0)), None),
			],
		)
		_, rows = cursor.executemany_calls[0]
		self.assertEqual(rows, [("edge-1", 1.0, 2.0, None, None, None, "h1")])


if __name__ == "__main__":
	unittest.main()
