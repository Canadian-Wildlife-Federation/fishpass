"""Tests for fishpass_engine/scripts/create_combined_view.py -- view building
and configuration without database access.

Run with: python -m unittest fishpass_engine.tests.test_create_combined_view
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
import create_combined_view as ccv  # noqa: E402


def write_plan(tmp_dir, code, extra_yaml=""):
    """Helper to write a minimal test plan YAML."""
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


class LoadNaturalFeatureTypesTests(unittest.TestCase):
    def test_override_takes_precedence(self):
        """_load_natural_feature_types uses plan override if set."""
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = write_plan(
                tmp, "myplan",
                extra_yaml="natural_feature_types_override:\n  - waterfall\n  - gradient"
            )
            plan = mp.load_model_plan("myplan", models_dir=models_dir)

        types_list = ccv._load_natural_feature_types(plan)
        self.assertEqual(types_list, ["waterfall", "gradient"])

    def test_empty_override_allowed(self):
        """_load_natural_feature_types allows empty override list."""
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = write_plan(tmp, "myplan", extra_yaml="natural_feature_types_override: []")
            plan = mp.load_model_plan("myplan", models_dir=models_dir)

        types_list = ccv._load_natural_feature_types(plan)
        self.assertEqual(types_list, [])

    def test_invalid_identifier_in_override_exits(self):
        """_load_natural_feature_types exits if override has unsafe identifiers."""
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = write_plan(
                tmp, "myplan",
                extra_yaml="natural_feature_types_override:\n  - 'invalid; drop table'"
            )
            plan = mp.load_model_plan("myplan", models_dir=models_dir)

        with self.assertRaises(SystemExit):
            ccv._load_natural_feature_types(plan)

    def test_fallback_to_fishpass_yaml_when_no_override(self):
        """_load_natural_feature_types reads config when no override."""
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = write_plan(tmp, "myplan")
            plan = mp.load_model_plan("myplan", models_dir=models_dir)

        types_list = ccv._load_natural_feature_types(plan)
        # Should return a list (may be empty if config file doesn't set anything)
        self.assertIsInstance(types_list, list)


class SpeciesLifecyclesTests(unittest.TestCase):
    def test_returns_dict_and_sorted_pairs(self):
        """_species_lifecycles returns both by_species dict and sorted pairs."""
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = write_plan(tmp, "myplan")
            plan = mp.load_model_plan("myplan", models_dir=models_dir)

        by_species, pairs = ccv._species_lifecycles(plan)

        self.assertIsInstance(by_species, dict)
        self.assertIsInstance(pairs, list)
        self.assertEqual(len(pairs), 6)  # 2 species x 3 lifecycles

    def test_by_species_groups_lifecycles(self):
        """_species_lifecycles.by_species groups lifecycles by species."""
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = write_plan(tmp, "myplan")
            plan = mp.load_model_plan("myplan", models_dir=models_dir)

        by_species, _ = ccv._species_lifecycles(plan)

        self.assertEqual(set(by_species["chn"]), {"rear", "spawn", "spawnrear"})
        self.assertEqual(set(by_species["sth"]), {"rear", "spawn", "spawnrear"})

    def test_pairs_are_sorted(self):
        """_species_lifecycles returns sorted (species, lifecycle) pairs."""
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = write_plan(tmp, "myplan")
            plan = mp.load_model_plan("myplan", models_dir=models_dir)

        _, pairs = ccv._species_lifecycles(plan)

        self.assertEqual(pairs, sorted(pairs))


if __name__ == "__main__":
    unittest.main()
