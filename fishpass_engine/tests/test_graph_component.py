"""Tests for fishpass_engine/scripts/graph_component.py: SQL-shape checks against a stubbed
cursor, the pure bundling/write-row helpers, plus a full synthetic end-to-end run of
process_component on the confluence test network (no database).

Run with: python -m unittest fishpass_engine.tests.test_graph_component
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

import graph_component as gc  # noqa: E402


class FakeCursor:
	def __init__(self, fetch_results=None):
		self.executed = []
		self.executemany_calls = []
		self._fetch_results = list(fetch_results or [])

	def execute(self, sql, params=None):
		self.executed.append((" ".join(sql.split()), params))

	def executemany(self, sql, params_seq=None):
		self.executemany_calls.append((" ".join(sql.split()), list(params_seq or [])))

	def fetchall(self):
		return self._fetch_results.pop(0) if self._fetch_results else []


class FetchGraphIdCountsTests(unittest.TestCase):
	def test_groups_orders_and_excludes_isolated(self):
		cursor = FakeCursor(fetch_results=[[(1, 50), (2, 10)]])
		result = gc.fetch_graph_id_counts(cursor, "model_test")
		self.assertEqual(result, [(1, 50), (2, 10)])
		sql, _ = cursor.executed[0]
		self.assertIn("is_isolated IS NULL OR is_isolated = false", sql)
		self.assertIn("graph_id IS NOT NULL", sql)
		self.assertIn("GROUP BY graph_id", sql)
		self.assertIn("ORDER BY COUNT(*) DESC", sql)


class BuildGraphIdBundlesTests(unittest.TestCase):
	def test_packs_small_components_together(self):
		counts = [(1, 40), (2, 30), (3, 20)]
		bundles = gc.build_graph_id_bundles(counts, max_bundle_edges=100)
		self.assertEqual(bundles, [[1, 2, 3]])

	def test_large_component_gets_its_own_bundle(self):
		counts = [(1, 150), (2, 10), (3, 10)]
		bundles = gc.build_graph_id_bundles(counts, max_bundle_edges=100)
		self.assertEqual(bundles, [[1], [2, 3]])

	def test_descending_input_isolates_big_components_first(self):
		counts = [(1, 90), (2, 80), (3, 5), (4, 5), (5, 5)]
		bundles = gc.build_graph_id_bundles(counts, max_bundle_edges=100)
		# 1 (90) alone exceeds the budget paired with anything else; 2 (80) then packs with the
		# three small (5-edge) components since 80+5+5+5 = 95 <= 100.
		self.assertEqual(bundles, [[1], [2, 3, 4, 5]])

	def test_empty_input_produces_no_bundles(self):
		self.assertEqual(gc.build_graph_id_bundles([], max_bundle_edges=100), [])


class FetchBundleEdgesTests(unittest.TestCase):
	def test_groups_by_graph_id(self):
		cursor = FakeCursor(fetch_results=[[
			(5, "e1", "n1", "n2", "m1", 9.5, 10.0, 0.1, 2),
			(6, "e2", "n3", "n4", "m2", 19.5, 20.0, 0.2, 3),
		]])
		result = gc.fetch_bundle_edges(cursor, "model_test", [5, 6])
		self.assertEqual(result, {
			5: [{
				"id": "e1", "from_nexus_id": "n1", "to_nexus_id": "n2", "mainstem_id": "m1",
				"length": 9.5, "effective_length": 10.0, "segment_gradient": 0.1, "strahler_order": 2,
			}],
			6: [{
				"id": "e2", "from_nexus_id": "n3", "to_nexus_id": "n4", "mainstem_id": "m2",
				"length": 19.5, "effective_length": 20.0, "segment_gradient": 0.2, "strahler_order": 3,
			}],
		})
		sql, params = cursor.executed[0]
		self.assertIn("WHERE graph_id = ANY(%s)", sql)
		self.assertEqual(params, ([5, 6],))


class FetchBundleBarriersTests(unittest.TestCase):
	def test_groups_by_graph_id(self):
		cursor = FakeCursor(fetch_results=[[(5, "b1", "e1", "e0", {"es_rear": 0}, "natural")]])
		result = gc.fetch_bundle_barriers(cursor, "model_test", [5, 6])
		self.assertEqual(result, {
			5: [{
				"id": "b1", "edge_id": "e1", "upstream_edge_id": "e0",
				"species_passability_value": {"es_rear": 0}, "structure_type": "natural",
			}],
		})
		sql, params = cursor.executed[0]
		self.assertIn("s.upstream_edge_id", sql)
		self.assertIn("WHERE e.graph_id = ANY(%s)", sql)
		self.assertEqual(params, ([5, 6],))


class FetchBundleHabitatUpdatesTests(unittest.TestCase):
	def test_orders_by_update_date_and_groups(self):
		cursor = FakeCursor(fetch_results=[[]])
		gc.fetch_bundle_habitat_updates(cursor, "model_test", [5, 6])
		sql, params = cursor.executed[0]
		self.assertIn("ORDER BY hu.update_date ASC NULLS FIRST", sql)
		self.assertEqual(params, ([5, 6], [5, 6]))

	def test_row_spanning_two_graph_ids_attached_to_both(self):
		cursor = FakeCursor(fetch_results=[[
			(5, 6, "h1", "es_rear", "point", "eu1", "ed1", None),
		]])
		result = gc.fetch_bundle_habitat_updates(cursor, "model_test", [5, 6])
		expected_update = {
			"id": "h1", "species_lifestage": "es_rear", "location_type": "point",
			"upstream_snapped_edge_id": "eu1", "downstream_snapped_edge_id": "ed1",
		}
		self.assertEqual(result, {5: [expected_update], 6: [expected_update]})

	def test_preserves_relative_order_within_each_graph_id(self):
		cursor = FakeCursor(fetch_results=[[
			(5, None, "h-first", "es_rear", "point", "eu1", None, "2020-01-01"),
			(5, None, "h-second", "es_rear", "point", "eu1", None, "2020-06-01"),
		]])
		result = gc.fetch_bundle_habitat_updates(cursor, "model_test", [5])
		self.assertEqual([u["id"] for u in result[5]], ["h-first", "h-second"])


class BuildStatsWriteRowsTests(unittest.TestCase):
	def test_builds_json_measure_id_tuples(self):
		species_stats = {"e1": {"es": {"accessibility": "connected"}}}
		route_measures = {"e1": (3.0, 8.0)}
		rows = gc.build_stats_write_rows(species_stats, route_measures)
		self.assertEqual(len(rows), 1)
		species_json, downstream_measure, upstream_measure, edge_id = rows[0]
		self.assertIn('"accessibility": "connected"', species_json)
		self.assertEqual(downstream_measure, 3.0)
		self.assertEqual(upstream_measure, 8.0)
		self.assertEqual(edge_id, "e1")

	def test_missing_route_measure_writes_none(self):
		species_stats = {"e1": {"es": {"accessibility": "connected"}}}
		rows = gc.build_stats_write_rows(species_stats, {})
		_species_json, downstream_measure, upstream_measure, _edge_id = rows[0]
		self.assertIsNone(downstream_measure)
		self.assertIsNone(upstream_measure)


class FlushStatsWritesTests(unittest.TestCase):
	def test_executes_single_statement_when_rows_present(self):
		cursor = FakeCursor()
		gc.flush_stats_writes(cursor, "model_test", [("{}", 0.0, 3.0, "e1")])
		self.assertEqual(len(cursor.executed), 1)
		sql, params = cursor.executed[0]
		self.assertIn("SET species_stats", sql)
		self.assertIn("downstream_route_measure", sql)
		self.assertIn("upstream_route_measure", sql)
		self.assertIn("UNNEST", sql)
		self.assertEqual(params, (["{}"], [0.0], [3.0], ["e1"]))

	def test_arrays_stay_column_aligned_across_multiple_rows(self):
		cursor = FakeCursor()
		gc.flush_stats_writes(cursor, "model_test", [
			('{"s": 1}', 0.0, 10.0, "e1"),
			('{"s": 2}', 10.0, 30.0, "e2"),
		])
		self.assertEqual(len(cursor.executed), 1)
		_sql, params = cursor.executed[0]
		self.assertEqual(params, (
			['{"s": 1}', '{"s": 2}'],
			[0.0, 10.0],
			[10.0, 30.0],
			["e1", "e2"],
		))

	def test_skips_when_no_rows(self):
		cursor = FakeCursor()
		gc.flush_stats_writes(cursor, "model_test", [])
		self.assertEqual(cursor.executed, [])
		self.assertEqual(cursor.executemany_calls, [])


class AssembleEdgeJsonTests(unittest.TestCase):
	def test_species_stats_shape(self):
		edge_ids = ["E1"]
		reporting = [("es", "rear"), ("es", "spawnrear")]
		accessibility = {"es": {"spawn": {"E1": "naturally_accessible"}, "rear": {"E1": "naturally_inaccessible"}}}
		barrier_stats = {"es": {
			"upstream_anthro_spawnrear_count": {"E1": 0}, "downstream_anthro_spawnrear_count": {"E1": 0},
			"upstream_anthro_spawn_count": {"E1": 0}, "upstream_anthro_rear_count": {"E1": 0},
			"downstream_anthro_spawn_count": {"E1": 0}, "downstream_anthro_rear_count": {"E1": 0},
			"upstream_natural_spawnrear_count": {"E1": 0}, "downstream_natural_spawnrear_count": {"E1": 0},
			"upstream_natural_spawn_count": {"E1": 0}, "upstream_natural_rear_count": {"E1": 0},
			"downstream_natural_spawn_count": {"E1": 0}, "downstream_natural_rear_count": {"E1": 0},
			"upstream_anthro_spawn_ids": {"E1": []}, "upstream_anthro_rear_ids": {"E1": []},
			"downstream_anthro_spawn_ids": {"E1": []}, "downstream_anthro_rear_ids": {"E1": []},
		}}
		habitat = {"es": {"rear": {"E1": True}, "spawn": {"E1": False}, "spawnrear": {"E1": True}}}
		species_length_stats = {"es": {
			"rear_weighted_length": {"E1": 3.5},
			"rear_weighted_connected_length": {"E1": 1.5},
			"rear_weighted_disconnected_length": {"E1": 2.0},
		}}

		species_stats = gc.assemble_edge_json(edge_ids, reporting, accessibility, barrier_stats, habitat, species_length_stats)
		self.assertEqual(species_stats["E1"]["es"]["spawn_accessibility"], "naturally_accessible")
		self.assertEqual(species_stats["E1"]["es"]["rear_accessibility"], "naturally_inaccessible")
		self.assertTrue(species_stats["E1"]["es"]["rear_habitat"])
		self.assertNotIn("rear_upstream_length", species_stats["E1"]["es"])
		self.assertNotIn("spawn_upstream_accessible_length", species_stats["E1"]["es"])
		self.assertNotIn("rear_upstream_accessible_length", species_stats["E1"]["es"])
		# "rear" was requested (reporting includes ("es", "rear")), so its weighted length fields are written...
		self.assertEqual(species_stats["E1"]["es"]["rear_weighted_length"], 3.5)
		self.assertEqual(species_stats["E1"]["es"]["rear_weighted_connected_length"], 1.5)
		self.assertEqual(species_stats["E1"]["es"]["rear_weighted_disconnected_length"], 2.0)
		# ...but "spawn" was never requested, so no spawn_weighted_length is written (and
		# species_length_stats has no such key to read for it, unlike the always-present habitat fields).
		self.assertNotIn("spawn_weighted_length", species_stats["E1"]["es"])


class ProcessComponentEndToEndTests(unittest.TestCase):
	"""Confluence network: E1/E2 -> E3 -> E4 (outlet)."""

	def _edges(self):
		fields = ("id", "from_nexus_id", "to_nexus_id", "mainstem_id", "length", "effective_length", "segment_gradient", "strahler_order")
		rows = [
			("E1", "N1", "N3", "M1", 10.0, 10.0, 1.0, 1),
			("E2", "N2", "N3", "M2", 20.0, 20.0, 1.0, 1),
			("E3", "N3", "N4", "M1", 5.0, 5.0, 1.0, 2),
			("E4", "N4", "N5", "M1", 3.0, 3.0, 1.0, 2),
		]
		return [dict(zip(fields, row)) for row in rows]

	def _plan_and_species_params(self):
		plan = {
			"reporting_species_lifecycles": [("es", "rear")],
			"impassable_threshold": 1.0,
		}
		species_params = {"es": {
			"rear_gradient_min": 0.0, "rear_gradient_max": 5.0,
			"strahler_order_rearing_min": 1, "strahler_order_rearing_max": 6,
		}}
		return plan, species_params

	def test_runs_without_error_and_returns_stats(self):
		plan, species_params = self._plan_and_species_params()
		species_stats, barrier_rows, route_measures = gc.process_component(
			1, self._edges(), [], [], plan, species_params,
		)

		self.assertEqual(barrier_rows, [])
		self.assertEqual(set(species_stats.keys()), {"E1", "E2", "E3", "E4"})
		self.assertEqual(route_measures["E4"], (0.0, 3.0))
		self.assertEqual(route_measures["E3"], (3.0, 8.0))
		self.assertEqual(route_measures["E1"], (8.0, 18.0))
		self.assertEqual(route_measures["E2"], (0.0, 20.0))  # tributary mainstem, resets at its own mouth

	def test_natural_spawn_barrier_blocks_upstream_spawn_accessibility_only(self):
		plan, species_params = self._plan_and_species_params()
		# edge_id="E3" sits at the E1/E2 confluence, so upstream_edge_id is ambiguous/None here
		# (see network_break.py's marker-attachment convention).
		barriers = [{
			"id": "b1", "edge_id": "E3", "upstream_edge_id": None,
			"species_passability_value": {"es_rear": 1, "es_spawn": 0}, "structure_type": "natural",
		}]

		species_stats, barrier_rows, _route_measures = gc.process_component(
			1, self._edges(), barriers, [], plan, species_params,
		)

		self.assertEqual(len(barrier_rows), 1)
		self.assertEqual(barrier_rows[0]["id"], "b1")
		self.assertIn("es", barrier_rows[0]["stats"])
		self.assertIn("rear_upstream_length", barrier_rows[0]["stats"]["es"])
		self.assertIn("spawn_upstream_accessible_length", barrier_rows[0]["stats"]["es"])
		self.assertIn("rear_upstream_accessible_length", barrier_rows[0]["stats"]["es"])
		# spawn-impassable natural barrier flips spawn_accessibility upstream of it...
		self.assertEqual(species_stats["E1"]["es"]["spawn_accessibility"], "naturally_inaccessible")
		# ...but rear_accessibility is unaffected, since this barrier is fully passable for rear
		self.assertEqual(species_stats["E1"]["es"]["rear_accessibility"], "naturally_accessible")

	def test_natural_rear_barrier_blocks_upstream_rear_accessibility_only(self):
		plan, species_params = self._plan_and_species_params()
		barriers = [{"id": "b1", "edge_id": "E3", "species_passability_value": {"es_rear": 0, "es_spawn": 1}, "structure_type": "natural"}]

		species_stats, _barrier_rows, _route_measures = gc.process_component(
			1, self._edges(), barriers, [], plan, species_params,
		)

		# rear-impassable natural barrier flips rear_accessibility upstream of it...
		self.assertEqual(species_stats["E1"]["es"]["rear_accessibility"], "naturally_inaccessible")
		# ...but spawn_accessibility is unaffected, since this barrier is fully passable for spawn
		self.assertEqual(species_stats["E1"]["es"]["spawn_accessibility"], "naturally_accessible")


class BranchingNetworkGradientOrderAndBarrierTests(unittest.TestCase):
	"""Branching network: (E1,E2)->E5, (E3,E4)->E6, (E5,E6)->E7->E8->E9 (outlet). E1/E2 are order-1
	tributary pairs, E5/E6 order-2, E7/E8/E9 an order-3 mainstem chain. E1's gradient is too steep
	for both rear and spawn habitat; E2's is too steep for rear only. Two anthropogenic dams: B1 at
	the E7/E8 boundary (edge_id="E8", upstream_edge_id="E7"), B2 at the E8/E9 boundary
	(edge_id="E9", upstream_edge_id="E8"). Exercises gradient-driven habitat exclusion, order 1/2
	downweighting, and downstream-first-barrier degradation together in one component."""

	def _edges(self):
		fields = ("id", "from_nexus_id", "to_nexus_id", "mainstem_id", "length", "effective_length", "segment_gradient", "strahler_order")
		rows = [
			("E1", "N1", "N7", "M1", 100.0, 100.0, 9.0, 1),
			("E2", "N2", "N7", "M1", 100.0, 100.0, 6.0, 1),
			("E3", "N3", "N8", "M1", 100.0, 100.0, 1.0, 1),
			("E4", "N4", "N8", "M1", 100.0, 100.0, 1.0, 1),
			("E5", "N7", "N10", "M1", 100.0, 100.0, 1.0, 2),
			("E6", "N8", "N10", "M1", 100.0, 100.0, 1.0, 2),
			("E7", "N10", "N11", "M1", 100.0, 100.0, 1.0, 3),
			("E8", "N11", "N12", "M1", 100.0, 100.0, 1.0, 3),
			("E9", "N12", "N13", "M1", 100.0, 100.0, 1.0, 3),
		]
		return [dict(zip(fields, row)) for row in rows]

	def _barriers(self):
		return [
			{
				"id": "b1", "edge_id": "E8", "upstream_edge_id": "E7",
				"species_passability_value": {"es_spawn": 0.5, "es_rear": 0}, "structure_type": "anthropogenic",
			},
			{
				"id": "b2", "edge_id": "E9", "upstream_edge_id": "E8",
				"species_passability_value": {"es_spawn": 0.5, "es_rear": 0.25}, "structure_type": "anthropogenic",
			},
		]

	def _plan_and_species_params(self):
		plan = {
			"reporting_species_lifecycles": [("es", "rear"), ("es", "spawn"), ("es", "spawnrear")],
			"impassable_threshold": 1.0,
		}
		species_params = {"es": {
			"rear_gradient_min": 0.0, "rear_gradient_max": 5.0,
			"spawn_gradient_min": 0.0, "spawn_gradient_max": 8.0,
			"strahler_order_rearing_min": 1, "strahler_order_rearing_max": 10,
			"strahler_order_spawning_min": 1, "strahler_order_spawning_max": 10,
			"stream_order_1_rearing_weight": 0.5, "stream_order_1_spawning_weight": 0.25,
			"stream_order_2_rearing_weight": 0.75, "stream_order_2_spawning_weight": 0.25,
		}}
		return plan, species_params

	def setUp(self):
		plan, species_params = self._plan_and_species_params()
		self.species_stats, barrier_rows, _route_measures = gc.process_component(
			1, self._edges(), self._barriers(), [], plan, species_params,
		)
		self.barrier_rows_by_id = {b["id"]: b for b in barrier_rows}

	def test_no_natural_barriers_so_every_edge_is_naturally_accessible(self):
		for eid in ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9"):
			self.assertEqual(self.species_stats[eid]["es"]["spawn_accessibility"], "naturally_accessible")
			self.assertEqual(self.species_stats[eid]["es"]["rear_accessibility"], "naturally_accessible")

	def test_gradient_drives_habitat_exclusions(self):
		expected = {
			"E1": (False, False, False),
			"E2": (False, True, True),
			"E3": (True, True, True),
			"E4": (True, True, True),
			"E5": (True, True, True),
			"E6": (True, True, True),
			"E7": (True, True, True),
			"E8": (True, True, True),
			"E9": (True, True, True),
		}
		for eid, (rear, spawn, spawnrear) in expected.items():
			stats = self.species_stats[eid]["es"]
			self.assertEqual(stats["rear_habitat"], rear, eid)
			self.assertEqual(stats["spawn_habitat"], spawn, eid)
			self.assertEqual(stats["spawnrear_habitat"], spawnrear, eid)

	def test_weighted_length_reflects_only_order_downweight_not_barrier_degradation(self):
		# <lc>_weighted_length is now the base quantity -- effective_length * strahler weight,
		# habitat/accessibility masked -- with no barrier degradation at all.
		expected = {
			"E1": (0.0, 0.0),
			"E2": (0.0, 25.0),
			"E3": (50.0, 25.0),
			"E4": (50.0, 25.0),
			"E5": (75.0, 25.0),
			"E6": (75.0, 25.0),
			"E7": (100.0, 100.0),
			"E8": (100.0, 100.0),
			"E9": (100.0, 100.0),
		}
		for eid, (rear_weighted, spawn_weighted) in expected.items():
			stats = self.species_stats[eid]["es"]
			self.assertEqual(stats["rear_weighted_length"], rear_weighted, eid)
			self.assertEqual(stats["spawn_weighted_length"], spawn_weighted, eid)

	def test_weighted_connected_disconnected_length_reflects_order_downweight_and_nearest_barrier(self):
		# <lc>_weighted_connected_length/<lc>_weighted_disconnected_length split the base value
		# (from the table above) by the passability of the nearest not-fully-passable downstream
		# anthropogenic barrier -- this reproduces the pre-change "weighted_length" numbers for the
		# connected half.
		expected_rear = {
			"E1": (0.0, 0.0), "E2": (0.0, 0.0), "E3": (0.0, 50.0), "E4": (0.0, 50.0),
			"E5": (0.0, 75.0), "E6": (0.0, 75.0), "E7": (0.0, 100.0), "E8": (25.0, 75.0), "E9": (100.0, 0.0),
		}
		expected_spawn = {
			"E1": (0.0, 0.0), "E2": (12.5, 12.5), "E3": (12.5, 12.5), "E4": (12.5, 12.5),
			"E5": (12.5, 12.5), "E6": (12.5, 12.5), "E7": (50.0, 50.0), "E8": (50.0, 50.0), "E9": (100.0, 0.0),
		}
		for eid, (connected, disconnected) in expected_rear.items():
			stats = self.species_stats[eid]["es"]
			self.assertAlmostEqual(stats["rear_weighted_connected_length"], connected, msg=eid)
			self.assertAlmostEqual(stats["rear_weighted_disconnected_length"], disconnected, msg=eid)
		for eid, (connected, disconnected) in expected_spawn.items():
			stats = self.species_stats[eid]["es"]
			self.assertAlmostEqual(stats["spawn_weighted_connected_length"], connected, msg=eid)
			self.assertAlmostEqual(stats["spawn_weighted_disconnected_length"], disconnected, msg=eid)

	def test_barrier_b1_stats_read_at_its_upstream_edge_e7(self):
		stats = self.barrier_rows_by_id["b1"]["stats"]["es"]

		self.assertEqual(stats["upstream_anthro_spawn_count"], 0)
		self.assertEqual(stats["upstream_anthro_rear_count"], 0)
		self.assertEqual(stats["upstream_anthro_spawnrear_count"], 0)
		self.assertEqual(stats["downstream_anthro_spawn_count"], 1)
		self.assertEqual(stats["downstream_anthro_rear_count"], 1)
		self.assertEqual(stats["downstream_anthro_spawnrear_count"], 1)
		self.assertEqual(stats["upstream_natural_spawn_count"], 0)
		self.assertEqual(stats["upstream_natural_rear_count"], 0)
		self.assertEqual(stats["upstream_natural_spawnrear_count"], 0)
		self.assertEqual(stats["downstream_natural_spawn_count"], 0)
		self.assertEqual(stats["downstream_natural_rear_count"], 0)
		self.assertEqual(stats["downstream_natural_spawnrear_count"], 0)
		self.assertEqual(stats["downstream_anthro_spawn_ids"], ["b2"])
		self.assertEqual(stats["downstream_anthro_rear_ids"], ["b2"])
		self.assertEqual(stats["downstream_natural_spawn_ids"], [])
		self.assertEqual(stats["downstream_natural_rear_ids"], [])

		self.assertEqual(stats["spawn_upstream_accessible_length"], 700.0)
		self.assertEqual(stats["rear_upstream_accessible_length"], 700.0)
		self.assertEqual(stats["rear_upstream_length"], 500.0)
		self.assertEqual(stats["rear_functional_upstream_length"], 500.0)
		self.assertEqual(stats["spawn_upstream_length"], 600.0)
		self.assertEqual(stats["spawn_functional_upstream_length"], 600.0)
		self.assertEqual(stats["spawnrear_upstream_length"], 600.0)
		self.assertEqual(stats["spawnrear_functional_upstream_length"], 600.0)

		# b1's own passability: rear=0, spawn=0.5, spawnrear=min(0,0.5)=0. Base weighted length
		# summed over E1..E7 (upstream_edge_id=E7, no anthro barrier sits within that range so the
		# functional/reset sum equals the plain sum here): rear base sum = 350, spawn base sum = 225,
		# spawnrear base sum (per-edge max of rear/spawn base) = 375.
		self.assertAlmostEqual(stats["rear_weighted_connected_upstream_length"], 350.0 * 0.0)
		self.assertAlmostEqual(stats["rear_weighted_disconnected_upstream_length"], 350.0 * 1.0)
		self.assertAlmostEqual(stats["rear_functional_weighted_connected_upstream_length"], 350.0 * 0.0)
		self.assertAlmostEqual(stats["rear_functional_weighted_disconnected_upstream_length"], 350.0 * 1.0)
		self.assertAlmostEqual(stats["spawn_weighted_connected_upstream_length"], 225.0 * 0.5)
		self.assertAlmostEqual(stats["spawn_weighted_disconnected_upstream_length"], 225.0 * 0.5)
		self.assertAlmostEqual(stats["spawn_functional_weighted_connected_upstream_length"], 225.0 * 0.5)
		self.assertAlmostEqual(stats["spawn_functional_weighted_disconnected_upstream_length"], 225.0 * 0.5)
		self.assertAlmostEqual(stats["spawnrear_weighted_connected_upstream_length"], 375.0 * 0.0)
		self.assertAlmostEqual(stats["spawnrear_weighted_disconnected_upstream_length"], 375.0 * 1.0)
		self.assertAlmostEqual(stats["spawnrear_functional_weighted_connected_upstream_length"], 375.0 * 0.0)
		self.assertAlmostEqual(stats["spawnrear_functional_weighted_disconnected_upstream_length"], 375.0 * 1.0)

	def test_barrier_b2_stats_read_at_its_upstream_edge_e8(self):
		stats = self.barrier_rows_by_id["b2"]["stats"]["es"]

		self.assertEqual(stats["upstream_anthro_spawn_count"], 1)
		self.assertEqual(stats["upstream_anthro_rear_count"], 1)
		self.assertEqual(stats["upstream_anthro_spawnrear_count"], 1)
		self.assertEqual(stats["downstream_anthro_spawn_count"], 0)
		self.assertEqual(stats["downstream_anthro_rear_count"], 0)
		self.assertEqual(stats["downstream_anthro_spawnrear_count"], 0)
		self.assertEqual(stats["upstream_natural_spawn_count"], 0)
		self.assertEqual(stats["upstream_natural_rear_count"], 0)
		self.assertEqual(stats["upstream_natural_spawnrear_count"], 0)
		self.assertEqual(stats["downstream_natural_spawn_count"], 0)
		self.assertEqual(stats["downstream_natural_rear_count"], 0)
		self.assertEqual(stats["downstream_natural_spawnrear_count"], 0)
		self.assertEqual(stats["downstream_anthro_spawn_ids"], [])
		self.assertEqual(stats["downstream_anthro_rear_ids"], [])
		self.assertEqual(stats["downstream_natural_spawn_ids"], [])
		self.assertEqual(stats["downstream_natural_rear_ids"], [])

		self.assertEqual(stats["spawn_upstream_accessible_length"], 800.0)
		self.assertEqual(stats["rear_upstream_accessible_length"], 800.0)
		self.assertEqual(stats["rear_upstream_length"], 600.0)
		self.assertEqual(stats["rear_functional_upstream_length"], 100.0)
		self.assertEqual(stats["spawn_upstream_length"], 700.0)
		self.assertEqual(stats["spawn_functional_upstream_length"], 100.0)
		self.assertEqual(stats["spawnrear_upstream_length"], 700.0)
		self.assertEqual(stats["spawnrear_functional_upstream_length"], 100.0)

		# b2's own passability: rear=0.25, spawn=0.5, spawnrear=min(0.5,0.25)=0.25. Base weighted
		# length summed over E1..E8 (upstream_edge_id=E8): rear base sum = 450, spawn base sum = 325,
		# spawnrear base sum = 475. b1 sits at E8 (an anthro barrier "at" E8's own start), so the
		# functional/reset sum resets there -- it's just E8's own base local value: rear=100,
		# spawn=100, spawnrear=max(100,100)=100.
		self.assertAlmostEqual(stats["rear_weighted_connected_upstream_length"], 450.0 * 0.25)
		self.assertAlmostEqual(stats["rear_weighted_disconnected_upstream_length"], 450.0 * 0.75)
		self.assertAlmostEqual(stats["rear_functional_weighted_connected_upstream_length"], 100.0 * 0.25)
		self.assertAlmostEqual(stats["rear_functional_weighted_disconnected_upstream_length"], 100.0 * 0.75)
		self.assertAlmostEqual(stats["spawn_weighted_connected_upstream_length"], 325.0 * 0.5)
		self.assertAlmostEqual(stats["spawn_weighted_disconnected_upstream_length"], 325.0 * 0.5)
		self.assertAlmostEqual(stats["spawn_functional_weighted_connected_upstream_length"], 100.0 * 0.5)
		self.assertAlmostEqual(stats["spawn_functional_weighted_disconnected_upstream_length"], 100.0 * 0.5)
		self.assertAlmostEqual(stats["spawnrear_weighted_connected_upstream_length"], 475.0 * 0.25)
		self.assertAlmostEqual(stats["spawnrear_weighted_disconnected_upstream_length"], 475.0 * 0.75)
		self.assertAlmostEqual(stats["spawnrear_functional_weighted_connected_upstream_length"], 100.0 * 0.25)
		self.assertAlmostEqual(stats["spawnrear_functional_weighted_disconnected_upstream_length"], 100.0 * 0.75)


if __name__ == "__main__":
	unittest.main()
