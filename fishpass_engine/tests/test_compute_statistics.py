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
		self.assertIn(f"NULLIF(ST_M(ST_StartPoint(s.geometry)), {cs.NO_DATA})", sql)
		self.assertIn(f"NULLIF(ST_M(ST_EndPoint(s.geometry)), {cs.NO_DATA})", sql)
		self.assertIn("WHEN s.length > 0", sql)
		self.assertIn("/ s.length", sql)
		self.assertIn("SET", sql)
		self.assertIn("segment_gradient = CASE", sql)

	def test_gradient_reads_each_endpoint_once(self):
		# Every reference to s.geometry is a detoast per row -- one per endpoint, no more.
		cursor = FakeCursor()
		cs.compute_effective_length_and_gradient(cursor, "model_test")
		sql, _ = cursor.executed[0]
		self.assertEqual(sql.count("s.geometry"), 2)


class ComputeStatisticsIndexTests(unittest.TestCase):
	"""compute_statistics drops the snapping-only streams indexes before the whole-table
	rewrites (network break, steps 3-4, steps 5-9) and rebuilds them once those are done."""

	def test_indexes_dropped_before_and_rebuilt_after_rewrites(self):
		calls = []

		def record(name, return_value=None):
			def _recorder(*args, **kwargs):
				calls.append(name)
				return return_value

			return _recorder

		plan = {"output_schema": "model_test", "include_gradient_barriers": False}

		with (
			mock.patch.object(cs, "drop_streams_bulk_write_indexes", side_effect=record("drop")),
			mock.patch.object(cs, "break_network", side_effect=record("break", 0)),
			mock.patch.object(cs, "compute_effective_length_and_gradient", side_effect=record("gradient")),
			mock.patch.object(cs, "load_species_params", return_value={}),
			mock.patch.object(cs, "run_component_statistics", side_effect=record("stats", 0)),
			mock.patch.object(cs, "create_streams_bulk_write_indexes", side_effect=record("create")),
		):
			cs.compute_statistics(mock.Mock(), FakeCursor(), plan, 4617)

		self.assertEqual(calls, ["drop", "break", "gradient", "stats", "create"])


class RunComponentStatisticsTests(unittest.TestCase):
	"""Control-flow coverage against mocked graph_component helpers -- process_component's own
	logic is covered in test_graph_component.py; this just checks run_component_statistics wires
	bundling and write-batching together correctly."""

	def _fake_process_component(self, graph_id, edges, barriers, habitat_rows, plan, species_params):
		eid = f"E{graph_id}"
		return [(eid, {})], [], {}

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

		with (
			mock.patch.object(cs, "BUNDLE_EDGE_BUDGET", 15),
			mock.patch.object(cs, "WRITE_BATCH_SIZE", 2),
			mock.patch.object(cs, "fetch_graph_id_counts", return_value=graph_id_counts),
			mock.patch.object(cs, "fetch_bundle_edges", side_effect=fake_fetch_edges),
			mock.patch.object(cs, "fetch_bundle_barriers", side_effect=fake_fetch_empty),
			mock.patch.object(cs, "fetch_bundle_habitat_updates", side_effect=fake_fetch_empty),
			mock.patch.object(cs, "process_component", side_effect=self._fake_process_component),
			mock.patch.object(cs, "flush_stats_writes", side_effect=fake_flush),
		):
			cs.run_component_statistics(cursor, "model_test", plan, species_params)

		# One write row per component (3 total), flushed in batches of WRITE_BATCH_SIZE=2:
		# a mid-loop flush of 2 rows once the threshold is crossed, then a final flush of the
		# 1 remaining row.
		self.assertEqual([len(rows) for rows in flush_calls], [2, 1])

	def test_large_component_writes_are_sliced(self):
		# A single component returning more rows than WRITE_BATCH_SIZE must not go out as one
		# statement -- it's flushed every WRITE_BATCH_SIZE rows as they are produced.
		def fake_process_component(graph_id, edges, barriers, habitat_rows, plan, species_params):
			return [(f"E{i}", {}) for i in range(5)], [], {}

		flush_calls = []

		def fake_flush(cursor, output_schema, rows):
			if rows:  # the trailing flush of an empty remainder is a no-op in the real function
				flush_calls.append(list(rows))

		with (
			mock.patch.object(cs, "WRITE_BATCH_SIZE", 2),
			mock.patch.object(cs, "fetch_graph_id_counts", return_value=[(1, 5)]),
			mock.patch.object(cs, "fetch_bundle_edges", return_value={1: [{"id": "E0"}]}),
			mock.patch.object(cs, "fetch_bundle_barriers", return_value={}),
			mock.patch.object(cs, "fetch_bundle_habitat_updates", return_value={}),
			mock.patch.object(cs, "process_component", side_effect=fake_process_component),
			mock.patch.object(cs, "flush_stats_writes", side_effect=fake_flush),
		):
			cs.run_component_statistics(object(), "model_test", {}, {})

		self.assertEqual([len(rows) for rows in flush_calls], [2, 2, 1])
		self.assertEqual(sum(len(rows) for rows in flush_calls), 5)

	def test_large_component_processed_alone(self):
		cursor = object()
		plan = {}
		species_params = {}
		graph_id_counts = [(1, 500)]

		with (
			mock.patch.object(cs, "BUNDLE_EDGE_BUDGET", 100),
			mock.patch.object(cs, "fetch_graph_id_counts", return_value=graph_id_counts),
			mock.patch.object(cs, "fetch_bundle_edges", return_value={1: [{"id": "E1"}]}) as fetch_edges,
			mock.patch.object(cs, "fetch_bundle_barriers", return_value={}),
			mock.patch.object(cs, "fetch_bundle_habitat_updates", return_value={}),
			mock.patch.object(cs, "process_component", side_effect=self._fake_process_component),
			mock.patch.object(cs, "flush_stats_writes"),
		):
			cs.run_component_statistics(cursor, "model_test", plan, species_params)

		fetch_edges.assert_called_once_with(cursor, "model_test", [1])

	def test_writes_are_flushed_while_a_component_is_still_being_consumed(self):
		# The point of streaming: the first batch reaches the database before the component's
		# later edges have been assembled or serialised.
		events = []

		def fake_process_component(graph_id, edges, barriers, habitat_rows, plan, species_params):
			def edge_stats():
				for i in range(4):
					events.append(f"edge{i}")
					yield f"E{i}", {}

			return edge_stats(), [], {}

		def fake_flush(cursor, output_schema, rows):
			if rows:
				events.append(f"flush{len(rows)}")

		with (
			mock.patch.object(cs, "WRITE_BATCH_SIZE", 2),
			mock.patch.object(cs, "fetch_graph_id_counts", return_value=[(1, 4)]),
			mock.patch.object(cs, "fetch_bundle_edges", return_value={1: [{"id": "E0"}]}),
			mock.patch.object(cs, "fetch_bundle_barriers", return_value={}),
			mock.patch.object(cs, "fetch_bundle_habitat_updates", return_value={}),
			mock.patch.object(cs, "process_component", side_effect=fake_process_component),
			mock.patch.object(cs, "flush_stats_writes", side_effect=fake_flush),
		):
			cs.run_component_statistics(object(), "model_test", {}, {})

		self.assertEqual(events, ["edge0", "edge1", "flush2", "edge2", "edge3", "flush2"])

	def test_writes_are_flushed_on_json_size_as_well_as_row_count(self):
		# Rows carrying long barrier id lists must not pile up to WRITE_BATCH_SIZE of them.
		def fake_process_component(graph_id, edges, barriers, habitat_rows, plan, species_params):
			return [(f"E{i}", {"ids": "x" * 50}) for i in range(3)], [], {}

		flush_calls = []

		def fake_flush(cursor, output_schema, rows):
			if rows:
				flush_calls.append(list(rows))

		with (
			mock.patch.object(cs, "WRITE_BATCH_BYTES", 100),
			mock.patch.object(cs, "fetch_graph_id_counts", return_value=[(1, 3)]),
			mock.patch.object(cs, "fetch_bundle_edges", return_value={1: [{"id": "E0"}]}),
			mock.patch.object(cs, "fetch_bundle_barriers", return_value={}),
			mock.patch.object(cs, "fetch_bundle_habitat_updates", return_value={}),
			mock.patch.object(cs, "process_component", side_effect=fake_process_component),
			mock.patch.object(cs, "flush_stats_writes", side_effect=fake_flush),
		):
			cs.run_component_statistics(object(), "model_test", {}, {})

		self.assertEqual([len(rows) for rows in flush_calls], [2, 1])

	def test_barrier_stats_are_written_per_bundle(self):
		# Bundles are [[1], [2, 3]] (see test_bundles_components_and_batches_writes): each bundle's
		# barrier rows go out before the next bundle is fetched, and the total is returned.
		events = []

		def fake_fetch_edges(cursor, output_schema, graph_ids):
			events.append(("fetch", list(graph_ids)))
			return {gid: [{"id": f"E{gid}"}] for gid in graph_ids}

		def fake_process_component(graph_id, edges, barriers, habitat_rows, plan, species_params):
			return [], [{"id": f"B{graph_id}", "stats": {}}], {}

		def fake_write_barriers(cursor, output_schema, barrier_rows):
			events.append(("write", [b["id"] for b in barrier_rows]))

		with (
			mock.patch.object(cs, "BUNDLE_EDGE_BUDGET", 15),
			mock.patch.object(cs, "fetch_graph_id_counts", return_value=[(1, 10), (2, 8), (3, 4)]),
			mock.patch.object(cs, "fetch_bundle_edges", side_effect=fake_fetch_edges),
			mock.patch.object(cs, "fetch_bundle_barriers", return_value={}),
			mock.patch.object(cs, "fetch_bundle_habitat_updates", return_value={}),
			mock.patch.object(cs, "process_component", side_effect=fake_process_component),
			mock.patch.object(cs, "flush_stats_writes"),
			mock.patch.object(cs, "write_barrier_stat_tables", side_effect=fake_write_barriers),
		):
			barriers_done = cs.run_component_statistics(object(), "model_test", {}, {})

		self.assertEqual(
			events,
			[("fetch", [1]), ("write", ["B1"]), ("fetch", [2, 3]), ("write", ["B2", "B3"])],
		)
		self.assertEqual(barriers_done, 3)


if __name__ == "__main__":
	unittest.main()
