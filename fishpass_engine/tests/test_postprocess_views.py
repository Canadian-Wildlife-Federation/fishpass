"""Tests for fishpass_engine/scripts/postprocess_views.py -- SQL-shape checks against a stubbed
cursor (no database).

Run with: python -m unittest fishpass_engine.tests.test_postprocess_views
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

import postprocess_views as pv  # noqa: E402


class FakeCursor:
	def __init__(self):
		self.executed = []

	def execute(self, sql, params=None):
		self.executed.append((" ".join(sql.split()), params))


class FakeConn:
	def __init__(self):
		self.commits = 0

	def commit(self):
		self.commits += 1


class CreateNaturalAnthropogenicViewsTests(unittest.TestCase):
	def test_creates_natural_and_anthropogenic_views(self):
		cursor = FakeCursor()
		pv.create_natural_anthropogenic_views(cursor, "model_test")
		self.assertEqual(len(cursor.executed), 2)
		sql_natural, _ = cursor.executed[0]
		sql_anthro, _ = cursor.executed[1]
		self.assertIn("CREATE VIEW \"model_test\".natural_barriers", sql_natural)
		self.assertIn("WHERE structure_type = 'natural' AND species_stats IS NOT NULL", sql_natural)
		self.assertIn("CREATE VIEW \"model_test\".anthropogenic_barriers", sql_anthro)
		self.assertIn("WHERE structure_type = 'anthropogenic' AND species_stats IS NOT NULL", sql_anthro)


class CreateSpeciesBarrierViewsTests(unittest.TestCase):
	def test_creates_one_view_per_species_per_structure_type(self):
		cursor = FakeCursor()
		reporting_species_lifecycles = [("as", "rear"), ("as", "spawn"), ("ae", "rear")]
		pv.create_species_barrier_views(cursor, "model_test", reporting_species_lifecycles)

		self.assertEqual(len(cursor.executed), 4)
		executed_sql = " ".join(sql for sql, _ in cursor.executed)

		self.assertIn("CREATE VIEW \"model_test\".\"natural_barriers_as\"", executed_sql)
		self.assertIn("CREATE VIEW \"model_test\".\"anthropogenic_barriers_as\"", executed_sql)
		self.assertIn("CREATE VIEW \"model_test\".\"natural_barriers_ae\"", executed_sql)
		self.assertIn("CREATE VIEW \"model_test\".\"anthropogenic_barriers_ae\"", executed_sql)

		sql_natural_as = next(sql for sql, _ in cursor.executed if "natural_barriers_as\"" in sql)
		self.assertIn("WHERE structure_type = 'natural' AND species_stats IS NOT NULL", sql_natural_as)
		self.assertIn("id, feature_id, feature_type,", sql_natural_as)
		self.assertIn(
			"(species_passability_value->>'as_spawn')::double precision AS passability_status_spawn",
			sql_natural_as,
		)
		self.assertIn(
			"(species_passability_value->>'as_rear')::double precision AS passability_status_rear",
			sql_natural_as,
		)
		self.assertIn("geometry, snapped_geometry", sql_natural_as)
		self.assertIn("(species_stats->'as'->>'upstream_natural_spawnrear_count')::int AS upstream_natural_spawnrear_count", sql_natural_as)
		self.assertIn("(species_stats->'as'->>'upstream_anthro_spawnrear_count')::int AS upstream_anthro_spawnrear_count", sql_natural_as)
		self.assertIn("(species_stats->'as'->>'downstream_natural_spawnrear_count')::int AS downstream_natural_spawnrear_count", sql_natural_as)
		self.assertIn("(species_stats->'as'->>'downstream_anthro_spawnrear_count')::int AS downstream_anthro_spawnrear_count", sql_natural_as)
		self.assertIn("(species_stats->'as'->>'upstream_natural_spawn_count')::int AS upstream_natural_spawn_count", sql_natural_as)
		self.assertIn("(species_stats->'as'->>'downstream_natural_rear_count')::int AS downstream_natural_rear_count", sql_natural_as)
		self.assertIn(
			"ARRAY(SELECT jsonb_array_elements_text(species_stats->'as'->'downstream_natural_spawn_ids'))::uuid[] "
			"AS downstream_natural_spawn_ids",
			sql_natural_as,
		)
		self.assertIn(
			"ARRAY(SELECT jsonb_array_elements_text(species_stats->'as'->'downstream_natural_rear_ids'))::uuid[] "
			"AS downstream_natural_rear_ids",
			sql_natural_as,
		)
		self.assertIn(
			"ARRAY(SELECT jsonb_array_elements_text(species_stats->'as'->'downstream_anthro_spawn_ids'))::uuid[] "
			"AS downstream_anthro_spawn_ids",
			sql_natural_as,
		)
		self.assertIn(
			"ARRAY(SELECT jsonb_array_elements_text(species_stats->'as'->'downstream_anthro_rear_ids'))::uuid[] "
			"AS downstream_anthro_rear_ids",
			sql_natural_as,
		)
		self.assertIn(
			"ARRAY(SELECT jsonb_array_elements_text(species_stats->'as'->'upstream_anthro_spawn_ids'))::uuid[] "
			"AS upstream_anthro_spawn_ids",
			sql_natural_as,
		)
		self.assertIn(
			"ARRAY(SELECT jsonb_array_elements_text(species_stats->'as'->'upstream_anthro_rear_ids'))::uuid[] "
			"AS upstream_anthro_rear_ids",
			sql_natural_as,
		)
		self.assertIn(
			"(species_stats->'as'->>'spawn_upstream_accessible_length')::double precision AS spawn_upstream_accessible_length",
			sql_natural_as,
		)
		self.assertIn(
			"(species_stats->'as'->>'rear_upstream_accessible_length')::double precision AS rear_upstream_accessible_length",
			sql_natural_as,
		)
		self.assertIn("(species_stats->'as'->>'rear_upstream_length')::double precision AS rear_upstream_length", sql_natural_as)
		self.assertIn(
			"(species_stats->'as'->>'spawn_functional_weighted_upstream_length')::double precision "
			"AS spawn_functional_weighted_upstream_length",
			sql_natural_as,
		)

		sql_anthro_ae = next(sql for sql, _ in cursor.executed if "anthropogenic_barriers_ae\"" in sql)
		self.assertIn("WHERE structure_type = 'anthropogenic' AND species_stats IS NOT NULL", sql_anthro_ae)
		self.assertIn("(species_stats->'ae'->>'upstream_natural_spawnrear_count')::int AS upstream_natural_spawnrear_count", sql_anthro_ae)
		self.assertIn("(species_stats->'ae'->>'rear_upstream_length')::double precision AS rear_upstream_length", sql_anthro_ae)
		self.assertNotIn("spawn_upstream_length", sql_anthro_ae)

	def test_rejects_unsafe_species_code(self):
		cursor = FakeCursor()
		with self.assertRaises(SystemExit):
			pv.create_species_barrier_views(cursor, "model_test", [("as; DROP TABLE x", "rear")])

	def test_spawnrear_lifecycle_has_weighted_columns(self):
		# spawnrear has no raw weighted_length, but its weighted upstream aggregates (the per-edge
		# spawn/rear maximum) are still exposed like every other lifecycle's.
		cursor = FakeCursor()
		pv.create_species_barrier_views(cursor, "model_test", [("as", "spawnrear")])
		sql, _ = cursor.executed[0]
		self.assertIn("(species_stats->'as'->>'spawnrear_upstream_length')::double precision AS spawnrear_upstream_length", sql)
		self.assertIn("(species_stats->'as'->>'spawnrear_weighted_upstream_length')::double precision AS spawnrear_weighted_upstream_length", sql)
		self.assertIn("(species_stats->'as'->>'spawnrear_functional_weighted_upstream_length')::double precision AS spawnrear_functional_weighted_upstream_length", sql)


class CreateUnsnappedBarriersViewTests(unittest.TestCase):
	def test_creates_unsnapped_barriers_view(self):
		cursor = FakeCursor()
		pv.create_unsnapped_barriers_view(cursor, "model_test")
		self.assertEqual(len(cursor.executed), 1)
		sql, _ = cursor.executed[0]
		self.assertIn("CREATE VIEW \"model_test\".unsnapped_barriers", sql)
		self.assertIn("FROM \"model_test\".all_barriers", sql)
		self.assertIn("WHERE snapped_geometry IS NULL", sql)


class CreateSpeciesViewsTests(unittest.TestCase):
	def test_creates_one_view_per_species_with_its_own_lifecycle_columns(self):
		cursor = FakeCursor()
		reporting_species_lifecycles = [("as", "rear"), ("as", "spawn"), ("ae", "rear")]
		pv.create_species_views(cursor, "model_test", reporting_species_lifecycles)

		self.assertEqual(len(cursor.executed), 2)
		sql_as, sql_ae = (sql for sql, _ in cursor.executed)

		self.assertIn("CREATE VIEW \"model_test\".\"streams_as\"", sql_as)
		self.assertIn("id, geometry, length, strahler_order, effective_length, segment_gradient", sql_as)
		self.assertIn("(species_stats->'as'->>'spawn_accessibility') AS spawn_accessibility", sql_as)
		self.assertIn("(species_stats->'as'->>'rear_accessibility') AS rear_accessibility", sql_as)
		self.assertIn("(species_stats->'as'->>'upstream_natural_spawn_count')::int AS upstream_natural_spawn_count", sql_as)
		self.assertIn("(species_stats->'as'->>'downstream_natural_rear_count')::int AS downstream_natural_rear_count", sql_as)
		self.assertIn("(species_stats->'as'->>'upstream_anthro_spawnrear_count')::int AS upstream_anthro_spawnrear_count", sql_as)
		self.assertIn(
			"ARRAY(SELECT jsonb_array_elements_text(species_stats->'as'->'upstream_anthro_spawn_ids'))::uuid[] "
			"AS upstream_anthro_spawn_ids",
			sql_as,
		)
		self.assertIn(
			"ARRAY(SELECT jsonb_array_elements_text(species_stats->'as'->'upstream_anthro_rear_ids'))::uuid[] "
			"AS upstream_anthro_rear_ids",
			sql_as,
		)
		self.assertNotIn("rear_upstream_length", sql_as)
		self.assertNotIn("spawn_upstream_accessible_length", sql_as)
		self.assertNotIn("rear_upstream_accessible_length", sql_as)
		self.assertIn("(species_stats->'as'->>'rear_weighted_length')::double precision AS rear_weighted_length", sql_as)
		self.assertIn("(species_stats->'as'->>'spawn_weighted_length')::double precision AS spawn_weighted_length", sql_as)
		self.assertIn("WHERE species_stats->'as' IS NOT NULL", sql_as)

		self.assertIn("CREATE VIEW \"model_test\".\"streams_ae\"", sql_ae)
		self.assertNotIn("rear_upstream_length", sql_ae)
		# ae only reports "rear" (reporting_species_lifecycles has ("ae", "rear")), not "spawn"
		self.assertIn("(species_stats->'ae'->>'rear_weighted_length')::double precision AS rear_weighted_length", sql_ae)
		self.assertNotIn("spawn_weighted_length", sql_ae)

	def test_rejects_unsafe_species_code(self):
		cursor = FakeCursor()
		with self.assertRaises(SystemExit):
			pv.create_species_views(cursor, "model_test", [("as; DROP TABLE x", "rear")])


class CreateBarrierViewsOrchestratorTests(unittest.TestCase):
	def test_creates_all_views_and_commits_twice(self):
		cursor = FakeCursor()
		conn = FakeConn()
		plan = {
			"output_schema": "model_test",
			"reporting_species_lifecycles": [("as", "rear"), ("ae", "rear")],
		}
		pv.create_barrier_views(conn, cursor, plan)

		executed_sql = " ".join(sql for sql, _ in cursor.executed)
		self.assertIn("CREATE VIEW \"model_test\".natural_barriers", executed_sql)
		self.assertIn("CREATE VIEW \"model_test\".anthropogenic_barriers", executed_sql)
		self.assertIn("CREATE VIEW \"model_test\".\"natural_barriers_as\"", executed_sql)
		self.assertIn("CREATE VIEW \"model_test\".\"anthropogenic_barriers_as\"", executed_sql)
		self.assertIn("CREATE VIEW \"model_test\".\"natural_barriers_ae\"", executed_sql)
		self.assertIn("CREATE VIEW \"model_test\".\"anthropogenic_barriers_ae\"", executed_sql)
		self.assertIn("CREATE VIEW \"model_test\".unsnapped_barriers", executed_sql)
		self.assertIn("CREATE VIEW \"model_test\".\"streams_as\"", executed_sql)
		self.assertIn("CREATE VIEW \"model_test\".\"streams_ae\"", executed_sql)
		self.assertEqual(conn.commits, 2)


if __name__ == "__main__":
	unittest.main()
