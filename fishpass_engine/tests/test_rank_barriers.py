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
import postprocess_views as pv  # noqa: E402
import rank_barriers as rb  # noqa: E402
import db # noqa: E402


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

    def test_empty_pairs_exits_with_clear_error(self):
        """resolve_ranking_pairs exits (not IndexError) when there is nothing to rank."""
        with self.assertRaises(SystemExit) as cm:
            rb.resolve_ranking_pairs({"code": "ns", "reporting_species_lifecycles": []})
        self.assertIn("reporting_values", str(cm.exception.code))

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
        self.assertEqual(config.wcrp_schema_name, "ns_wcrp")
        self.assertEqual(config.wcrp_schema, '"ns_wcrp"')
        self.assertEqual(config.watershed, "ns")
        self.assertEqual(config.species, "chn")
        self.assertEqual(config.lifecycle, "rear")

    def test_barriers_view_name(self):
        """RankingConfig builds a quoted barriers view name."""
        config = rb.RankingConfig(self.plan, "chn", "rear")
        self.assertEqual(config.barriers_view, '"model_ns"."anthropogenic_barriers_chn"')

    def test_ranked_table_name(self):
        """RankingConfig builds a quoted ranked output table name (raw name kept too)."""
        config = rb.RankingConfig(self.plan, "chn", "spawn")
        self.assertEqual(config.ranked, '"ns_wcrp"."ranked_barriers_chn_spawn_ns"')
        self.assertEqual(config.ranked_name, "ranked_barriers_chn_spawn_ns")

    def test_work_table_name(self):
        """RankingConfig builds a quoted working table name (raw name kept too)."""
        config = rb.RankingConfig(self.plan, "sth", "spawnrear")
        self.assertEqual(config.work, '"ns_wcrp"."ranked_barriers_sth_spawnrear_ns_work"')
        self.assertEqual(config.work_name, "ranked_barriers_sth_spawnrear_ns_work")

    def test_tracking_table_name(self):
        """RankingConfig builds a quoted tracking table name; raw name for lookups."""
        config = rb.RankingConfig(self.plan, "chn", "rear")
        self.assertEqual(config.tracking_table, '"ns_wcrp"."tracking_table_ns"')
        self.assertEqual(config.tracking_table_name, "tracking_table_ns")
        self.assertEqual(config.col_tracking_status, "structure_list_status_chn")

    def test_leading_digit_plan_code_is_quoted(self):
        """A plan code starting with a digit (allowed by PLAN_CODE_RE) yields
        quoted -- and therefore valid -- identifiers."""
        with tempfile.TemporaryDirectory() as tmp:
            plan = mp.load_model_plan("1ns", models_dir=write_plan(tmp, "1ns"))
        config = rb.RankingConfig(plan, "chn", "rear")
        self.assertEqual(config.wcrp_schema, '"1ns_wcrp"')
        self.assertTrue(config.ranked.startswith('"1ns_wcrp".'))
        sql = rb.sql_assign_mainstem_and_initial_groups(config)
        self.assertIn('"ranked_barriers_chn_rear_1ns_work_idx_mainstem"', sql)
        # every reference to the schema is the quoted form
        self.assertNotIn("1ns_wcrp.", sql.replace('"1ns_wcrp".', ""))

    def test_lifecycle_specific_columns(self):
        """RankingConfig includes lifecycle-specific column names."""
        config_rear = rb.RankingConfig(self.plan, "chn", "rear")
        config_spawn = rb.RankingConfig(self.plan, "chn", "spawn")
        self.assertEqual(config_rear.gain_total, "total_rear_hab_gain_group_km")
        self.assertEqual(config_spawn.gain_total, "total_spawn_hab_gain_group_km")
        self.assertEqual(config_rear.src_func_upstr, "rear_functional_upstream_length")
        self.assertEqual(config_spawn.src_func_upstr, "spawn_functional_upstream_length")

    def test_min_avg_gain_default_from_config(self):
        """min_avg_gain_km defaults to config/fishpass.yaml's wcrp value."""
        config = rb.RankingConfig(self.plan, "chn", "rear")
        self.assertEqual(config.min_avg_gain_km, db.wcrp_setting({}, "min_avg_gain_km"))

    def test_min_avg_gain_plan_override(self):
        """A plan's min_avg_gain_km overrides the config default."""
        override = db.wcrp_setting({}, "min_avg_gain_km") + 1
        with tempfile.TemporaryDirectory() as tmp:
            plan = mp.load_model_plan(
                "ns", models_dir=write_plan(tmp, "ns", f"min_avg_gain_km: {override}")
            )
        config = rb.RankingConfig(plan, "chn", "rear")
        self.assertEqual(config.min_avg_gain_km, override)
        self.assertIn(f">= {override}", rb.sql_assign_ranks(config))


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

    def test_create_working_table_joins_tracking(self):
        """sql_create_working_table always folds in the tracking table."""
        sql = rb.sql_create_working_table(self.config)
        self.assertIn("DROP TABLE IF EXISTS", sql)
        self.assertIn("SELECT b.*", sql)
        self.assertIn('LEFT JOIN "ns_wcrp"."tracking_table_ns" tt', sql)
        self.assertIn("'Rehabilitated barrier'", sql)

    def test_convert_lengths_covers_every_view_length_column(self):
        """sql_convert_lengths_to_km converts exactly the view's length columns --
        including both accessible lengths -- derived from postprocess_views."""
        sql = rb.sql_convert_lengths_to_km(self.config)
        expected = pv.barrier_length_fields(["rear", "spawn", "spawnrear"])
        self.assertEqual(self.config.length_fields, expected)
        for field in expected:
            self.assertIn(f"RENAME COLUMN {field} TO {field}_km", sql)
        self.assertIn("spawn_upstream_accessible_length_km", sql)
        self.assertIn("rear_upstream_accessible_length_km", sql)

    def test_convert_lengths_only_touches_reported_lifecycles(self):
        """A plan reporting only chn_spawn must not ALTER rear/spawnrear columns
        (they don't exist on the view -- this used to crash)."""
        with tempfile.TemporaryDirectory() as tmp:
            write_plan(tmp, "sp")
            path = Path(tmp) / "sp.yaml"
            path.write_text(path.read_text().replace("- all_all", "- chn_spawn"))
            plan = mp.load_model_plan("sp", models_dir=Path(tmp))
        config = rb.RankingConfig(plan, "chn", "spawn")
        sql = rb.sql_convert_lengths_to_km(config)
        self.assertIn("spawn_functional_upstream_length_km", sql)
        self.assertIn("rear_upstream_accessible_length_km", sql)  # per-species field
        self.assertNotIn("RENAME COLUMN rear_upstream_length ", sql)
        self.assertNotIn("spawnrear_", sql)

    def test_finalize_uses_config_roles_quoted(self):
        """sql_finalize_output_table takes owner/grant roles from fishpass.yaml."""
        roles = db.get_db_roles()
        sql = rb.sql_finalize_output_table(self.config)
        table = self.config.ranked
        self.assertIn(f"ALTER TABLE {table} OWNER TO {db.quote_ident(roles['owner'])}", sql)
        for role in roles["grant_all"]:
            self.assertIn(f"GRANT ALL ON TABLE {table} TO {db.quote_ident(role)}", sql)
        for role in roles["grant_select"]:
            self.assertIn(f"GRANT SELECT ON TABLE {table} TO {db.quote_ident(role)}", sql)

    def test_not_runnable_standalone(self):
        """rank_barriers is only run via run_model.py."""
        self.assertFalse(hasattr(rb, "main"))

    def test_stages_list_non_empty(self):
        """STAGES list includes all ranking phases."""
        self.assertGreater(len(rb.STAGES), 5)
        stage_names = [name for name, _ in rb.STAGES]
        self.assertIn("create working table", stage_names)
        self.assertIn("assign ranks", stage_names)
        self.assertIn("finalize slim output table", stage_names)


if __name__ == "__main__":
    unittest.main()
