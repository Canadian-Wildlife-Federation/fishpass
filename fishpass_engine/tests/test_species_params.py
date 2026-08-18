"""Tests for fishpass_engine/scripts/species_params.py.

Run with: python -m unittest fishpass_engine.tests.test_species_params
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import species_params as sp  # noqa: E402


SAMPLE_YAML = """
species:
  - code: chn
    name: Chinook Salmon
    rear_gradient_min: 0.0
    rear_gradient_max: 5.0
    spawn_gradient_min: 0.0
    spawn_gradient_max: 3.0
    strahler_order_rearing_min: 1
    strahler_order_rearing_max: 6
    strahler_order_spawning_min: 2
    strahler_order_spawning_max: 5
    stream_order_1_weight: 0.2
    stream_order_2_weight: 0.5
  - code: noh
    name: No Habitat Species
"""


class LoadSpeciesParamsTests(unittest.TestCase):
	def test_loads_species(self):
		with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
			f.write(SAMPLE_YAML)
			path = f.name
		params = sp.load_species_params(path)
		self.assertIn("chn", params)
		self.assertEqual(params["chn"]["rear_gradient_max"], 5.0)

	def test_missing_file_exits(self):
		with self.assertRaises(SystemExit):
			sp.load_species_params("/no/such/file.yaml")


class HabitatGradientOkTests(unittest.TestCase):
	def setUp(self):
		with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
			f.write(SAMPLE_YAML)
			path = f.name
		self.params = sp.load_species_params(path)

	def test_within_range(self):
		self.assertTrue(sp.habitat_gradient_ok(self.params["chn"], "rear", 2.5))

	def test_at_min_inclusive(self):
		self.assertTrue(sp.habitat_gradient_ok(self.params["chn"], "rear", 0.0))

	def test_at_max_exclusive(self):
		self.assertFalse(sp.habitat_gradient_ok(self.params["chn"], "rear", 5.0))

	def test_out_of_range(self):
		self.assertFalse(sp.habitat_gradient_ok(self.params["chn"], "spawn", 4.0))

	def test_none_gradient_is_false(self):
		self.assertFalse(sp.habitat_gradient_ok(self.params["chn"], "rear", None))

	def test_missing_thresholds_means_no_habitat(self):
		self.assertFalse(sp.habitat_gradient_ok(self.params["noh"], "rear", 1.0))


class HabitatStrahlerOkTests(unittest.TestCase):
	def setUp(self):
		with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
			f.write(SAMPLE_YAML)
			path = f.name
		self.params = sp.load_species_params(path)

	def test_within_range(self):
		self.assertTrue(sp.habitat_strahler_ok(self.params["chn"], "spawn", 3))

	def test_out_of_range(self):
		self.assertFalse(sp.habitat_strahler_ok(self.params["chn"], "spawn", 1))

	def test_none_order_is_false(self):
		self.assertFalse(sp.habitat_strahler_ok(self.params["chn"], "rear", None))

	def test_missing_thresholds_means_no_habitat(self):
		self.assertFalse(sp.habitat_strahler_ok(self.params["noh"], "spawn", 3))


class StreamOrderWeightTests(unittest.TestCase):
	def setUp(self):
		with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
			f.write(SAMPLE_YAML)
			path = f.name
		self.params = sp.load_species_params(path)

	def test_order_1_weight(self):
		self.assertEqual(sp.stream_order_weight(self.params["chn"], 1), 0.2)

	def test_order_2_weight(self):
		self.assertEqual(sp.stream_order_weight(self.params["chn"], 2), 0.5)

	def test_order_3_defaults_to_one(self):
		self.assertEqual(sp.stream_order_weight(self.params["chn"], 3), 1.0)

	def test_missing_order_1_weight_defaults_to_one(self):
		self.assertEqual(sp.stream_order_weight(self.params["noh"], 1), 1.0)


if __name__ == "__main__":
	unittest.main()
