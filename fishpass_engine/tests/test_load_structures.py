"""Tests for fishpass_engine/scripts/load_structures.py -- pure logic (passability
mapping/exploding, classification) plus SQL-shape checks against a stubbed cursor.

Run with: python -m unittest fishpass_engine.tests.test_load_structures
"""

import json
import sys
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
		cabd_id, feature_type, species_json, lon, lat = ls.build_cabd_row(feature, ["chn", "sth"])
		self.assertEqual(cabd_id, "f1")
		self.assertEqual(feature_type, "dams")
		self.assertEqual(lon, -63.5)
		self.assertEqual(lat, 45.1)
		species_map = json.loads(species_json)
		self.assertEqual(
			species_map,
			{"chn_rear": 1, "chn_spawn": 1, "sth_rear": 1, "sth_spawn": 1},
		)


class ExplodePassabilityTests(unittest.TestCase):
	def test_key_without_lifestage_applies_to_both(self):
		result = ls.explode_passability({"es": 0.25})
		self.assertEqual(result, {"es_rear": 0.25, "es_spawn": 0.25})

	def test_key_with_lifestage_kept_as_is(self):
		result = ls.explode_passability({"es_rear": 1})
		self.assertEqual(result, {"es_rear": 1})

	def test_mixed_keys(self):
		result = ls.explode_passability({"es": 0.5, "wl_spawn": 1})
		self.assertEqual(result, {"es_rear": 0.5, "es_spawn": 0.5, "wl_spawn": 1})

	def test_null_with_target_species_means_full_barrier(self):
		result = ls.explode_passability(None, target_species=["chn", "sth"])
		self.assertEqual(
			result,
			{"chn_rear": 0, "chn_spawn": 0, "sth_rear": 0, "sth_spawn": 0},
		)

	def test_null_without_target_species_returns_empty(self):
		self.assertEqual(ls.explode_passability(None), {})

	def test_species_not_mentioned_is_left_untouched(self):
		# partial update: only the keys present in the JSON are exploded, no default filled in
		# for species not mentioned -- see the design note in load_structures.py.
		result = ls.explode_passability({"es_rear": 1}, target_species=["es", "wl"])
		self.assertEqual(result, {"es_rear": 1})


class ClassifyStructuresLogicTests(unittest.TestCase):
	def test_natural_feature_types(self):
		self.assertIn("waterfalls", ls.NATURAL_FEATURE_TYPES)
		self.assertIn("gradient", ls.NATURAL_FEATURE_TYPES)
		self.assertNotIn("dams", ls.NATURAL_FEATURE_TYPES)
		self.assertNotIn("stream_crossings", ls.NATURAL_FEATURE_TYPES)


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


class ApplyStructureUpdatesOrderingTests(unittest.TestCase):
	def test_local_override_wins_over_authoritative(self):
		# fetch_candidate rows arrive pre-ordered by the SQL (authoritative asc date, then
		# local_override asc date) -- simulate that ordering directly.
		rows = [
			("f1", {"es_rear": 1}),  # authoritative
			("f1", {"es_rear": 0}),  # local_override, applied after -- should win
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


if __name__ == "__main__":
	unittest.main()
