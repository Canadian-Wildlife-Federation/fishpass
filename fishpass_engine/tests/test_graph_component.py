"""Tests for fishpass_engine/scripts/graph_component.py: SQL-shape checks against a stubbed
cursor, plus a full synthetic end-to-end run of process_component on the confluence test network
(no database).

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


class FetchGraphIdsTests(unittest.TestCase):
	def test_excludes_isolated(self):
		cursor = FakeCursor(fetch_results=[[(1,), (2,)]])
		result = gc.fetch_graph_ids(cursor, "model_test")
		self.assertEqual(result, [1, 2])
		sql, _ = cursor.executed[0]
		self.assertIn("is_isolated IS NULL OR is_isolated = false", sql)
		self.assertIn("graph_id IS NOT NULL", sql)


class FetchComponentEdgesTests(unittest.TestCase):
	def test_maps_fields(self):
		cursor = FakeCursor(fetch_results=[[("e1", "n1", "n2", "m1", 10.0, 0.1, 2)]])
		result = gc.fetch_component_edges(cursor, "model_test", 5)
		self.assertEqual(result, [{
			"id": "e1", "from_nexus_id": "n1", "to_nexus_id": "n2", "mainstem_id": "m1",
			"effective_length": 10.0, "segment_gradient": 0.1, "strahler_order": 2,
		}])
		sql, params = cursor.executed[0]
		self.assertIn("WHERE graph_id = %s", sql)
		self.assertEqual(params, (5,))


class FetchComponentBarriersTests(unittest.TestCase):
	def test_maps_fields(self):
		cursor = FakeCursor(fetch_results=[[("b1", "e1", {"es_rear": 0}, "natural")]])
		result = gc.fetch_component_barriers(cursor, "model_test", ["e1"])
		self.assertEqual(result, [{
			"id": "b1", "edge_id": "e1", "species_passability_value": {"es_rear": 0}, "structure_type": "natural",
		}])


class FetchComponentHabitatUpdatesTests(unittest.TestCase):
	def test_orders_by_update_date(self):
		cursor = FakeCursor(fetch_results=[[]])
		gc.fetch_component_habitat_updates(cursor, "model_test", ["e1"])
		sql, params = cursor.executed[0]
		self.assertIn("ORDER BY update_date ASC NULLS FIRST", sql)
		self.assertEqual(params, (["e1"], ["e1"]))


class AssembleEdgeJsonTests(unittest.TestCase):
	def test_species_and_lifecycle_stats_shape(self):
		edge_ids = ["E1"]
		reporting = [("es", "rear")]
		accessibility = {"es": {"E1": "connected_naturally_accessible"}}
		barrier_stats = {"es": {
			"upstream_anthro_count": {"E1": 0}, "downstream_anthro_count": {"E1": 0},
			"upstream_natural_count": {"E1": 0}, "downstream_natural_count": {"E1": 0},
			"upstream_anthro_ids": {"E1": []}, "downstream_anthro_ids": {"E1": []},
		}}
		habitat = {"es": {"rear": {"E1": True}, "spawn": {"E1": False}, "general": {"E1": True}}}
		species_length_stats = {"es": {
			"upstream_accessible_length": {"E1": 10.0},
			"rear_upstream_length": {"E1": 10.0},
			"rear_functional_upstream_length": {"E1": 10.0},
			"rear_weighted_upstream_length": {"E1": 5.0},
			"rear_functional_weighted_upstream_length": {"E1": 5.0},
		}}
		lifecycle_rollups = {"rear": {
			"upstream_length": {"E1": 10.0}, "functional_upstream_length": {"E1": 10.0}, "weighted_upstream_length": {"E1": 5.0},
		}}

		species_stats, lifecycle_stats = gc.assemble_edge_json(
			edge_ids, reporting, accessibility, barrier_stats, habitat, species_length_stats, lifecycle_rollups,
		)
		self.assertEqual(species_stats["E1"]["es"]["accessibility"], "connected_naturally_accessible")
		self.assertTrue(species_stats["E1"]["es"]["rear_habitat"])
		self.assertEqual(species_stats["E1"]["es"]["rear_upstream_length"], 10.0)
		self.assertEqual(lifecycle_stats["E1"]["rear_upstream_length"], 10.0)


class ProcessComponentEndToEndTests(unittest.TestCase):
	"""Confluence network: E1/E2 -> E3 -> E4 (outlet)."""

	def _edge_rows(self):
		return [
			("E1", "N1", "N3", "M1", 10.0, 1.0, 1),
			("E2", "N2", "N3", "M2", 20.0, 1.0, 1),
			("E3", "N3", "N4", "M1", 5.0, 1.0, 2),
			("E4", "N4", "N5", "M1", 3.0, 1.0, 2),
		]

	def test_runs_without_error_and_writes_stats(self):
		cursor = FakeCursor(fetch_results=[
			self._edge_rows(),  # fetch_component_edges
			[],                  # fetch_component_barriers -- no barriers
			[],                  # fetch_component_habitat_updates -- no overrides
		])
		plan = {
			"reporting_species_lifecycles": [("es", "rear")],
			"impassable_threshold": 1.0,
		}
		species_params = {"es": {
			"rear_gradient_min": 0.0, "rear_gradient_max": 5.0,
			"strahler_order_rearing_min": 1, "strahler_order_rearing_max": 6,
		}}

		barrier_rows = gc.process_component(cursor, "model_test", 1, plan, species_params)

		self.assertEqual(barrier_rows, [])
		self.assertEqual(len(cursor.executemany_calls), 1)
		sql, rows = cursor.executemany_calls[0]
		self.assertIn("SET species_stats", sql)
		self.assertEqual(len(rows), 4)  # one row per edge

	def test_barrier_blocks_upstream_accessibility(self):
		cursor = FakeCursor(fetch_results=[
			self._edge_rows(),
			[("b1", "E3", {"es_rear": 0, "es_spawn": 0}, "anthropogenic")],
			[],
		])
		plan = {
			"reporting_species_lifecycles": [("es", "rear")],
			"impassable_threshold": 1.0,
		}
		species_params = {"es": {
			"rear_gradient_min": 0.0, "rear_gradient_max": 5.0,
			"strahler_order_rearing_min": 1, "strahler_order_rearing_max": 6,
		}}

		barrier_rows = gc.process_component(cursor, "model_test", 1, plan, species_params)

		self.assertEqual(len(barrier_rows), 1)
		self.assertEqual(barrier_rows[0]["id"], "b1")
		self.assertIn("es", barrier_rows[0]["stats"])

		_, rows = cursor.executemany_calls[0]
		# find E1's written species_stats json string and confirm it shows disconnected access
		import json
		e1_row = next(r for r in rows if r[2] == "E1")
		e1_stats = json.loads(e1_row[0])
		self.assertEqual(e1_stats["es"]["accessibility"], "disconnected_naturally_accessible")


if __name__ == "__main__":
	unittest.main()
