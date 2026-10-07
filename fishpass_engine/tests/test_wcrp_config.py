"""Tests for the shared WCRP config/role helpers in fishpass_engine/scripts/db.py and the
related model_plan.py validation (reporting_values, per-plan WCRP overrides) -- no
database access.

Run with: python -m unittest fishpass_engine.tests.test_wcrp_config
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import db  # noqa: E402
import model_plan as mp  # noqa: E402


def write_plan(tmp_dir, code, extra_yaml="", reporting_values="  - all_all"):
	path = Path(tmp_dir) / f"{code}.yaml"
	path.write_text(f"""
code: {code}
output_schema: model_{code}
aoi:
  workunit:
    - 03EBA001
target_species:
  - chn
reporting_values:
{reporting_values}
structure_types:
  - dams
{extra_yaml}
""")
	return Path(tmp_dir)


def write_config(tmp_dir, text):
	path = Path(tmp_dir) / "fishpass.yaml"
	path.write_text(text)
	return path


class FakeCursor:
	def __init__(self, fail_on=None):
		self.executed = []
		self.fail_on = fail_on

	def execute(self, sql, params=None):
		self.executed.append(sql)
		if self.fail_on and self.fail_on in sql:
			raise RuntimeError("boom")


class FakeConn:
	def __init__(self, cursor):
		self.cursor = cursor

	def commit(self):
		self.cursor.executed.append("COMMIT")

	def rollback(self):
		self.cursor.executed.append("ROLLBACK")


class AsRoleTests(unittest.TestCase):
	def test_sets_and_resets_role(self):
		cursor = FakeCursor()
		with db.as_role(FakeConn(cursor), cursor, "fishpass"):
			cursor.execute("select 1;")
		self.assertEqual(
			cursor.executed,
			['set role "fishpass";', "COMMIT", "select 1;", "ROLLBACK", "reset role;", "COMMIT"],
		)

	def test_rolls_back_before_reset_on_error(self):
		"""The original error propagates, and ROLLBACK precedes RESET ROLE so the
		reset isn't rejected by an aborted transaction."""
		cursor = FakeCursor(fail_on="bad sql")
		with self.assertRaises(RuntimeError):
			with db.as_role(FakeConn(cursor), cursor, "fishpass"):
				cursor.execute("bad sql")
		self.assertEqual(cursor.executed[-3:], ["ROLLBACK", "reset role;", "COMMIT"])


class ConfigTests(unittest.TestCase):
	def test_repo_config_is_valid(self):
		"""The shipped config/fishpass.yaml loads and every value passes validation
		(get_db_roles / wcrp_setting exit on a missing section, bad role name, or
		out-of-range value)."""
		roles = db.get_db_roles()
		self.assertTrue(roles["owner"])
		for key in db.WCRP_SETTINGS:
			db.wcrp_setting({}, key)

	def test_plan_value_overrides_config(self):
		plan = {"label_in_wcrp_rank_threshold": 10, "min_avg_gain_km": None}
		self.assertEqual(db.wcrp_setting(plan, "label_in_wcrp_rank_threshold"), 10)
		self.assertEqual(
			db.wcrp_setting(plan, "min_avg_gain_km"),
			db.config_section("wcrp")["min_avg_gain_km"],
		)

	def test_unknown_setting_raises(self):
		with self.assertRaises(KeyError):
			db.wcrp_setting({}, "not_a_setting")

	def test_invalid_config_value_exits(self):
		with tempfile.TemporaryDirectory() as tmp:
			cfg = write_config(tmp, "wcrp:\n  label_in_wcrp_rank_threshold: 0\n")
			with self.assertRaises(SystemExit):
				db.wcrp_setting({}, "label_in_wcrp_rank_threshold", config_path=cfg)

	def test_missing_section_exits(self):
		with tempfile.TemporaryDirectory() as tmp:
			cfg = write_config(tmp, "structure_classification: {}\n")
			with self.assertRaises(SystemExit):
				db.get_db_roles(config_path=cfg)

	def test_unsafe_role_name_exits(self):
		with tempfile.TemporaryDirectory() as tmp:
			cfg = write_config(tmp, 'database_roles:\n  owner: "fishpass; drop table x"\n')
			with self.assertRaises(SystemExit):
				db.get_db_roles(config_path=cfg)


class ModelPlanWcrpTests(unittest.TestCase):
	def test_empty_reporting_values_rejected(self):
		with tempfile.TemporaryDirectory() as tmp:
			models_dir = write_plan(tmp, "ns", reporting_values="  []")
			with self.assertRaises(SystemExit) as cm:
				mp.load_model_plan("ns", models_dir=models_dir)
		self.assertIn("reporting_values must be a non-empty list", str(cm.exception.code))

	def test_overrides_default_to_none(self):
		with tempfile.TemporaryDirectory() as tmp:
			plan = mp.load_model_plan("ns", models_dir=write_plan(tmp, "ns"))
		self.assertIsNone(plan["label_in_wcrp_rank_threshold"])
		self.assertIsNone(plan["min_avg_gain_km"])

	def test_valid_overrides_accepted(self):
		extra = "label_in_wcrp_rank_threshold: 50\nmin_avg_gain_km: 0"
		with tempfile.TemporaryDirectory() as tmp:
			plan = mp.load_model_plan("ns", models_dir=write_plan(tmp, "ns", extra))
		self.assertEqual(plan["label_in_wcrp_rank_threshold"], 50)
		self.assertEqual(plan["min_avg_gain_km"], 0)

	def test_invalid_overrides_rejected(self):
		for extra in (
			"label_in_wcrp_rank_threshold: 2.5",
			"label_in_wcrp_rank_threshold: 0",
			"label_in_wcrp_rank_threshold: true",
			"min_avg_gain_km: -1",
			"min_avg_gain_km: lots",
		):
			with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
				with self.assertRaises(SystemExit):
					mp.load_model_plan("ns", models_dir=write_plan(tmp, "ns", extra))


if __name__ == "__main__":
	unittest.main()
