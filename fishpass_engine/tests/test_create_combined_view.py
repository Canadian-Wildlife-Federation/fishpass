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
import postprocess_views as pv  # noqa: E402
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


class EmptyReportingValuesTests(unittest.TestCase):
    def test_empty_pairs_exits_with_clear_error(self):
        """_species_lifecycles exits (with a clear message) when nothing is reported."""
        with self.assertRaises(SystemExit) as cm:
            ccv._species_lifecycles({"code": "ns", "reporting_species_lifecycles": []})
        self.assertIn("reporting_values", str(cm.exception.code))


class BuildViewSqlTests(unittest.TestCase):
    def _plan(self, extra_yaml="", reporting="all_all"):
        with tempfile.TemporaryDirectory() as tmp:
            write_plan(tmp, "ns", extra_yaml)
            path = Path(tmp) / "ns.yaml"
            path.write_text(path.read_text().replace("- all_all", f"- {reporting}"))
            return mp.load_model_plan("ns", models_dir=Path(tmp))

    def _sql(self, extra_yaml="", reporting="all_all"):
        return ccv.build_view_sql(
            self._plan(extra_yaml, reporting), ["waterfalls", "gradients"]
        )

    def test_every_view_length_column_surfaced_in_km(self):
        """Every length column from postprocess_views.barrier_length_fields --
        including the two accessible lengths -- is divided by 1000 and _km-suffixed."""
        sql = self._sql()
        for sp in ("chn", "sth"):
            for col in pv.barrier_length_fields(["rear", "spawn", "spawnrear"]):
                self.assertIn(
                    f'bp_{sp}."{col}" / 1000.0 AS "{sp}_{col}_km"', sql
                )
        self.assertIn('"chn_spawn_upstream_accessible_length_km"', sql)
        self.assertIn('"sth_rear_upstream_accessible_length_km"', sql)

    def test_no_metre_length_columns_remain(self):
        """No habitat length is surfaced without the _km conversion."""
        sql = self._sql()
        self.assertNotIn('AS "chn_spawn_upstream_length"', sql)

    def test_only_reported_lifecycles_surfaced(self):
        """A plan reporting only chn_spawn surfaces spawn lengths (+ accessible) only."""
        sql = self._sql(reporting="chn_spawn")
        self.assertIn('"chn_spawn_functional_upstream_length_km"', sql)
        self.assertIn('"chn_rear_upstream_accessible_length_km"', sql)
        self.assertNotIn("chn_rear_upstream_length_km", sql)
        self.assertNotIn("bp_sth", sql)

    def test_label_threshold_default_and_override(self):
        """label_in_wcrp uses the fishpass.yaml default (30) unless the plan overrides."""
        self.assertIn("rank_combined <= 30::numeric", self._sql())
        sql = self._sql("label_in_wcrp_rank_threshold: 50")
        self.assertIn("rank_combined <= 50::numeric", sql)
        self.assertNotIn("<= 30::numeric", sql)

    def test_roles_from_config(self):
        """Owner/grants on the view match database_roles in fishpass.yaml, quoted."""
        roles = db.get_db_roles()
        plan = self._plan()
        sql = ccv.build_view_sql(plan, ["waterfalls", "gradients"])
        view = f"{db.quote_ident(plan['code'] + '_wcrp')}.{db.quote_ident(ccv.VIEW_NAME)}"
        self.assertIn(f'ALTER VIEW {view} OWNER TO {db.quote_ident(roles["owner"])};', sql)
        for role in roles["grant_all"]:
            self.assertIn(f"GRANT ALL ON TABLE {view} TO {db.quote_ident(role)};", sql)
        for role in roles["grant_select"]:
            self.assertIn(f"GRANT SELECT ON TABLE {view} TO {db.quote_ident(role)};", sql)

    def test_not_runnable_standalone(self):
        """create_combined_view is only run via run_model.py."""
        self.assertFalse(hasattr(ccv, "main"))


class SourcesCursor:
    """Cursor for db.table_columns(): fetchall() returns the columns registered for
    the (schema, table) last queried, or nothing if that relation is absent."""

    def __init__(self, relations):
        self.relations = relations

    def execute(self, sql, params=None):
        self.params = params

    def fetchall(self):
        return [(c,) for c in self.relations.get(self.params, [])]


class CheckCabdFdwSourcesTests(unittest.TestCase):
    def _relations(self):
        return {
            tuple(ccv.DAMS_FDW.split(".")): [ccv.CABD_JOIN_KEY] + ccv.DAM_ATTRIBUTES,
            tuple(ccv.STREAM_CROSSINGS_FDW.split(".")): (
                [ccv.CABD_JOIN_KEY] + ccv.STREAM_CROSSING_ATTRIBUTES
            ),
        }

    def test_all_sources_present_passes(self):
        ccv.check_cabd_fdw_sources(SourcesCursor(self._relations()))

    def test_missing_foreign_table_exits(self):
        relations = self._relations()
        del relations[tuple(ccv.STREAM_CROSSINGS_FDW.split("."))]
        with self.assertRaises(SystemExit) as cm:
            ccv.check_cabd_fdw_sources(SourcesCursor(relations))
        message = str(cm.exception.code)
        self.assertIn(f"{ccv.STREAM_CROSSINGS_FDW} does not exist", message)
        self.assertIn(ccv.CABD_FDW_SQL_SCRIPT, message)
        self.assertNotIn(f"{ccv.DAMS_FDW} ", message)

    def test_missing_attribute_exits(self):
        relations = self._relations()
        relations[tuple(ccv.DAMS_FDW.split("."))].remove(ccv.DAM_ATTRIBUTES[0])
        with self.assertRaises(SystemExit) as cm:
            ccv.check_cabd_fdw_sources(SourcesCursor(relations))
        self.assertIn(ccv.DAM_ATTRIBUTES[0], str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
