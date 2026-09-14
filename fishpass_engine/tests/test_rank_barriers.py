"""Tests for fishpass_engine/scripts/rank_barriers.py -- SQL-shape checks and configuration
building without database access.

Run with: python -m unittest fishpass_engine.tests.test_rank_barriers
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
import rank_barriers as rb  # noqa: E402


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


class ResolveRankingPairsTests(unittest.TestCase):
    def test_returns_all_reporting_species_lifecycles(self):
        """resolve_ranking_pairs returns the plan's reporting_species_lifecycles."""
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = write_plan(tmp, "myplan")
            plan = mp.load_model_plan("myplan", models_dir=models_dir)

        pairs = rb.resolve_ranking_pairs(plan)

        # 2 species x 3 lifecycles = 6 pairs
        self.assertEqual(len(pairs), 6)
        self.assertIn(("chn", "rear"), pairs)
        self.assertIn(("chn", "spawn"), pairs)
        self.assertIn(("chn", "spawnrear"), pairs)
        self.assertIn(("sth", "rear"), pairs)
        self.assertIn(("sth", "spawn"), pairs)
        self.assertIn(("sth", "spawnrear"), pairs)

    def test_species_filter_narrows_pairs(self):
        """resolve_ranking_pairs with species_filter returns only that species."""
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = write_plan(tmp, "myplan")
            plan = mp.load_model_plan("myplan", models_dir=models_dir)

        pairs = rb.resolve_ranking_pairs(plan, species_filter="chn")

        # Only 1 species x 3 lifecycles = 3 pairs
        self.assertEqual(len(pairs), 3)
        self.assertTrue(all(sp == "chn" for sp, _ in pairs))

    def test_invalid_species_filter_exits(self):
        """resolve_ranking_pairs exits if species_filter not in plan."""
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = write_plan(tmp, "myplan")
            plan = mp.load_model_plan("myplan", models_dir=models_dir)

        with self.assertRaises(SystemExit):
            rb.resolve_ranking_pairs(plan, species_filter="invalid_sp")

    def test_invalid_species_code_in_plan_exits(self):
        """resolve_ranking_pairs exits if plan has unsafe species code."""
        # This is a defense-in-depth check; model_plan should catch this first
        plan = {
            "reporting_species_lifecycles": [("chn; DROP TABLE x", "rear")]
        }
        with self.assertRaises(SystemExit):
            rb.resolve_ranking_pairs(plan)


class RankingConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.models_dir = write_plan(self.tmp.name, "ns")
        self.plan = mp.load_model_plan("ns", models_dir=self.models_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_names(self):
        """RankingConfig correctly names output schema/tables."""
        config = rb.RankingConfig(self.plan, "chn", "rear")

        self.assertEqual(config.schema, "model_ns")
        self.assertEqual(config.wcrp_schema, "ns_wcrp")
        self.assertEqual(config.watershed, "ns")
        self.assertEqual(config.species, "chn")
        self.assertEqual(config.lifecycle, "rear")

    def test_barriers_view_name(self):
        """RankingConfig builds correct barriers view name."""
        config = rb.RankingConfig(self.plan, "chn", "rear")
        self.assertEqual(config.barriers_view, "model_ns.anthropogenic_barriers_chn")

    def test_ranked_table_name(self):
        """RankingConfig builds correct ranked output table name."""
        config = rb.RankingConfig(self.plan, "chn", "spawn")
        self.assertEqual(config.ranked, "ns_wcrp.ranked_barriers_chn_spawn_ns")

    def test_work_table_name(self):
        """RankingConfig builds correct working table name."""
        config = rb.RankingConfig(self.plan, "sth", "spawnrear")
        self.assertEqual(config.work, "ns_wcrp.ranked_barriers_sth_spawnrear_ns_work")

    def test_tracking_table_name(self):
        """RankingConfig builds correct tracking table name."""
        config = rb.RankingConfig(self.plan, "chn", "rear")
        self.assertEqual(config.tracking_table, "ns_wcrp.tracking_table_ns")
        self.assertEqual(config.col_tracking_status, "structure_list_status_chn")

    def test_lifecycle_specific_columns(self):
        """RankingConfig includes lifecycle-specific column names."""
        config_rear = rb.RankingConfig(self.plan, "chn", "rear")
        config_spawn = rb.RankingConfig(self.plan, "chn", "spawn")

        # Column names should differ by lifecycle
        self.assertEqual(config_rear.gain_total, "total_rear_hab_gain_group_km")
        self.assertEqual(config_spawn.gain_total, "total_spawn_hab_gain_group_km")

        self.assertEqual(config_rear.src_func_upstr, "rear_functional_upstream_length")
        self.assertEqual(config_spawn.src_func_upstr, "spawn_functional_upstream_length")

    def test_include_rehab_defaults_true(self):
        """RankingConfig.include_rehab defaults to True."""
        config = rb.RankingConfig(self.plan, "chn", "rear")
        self.assertTrue(config.include_rehab)


class PassabilityPredicateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.models_dir = write_plan(self.tmp.name, "ns")
        self.plan = mp.load_model_plan("ns", models_dir=self.models_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_spawn_predicate(self):
        """_passability_predicate for spawn checks spawn passability."""
        config = rb.RankingConfig(self.plan, "chn", "spawn")
        predicate = rb._passability_predicate(config)

        self.assertIn("passability_status_spawn", predicate)
        self.assertNotIn("passability_status_rear", predicate)

    def test_rear_predicate(self):
        """_passability_predicate for rear checks rear passability."""
        config = rb.RankingConfig(self.plan, "chn", "rear")
        predicate = rb._passability_predicate(config)

        self.assertIn("passability_status_rear", predicate)
        self.assertNotIn("passability_status_spawn", predicate)

    def test_spawnrear_predicate(self):
        """_passability_predicate for spawnrear checks both."""
        config = rb.RankingConfig(self.plan, "chn", "spawnrear")
        predicate = rb._passability_predicate(config)

        self.assertIn("passability_status_spawn", predicate)
        self.assertIn("passability_status_rear", predicate)
        self.assertIn("OR", predicate)


class SQLBuildersTests(unittest.TestCase):
    """Verify SQL builders produce syntactically valid SQL fragments."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.models_dir = write_plan(self.tmp.name, "ns")
        self.plan = mp.load_model_plan("ns", models_dir=self.models_dir)
        self.config = rb.RankingConfig(self.plan, "chn", "rear")

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_working_table_with_rehab(self):
        """sql_create_working_table includes tracking join when rehab enabled."""
        sql = rb.sql_create_working_table(self.config)

        self.assertIn("DROP TABLE IF EXISTS", sql)
        self.assertIn("SELECT b.*", sql)
        self.assertIn("FROM", sql)
        self.assertIn("ns_wcrp.tracking_table_ns", sql)
        self.assertIn("WHERE", sql)

    def test_create_working_table_without_rehab(self):
        """sql_create_working_table skips tracking join when rehab disabled."""
        self.config.include_rehab = False
        sql = rb.sql_create_working_table(self.config)

        self.assertIn("DROP TABLE IF EXISTS", sql)
        self.assertIn("SELECT b.*", sql)
        # Should have a comment instead of a join
        self.assertIn("-- tracking table not present yet", sql)

    def test_convert_lengths_to_km(self):
        """sql_convert_lengths_to_km converts all length fields."""
        sql = rb.sql_convert_lengths_to_km(self.config)

        # Should have one ALTER per LENGTH_FIELDS entry
        for field in rb.LENGTH_FIELDS:
            # Should rename to _km
            self.assertIn(f"RENAME COLUMN {field} TO {field}_km", sql)

    def test_stages_list_non_empty(self):
        """STAGES list includes all ranking phases."""
        self.assertGreater(len(rb.STAGES), 5)
        stage_names = [name for name, _ in rb.STAGES]
        self.assertIn("create working table", stage_names)
        self.assertIn("assign ranks", stage_names)
        self.assertIn("finalize slim output table", stage_names)


if __name__ == "__main__":
    unittest.main()
