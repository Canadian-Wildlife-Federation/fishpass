"""Tests for fishpass_engine/scripts/azure_export.py and the export SQL added by
postprocess_views.py, rank_barriers.py and create_combined_view.py -- no database or Azure.

Run with: python -m unittest fishpass_engine.tests.test_azure_export
"""

import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

for _module_name in ("psycopg", "requests"):
	if _module_name not in sys.modules:
		try:
			__import__(_module_name)
		except ImportError:
			sys.modules[_module_name] = types.ModuleType(_module_name)

import azure_export as ae  # noqa: E402
import create_combined_view as ccv  # noqa: E402
import postprocess_views as pv  # noqa: E402
import utils  # noqa: E402

RUN_DT = datetime(2026, 10, 7, 14, 30, 0, tzinfo=timezone.utc)


class FakeCursor:
	def __init__(self, fetch=None):
		self.executed = []
		self.fetch = list(fetch or [])

	def execute(self, sql, params=None):
		self.executed.append((" ".join(sql.split()), params))

	def fetchall(self):
		return self.fetch.pop(0) if self.fetch else []

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False


class FakeConn:
	def __init__(self, cursor):
		self._cursor = cursor
		self.commits = 0

	def cursor(self):
		return self._cursor

	def commit(self):
		self.commits += 1


class ContainerNameTests(unittest.TestCase):
	def test_doc_examples(self):
		for code, expected in (
			("ns", "fishpass-ns"),
			("asf_phase1", "fishpass-asf-phase1"),
			("ASF_Phase_1", "fishpass-asf-phase-1"),
			("ns__2026_", "fishpass-ns-2026"),
		):
			self.assertEqual(ae.container_name(code), expected)

	def test_spaces_and_other_characters(self):
		self.assertEqual(ae.container_name("My Plan!#1"), "fishpass-my-plan1")

	def test_too_long(self):
		self.assertEqual(len(ae.container_name("a" * 54)), 63)
		with self.assertRaises(SystemExit):
			ae.container_name("a" * 55)

	def test_empty(self):
		with self.assertRaises(SystemExit):
			ae.container_name("___")


class ValidateExportArgsTests(unittest.TestCase):
	def test_versioned(self):
		self.assertEqual(ae.validate_export_args("versioned", "v1.2_A"), "v1.2_A")
		self.assertEqual(ae.validate_export_args("versioned", "a" * 16), "a" * 16)

	def test_versioned_needs_version(self):
		for bad in (None, ""):
			with self.assertRaises(SystemExit):
				ae.validate_export_args("versioned", bad)

	def test_bad_versions(self):
		for bad in ("a" * 17, "v1-2", "v 1", "v1/2"):
			with self.assertRaises(SystemExit):
				ae.validate_export_args("versioned", bad)

	def test_unversioned_ignores_version(self):
		self.assertIsNone(ae.validate_export_args("unversioned", "v1.2"))
		self.assertIsNone(ae.validate_export_args("unversioned", None))

	def test_bad_type(self):
		with self.assertRaises(SystemExit):
			ae.validate_export_args("release", "v1")


class BlobNameTests(unittest.TestCase):
	def test_doc_examples(self):
		versioned = ae.run_folder("ns", "v1.2", RUN_DT)
		self.assertEqual(
			ae.blob_name("versioned", versioned, "streams_as.gpkg"),
			"runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_streams_as.gpkg",
		)
		self.assertEqual(
			ae.blob_name("versioned", versioned, "metadata.yaml"),
			"runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_metadata.yaml",
		)
		self.assertEqual(
			ae.blob_name("unversioned", ae.run_folder("ns", None, RUN_DT), "streams_as.gpkg"),
			"runs/unversioned/ns_20261007143000/ns_20261007143000_streams_as.gpkg",
		)

	def test_run_time_written_in_utc(self):
		local = RUN_DT.astimezone(timezone(timedelta(hours=-3)))
		self.assertEqual(ae.run_folder("ns", None, local), "ns_20261007143000")


class MetadataYamlTests(unittest.TestCase):
	META = {
		"plan_code": "ns",
		"run_datetime": RUN_DT,
		"total_runtime": timedelta(hours=1, minutes=34, seconds=48),
		"github_user": "egouge",
		"github_branch": "main",
		"commit_sha": "633f011d5c0e4b7a9f2e8d1c3b6a5f4e7d8c9b0a",
	}

	def test_matches_doc_example(self):
		text = ae.metadata_yaml(self.META, "versioned", "v1.2", datetime(2026, 10, 7, 16, 5, 12, tzinfo=timezone.utc))
		self.assertEqual(
			text,
			'plan_code: "ns"\n'
			'export_type: "versioned"\n'
			'version: "v1.2"\n'
			'run_datetime: "2026-10-07T14:30:00Z"\n'
			'export_datetime: "2026-10-07T16:05:12Z"\n'
			'total_runtime: "01:34:48"\n'
			'github_user: "egouge"\n'
			'github_branch: "main"\n'
			'commit_sha: "633f011d5c0e4b7a9f2e8d1c3b6a5f4e7d8c9b0a"\n',
		)

	def test_unversioned_version_is_null(self):
		text = ae.metadata_yaml(self.META, "unversioned", None, RUN_DT)
		self.assertIn("version: null\n", text)

	def test_is_valid_yaml_with_text_values(self):
		import yaml

		data = yaml.safe_load(ae.metadata_yaml(self.META, "versioned", "1.2", RUN_DT))
		self.assertEqual(data["version"], "1.2")
		self.assertEqual(data["total_runtime"], "01:34:48")

	def test_runtime_over_24_hours(self):
		self.assertEqual(utils.format_runtime(timedelta(hours=26, minutes=1, seconds=2)), "26:01:02")


class ControlTableTests(unittest.TestCase):
	def test_create_export_tables(self):
		cursor = FakeCursor()
		ae.create_export_tables(cursor, "model_test")
		sql = " ".join(s for s, _ in cursor.executed)
		self.assertIn('CREATE TABLE "model_test".azure_exports', sql)
		self.assertIn("CHECK (format IN ('csv', 'geopackage'))", sql)
		self.assertIn('CREATE TABLE "model_test".run_metadata', sql)

	def test_add_export_uses_parameters(self):
		cursor = FakeCursor()
		ae.add_export(cursor, "model_test", "streams_as", "SELECT 1", "geopackage")
		sql, params = cursor.executed[0]
		self.assertIn('INSERT INTO "model_test".azure_exports', sql)
		self.assertEqual(params, ("streams_as", "SELECT 1", "geopackage"))

	def test_add_export_rejects_bad_name_or_format(self):
		with self.assertRaises(SystemExit):
			ae.add_export(FakeCursor(), "model_test", "bad name", "SELECT 1", "csv")
		with self.assertRaises(SystemExit):
			ae.add_export(FakeCursor(), "model_test", "ok", "SELECT 1", "shp")

	def test_write_run_metadata_reads_files(self):
		cursor = FakeCursor()
		with mock.patch.dict("os.environ", {"GITHUB_ACTOR": "u", "GITHUB_REF_NAME": "b", "GITHUB_SHA": "s"}):
			ae.write_run_metadata(cursor, "model_test", "example", RUN_DT, timedelta(seconds=5))
		_, params = cursor.executed[-1]
		self.assertEqual(params[:6], (RUN_DT, "u", "b", "s", "example", timedelta(seconds=5)))
		self.assertEqual(params[6], (utils.MODELS_DIR / "example.yaml").read_text(encoding="utf-8"))
		self.assertEqual(params[7], utils.SPECIES_PARAMS_FILE.read_text(encoding="utf-8"))


class CatalogColumnTests(unittest.TestCase):
	def test_conversions(self):
		geom = "snapped_geometry"
		self.assertEqual(
			ae.catalog_column_expr("feature_id", "uuid", "b", "U", geom), '"feature_id"::text AS "feature_id"'
		)
		self.assertEqual(
			ae.catalog_column_expr("priority", "tt_priority", "e", "E", geom), '"priority"::text AS "priority"'
		)
		self.assertEqual(
			ae.catalog_column_expr("ids", "_uuid", "b", "A", geom), 'array_to_string("ids", \'|\') AS "ids"'
		)
		self.assertEqual(ae.catalog_column_expr("n", "int4", "b", "N", geom), '"n"')
		self.assertEqual(ae.catalog_column_expr(geom, "geometry", "b", "U", geom), '"snapped_geometry" AS "geometry"')
		self.assertIsNone(ae.catalog_column_expr("geometry", "geometry", "b", "U", geom))


class ExportSqlTests(unittest.TestCase):
	def test_natural_barriers(self):
		sql = pv.natural_barriers_export_sql("model_test", "as", {"spawn", "rear"})
		self.assertIn('"feature_id"::text AS "feature_id"', sql)
		self.assertIn('"stream_name_en"', sql)
		self.assertIn("array_to_string(\"upstream_anthro_spawn_ids\", '|')", sql)
		self.assertIn('"spawn_functional_weighted_disconnected_upstream_length"', sql)
		self.assertIn('"rear_upstream_length"', sql)
		self.assertNotIn("spawnrear_upstream_length", sql)
		self.assertNotIn("downstream_natural_spawn_ids", sql)
		self.assertNotIn("edge_id", sql)
		self.assertIn('"snapped_geometry" AS "geometry"', sql)
		self.assertEqual(sql.count("geometry"), 2)
		self.assertTrue(sql.endswith('FROM "model_test"."natural_barriers_as"'))

	def test_streams(self):
		sql = pv.streams_export_sql("model_test", "as", {"spawn"})
		self.assertIn('"id"::text AS "id"', sql)
		self.assertIn('"stream_name_en"', sql)
		self.assertIn('"spawn_weighted_connected_length"', sql)
		self.assertNotIn("rear_weighted_length", sql)
		self.assertIn("array_to_string(\"downstream_anthro_rear_ids\", '|')", sql)
		self.assertNotIn("edge_id", sql)
		self.assertTrue(sql.endswith('FROM "model_test"."streams_as"'))

	def test_connectivity_summary(self):
		sql = pv.connectivity_summary_export_sql("model_test")
		self.assertEqual(sql.count(","), 13)
		self.assertTrue(sql.endswith('FROM "model_test".watershed_summary_stats'))

	def test_ranked_barriers(self):
		import rank_barriers as rb

		plan = {"output_schema": "model_test", "code": "ns", "reporting_species_lifecycles": [("as", "spawn")]}
		with (
			mock.patch.object(rb, "wcrp_setting", return_value=0),
			mock.patch.object(rb, "get_db_roles", return_value={"owner": "o", "grant_all": (), "grant_select": ()}),
		):
			c = rb.RankingConfig(plan, "as", "spawn")
		sql = rb.ranked_barriers_export_sql(c)
		self.assertIn('"total_spawn_hab_gain_group_km"', sql)
		self.assertIn('array_to_string("downstr_group_ids", \'|\') AS "downstr_group_ids"', sql)
		self.assertIn('"rank_w_total_upstr_spawn_hab"', sql)
		self.assertIn('"as_rear_passability"', sql)
		self.assertTrue(sql.endswith('FROM "ns_wcrp"."ranked_barriers_as_spawn_ns"'))

	def test_final_structures(self):
		columns = [
			("feature_id", "uuid", "b", "U"),
			("feature_type", "varchar", "b", "S"),
			("snapped_geometry", "geometry", "b", "U"),
			("downstr_group_ids_as_spawn", "_int4", "b", "A"),
			("priority", "tt_priority_type", "e", "E"),
			("estimated_cost_dollars", "numeric", "b", "N"),
		]
		sql = ccv.final_structures_export_sql("ns", columns)
		self.assertIn('"feature_id"::text AS "feature_id"', sql)
		self.assertIn('"priority"::text AS "priority"', sql)
		self.assertIn("array_to_string(\"downstr_group_ids_as_spawn\", '|')", sql)
		self.assertIn('"snapped_geometry" AS "geometry"', sql)
		self.assertTrue(sql.endswith('FROM "ns_wcrp"."combined_output_table_vw"'))

	def test_dollar_columns_renamed(self):
		for raw, out in (
			("estimated_cost_$", "estimated_cost_dollars"),
			("estimated_rehabilitation_cost_$", "estimated_rehabilitation_cost_dollars"),
			("actual_project_cost_$", "actual_project_cost_dollars"),
		):
			self.assertEqual(ccv.TRACKING_ALIAS_OVERRIDES[raw], out)


class BarrierViewsAddExportsTests(unittest.TestCase):
	def test_rows_added_per_species(self):
		cursor = FakeCursor()
		conn = FakeConn(cursor)
		plan = {
			"output_schema": "model_test",
			"structure_types": ["dams"],
			"reporting_species_lifecycles": [("as", "rear"), ("ae", "spawn")],
		}
		pv.create_barrier_views(conn, cursor, plan)
		names = [params[0] for sql, params in cursor.executed if sql.startswith("INSERT INTO")]
		self.assertEqual(
			sorted(names),
			sorted(["natural_barriers_as", "natural_barriers_ae", "streams_as", "streams_ae", "connectivity_summary"]),
		)


class OgrCommandTests(unittest.TestCase):
	def test_password_not_in_command(self):
		env = {
			"FISHPASS_HOST": "h",
			"FISHPASS_PORT": "5432",
			"FISHPASS_DBNAME": "d b",
			"FISHPASS_USER": "u",
			"FISHPASS_PASSWORD": "secret",
		}
		with mock.patch.dict("os.environ", env):
			cmd = ae.ogr2ogr_command(Path("x.gpkg"), "SELECT 1;", "streams_as")
		self.assertNotIn("secret", " ".join(cmd))
		self.assertIn("PG:host='h' port='5432' dbname='d b' user='u'", cmd)
		self.assertEqual(cmd[cmd.index("-sql") + 1], "SELECT 1")
		self.assertEqual(cmd[cmd.index("-nln") + 1], "streams_as")


class RunExportTests(unittest.TestCase):
	META_ROW = (RUN_DT, "u", "main", "abc", "ns", timedelta(minutes=5), "plan: 1\n", "species: 1\n")

	def _conn(self, tables_exist=True, meta=None, exports=None):
		cursor = FakeCursor(fetch=[[meta or self.META_ROW], exports or []])
		return FakeConn(cursor)

	def test_missing_tables(self):
		conn = self._conn()
		with (
			mock.patch.object(ae, "check_export_environment"),
			mock.patch.object(ae, "table_exists", return_value=False),
			self.assertRaises(SystemExit) as ctx,
		):
			ae.run_export(conn, "ns", "ns_out", "azure", "unversioned", None)
		self.assertIn("current plan file", str(ctx.exception))

	def test_plan_code_mismatch(self):
		conn = self._conn(meta=(RUN_DT, "u", "main", "abc", "other", None, "", ""))
		with (
			mock.patch.object(ae, "check_export_environment"),
			mock.patch.object(ae, "table_exists", return_value=True),
			self.assertRaises(SystemExit),
		):
			ae.run_export(conn, "ns", "ns_out", "azure", "unversioned", None)

	def test_uploads_every_file(self):
		conn = self._conn(
			exports=[("streams_as", "SELECT 1", "geopackage"), ("connectivity_summary", "SELECT 2", "csv")]
		)
		container = mock.Mock(container_name="fishpass-ns", url="https://acct/fishpass-ns")
		uploads = []

		def fake_upload(_container, path, blob, content_type):
			uploads.append((blob, content_type))

		with (
			mock.patch.object(ae, "check_export_environment"),
			mock.patch.object(ae, "table_exists", return_value=True),
			mock.patch.object(ae, "get_blob_service_client"),
			mock.patch.object(ae, "get_container", return_value=container) as get_container,
			mock.patch.object(ae, "write_geopackage", side_effect=lambda sql, path, name: path.write_text("g")),
			mock.patch.object(ae, "write_csv", side_effect=lambda conn, sql, path: path.write_text("c")),
			mock.patch.object(ae, "upload_file", side_effect=fake_upload),
		):
			urls = ae.run_export(conn, "ns", "ns_out", "azure", "versioned", "v1")

		get_container.assert_called_once()
		self.assertEqual(get_container.call_args[0][1], "fishpass-ns")
		prefix = "runs/versioned/ns_v1_20261007143000/ns_v1_20261007143000_"
		self.assertEqual(
			uploads,
			[
				(prefix + "streams_as.gpkg", "application/geopackage+sqlite3"),
				(prefix + "connectivity_summary.csv", "text/csv"),
				(prefix + "model_plan.yaml", "application/yaml"),
				(prefix + "fish_species_parameters.yaml", "application/yaml"),
				(prefix + "metadata.yaml", "application/yaml"),
			],
		)
		self.assertEqual(len(urls), 5)

	def test_local_export_writes_every_file(self):
		conn = self._conn(
			exports=[("streams_as", "SELECT 1", "geopackage"), ("connectivity_summary", "SELECT 2", "csv")]
		)
		with (
			tempfile.TemporaryDirectory() as out,
			mock.patch.dict("os.environ", {"GITHUB_ACTIONS": ""}),
			mock.patch.object(ae, "check_ogr2ogr"),
			mock.patch.object(ae, "table_exists", return_value=True),
			mock.patch.object(ae, "get_container") as get_container,
			mock.patch.object(ae, "write_geopackage", side_effect=lambda sql, path, name: path.write_text("g")),
			mock.patch.object(ae, "write_csv", side_effect=lambda conn, sql, path: path.write_text("c")),
		):
			paths = ae.run_export(conn, "ns", "ns_out", "local", "versioned", "v1", out)
			folder = Path(out) / "fishpass-ns" / "runs" / "versioned" / "ns_v1_20261007143000"
			written = sorted(p.name for p in folder.iterdir())

		get_container.assert_not_called()
		prefix = "ns_v1_20261007143000_"
		self.assertEqual(
			written,
			sorted(
				prefix + name
				for name in (
					"streams_as.gpkg",
					"connectivity_summary.csv",
					"model_plan.yaml",
					"fish_species_parameters.yaml",
					"metadata.yaml",
				)
			),
		)
		self.assertEqual(len(paths), 5)


class ExportLocationTests(unittest.TestCase):
	def test_resolve(self):
		self.assertEqual(ae.resolve_export_location(True, False, None, "unversioned"), "azure")
		self.assertEqual(ae.resolve_export_location(False, True, "d", "unversioned"), "local")
		self.assertIsNone(ae.resolve_export_location(False, False, None, None))

	def test_resolve_errors(self):
		for args in (
			(True, True, "d", "unversioned"),  # both
			(False, True, None, "unversioned"),  # no directory
			(False, False, "d", "unversioned"),  # directory without --export-to-local
			(False, True, "d", None),  # no export type
		):
			with self.subTest(args=args), self.assertRaises(SystemExit):
				ae.resolve_export_location(*args)

	def test_local_refused_in_github_actions(self):
		with (
			mock.patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}),
			mock.patch.object(ae, "check_ogr2ogr"),
			self.assertRaises(SystemExit) as ctx,
		):
			ae.check_export_environment("ns", "local", "somewhere")
		self.assertIn("GitHub Actions", str(ctx.exception))

	def test_local_creates_directory(self):
		with (
			tempfile.TemporaryDirectory() as tmp,
			mock.patch.dict("os.environ", {"GITHUB_ACTIONS": ""}),
			mock.patch.object(ae, "check_ogr2ogr"),
		):
			target = Path(tmp) / "a" / "b"
			ae.check_export_environment("ns", "local", str(target))
			self.assertTrue(target.is_dir())


if __name__ == "__main__":
	unittest.main()
