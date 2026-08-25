"""Tests for fishpass_engine/scripts/load_stream_network.py against a stubbed
psycopg-less cursor -- checks the SQL/params shape (aoi resolution, filtering, schema
identifier quoting) without a real database.

Run with: python -m unittest fishpass_engine.tests.test_load_stream_network
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

import load_stream_network as lsn  # noqa: E402


class FakeCursor:
	def __init__(self, fetch_results=None, fetchone_results=None):
		self.executed = []
		self._fetch_results = list(fetch_results or [])
		self._fetchone_results = list(fetchone_results or [])
		self.rowcount = 0

	def execute(self, sql, params=None):
		self.executed.append((" ".join(sql.split()), params))
		self.rowcount = 1

	def fetchall(self):
		return self._fetch_results.pop(0) if self._fetch_results else []

	def fetchone(self):
		return self._fetchone_results.pop(0) if self._fetchone_results else None


class ResolveWorkunitAoiIdsTests(unittest.TestCase):
	def test_resolves_in_requested_order(self):
		cursor = FakeCursor(fetch_results=[[("id-b", "B"), ("id-a", "A")]])
		result = lsn.resolve_workunit_aoi_ids(cursor, ["A", "B"])
		self.assertEqual(result, ["id-a", "id-b"])

	def test_missing_short_name_exits(self):
		cursor = FakeCursor(fetch_results=[[("id-a", "A")]])
		with self.assertRaises(SystemExit):
			lsn.resolve_workunit_aoi_ids(cursor, ["A", "B"])


class ResolveProvinceAoiIdsTests(unittest.TestCase):
	def test_returns_ids(self):
		cursor = FakeCursor(fetch_results=[[("id-a",), ("id-b",)]])
		result = lsn.resolve_province_aoi_ids(cursor, ["ns"])
		self.assertEqual(result, ["id-a", "id-b"])
		sql, params = cursor.executed[0]
		self.assertIn("province_territory_code &&", sql)
		self.assertEqual(params, (["ns"],))

	def test_no_matches_exits(self):
		cursor = FakeCursor(fetch_results=[[]])
		with self.assertRaises(SystemExit):
			lsn.resolve_province_aoi_ids(cursor, ["zz"])


class ResolveAoiIdsTests(unittest.TestCase):
	def test_workunit_all_returns_none(self):
		cursor = FakeCursor()
		self.assertIsNone(lsn.resolve_aoi_ids(cursor, "workunit", "all"))

	def test_unsupported_kind_raises(self):
		cursor = FakeCursor()
		with self.assertRaises(ValueError):
			lsn.resolve_aoi_ids(cursor, "upstream_of", ["x"])


class CopyStreamsTests(unittest.TestCase):
	def test_filters_by_aoi_ids_when_given(self):
		cursor = FakeCursor()
		lsn.copy_streams(cursor, "model_test", ["id-a", "id-b"])
		sql, params = cursor.executed[0]
		self.assertIn('INSERT INTO "model_test".streams', sql)
		self.assertIn("WHERE aoi_id = ANY(%s)", sql)
		self.assertEqual(params, (["id-a", "id-b"],))

	def test_no_filter_when_aoi_ids_none(self):
		cursor = FakeCursor()
		lsn.copy_streams(cursor, "model_test", None)
		sql, params = cursor.executed[0]
		self.assertNotIn("WHERE", sql)
		self.assertIsNone(params)


class SchemaIdentifierQuotingTests(unittest.TestCase):
	def test_init_output_schema_quotes_identifier(self):
		cursor = FakeCursor()
		lsn.init_output_schema(cursor, "model_test")
		drop_sql, _ = cursor.executed[0]
		create_sql, _ = cursor.executed[1]
		self.assertIn('"model_test"', drop_sql)
		self.assertIn('"model_test"', create_sql)


if __name__ == "__main__":
	unittest.main()
