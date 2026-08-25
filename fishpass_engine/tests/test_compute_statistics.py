"""Tests for fishpass_engine/scripts/compute_statistics.py's steps 3-4 SQL shape against a
stubbed cursor (no database).

Run with: python -m unittest fishpass_engine.tests.test_compute_statistics
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

import compute_statistics as cs  # noqa: E402


class FakeCursor:
	def __init__(self):
		self.executed = []

	def execute(self, sql, params=None):
		self.executed.append((" ".join(sql.split()), params))


class ComputeEffectiveLengthAndGradientTests(unittest.TestCase):
	"""compute_effective_length_and_gradient combines steps 3-4 into a single UPDATE, so all
	assertions target the one statement in cursor.executed[0]."""

	def test_single_statement(self):
		cursor = FakeCursor()
		cs.compute_effective_length_and_gradient(cursor, "model_test")
		self.assertEqual(len(cursor.executed), 1)

	def test_null_ecatchment_or_mainstem_keeps_own_length(self):
		cursor = FakeCursor()
		cs.compute_effective_length_and_gradient(cursor, "model_test")
		sql, _ = cursor.executed[0]
		self.assertIn("WHEN s.ecatchment_id IS NULL OR s.mainstem_id IS NULL THEN s.length", sql)

	def test_defaults_everything_else_to_zero_then_restores_best_mainstem(self):
		cursor = FakeCursor()
		cs.compute_effective_length_and_gradient(cursor, "model_test")
		sql, _ = cursor.executed[0]
		self.assertIn("SUM(length) AS total_length", sql)
		self.assertIn("ORDER BY ecatchment_id, total_length DESC, mainstem_id", sql)
		self.assertIn("WHEN s.mainstem_id = r.best_mainstem_id THEN s.length", sql)
		self.assertIn("ELSE 0", sql)

	def test_gradient_query_shape(self):
		cursor = FakeCursor()
		cs.compute_effective_length_and_gradient(cursor, "model_test")
		sql, _ = cursor.executed[0]
		self.assertIn("ST_M(ST_PointN(s.geometry, 1))", sql)
		self.assertIn("ST_M(ST_PointN(s.geometry, ST_NPoints(s.geometry)))", sql)
		self.assertIn(f"!= {cs.NO_DATA}", sql)
		self.assertIn("SET", sql)
		self.assertIn("segment_gradient = CASE", sql)


class RunComponentStatisticsTests(unittest.TestCase):
	"""Control-flow coverage against mocked graph_component helpers -- process_component's own
	logic is covered in test_graph_component.py; this just checks run_component_statistics wires
	bundling and write-batching together correctly."""

	def _fake_process_component(self, graph_id, edges, barriers, habitat_rows, plan, species_params):
		eid = f"E{graph_id}"
		return {eid: {}}, [], {}

	def test_bundles_components_and_batches_writes(self):
		cursor = object()  # never touched directly -- every DB call is mocked out
		plan = {}
		species_params = {}

		# Descending counts, as fetch_graph_id_counts would return. With BUNDLE_EDGE_BUDGET
		# patched to 15, build_graph_id_bundles (real, pure) splits this into [[1], [2, 3]].
		graph_id_counts = [(1, 10), (2, 8), (3, 4)]

		def fake_fetch_edges(cursor, output_schema, graph_ids):
			return {gid: [{"id": f"E{gid}"}] for gid in graph_ids}

		def fake_fetch_empty(cursor, output_schema, graph_ids):
			return {}

		flush_calls = []

		def fake_flush(cursor, output_schema, rows):
			flush_calls.append(list(rows))

		with mock.patch.object(cs, "BUNDLE_EDGE_BUDGET", 15), \
			mock.patch.object(cs, "WRITE_BATCH_SIZE", 2), \
			mock.patch.object(cs, "fetch_graph_id_counts", return_value=graph_id_counts), \
			mock.patch.object(cs, "fetch_bundle_edges", side_effect=fake_fetch_edges), \
			mock.patch.object(cs, "fetch_bundle_barriers", side_effect=fake_fetch_empty), \
			mock.patch.object(cs, "fetch_bundle_habitat_updates", side_effect=fake_fetch_empty), \
			mock.patch.object(cs, "process_component", side_effect=self._fake_process_component), \
			mock.patch.object(cs, "flush_stats_writes", side_effect=fake_flush):
			cs.run_component_statistics(cursor, "model_test", plan, species_params)

		# One write row per component (3 total), flushed in batches of WRITE_BATCH_SIZE=2:
		# a mid-loop flush of 2 rows once the threshold is crossed, then a final flush of the
		# 1 remaining row.
		self.assertEqual([len(rows) for rows in flush_calls], [2, 1])

	def test_large_component_processed_alone(self):
		cursor = object()
		plan = {}
		species_params = {}
		graph_id_counts = [(1, 500)]

		with mock.patch.object(cs, "BUNDLE_EDGE_BUDGET", 100), \
			mock.patch.object(cs, "fetch_graph_id_counts", return_value=graph_id_counts), \
			mock.patch.object(cs, "fetch_bundle_edges", return_value={1: [{"id": "E1"}]}) as fetch_edges, \
			mock.patch.object(cs, "fetch_bundle_barriers", return_value={}), \
			mock.patch.object(cs, "fetch_bundle_habitat_updates", return_value={}), \
			mock.patch.object(cs, "process_component", side_effect=self._fake_process_component), \
			mock.patch.object(cs, "flush_stats_writes"):
			cs.run_component_statistics(cursor, "model_test", plan, species_params)

		fetch_edges.assert_called_once_with(cursor, "model_test", [1])


if __name__ == "__main__":
	unittest.main()
