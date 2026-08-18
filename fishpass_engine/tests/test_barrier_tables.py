"""Tests for fishpass_engine/scripts/barrier_tables.py -- SQL-shape checks against a stubbed
cursor (no database).

Run with: python -m unittest fishpass_engine.tests.test_barrier_tables
"""

import json
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

import barrier_tables as bt  # noqa: E402


class FakeCursor:
	def __init__(self):
		self.executed = []
		self.executemany_calls = []

	def execute(self, sql, params=None):
		self.executed.append((" ".join(sql.split()), params))

	def executemany(self, sql, params_seq=None):
		self.executemany_calls.append((" ".join(sql.split()), list(params_seq or [])))


class WriteBarrierStatTablesTests(unittest.TestCase):
	def test_no_rows_is_a_noop(self):
		cursor = FakeCursor()
		bt.write_barrier_stat_tables(cursor, "model_test", [])
		self.assertEqual(cursor.executemany_calls, [])

	def test_updates_species_stats_on_all_structures(self):
		cursor = FakeCursor()
		rows = [{"id": "b1", "stats": {"es": {"upstream_natural_count": 0}}}]
		bt.write_barrier_stat_tables(cursor, "model_test", rows)
		self.assertEqual(len(cursor.executemany_calls), 1)
		sql, params = cursor.executemany_calls[0]
		self.assertIn("UPDATE \"model_test\".all_structures", sql)
		self.assertIn("SET species_stats = v.species_stats::jsonb", sql)
		self.assertEqual(params, [(json.dumps({"es": {"upstream_natural_count": 0}}, default=str), "b1")])


class CreateBarrierViewsTests(unittest.TestCase):
	def test_creates_natural_and_anthropogenic_views(self):
		cursor = FakeCursor()
		bt.create_barrier_views(cursor, "model_test")
		self.assertEqual(len(cursor.executed), 2)
		sql_natural, _ = cursor.executed[0]
		sql_anthro, _ = cursor.executed[1]
		self.assertIn("CREATE VIEW \"model_test\".natural_barriers", sql_natural)
		self.assertIn("WHERE structure_type = 'natural' AND species_stats IS NOT NULL", sql_natural)
		self.assertIn("CREATE VIEW \"model_test\".anthropogenic_barriers", sql_anthro)
		self.assertIn("WHERE structure_type = 'anthropogenic' AND species_stats IS NOT NULL", sql_anthro)


class CreateAndPopulateFeatureTypeTablesTests(unittest.TestCase):
	def test_skips_gradient_barriers(self):
		cursor = FakeCursor()
		bt.create_and_populate_feature_type_tables(cursor, "model_test", ["dams", "gradients"], 4617)
		executed_sql = " ".join(sql for sql, _ in cursor.executed)
		self.assertIn("cabd_dams", executed_sql)
		self.assertNotIn("CREATE TABLE \"model_test\".\"cabd_gradients\"", executed_sql)

	def test_rejects_unsafe_feature_type_name(self):
		cursor = FakeCursor()
		with self.assertRaises(SystemExit):
			bt.create_and_populate_feature_type_tables(cursor, "model_test", ["dams; DROP TABLE x"], 4617)

	def test_creates_and_copies_for_each_type(self):
		cursor = FakeCursor()
		bt.create_and_populate_feature_type_tables(cursor, "model_test", ["dams", "waterfalls"], 4617)
		create_statements = [sql for sql, _ in cursor.executed if sql.startswith("CREATE TABLE")]
		self.assertEqual(len(create_statements), 2)
		self.assertIn("CREATE TABLE \"model_test\".\"cabd_dams\"", create_statements[0])
		self.assertIn("CREATE TABLE \"model_test\".\"cabd_waterfalls\"", create_statements[1])
		insert_statements = [(sql, params) for sql, params in cursor.executed if sql.startswith("INSERT INTO")]
		self.assertEqual(len(insert_statements), 2)
		self.assertIn("INSERT INTO \"model_test\".\"cabd_dams\"", insert_statements[0][0])
		self.assertEqual(insert_statements[0][1], ("dams",))


class CreateAndPopulateGradientBarriersCacheTests(unittest.TestCase):
	def test_filters_by_source(self):
		cursor = FakeCursor()
		bt.create_and_populate_gradient_barriers_cache(cursor, "model_test", 4617)
		insert_sql, _ = [e for e in cursor.executed if e[0].startswith("INSERT INTO")][0]
		self.assertIn("WHERE source = 'gradient_barriers'", insert_sql)


if __name__ == "__main__":
	unittest.main()
