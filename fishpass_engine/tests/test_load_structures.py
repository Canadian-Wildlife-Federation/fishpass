"""Tests for fishpass_engine/scripts/load_structures.py -- pure logic (passability
mapping/exploding, classification) plus SQL-shape checks against a stubbed cursor.

Run with: python -m unittest fishpass_engine.tests.test_load_structures
"""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

for _module_name in ("psycopg", "requests"):
	if _module_name not in sys.modules:
		try:
			__import__(_module_name)
		except ImportError:
			sys.modules[_module_name] = types.ModuleType(_module_name)

import load_structures as ls  # noqa: E402
from cabd_client import map_passability  # noqa: E402


class MapPassabilityTests(unittest.TestCase):
	def test_documented_codes(self):
		self.assertEqual(map_passability(1), 0)  # Barrier
		self.assertEqual(map_passability(2), 0)  # Partial Barrier
		self.assertEqual(map_passability(3), 1)  # Passable
		self.assertEqual(map_passability(4), 0)  # Unknown
		self.assertEqual(map_passability(5), 1)  # NA - No Structure
		self.assertEqual(map_passability(6), 1)  # NA - Decommissioned / Removed

	def test_missing_code_defaults_to_impassable(self):
		self.assertEqual(map_passability(None), 0)

	def test_unrecognized_code_defaults_to_impassable(self):
		self.assertEqual(map_passability(99), 0)


class BuildCabdRowTests(unittest.TestCase):
	def test_field_mapping(self):
		feature = {
			"properties": {"cabd_id": "f1", "feature_type": "dams", "passability_status_code": 3},
			"geometry": {"coordinates": [-63.5, 45.1]},
		}
		cabd_id, species_json, passability_status_code, lon, lat = ls.build_cabd_row(feature, ["chn", "sth"])
		self.assertEqual(cabd_id, "f1")
		self.assertEqual(passability_status_code, 3)
		self.assertEqual(lon, -63.5)
		self.assertEqual(lat, 45.1)
		species_map = json.loads(species_json)
		self.assertEqual(
			species_map,
			{"chn_rear": 1, "chn_spawn": 1, "sth_rear": 1, "sth_spawn": 1},
		)


class ExplodeNewStructurePassabilityTests(unittest.TestCase):
	def test_rear_only_key_leaves_spawn_defaulted(self):
		result = ls.explode_new_structure_passability({"es": 1}, None, ["es"])
		self.assertEqual(result, {"es_rear": 1, "es_spawn": 0})

	def test_both_columns_null_means_full_barrier(self):
		result = ls.explode_new_structure_passability(None, None, ["chn", "sth"])
		self.assertEqual(
			result,
			{"chn_rear": 0, "chn_spawn": 0, "sth_rear": 0, "sth_spawn": 0},
		)

	def test_species_not_in_target_species_ignored(self):
		result = ls.explode_new_structure_passability({"es": 0.5, "wl": 1}, None, ["es"])
		self.assertEqual(result, {"es_rear": 0.5, "es_spawn": 0})

	def test_species_missing_from_both_columns_defaults_to_impassable(self):
		# a target species absent from both columns is treated as a full barrier, per
		# structure_new_dataset.md.
		result = ls.explode_new_structure_passability({"es": 1}, None, ["es", "wl"])
		self.assertEqual(
			result,
			{"es_rear": 1, "es_spawn": 0, "wl_rear": 0, "wl_spawn": 0},
		)


class LoadNaturalFeatureTypesTests(unittest.TestCase):
	def _write_config(self, text):
		f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
		f.write(text)
		f.close()
		return f.name

	def test_reads_configured_list(self):
		path = self._write_config(
			"structure_classification:\n"
			"  natural_feature_types:\n"
			"    - waterfalls\n"
			"    - gradients\n"
		)
		self.assertEqual(ls.load_natural_feature_types(path), {"waterfalls", "gradients"})

	def test_empty_list_means_everything_anthropogenic(self):
		path = self._write_config("structure_classification:\n  natural_feature_types: []\n")
		self.assertEqual(ls.load_natural_feature_types(path), set())

	def test_missing_key_means_everything_anthropogenic(self):
		path = self._write_config("structure_classification: {}\n")
		self.assertEqual(ls.load_natural_feature_types(path), set())

	def test_missing_file_exits(self):
		with self.assertRaises(SystemExit):
			ls.load_natural_feature_types("/nonexistent/fishpass.yaml")


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


class ExplodeStructureUpdateTests(unittest.TestCase):
	def test_rear_only_sets_only_rear_key(self):
		result = ls.explode_structure_update({"es": 0.5}, None, ["es"])
		self.assertEqual(result, {"es_rear": 0.5})

	def test_both_columns_set_both_keys(self):
		result = ls.explode_structure_update({"es": 0.5}, {"es": 1}, ["es"])
		self.assertEqual(result, {"es_rear": 0.5, "es_spawn": 1})

	def test_species_not_in_target_species_ignored(self):
		result = ls.explode_structure_update({"es": 0.5, "wl": 1}, None, ["es"])
		self.assertEqual(result, {"es_rear": 0.5})

	def test_both_columns_null_returns_empty(self):
		self.assertEqual(ls.explode_structure_update(None, None, ["es"]), {})

	def test_missing_species_omitted_not_defaulted(self):
		# Unlike explode_passability, a species absent from both columns is simply omitted --
		# it is not forced to impassable, so the caller's jsonb `||` merge leaves that species'
		# existing default value (from CABD or new_structures) untouched.
		result = ls.explode_structure_update({"es": 1}, None, ["es", "wl"])
		self.assertEqual(result, {"es_rear": 1})


class ApplyStructureUpdatesOrderingTests(unittest.TestCase):
	def test_local_override_wins_over_authoritative(self):
		# fetch_candidate rows arrive pre-ordered by the SQL (authoritative asc date, then
		# local_override asc date) -- simulate that ordering directly.
		rows = [
			("f1", {"es": 1}, None),  # authoritative
			("f1", {"es": 0}, None),  # local_override, applied after -- should win
		]
		cursor = FakeCursor(fetch_results=[rows])
		import load_structures as mod
		updated = mod.apply_structure_updates(cursor, "model_test", {
			"structure_update_table": "support.structure_updates",
			"update_scope": "plan1",
			"target_species": ["es"],
		})
		self.assertEqual(updated, 1)
		update_sql, update_params = cursor.executed[-1]
		self.assertIn("species_passability_value || %s::jsonb", update_sql)
		self.assertEqual(json.loads(update_params[0]), {"es_rear": 0})
		self.assertEqual(update_params[1], "f1")

	def test_no_rows_returns_zero(self):
		cursor = FakeCursor(fetch_results=[[]])
		updated = ls.apply_structure_updates(cursor, "model_test", {
			"structure_update_table": "support.structure_updates",
			"update_scope": "plan1",
			"target_species": ["es"],
		})
		self.assertEqual(updated, 0)


class FakeConn:
	def __init__(self):
		self.commit_count = 0

	def commit(self):
		self.commit_count += 1


class AddGradientBarriersTests(unittest.TestCase):
	def test_uses_configured_table_name(self):
		cursor = FakeCursor(
			fetchone_results=[("support.gradient_barriers",)],
			fetch_results=[[("02YK000",)], [("gb1", ["es"], b"\x00")]],
		)
		plan = {"gradient_barriers_table": "support.gradient_barriers"}
		count = ls.add_gradient_barriers(cursor, "model_test", plan, 4617)

		self.assertEqual(count, 1)
		regclass_sql, regclass_params = cursor.executed[0]
		self.assertEqual(regclass_sql, "SELECT to_regclass(%s)")
		self.assertEqual(regclass_params, ("support.gradient_barriers",))
		select_sql, _ = cursor.executed[-1]
		self.assertIn('"support"."gradient_barriers"', select_sql)
		insert_sql, insert_params = cursor.executemany_calls[0]
		self.assertIn("'gradients'", insert_sql)
		self.assertIn("'gradient_barriers'", insert_sql)

	def test_override_table_name_used_in_queries(self):
		cursor = FakeCursor(
			fetchone_results=[("other.gb_table",)],
			fetch_results=[[("02YK000",)], [("gb1", ["es"], b"\x00")]],
		)
		plan = {"gradient_barriers_table": "other.gb_table"}
		ls.add_gradient_barriers(cursor, "model_test", plan, 4617)

		regclass_sql, regclass_params = cursor.executed[0]
		self.assertEqual(regclass_params, ("other.gb_table",))
		select_sql, _ = cursor.executed[-1]
		self.assertIn('"other"."gb_table"', select_sql)
		self.assertNotIn("support.gradient_barriers", select_sql)

	def test_missing_table_skips_and_reports_configured_name(self):
		cursor = FakeCursor(fetchone_results=[(None,)])
		plan = {"gradient_barriers_table": "other.gb_table"}
		count = ls.add_gradient_barriers(cursor, "model_test", plan, 4617)
		self.assertEqual(count, 0)


class PopulateCabdTableTests(unittest.TestCase):
	def setUp(self):
		self._orig_fetch_feature_type = ls.fetch_feature_type
		self.addCleanup(setattr, ls, "fetch_feature_type", self._orig_fetch_feature_type)

	def test_fetches_and_inserts_one_feature_type(self):
		feature = {
			"properties": {"cabd_id": "f1", "feature_type": "dams", "passability_status_code": 3},
			"geometry": {"coordinates": [-63.5, 45.1]},
		}
		ls.fetch_feature_type = lambda feature_type, short_names: iter([feature])

		cursor = FakeCursor()
		count = ls.populate_cabd_table(cursor, "model_test", "dams", ["02YK000"], ["es"], 4617)

		self.assertEqual(count, 1)
		self.assertEqual(len(cursor.executemany_calls), 1)
		sql, params = cursor.executemany_calls[0]
		self.assertIn("INSERT INTO \"model_test\".\"cabd_dams\"", sql)
		self.assertNotIn("all_barriers", sql)
		self.assertEqual(len(params), 1)
		self.assertEqual(params[0][0], "f1")
		self.assertEqual(params[0][2], 3)  # passability_status_code

	def test_no_short_names_skips_fetch_and_insert(self):
		ls.fetch_feature_type = lambda feature_type, short_names: (_ for _ in ()).throw(
			AssertionError("should not be called when there are no short_names")
		)

		cursor = FakeCursor()
		count = ls.populate_cabd_table(cursor, "model_test", "dams", [], ["es"], 4617)

		self.assertEqual(count, 0)
		self.assertEqual(cursor.executemany_calls, [])


class PopulateFromCabdRawCacheTests(unittest.TestCase):
	def setUp(self):
		self._orig_fetch_feature_type = ls.fetch_feature_type
		self.addCleanup(setattr, ls, "fetch_feature_type", self._orig_fetch_feature_type)

	def test_seeds_cabd_tables_before_all_barriers_insert(self):
		feature = {
			"properties": {"cabd_id": "f1", "feature_type": "dams", "passability_status_code": 3},
			"geometry": {"coordinates": [-63.5, 45.1]},
		}
		ls.fetch_feature_type = lambda feature_type, short_names: iter([feature] if feature_type == "dams" else [])

		cursor = FakeCursor(fetch_results=[[("02YK000",)]])  # get_aoi_short_names
		conn = FakeConn()
		plan = {"structure_types": ["dams", "waterfalls"], "target_species": ["es"]}
		# populate_from_cabd's return value is cursor.rowcount from the final all_barriers
		# INSERT -- FakeCursor.execute() sets rowcount=1 on every call as a simplistic stand-in
		# for "one statement affected rows", so the returned count here reflects that stub
		# convention rather than modelling real per-row affected-row counts.
		count = ls.populate_from_cabd(cursor, conn, "model_test", plan, 4617)

		self.assertEqual(count, 1)
		self.assertEqual(conn.commit_count, 2)  # once per feature type

		create_statements = [sql for sql, _ in cursor.executed if sql.startswith("CREATE TABLE")]
		self.assertEqual(len(create_statements), 2)
		self.assertIn("cabd_dams", create_statements[0])
		self.assertIn("cabd_waterfalls", create_statements[1])

		# cabd_dams and cabd_waterfalls are each populated via executemany (raw cache); confirm the
		# raw cache inserts don't touch all_barriers.
		self.assertEqual(len(cursor.executemany_calls), 2)
		dams_sql, dams_params = cursor.executemany_calls[0]
		self.assertIn("cabd_dams", dams_sql)
		self.assertNotIn("all_barriers", dams_sql)
		self.assertEqual(len(dams_params), 1)
		self.assertEqual(dams_params[0][0], "f1")

		waterfalls_sql, waterfalls_params = cursor.executemany_calls[1]
		self.assertIn("cabd_waterfalls", waterfalls_sql)
		self.assertEqual(waterfalls_params, [])

		# all_barriers is populated via a single execute() (UNION ALL over the cache tables), not
		# executemany -- it's the last statement executed.
		all_barriers_sql, all_barriers_params = cursor.executed[-1]
		self.assertIn("all_barriers", all_barriers_sql)
		self.assertIn("UNION ALL", all_barriers_sql)
		self.assertEqual(all_barriers_params, ["dams", "waterfalls"])

	def test_gradients_never_reach_cabd_table_creation(self):
		ls.fetch_feature_type = lambda feature_type, short_names: (_ for _ in ()).throw(
			AssertionError("should not be called for gradients")
		)

		cursor = FakeCursor(fetch_results=[[]])  # get_aoi_short_names returns nothing
		conn = FakeConn()
		plan = {"structure_types": ["gradients"], "target_species": ["es"]}
		count = ls.populate_from_cabd(cursor, conn, "model_test", plan, 4617)

		self.assertEqual(count, 0)
		self.assertEqual(cursor.executed, [])
		self.assertEqual(conn.commit_count, 0)

	def test_no_short_names_still_creates_empty_cabd_tables(self):
		ls.fetch_feature_type = lambda feature_type, short_names: (_ for _ in ()).throw(
			AssertionError("should not be called when there are no short_names")
		)

		cursor = FakeCursor(fetch_results=[[]])  # get_aoi_short_names returns nothing
		conn = FakeConn()
		plan = {"structure_types": ["dams"], "target_species": ["es"]}
		ls.populate_from_cabd(cursor, conn, "model_test", plan, 4617)

		create_statements = [sql for sql, _ in cursor.executed if sql.startswith("CREATE TABLE")]
		self.assertEqual(len(create_statements), 1)
		self.assertIn("cabd_dams", create_statements[0])
		self.assertEqual(cursor.executemany_calls, [])
		self.assertEqual(conn.commit_count, 1)

class CreateCabdTableTests(unittest.TestCase):
	def test_creates_table_without_source_with_passability_status_code(self):
		cursor = FakeCursor()
		ls.create_cabd_table(cursor, "model_test", "dams", 4617)
		self.assertEqual(len(cursor.executed), 1)
		sql, _ = cursor.executed[0]
		self.assertIn("CREATE TABLE \"model_test\".\"cabd_dams\"", sql)
		self.assertNotIn("source", sql)
		self.assertIn("passability_status_code integer", sql)
		self.assertNotIn("all_barriers", sql)

	def test_rejects_unsafe_feature_type_name(self):
		cursor = FakeCursor()
		with self.assertRaises(SystemExit):
			ls.create_cabd_table(cursor, "model_test", "dams; DROP TABLE x", 4617)

if __name__ == "__main__":
	unittest.main()
