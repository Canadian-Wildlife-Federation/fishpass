"""Tests for fishpass_engine/scripts/create_wcrp_tracking_table.py -- table schema
building and validation without database access.

Run with: python -m unittest fishpass_engine.tests.test_create_wcrp_tracking_table
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
import create_wcrp_tracking_table as ctt  # noqa: E402


class FakeCursor:
    """Records executed SQL; fetchone() answers catalog lookups from `exists`,
    a dict of (schema, name) -> bool (default True)."""

    def __init__(self, exists=None):
        self.exists = exists or {}
        self.executed = []
        self._last_params = None

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self._last_params = params

    def fetchone(self):
        if self._last_params is None:
            return None
        return (1,) if self.exists.get(tuple(self._last_params), True) else None


class FakeConn:
    def __init__(self):
        self.calls = []

    def commit(self):
        self.calls.append("commit")

    def rollback(self):
        self.calls.append("rollback")


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


class ValidateSpeciesTests(unittest.TestCase):
    def test_valid_species_codes_pass(self):
        """_validate_species accepts valid species codes."""
        # Should not raise
        ctt._validate_species(["chn", "sth", "as", "ae"])

    def test_invalid_species_code_exits(self):
        """_validate_species exits for unsafe identifiers."""
        with self.assertRaises(SystemExit):
            ctt._validate_species(["chn; DROP TABLE"])

    def test_invalid_hyphen_exits(self):
        """_validate_species exits for codes with hyphens."""
        with self.assertRaises(SystemExit):
            ctt._validate_species(["ch-n"])

    def test_empty_species_list_passes(self):
        """_validate_species allows empty list (though may fail elsewhere)."""
        # Should not raise
        ctt._validate_species([])


class BuildColumnsTests(unittest.TestCase):
    def test_leading_columns_come_first(self):
        """_build_columns places leading columns first."""
        cols = ctt._build_columns(["chn"])
        col_names = [name for name, _ in cols]

        # barrier_id should be early
        barrier_id_idx = col_names.index("barrier_id")
        self.assertLess(barrier_id_idx, 10)
        # internal_name should come before barrier_id (in leading)
        internal_name_idx = col_names.index("internal_name")
        self.assertLess(internal_name_idx, barrier_id_idx)

    def test_species_columns_inserted_in_order(self):
        """_build_columns includes per-species columns in correct positions."""
        cols = ctt._build_columns(["chn", "sth"])
        col_names = [name for name, _ in cols]

        # Should have structure_list_status for each species
        self.assertIn("structure_list_status_chn", col_names)
        self.assertIn("structure_list_status_sth", col_names)

        # Should have partial_passability for each species
        self.assertIn("partial_passability_chn", col_names)
        self.assertIn("partial_passability_sth", col_names)

        # partial_passability should come after structure_list_status
        struct_idx = col_names.index("structure_list_status_chn")
        partial_idx = col_names.index("partial_passability_chn")
        self.assertLess(struct_idx, partial_idx)

    def test_middle_columns_after_species_structure(self):
        """_build_columns places middle columns after per-species structure_list_status."""
        cols = ctt._build_columns(["chn"])
        col_names = [name for name, _ in cols]

        struct_idx = col_names.index("structure_list_status_chn")
        passability_asmt_idx = col_names.index("passability_assessment_type")

        self.assertLess(struct_idx, passability_asmt_idx)

    def test_trailing_columns_come_last(self):
        """_build_columns places trailing columns at the end."""
        cols = ctt._build_columns(["chn"])
        col_names = [name for name, _ in cols]

        # notes should be near the end (trailing columns)
        notes_idx = col_names.index("notes")
        self.assertGreater(notes_idx, len(col_names) - 10)

    def test_enum_types_qualified_to_support_schema(self):
        """_build_columns qualifies enum types to support schema."""
        cols = ctt._build_columns(["chn"])
        col_defs = {name: type_sql for name, type_sql in cols}

        # Enum columns should be qualified
        self.assertIn("support.", col_defs["structure_type"])
        self.assertIn("support.", col_defs["passability_assessment_type"])
        self.assertIn("support.", col_defs["priority"])

    def test_numeric_columns_correct_type(self):
        """_build_columns uses numeric type for money columns."""
        cols = ctt._build_columns(["chn"])
        col_defs = {name: type_sql for name, type_sql in cols}

        self.assertEqual(col_defs["estimated_cost_$"], "numeric")
        self.assertEqual(col_defs["estimated_rehabilitation_cost_$"], "numeric")
        self.assertEqual(col_defs["actual_project_cost_$"], "numeric")

    def test_text_columns_for_dates(self):
        """_build_columns uses text type for date columns."""
        cols = ctt._build_columns(["chn"])
        col_defs = {name: type_sql for name, type_sql in cols}

        self.assertEqual(col_defs["rehabilitated_date"], "text")
        self.assertEqual(col_defs["timeline_for_next_steps"], "text")


class BuildCreateTableSqlTests(unittest.TestCase):
    def test_creates_qualified_table_name(self):
        """_build_create_table_sql references correct schema and table."""
        sql = ctt._build_create_table_sql("ns_wcrp", "tracking_table_ns", ["chn"])

        self.assertIn('"ns_wcrp"."tracking_table_ns"', sql)
        self.assertIn("CREATE TABLE", sql)

    def test_barrier_id_is_primary_key(self):
        """_build_create_table_sql makes barrier_id the primary key."""
        sql = ctt._build_create_table_sql("ns_wcrp", "tracking_table_ns", ["chn"])

        self.assertIn("PRIMARY KEY", sql)
        self.assertIn("barrier_id", sql)
        # Should be in the CONSTRAINT
        self.assertIn("tracking_table_ns_pkey", sql)

    def test_barrier_id_is_uuid_not_null(self):
        """_build_create_table_sql defines barrier_id as NOT NULL uuid."""
        sql = ctt._build_create_table_sql("ns_wcrp", "tracking_table_ns", ["chn"])

        # Should have uuid NOT NULL
        self.assertIn("uuid NOT NULL", sql)

    def test_includes_per_species_columns(self):
        """_build_create_table_sql includes columns for all species."""
        sql = ctt._build_create_table_sql("ns_wcrp", "tracking_table_ns", ["chn", "sth"])

        self.assertIn("structure_list_status_chn", sql)
        self.assertIn("structure_list_status_sth", sql)
        self.assertIn("partial_passability_chn", sql)
        self.assertIn("partial_passability_sth", sql)

    def test_no_if_not_exists(self):
        """_build_create_table_sql uses CREATE TABLE (not IF NOT EXISTS)."""
        sql = ctt._build_create_table_sql("ns_wcrp", "tracking_table_ns", ["chn"])

        # Should NOT have IF NOT EXISTS (creation is guarded separately)
        self.assertNotIn("IF NOT EXISTS", sql)
        self.assertIn("CREATE TABLE", sql)


class WcrpSchemaNameTests(unittest.TestCase):
    def test_wcrp_schema_suffix(self):
        """_wcrp_schema appends _wcrp to plan code."""
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = write_plan(tmp, "ns")
            plan = mp.load_model_plan("ns", models_dir=models_dir)

        schema = ctt._wcrp_schema(plan)
        self.assertEqual(schema, "ns_wcrp")

    def test_wcrp_schema_with_alphanumeric_code(self):
        """_wcrp_schema works with alphanumeric plan codes."""
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = write_plan(tmp, "test123")
            plan = mp.load_model_plan("test123", models_dir=models_dir)

        schema = ctt._wcrp_schema(plan)
        self.assertEqual(schema, "test123_wcrp")


class SupportObjectTests(unittest.TestCase):
    def test_blank2null_sql_no_longer_in_script(self):
        """blank2null() moved to init/database/wcrp_support.sql."""
        self.assertFalse(hasattr(ctt, "BLANK2NULL_FUNCTION_SQL"))

    def test_required_enum_types(self):
        """_required_enum_types lists every support.tt_* type the table uses."""
        types_needed = ctt._required_enum_types(["chn"])
        self.assertIn("tt_structure_type", types_needed)
        self.assertIn("tt_structure_list_status_type", types_needed)
        self.assertIn("tt_partial_passability_notes_type", types_needed)
        self.assertEqual(len(types_needed), 13)

    def test_missing_support_objects_exit_before_any_ddl(self):
        """create_tracking_table stops (pointing at wcrp_support.sql) if the
        blank2null function is missing, before SET ROLE or any DDL."""
        cursor = FakeCursor(exists={("support", "blank2null"): False})
        plan = {"code": "ns", "target_species": ["chn"]}
        with self.assertRaises(SystemExit) as cm:
            ctt.create_tracking_table(FakeConn(), cursor, plan)
        self.assertIn("wcrp_support.sql", str(cm.exception.code))
        self.assertFalse(any("set role" in q.lower() for q in cursor.executed))
        self.assertFalse(any("create" in q.lower() for q in cursor.executed))

    def test_existing_table_exits_and_role_is_reset(self):
        """An existing tracking table aborts; as_role rolls back then resets."""
        cursor = FakeCursor()  # everything exists, including the table
        conn = FakeConn()
        plan = {"code": "ns", "target_species": ["chn"]}
        with self.assertRaises(SystemExit) as cm:
            ctt.create_tracking_table(conn, cursor, plan)
        self.assertIn("already exists", str(cm.exception.code))
        self.assertFalse(any(q.startswith("CREATE TABLE") for q in cursor.executed))
        self.assertEqual(cursor.executed[-1], "reset role;")
        self.assertEqual(conn.calls[-2:], ["rollback", "commit"])


class CheckWcrpPrerequisitesTests(unittest.TestCase):
    PLAN = {"code": "ns"}

    def test_passes_when_everything_exists(self):
        ctt.check_wcrp_prerequisites(FakeCursor(), self.PLAN)

    def test_missing_tracking_table_exits_with_action_hint(self):
        cursor = FakeCursor(exists={("ns_wcrp", "tracking_table_ns"): False})
        with self.assertRaises(SystemExit) as cm:
            ctt.check_wcrp_prerequisites(cursor, self.PLAN)
        self.assertIn("FishPass WCRP Tracking Table Setup", str(cm.exception.code))

    def test_missing_blank2null_exits(self):
        cursor = FakeCursor(exists={("support", "blank2null"): False})
        with self.assertRaises(SystemExit) as cm:
            ctt.check_wcrp_prerequisites(cursor, self.PLAN)
        self.assertIn("wcrp_support.sql", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
    