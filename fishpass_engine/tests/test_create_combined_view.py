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

import create_combined_view as ccv  # noqa: E402
import db  # noqa: E402
import model_plan as mp  # noqa: E402
import postprocess_views as pv  # noqa: E402
import utils  # noqa: E402


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
		return ccv.build_view_sql(self._plan(extra_yaml, reporting), ["waterfalls", "gradients"])

	def test_every_view_length_column_surfaced_in_km(self):
		"""Every length column from postprocess_views.barrier_length_fields --
		including the two accessible lengths -- is divided by 1000 and _km-suffixed."""
		sql = self._sql()
		for sp in ("chn", "sth"):
			for col in pv.barrier_length_fields(["rear", "spawn", "spawnrear"]):
				self.assertIn(f'bp_{sp}."{col}" / 1000.0 AS "{sp}_{col}_km"', sql)
		self.assertIn('"chn_spawn_upstream_accessible_length_km"', sql)
		self.assertIn('"sth_rear_upstream_accessible_length_km"', sql)

	def test_no_metre_length_columns_remain(self):
		"""No habitat length is surfaced without the _km conversion."""
		sql = self._sql()
		self.assertNotIn('AS "chn_spawn_upstream_length"', sql)

	def test_ranked_tables_join_on_feature_id(self):
		"""Ranked and tracking feature_id values both match all_barriers.feature_id."""
		sql = self._sql()
		self.assertIn('ON rk_chn_rear."feature_id" = ab."feature_id"', sql)
		self.assertNotIn('ON rk_chn_rear."feature_id" = ab."id"', sql)
		self.assertIn('ON tt."feature_id" = ab."feature_id"', sql)
		self.assertIn('ON bp_chn."feature_id" = ab."feature_id"', sql)
		self.assertNotIn('ON bp_chn."id" = ab."id"', sql)

	def test_only_reported_lifecycles_surfaced(self):
		"""A plan reporting only chn_spawn surfaces spawn lengths (+ accessible) only."""
		sql = self._sql(reporting="chn_spawn")
		self.assertIn('"chn_spawn_functional_upstream_length_km"', sql)
		self.assertIn('"chn_rear_upstream_accessible_length_km"', sql)
		self.assertNotIn("chn_rear_upstream_length_km", sql)
		self.assertNotIn("bp_sth", sql)

	def test_isolated_structures_require_reported_lifecycle_passability(self):
		"""Isolated structures need non-NULL passability for reported life stages only."""
		for lifecycle in ("spawn", "rear"):
			with self.subTest(lifecycle=lifecycle):
				sql = self._sql(reporting=f"chn_{lifecycle}")
				where = sql.rsplit("WHERE ", 1)[1].split(";", 1)[0]
				self.assertIn('stream_segment."is_isolated" IS NOT TRUE', where)
				self.assertIn('ON stream_segment."id" = ab."downstream_edge_id"', sql)
				self.assertIn(f'bp_chn."passability_status_{lifecycle}" IS NOT NULL', where)
				other_lifecycle = "rear" if lifecycle == "spawn" else "spawn"
				self.assertNotIn(f'bp_chn."passability_status_{other_lifecycle}" IS NOT NULL', where)
		combined_sql = self._sql(reporting="chn_spawnrear")
		combined_where = combined_sql.rsplit("WHERE ", 1)[1].split(";", 1)[0]
		self.assertIn('bp_chn."passability_status_spawn" IS NOT NULL', combined_where)
		self.assertIn('bp_chn."passability_status_rear" IS NOT NULL', combined_where)

	def test_combined_view_requires_snapped_geometry(self):
		"""Features without snapped geometry are excluded from the combined view."""
		sql = self._sql()
		where = sql.rsplit("WHERE ", 1)[1].split(";", 1)[0]
		self.assertIn('ab."snapped_geometry" IS NOT NULL', where)

	def test_label_threshold_default_and_override(self):
		"""label_in_wcrp uses the fishpass.yaml default (30) unless the plan overrides."""
		self.assertIn("rank_combined <= 30::numeric", self._sql())
		sql = self._sql("label_in_wcrp_rank_threshold: 50")
		self.assertIn("rank_combined <= 50::numeric", sql)
		self.assertNotIn("<= 30::numeric", sql)

	def test_roles_from_config(self):
		"""Owner/grants on the view match database_roles in fishpass.yaml, quoted."""
		roles = utils.get_db_roles()
		plan = self._plan()
		sql = ccv.build_view_sql(plan, ["waterfalls", "gradients"])
		view = f"{db.quote_ident(plan['code'] + '_wcrp')}.{db.quote_ident(ccv.VIEW_NAME)}"
		self.assertIn(f"ALTER VIEW {view} OWNER TO {db.quote_ident(roles['owner'])};", sql)
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
			tuple(ccv.STREAM_CROSSINGS_FDW.split(".")): ([ccv.CABD_JOIN_KEY] + ccv.STREAM_CROSSING_ATTRIBUTES),
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
