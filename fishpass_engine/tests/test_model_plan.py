"""Tests for fishpass_engine/scripts/model_plan.py -- pure parsing/validation logic, no DB
or real files beyond a temp plan yaml, so no stubbing is needed beyond yaml itself.

Run with: python -m unittest fishpass_engine.tests.test_model_plan
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

try:
	import yaml  # noqa: F401
except ImportError:
	sys.modules["yaml"] = types.ModuleType("yaml")

import model_plan as mp  # noqa: E402


def write_plan(tmp_dir, code, extra_yaml=""):
	path = Path(tmp_dir) / f"{code}.yaml"
	path.write_text(f"""
code: {code}
output_schema: model_{code}
aoi:
  workunit:
    - 03EBA001
target_species:
  - chn
  - sth
reporting_values:
  - all_all
structure_types:
  - dams
{extra_yaml}
""")
	return Path(tmp_dir)


class ExpandReportingValuesTests(unittest.TestCase):
	def test_explicit_species_and_lifecycle(self):
		result = mp.expand_reporting_values(["chn_rear"], ["chn", "sth"], "plan.yaml")
		self.assertEqual(result, [("chn", "rear")])

	def test_all_lifecycle(self):
		result = mp.expand_reporting_values(["chn_all"], ["chn", "sth"], "plan.yaml")
		self.assertEqual(result, [("chn", "general"), ("chn", "rear"), ("chn", "spawn")])

	def test_all_species(self):
		result = mp.expand_reporting_values(["all_rear"], ["chn", "sth"], "plan.yaml")
		self.assertEqual(result, [("chn", "rear"), ("sth", "rear")])

	def test_all_all(self):
		result = mp.expand_reporting_values(["all_all"], ["chn", "sth"], "plan.yaml")
		self.assertEqual(len(result), 6)  # 2 species x 3 lifecycles

	def test_species_not_in_target_species_exits(self):
		with self.assertRaises(SystemExit):
			mp.expand_reporting_values(["ae_rear"], ["chn"], "plan.yaml")

	def test_invalid_lifecycle_exits(self):
		with self.assertRaises(SystemExit):
			mp.expand_reporting_values(["chn_juvenile"], ["chn"], "plan.yaml")

	def test_malformed_entry_exits(self):
		with self.assertRaises(SystemExit):
			mp.expand_reporting_values(["chn"], ["chn"], "plan.yaml")


class LoadModelPlanTests(unittest.TestCase):
	def test_defaults_applied(self):
		with tempfile.TemporaryDirectory() as tmp:
			models_dir = write_plan(tmp, "myplan")
			plan = mp.load_model_plan("myplan", models_dir=models_dir)

		self.assertEqual(plan["structure_snap_edge_distance_m"], 100)
		self.assertEqual(plan["structure_snap_vertex_distance_m"], 50)
		self.assertEqual(plan["habitat_point_snap_edge_distance_m"], 100)
		self.assertEqual(plan["habitat_point_snap_vertex_distance_m"], 50)
		self.assertEqual(plan["structure_update_table"], "support.structure_updates")
		self.assertEqual(plan["structure_new_table"], "support.new_structures")
		self.assertEqual(plan["habitat_update_table"], "support.habitat_updates")
		self.assertEqual(plan["gradient_barriers_table"], "support.gradient_barriers")
		self.assertEqual(plan["update_scope"], "myplan")
		self.assertEqual(plan["aoi_kind"], "workunit")
		self.assertEqual(plan["aoi_value"], ["03EBA001"])
		self.assertFalse(plan["include_gradient_barriers"])
		self.assertEqual(plan["impassable_threshold"], 1.0)

	def test_impassable_threshold_override(self):
		with tempfile.TemporaryDirectory() as tmp:
			models_dir = write_plan(tmp, "myplan", extra_yaml="impassable_threshold: 0.5")
			plan = mp.load_model_plan("myplan", models_dir=models_dir)
		self.assertEqual(plan["impassable_threshold"], 0.5)

	def test_invalid_impassable_threshold_exits(self):
		with tempfile.TemporaryDirectory() as tmp:
			models_dir = write_plan(tmp, "myplan", extra_yaml="impassable_threshold: 1.5")
			with self.assertRaises(SystemExit):
				mp.load_model_plan("myplan", models_dir=models_dir)

	def test_explicit_update_scope_not_overridden(self):
		with tempfile.TemporaryDirectory() as tmp:
			models_dir = write_plan(tmp, "myplan", extra_yaml="update_scope: cheticamp_wcrp")
			plan = mp.load_model_plan("myplan", models_dir=models_dir)
		self.assertEqual(plan["update_scope"], "cheticamp_wcrp")

	def test_gradient_barriers_opt_in(self):
		with tempfile.TemporaryDirectory() as tmp:
			models_dir = write_plan(tmp, "myplan", extra_yaml="structure_types:\n  - dams\n  - gradient_barriers")
			plan = mp.load_model_plan("myplan", models_dir=models_dir)
		self.assertTrue(plan["include_gradient_barriers"])

	def test_gradient_barriers_table_override(self):
		with tempfile.TemporaryDirectory() as tmp:
			models_dir = write_plan(tmp, "myplan", extra_yaml="gradient_barriers_table: other.gb_table")
			plan = mp.load_model_plan("myplan", models_dir=models_dir)
		self.assertEqual(plan["gradient_barriers_table"], "other.gb_table")

	def test_code_mismatch_exits(self):
		with tempfile.TemporaryDirectory() as tmp:
			models_dir = write_plan(tmp, "myplan")
			(models_dir / "otherplan.yaml").write_text((models_dir / "myplan.yaml").read_text())
			with self.assertRaises(SystemExit):
				mp.load_model_plan("otherplan", models_dir=models_dir)

	def test_missing_file_exits(self):
		with tempfile.TemporaryDirectory() as tmp:
			with self.assertRaises(SystemExit):
				mp.load_model_plan("nope", models_dir=Path(tmp))

	def test_missing_required_field_exits(self):
		with tempfile.TemporaryDirectory() as tmp:
			models_dir = Path(tmp)
			(models_dir / "bad.yaml").write_text("code: bad\noutput_schema: model_bad\n")
			with self.assertRaises(SystemExit):
				mp.load_model_plan("bad", models_dir=models_dir)

	def test_upstream_of_not_supported_exits(self):
		with tempfile.TemporaryDirectory() as tmp:
			models_dir = Path(tmp)
			(models_dir / "bad.yaml").write_text("""
code: bad
output_schema: model_bad
aoi:
  upstream_of:
    - 12345678-1234-1234-1234-123456789012
target_species:
  - chn
reporting_values:
  - all_all
structure_types:
  - dams
""")
			with self.assertRaises(SystemExit):
				mp.load_model_plan("bad", models_dir=models_dir)

	def test_multiple_aoi_kinds_exits(self):
		with tempfile.TemporaryDirectory() as tmp:
			models_dir = Path(tmp)
			(models_dir / "bad.yaml").write_text("""
code: bad
output_schema: model_bad
aoi:
  workunit:
    - 03EBA001
  province:
    - ns
target_species:
  - chn
reporting_values:
  - all_all
structure_types:
  - dams
""")
			with self.assertRaises(SystemExit):
				mp.load_model_plan("bad", models_dir=models_dir)

	def test_invalid_output_schema_exits(self):
		with tempfile.TemporaryDirectory() as tmp:
			models_dir = Path(tmp)
			(models_dir / "bad.yaml").write_text("""
code: bad
output_schema: "model; drop table foo"
aoi:
  workunit:
    - 03EBA001
target_species:
  - chn
reporting_values:
  - all_all
structure_types:
  - dams
""")
			with self.assertRaises(SystemExit):
				mp.load_model_plan("bad", models_dir=models_dir)

	def test_workunit_all(self):
		with tempfile.TemporaryDirectory() as tmp:
			models_dir = Path(tmp)
			(models_dir / "allplan.yaml").write_text("""
code: allplan
output_schema: model_all
aoi:
  workunit: all
target_species:
  - chn
reporting_values:
  - all_all
structure_types:
  - dams
""")
			plan = mp.load_model_plan("allplan", models_dir=models_dir)
		self.assertEqual(plan["aoi_value"], "all")


if __name__ == "__main__":
	unittest.main()
