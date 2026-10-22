"""Tests for fishpass_engine/scripts/utils.py helpers not covered elsewhere -- no database access.
(The WCRP config/role helpers are tested in test_wcrp_config.py.)

Run with: python -m unittest fishpass_engine.tests.test_utils
"""

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import utils  # noqa: E402


def write_config(tmp_dir, text):
	path = Path(tmp_dir) / "fishpass.yaml"
	path.write_text(text, encoding="utf-8")
	return path


class NaturalFeatureTypesTests(unittest.TestCase):
	NO_OVERRIDE = {"natural_feature_types_override": None}

	def test_reads_configured_list(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = write_config(
				tmp, "structure_classification:\n  natural_feature_types:\n    - waterfalls\n    - gradients\n"
			)
			self.assertEqual(utils.natural_feature_types(self.NO_OVERRIDE, path), ["waterfalls", "gradients"])

	def test_empty_list_means_everything_anthropogenic(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = write_config(tmp, "structure_classification:\n  natural_feature_types: []\n")
			self.assertEqual(utils.natural_feature_types(self.NO_OVERRIDE, path), [])

	def test_missing_key_means_everything_anthropogenic(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = write_config(tmp, "structure_classification: {}\n")
			self.assertEqual(utils.natural_feature_types(self.NO_OVERRIDE, path), [])

	def test_missing_file_exits(self):
		with self.assertRaises(SystemExit):
			utils.natural_feature_types(self.NO_OVERRIDE, "/nonexistent/fishpass.yaml")

	def test_default_config_file_is_valid(self):
		self.assertIsInstance(utils.natural_feature_types(self.NO_OVERRIDE), list)

	def test_override_takes_precedence(self):
		plan = {"natural_feature_types_override": ["waterfall", "gradient"]}
		self.assertEqual(utils.natural_feature_types(plan, "/nonexistent/fishpass.yaml"), ["waterfall", "gradient"])

	def test_empty_override_allowed(self):
		plan = {"natural_feature_types_override": []}
		self.assertEqual(utils.natural_feature_types(plan, "/nonexistent/fishpass.yaml"), [])

	def test_unsafe_override_exits(self):
		with self.assertRaises(SystemExit):
			utils.natural_feature_types({"natural_feature_types_override": ["invalid; drop table"]})

	def test_unsafe_config_value_exits(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = write_config(tmp, "structure_classification:\n  natural_feature_types:\n    - 'bad name'\n")
			with self.assertRaises(SystemExit):
				utils.natural_feature_types(self.NO_OVERRIDE, path)


class SharedRegexTests(unittest.TestCase):
	def test_identifier(self):
		self.assertTrue(utils.IDENTIFIER_RE.match("model_ab1"))
		self.assertFalse(utils.IDENTIFIER_RE.match("1ab"))
		self.assertFalse(utils.IDENTIFIER_RE.match("a.b"))

	def test_safe_name_allows_leading_digit(self):
		self.assertTrue(utils.SAFE_NAME_RE.match("1ns"))
		self.assertFalse(utils.SAFE_NAME_RE.match("a-b"))


class FormatRuntimeTests(unittest.TestCase):
	def test_hours_past_24(self):
		self.assertEqual(utils.format_runtime(timedelta(hours=26, minutes=1, seconds=2)), "26:01:02")

	def test_none(self):
		self.assertIsNone(utils.format_runtime(None))


if __name__ == "__main__":
	unittest.main()
