import argparse
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fishpass_qa  # noqa: E402


class ApplyYamlConfigTests(unittest.TestCase):
	def test_ignores_target_species_without_reporting_lifestages(self):
		plan = {
			"target_species": ["as", "ae"],
			"reporting_values": ["as_all"],
			"code": "test",
			"output_schema": "model_test",
		}
		species = {"species": [{"code": "as", "name": "Atlantic salmon"}, {"code": "ae", "name": "Arctic char"}]}
		args = argparse.Namespace(
			model_plan="plan.yaml",
			species_parameters="species.yaml",
			species_code=None,
			output_schema=None,
			streams_view=None,
			barriers_view=None,
			wcrp_schema=None,
			ranked_table=None,
		)

		with patch.object(fishpass_qa, "load_yaml", side_effect=[plan, species]):
			fishpass_qa.apply_yaml_config(args)

		self.assertEqual(set(args.species), {"as"})
		self.assertTrue(args.species["as"]["lifestages"])


if __name__ == "__main__":
	unittest.main()
