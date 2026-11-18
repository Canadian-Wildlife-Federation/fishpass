#!/usr/bin/env python3
"""Automated QA suite for ASF Phase 1 FishPass model outputs.

Runs read-only SQL checks, prints a concise PASS/FAIL/SKIP report, and writes:
  * a summary CSV (one row per check), and
  * one <check_id>_failures.csv per FAILED check containing every failing row.

Every check query returns its failing rows; a check passes when it returns zero rows.
Failure CSVs put the record's primary geometry in a "wkt" column (with its SRID in
"srid") so they can be loaded directly in QGIS via Layer > Add Delimited Text Layer
(Geometry definition: Well known text (WKT), Geometry field: wkt). Any additional
geometry columns are written as <name>_wkt text columns.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

psycopg = None
DRIVER = None


def load_db_driver():
	global psycopg, DRIVER
	try:
		import psycopg as driver

		psycopg, DRIVER = driver, "psycopg"
		return
	except ImportError:
		pass
	try:
		import psycopg2 as driver

		psycopg, DRIVER = driver, "psycopg2"
		return
	except ImportError as exc:
		raise RuntimeError("Install psycopg[binary] or psycopg2-binary.") from exc


TOL = "0.000001"
NODE_TOL = "1e-9"  # node-matching tolerance in degrees (~0.1 mm); streams are split at barriers

# Preferred geometry column (in order) to use as the QGIS "wkt" column in failure CSVs.
GEOM_PREFERENCE = ("snapped_geometry", "geometry", "barrier_geometry")
FETCH_BATCH = 5000


@dataclass(frozen=True)
class Check:
	check_id: str
	name: str
	sql: str  # must return the failing rows (zero rows = PASS)
	category: str
	enabled: Callable[[argparse.Namespace], bool] = lambda _: True
	skip_reason: str = "Required configuration was not provided."


def load_yaml(path: str) -> dict[str, Any]:
	with open(path, "r", encoding="utf-8") as f:
		data = yaml.safe_load(f)
	if not isinstance(data, dict):
		raise TypeError(f"YAML root must be a mapping: {path}")
	return data


LIFESTAGES = ("spawn", "rear", "spawnrear")

THRESHOLD_KEYS = {
	"spawn": {
		"min_gradient": "spawn_gradient_min",
		"max_gradient": "spawn_gradient_max",
		"min_strahler": "strahler_order_spawning_min",
		"max_strahler": "strahler_order_spawning_max",
	},
	"rear": {
		"min_gradient": "rear_gradient_min",
		"max_gradient": "rear_gradient_max",
		"min_strahler": "strahler_order_rearing_min",
		"max_strahler": "strahler_order_rearing_max",
	},
}

CATEGORY_ORDER = [
	"Accessibility",
	"Stream lengths",
	"Barrier calculations",
	"Gradient barriers",
	"Barrier IDs",
	"Habitat parameters",
	"Rankings",
]


def parse_reporting_values(values: list[Any], target_species: list[str]) -> dict[str, list[str]]:
	"""Expand <species>_<lifecycle> reporting values (with 'all' wildcards) into lifestages per species."""
	stages: dict[str, set[str]] = {sp: set() for sp in target_species}
	for raw in values:
		sp, sep, stage = str(raw).strip().rpartition("_")
		if not sep or not sp or (stage != "all" and stage not in LIFESTAGES):
			raise ValueError(
				f"Invalid reporting_values entry {raw!r}; expected <species>_<{'|'.join(LIFESTAGES)}|all>."
			)
		for s in target_species if sp == "all" else [sp]:
			if s not in stages:
				raise ValueError(
					f"reporting_values entry {raw!r} references species {s!r}, which is not in target_species."
				)
			stages[s].update(LIFESTAGES if stage == "all" else [stage])
	return {sp: [st for st in LIFESTAGES if st in found] for sp, found in stages.items()}


def apply_yaml_config(args: argparse.Namespace) -> argparse.Namespace:
	"""Populate plan/species settings from runtime-selected YAML files."""
	plan = load_yaml(args.model_plan)
	species_doc = load_yaml(args.species_parameters)
	target_species = [str(sp) for sp in (plan.get("target_species") or [])]
	if not target_species:
		raise ValueError("Model plan must define at least one target_species value.")
	if args.species_code and args.species_code not in target_species:
		raise ValueError(f"--species-code {args.species_code!r} is not in the plan's target_species {target_species}.")
	selected = [args.species_code] if args.species_code else target_species
	reporting_values = plan.get("reporting_values") or []
	if not reporting_values:
		raise ValueError("Model plan must define reporting_values.")
	lifestages = parse_reporting_values(reporting_values, target_species)
	plan_code = str(plan.get("code") or "").strip()
	output_schema = str(plan.get("output_schema") or "").strip()
	if not plan_code or not output_schema:
		raise ValueError("Model plan must define non-empty code and output_schema values.")
	species_rows = species_doc.get("species") or []
	args.species = {}
	for code in selected:
		row = next((r for r in species_rows if str(r.get("code")) == code), None)
		if row is None:
			raise ValueError(f"Species code {code!r} was not found in {args.species_parameters}.")
		thresholds = {
			stage: {attr: row.get(key) for attr, key in THRESHOLD_KEYS[stage].items()}
			for stage in ("spawn", "rear")
			if stage in lifestages[code]
		}
		args.species[code] = {"name": row.get("name"), "lifestages": lifestages[code], "thresholds": thresholds}
	# View/table names are templates; {species}, {lifestage} and {plan_code} are substituted per check.
	args.plan_code = plan_code
	args.output_schema = args.output_schema or output_schema
	args.streams_view = args.streams_view or "streams_{species}"
	args.barriers_view = args.barriers_view or "anthropogenic_barriers_{species}"
	args.wcrp_schema = args.wcrp_schema or f"{plan_code}_wcrp"
	args.ranked_table = args.ranked_table or "ranked_barriers_{species}_{lifestage}_{plan_code}"
	return args


DEFAULT_ENGINE_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "fishpass_engine" / "scripts"


def load_accessibility_values(scripts_dir: str) -> tuple[str, str]:
	"""Import the accessibility strings the engine writes from graph_stats, so the QA suite cannot
	drift from the engine. Returns (accessible, inaccessible). No hardcoded fallback: if graph_stats
	can't be imported, the run stops rather than silently checking against stale values."""
	path = Path(scripts_dir).resolve()
	if not (path / "graph_stats.py").is_file():
		raise RuntimeError(
			f"graph_stats.py not found in {path}. Pass --engine-scripts-dir (or set FISHPASS_ENGINE_SCRIPTS_DIR) "
			"to the fishpass_engine/scripts folder."
		)
	# graph_stats imports sibling modules (species_params, utils), so the folder itself must be on sys.path.
	if str(path) not in sys.path:
		sys.path.insert(0, str(path))
	try:
		graph_stats = importlib.import_module("graph_stats")
	except ImportError as exc:
		raise RuntimeError(f"Could not import graph_stats from {path}: {exc}") from exc
	return graph_stats.ACCESSIBILITY_ACCESSIBLE, graph_stats.ACCESSIBILITY_INACCESSIBLE


def qliteral(value: str) -> str:
	"""Quote a Python string as a SQL string literal."""
	return "'" + str(value).replace("'", "''") + "'"


def qident(name: str) -> str:
	"""Safely quote a schema/table/view identifier supplied through CLI."""
	return '"' + name.replace('"', '""') + '"'


def relation(schema: str, table: str) -> str:
	return f"{qident(schema)}.{qident(table)}"


def rows_sql(rel: str, where: str) -> str:
	"""Return every column (including geometry) of rows in rel matching where."""
	return f"SELECT t.* FROM {rel} t WHERE {where}"


def id_count_sql(streams: str, combined: str, columns: list[tuple[str, str, str]]) -> str:
	"""Stream segments with count/ID mismatches not explained by coincident final barrier locations.
	columns = [(test_id, count_column, ids_column), ...]

	Performance notes:
	  * barrier_locations / mismatches are MATERIALIZED so the combined view is scanned once,
	    not re-evaluated per mismatched segment inside a LATERAL subquery.
	  * Locations are compared as ST_AsBinary bytea (exact equality, same semantics as before)
	    instead of COUNT(DISTINCT geometry).
	  * One grouped join replaces the per-row LATERAL lookup."""
	values = ",\n                    ".join(f"('{t}', s.{cnt}, s.{ids})" for t, cnt, ids in columns)
	return f"""WITH barrier_locations AS MATERIALIZED (
                SELECT DISTINCT feature_id, ST_AsBinary(snapped_geometry) AS loc
                FROM {combined}
                WHERE feature_id IS NOT NULL AND snapped_geometry IS NOT NULL
            ), mismatches AS MATERIALIZED (
                SELECT s.id, v.test_id, v.reported_count, v.barrier_ids, s.geometry
                FROM {streams} s
                CROSS JOIN LATERAL (VALUES
                    {values}
                ) AS v(test_id, reported_count, barrier_ids)
                WHERE v.reported_count <> COALESCE(cardinality(v.barrier_ids), 0)
            ), loc_counts AS (
                SELECT m.id, m.test_id, COUNT(DISTINCT bl.loc) AS distinct_barrier_locations
                FROM mismatches m
                CROSS JOIN LATERAL unnest(m.barrier_ids) AS u(barrier_id)
                JOIN barrier_locations bl ON bl.feature_id = u.barrier_id
                GROUP BY m.id, m.test_id
            )
            SELECT m.id, m.test_id, m.reported_count,
                   COALESCE(lc.distinct_barrier_locations, 0) AS distinct_barrier_locations,
                   m.barrier_ids, m.geometry
            FROM mismatches m
            LEFT JOIN loc_counts lc ON lc.id = m.id AND lc.test_id = m.test_id
            WHERE m.reported_count <> COALESCE(lc.distinct_barrier_locations, 0)"""


def ranking_checks(args: argparse.Namespace, sp: str, st: str, barriers: str) -> list[Check]:
	"""R01-R10 for one ranked table: ranked_barriers_<species>_<lifestage>_<plan_code>.
	Ranked rows are joined to the model barriers view to pick up barrier geometry."""
	ranked = relation(args.wcrp_schema, args.ranked_table.format(species=sp, lifestage=st, plan_code=args.plan_code))
	# spawnrear is passable/impassable through both component lifestages.
	parts = ["spawn", "rear"] if st == "spawnrear" else [st]

	def cid(base):
		return f"{base}_{sp}_{st}"

	def label(text):
		return f"{text} [{sp} {st}]"

	def ranked_rows(where: str) -> str:
		return (
			f"SELECT r.*, b.snapped_geometry AS barrier_geometry FROM {ranked} r "
			f"LEFT JOIN {barriers} b ON b.feature_id=r.feature_id WHERE {where}"
		)

	r01 = " OR ".join(f"r.{sp}_{p}_passability IS DISTINCT FROM b.passability_status_{p}" for p in parts)
	r10 = " AND ".join(f"r.{sp}_{p}_passability=1" for p in parts)
	return [
		Check(
			cid("R01"),
			label("Ranked passability differs from source model"),
			f"SELECT r.*, b.snapped_geometry AS barrier_geometry FROM {ranked} r JOIN {barriers} b ON b.feature_id=r.feature_id WHERE {r01}",
			"Rankings",
		),
		Check(
			cid("R02"),
			label("Ranked barrier missing from model output"),
			# No model barrier exists, so only geometry stored on the ranked table (if any) is available.
			f"SELECT r.* FROM {ranked} r LEFT JOIN {barriers} b ON b.feature_id=r.feature_id WHERE b.feature_id IS NULL",
			"Rankings",
		),
		Check(
			cid("R03"),
			label(f"Ranked {st} barrier has no upstream {st} habitat"),
			f"SELECT r.*, b.snapped_geometry AS barrier_geometry FROM {ranked} r JOIN {barriers} b ON b.feature_id=r.feature_id WHERE COALESCE(b.{st}_upstream_length,0)<=0",
			"Rankings",
		),
		Check(
			cid("R04"),
			label("Negative ranking gain"),
			ranked_rows(
				f"r.total_{st}_hab_gain_group_km<0 OR r.w_total_hab_gain_group_km<0 OR r.avg_gain_per_barrier_km<0 OR r.w_avg_gain_per_barrier_km<0"
			),
			"Rankings",
		),
		Check(
			cid("R05"),
			label("Reported group size differs from actual membership"),
			# Returns every barrier in each mismatched group.
			f"""WITH actual AS (SELECT group_id, COUNT(*) n FROM {ranked} GROUP BY group_id),
            bad AS (
                SELECT r.group_id FROM {ranked} r JOIN actual a USING(group_id)
                GROUP BY r.group_id, a.n
                HAVING MIN(r.num_barriers_group)<>a.n OR MIN(r.num_barriers_group)<>MAX(r.num_barriers_group)
            )
            SELECT r.*, b.snapped_geometry AS barrier_geometry
            FROM {ranked} r
            JOIN bad ON bad.group_id=r.group_id
            LEFT JOIN {barriers} b ON b.feature_id=r.feature_id
            ORDER BY r.group_id""",
			"Rankings",
		),
		Check(
			cid("R06"),
			label("Average gain math mismatch"),
			ranked_rows(
				f"ABS(r.avg_gain_per_barrier_km-r.total_{st}_hab_gain_group_km/NULLIF(r.num_barriers_group,0))>{TOL}"
			),
			"Rankings",
		),
		Check(
			cid("R07"),
			label("Weighted average gain math mismatch"),
			ranked_rows(
				f"ABS(r.w_avg_gain_per_barrier_km-r.w_total_hab_gain_group_km/NULLIF(r.num_barriers_group,0))>{TOL}"
			),
			"Rankings",
		),
		Check(
			cid("R08"),
			label("Positive weighted average gain is missing tiered rank"),
			ranked_rows("r.w_avg_gain_per_barrier_km > 0 AND r.rank_w_avg_gain_tiered IS NULL"),
			"Rankings",
		),
		Check(
			cid("R09"),
			label("Any required ranking fields are null"),
			ranked_rows(f"r.group_id IS NULL OR r.rank_w_total_upstr_{st}_hab IS NULL OR r.rank_combined IS NULL"),
			"Rankings",
		),
		Check(
			cid("R10"),
			label(f"Fully passable {st} barrier appears in ranking (review exceptions)"),
			ranked_rows(r10),
			"Rankings",
		),
	]


def species_checks(args: argparse.Namespace, sp: str, cfg: dict[str, Any]) -> list[Check]:
	"""All species-specific checks for one target species, limited to its reported lifestages."""

	def name(template: str) -> str:
		return template.format(species=sp, plan_code=args.plan_code)

	streams = relation(args.output_schema, name(args.streams_view))
	barriers = relation(args.output_schema, name(args.barriers_view))
	natural = relation(args.output_schema, name(args.natural_barriers_view))
	combined = relation(args.wcrp_schema, "combined_output_table_vw")
	model_streams = relation(args.output_schema, args.model_streams_table)
	stages = cfg["lifestages"]
	staged = [st for st in ("spawn", "rear") if st in stages]
	# Accessibility values come from graph_stats (see load_accessibility_values), never hardcoded.
	accessible = qliteral(args.accessibility_accessible)
	inaccessible = qliteral(args.accessibility_inaccessible)

	def cid(base: str) -> str:
		return f"{base}_{sp}"

	def label(text: str) -> str:
		return f"{text} [{sp}]"

	# Original check IDs per lifestage: (spawn_id, rear_id)
	ids = {
		"A_ninc": ("A01", "A03"),
		"A_acc": ("A02", "A04"),
		"A_hab": ("A05", "A06"),
		"L_nonhab": ("L01", "L02"),
		"L_sum": ("L03", "L04"),
		"L_inv": ("L05", "L06"),
		"B_func": ("B01", "B02"),
		"B_pass": ("B04", "B05"),
		"B_imp": ("B06", "B07"),
		"I_up": ("I01", "I02"),
		"I_down": ("I03", "I04"),
		"H": ("H01", "H02"),
	}
	c: list[Check] = []
	for st in staged:
		i = 0 if st == "spawn" else 1
		S = st.capitalize()
		c += [
			Check(
				cid(ids["A_ninc"][i]),
				label(f"{S} inaccessible with zero downstream natural barriers"),
				rows_sql(streams, f"{st}_accessibility={inaccessible} AND downstream_natural_{st}_count=0"),
				"Accessibility",
			),
			Check(
				cid(ids["A_acc"][i]),
				label(f"{S} accessible with downstream natural barriers"),
				rows_sql(streams, f"{st}_accessibility={accessible} AND downstream_natural_{st}_count>0"),
				"Accessibility",
			),
			Check(
				cid(ids["A_hab"][i]),
				label(f"Inaccessible {st}ing segments marked as habitat"),
				rows_sql(streams, f"{st}_accessibility={inaccessible} AND {st}_habitat IS TRUE"),
				"Accessibility",
			),
			Check(
				cid(ids["L_nonhab"][i]),
				label(f"{S} non-habitat has non-zero weighted length"),
				rows_sql(streams, f"{st}_habitat IS FALSE AND ABS(COALESCE({st}_weighted_length,0))>{TOL}"),
				"Stream lengths",
			),
			Check(
				cid(ids["L_sum"][i]),
				label(f"{S} connected + disconnected differs from weighted length"),
				rows_sql(
					streams,
					f"ABS(COALESCE({st}_weighted_connected_length,0)+COALESCE({st}_weighted_disconnected_length,0)-COALESCE({st}_weighted_length,0))>{TOL}",
				),
				"Stream lengths",
			),
			Check(
				cid(ids["L_inv"][i]),
				label(f"Invalid {st} connected/disconnected weighted lengths"),
				rows_sql(
					streams,
					f"{st}_weighted_length<-{TOL} OR {st}_weighted_connected_length<-{TOL} OR {st}_weighted_disconnected_length<-{TOL} OR {st}_weighted_connected_length>{st}_weighted_length+{TOL} OR {st}_weighted_disconnected_length>{st}_weighted_length+{TOL}",
				),
				"Stream lengths",
			),
			Check(
				cid(ids["B_func"][i]),
				label(f"{S} functional upstream exceeds total upstream"),
				rows_sql(barriers, f"{st}_functional_upstream_length>{st}_upstream_length+{TOL}"),
				"Barrier calculations",
			),
			Check(
				cid(ids["B_pass"][i]),
				label(f"Fully passable {st} barrier has disconnected upstream habitat"),
				rows_sql(
					barriers,
					f"passability_status_{st}=1 AND ABS(COALESCE({st}_weighted_disconnected_upstream_length,0))>{TOL}",
				),
				"Barrier calculations",
			),
			Check(
				cid(ids["B_imp"][i]),
				label(f"Fully impassable {st} barrier has connected upstream habitat"),
				rows_sql(
					barriers,
					f"passability_status_{st}=0 AND ABS(COALESCE({st}_weighted_connected_upstream_length,0))>{TOL}",
				),
				"Barrier calculations",
			),
			Check(
				cid(ids["I_up"][i]),
				label(f"Upstream {st} count differs from distinct final barrier locations"),
				id_count_sql(
					streams, combined, [(ids["I_up"][i], f"upstream_anthro_{st}_count", f"upstream_anthro_{st}_ids")]
				),
				"Barrier IDs",
			),
			Check(
				cid(ids["I_down"][i]),
				label(f"Downstream {st} count differs from distinct final barrier locations"),
				id_count_sql(
					streams,
					combined,
					[(ids["I_down"][i], f"downstream_anthro_{st}_count", f"downstream_anthro_{st}_ids")],
				),
				"Barrier IDs",
			),
		]
		# Habitat parameter check is skipped (not failed) if any threshold is missing for this species/lifestage.
		t = cfg["thresholds"][st]
		t_ok = all(v is not None for v in t.values())
		c.append(
			Check(
				cid(ids["H"][i]),
				label(f"{S} habitat violates configured gradient/Strahler limits"),
				rows_sql(
					streams,
					f"{st}_habitat IS TRUE AND (segment_gradient<{t['min_gradient']} OR segment_gradient>={t['max_gradient']} OR strahler_order<{t['min_strahler']} OR strahler_order>={t['max_strahler']})",
				)
				if t_ok
				else "",
				"Habitat parameters",
				enabled=lambda _, ok=t_ok: ok,
				skip_reason=f"Species {sp!r} is missing one or more {st} gradient/Strahler parameters.",
			)
		)
	if "spawnrear" in stages:
		c.append(
			Check(
				cid("B03"),
				label("Spawnrear functional upstream exceeds total upstream"),
				rows_sql(barriers, f"spawnrear_functional_upstream_length>spawnrear_upstream_length+{TOL}"),
				"Barrier calculations",
			)
		)
	if staged:
		out_of_range = " OR ".join(f"passability_status_{st}<0 OR passability_status_{st}>1" for st in staged)
		c.append(
			Check(
				cid("B08"),
				label("Passability value outside 0 to 1"),
				rows_sql(barriers, out_of_range),
				"Barrier calculations",
			)
		)
		c.append(
			Check(
				cid("I05"),
				label("Count/ID mismatches not explained by coincident final barrier locations"),
				id_count_sql(
					streams,
					combined,
					[
						(
							ids[key][0 if st == "spawn" else 1],
							f"{direction}_anthro_{st}_count",
							f"{direction}_anthro_{st}_ids",
						)
						for st in staged
						for key, direction in (("I_up", "upstream"), ("I_down", "downstream"))
					],
				),
				"Barrier IDs",
			)
		)
	c += [
		Check(
			cid("L07"),
			label("Effective length negative or greater than raw length"),
			rows_sql(streams, f"effective_length<-{TOL} OR effective_length>length+{TOL}"),
			"Stream lengths",
		),
		Check(
			cid("L08"),
			label("Effective length is neither zero nor raw length"),
			rows_sql(streams, f"ABS(effective_length)>{TOL} AND ABS(effective_length-length)>{TOL}"),
			"Stream lengths",
		),
		Check(
			cid("L09"),
			label("Negative raw stream length"),
			rows_sql(streams, "length<0"),
			"Stream lengths",
		),
		Check(
			cid("G01"),
			label("Gradient barrier located on a 3+ segment node (confluence/divergence)"),
			f"""WITH gradient_nb AS (
                  SELECT nb.feature_id, nb.geometry, nb.snapped_geometry
                  FROM {natural} nb where feature_type = 'gradients'
              ), barrier_degree AS (
                  SELECT b.feature_id, b.geometry, b.snapped_geometry,
                         count(*) FILTER (WHERE ST_DWithin(ST_EndPoint(s.geometry),   b.snapped_geometry, {NODE_TOL})) AS n_in,
                         count(*) FILTER (WHERE ST_DWithin(ST_StartPoint(s.geometry), b.snapped_geometry, {NODE_TOL})) AS n_out
                  FROM gradient_nb b
                  JOIN {model_streams} s ON ST_DWithin(s.geometry, b.snapped_geometry, {NODE_TOL})
                  GROUP BY b.feature_id, b.geometry, b.snapped_geometry
              )
              SELECT bd.feature_id,
                     bd.n_in,
                     bd.n_out,
                     round(ST_Distance(bd.geometry::geography, bd.snapped_geometry::geography)::numeric, 3) AS snap_shift_m,
                     e.edge_id AS original_nearest_edge,
                     round(e.dist_m::numeric, 3) AS original_dist_to_edge_m,
                     bd.snapped_geometry,
                     bd.geometry AS original_geometry
              FROM barrier_degree bd
              LEFT JOIN LATERAL (
                  SELECT s.id AS edge_id,
                         ST_Distance(s.geometry::geography, bd.geometry::geography) AS dist_m
                  FROM {model_streams} s
                  WHERE ST_DWithin(s.geometry, bd.snapped_geometry, {NODE_TOL})   -- edges touching this node
                  ORDER BY s.geometry <-> bd.geometry
                  LIMIT 1
              ) e ON true
              WHERE bd.n_in + bd.n_out >= 3
              ORDER BY snap_shift_m DESC""",
			"Gradient barriers",
		),
	]
	# One set of ranking checks per reported lifestage: ranked_barriers_<species>_<lifestage>_<plan_code>.
	if not args.skip_rankings:
		for st in stages:
			c += ranking_checks(args, sp, st, barriers)
	return c


def checks(args: argparse.Namespace) -> list[Check]:
	c: list[Check] = []
	for sp, cfg in args.species.items():
		c += species_checks(args, sp, cfg)
	return sorted(c, key=lambda ch: (CATEGORY_ORDER.index(ch.category), ch.check_id.split("_", 1)[0], ch.check_id))


def connect(args: argparse.Namespace):
	load_db_driver()
	params = dict(host=args.host, port=args.port, dbname=args.dbname, user=args.user, password=args.password)
	return psycopg.connect(**params, connect_timeout=args.connect_timeout)


def geometry_type_oids(conn) -> set[int]:
	"""OIDs of the PostGIS geometry/geography types, used to detect spatial columns in check results."""
	with conn.cursor() as cur:
		cur.execute("SELECT oid FROM pg_type WHERE typname IN ('geometry', 'geography')")
		oids = {int(r[0]) for r in cur.fetchall()}
	conn.commit()
	return oids


def qgis_select(check_sql: str, columns: list[tuple[str, int]], geom_oids: set[int]) -> tuple[str, bool]:
	"""Wrap a check query so geometry is returned as WKT text QGIS can read.
	The primary geometry becomes "wkt" + "srid" (first columns); other geometries become <name>_wkt."""
	geom_cols = [n for n, t in columns if t in geom_oids]
	primary = next((g for g in GEOM_PREFERENCE if g in geom_cols), geom_cols[0] if geom_cols else None)
	parts: list[str] = []
	if primary:
		parts += [f"ST_AsText(q.{qident(primary)}) AS wkt", f"ST_SRID(q.{qident(primary)}) AS srid"]
	for n, _ in columns:
		if n == primary:
			continue
		if n in geom_cols:
			parts.append(f"ST_AsText(q.{qident(n)}) AS {qident(n + '_wkt')}")
		else:
			parts.append(f"q.{qident(n)}")
	return f"SELECT {', '.join(parts)} FROM ({check_sql}) q", primary is not None


def execute(conn, check: Check, csv_path: Path, geom_oids: set[int]) -> tuple[str, int, float, str]:
	"""Run a check; if it returns rows, stream every failing row to csv_path. No file is written on PASS/ERROR."""
	started = time.perf_counter()
	failures = 0
	f = None
	detail = ""
	try:
		with conn.cursor() as cur:
			cur.execute("SET TRANSACTION READ ONLY")
			cur.execute(f"SET LOCAL statement_timeout = '{int(args_global.statement_timeout * 1000)}ms'")
			# Describe result columns without running the full query, then wrap with WKT conversion.
			cur.execute(f"SELECT * FROM ({check.sql}) q LIMIT 0")
			columns = [(d[0], int(d[1])) for d in cur.description]
			sql, has_geom = qgis_select(check.sql, columns, geom_oids)
			if not has_geom:
				detail = "No geometry column in result; failure CSV is non-spatial."
			cur.execute(sql)
			header = [d[0] for d in cur.description]
			while True:
				batch = cur.fetchmany(FETCH_BATCH)
				if not batch:
					break
				if f is None:  # only create the file once a failing row exists
					f = csv_path.open("w", newline="", encoding="utf-8")
					w = csv.writer(f)
					w.writerow(header)
				w.writerows(batch)
				failures += len(batch)
		conn.commit()
		if f is not None:
			f.close()
		status = "PASS" if failures == 0 else "FAIL"
		return status, failures, time.perf_counter() - started, detail if failures else ""
	except Exception as exc:
		conn.rollback()
		if f is not None:
			f.close()
			csv_path.unlink(missing_ok=True)  # don't leave a partial failure file behind
		return "ERROR", 0, time.perf_counter() - started, str(exc).strip()


def parse_args() -> argparse.Namespace:
	p = argparse.ArgumentParser(description="Run automated FishPass ASF QA checks.")
	p.add_argument("--host", default=os.getenv("FISHPASS_HOST"))
	p.add_argument("--port", type=int, default=int(os.getenv("FISHPASS_PORT", "5432")))
	p.add_argument("--dbname", default=os.getenv("FISHPASS_DBNAME"))
	p.add_argument("--user", default=os.getenv("FISHPASS_USER"))
	p.add_argument("--password", default=os.getenv("FISHPASS_PASSWORD"))
	p.add_argument("--model-plan", required=True, help="Path to the model plan YAML, e.g. asf_phase1.yaml.")
	p.add_argument("--species-parameters", required=True, help="Path to fish_species_parameters.yaml.")
	p.add_argument(
		"--species-code", help="Optional: check only this target species (default: all target_species in the plan)."
	)
	p.add_argument("--output-schema", help="Override output_schema from the model plan.")
	p.add_argument("--streams-view", help="Override streams view name template (default streams_{species}).")
	p.add_argument(
		"--barriers-view", help="Override barriers view name template (default anthropogenic_barriers_{species})."
	)
	p.add_argument("--wcrp-schema", help="Override derived <plan_code>_wcrp schema.")
	p.add_argument(
		"--ranked-table",
		help="Override ranked table name template (default ranked_barriers_{species}_{lifestage}_{plan_code}).",
	)
	p.add_argument(
		"--natural-barriers-view",
		default="natural_barriers_{species}",
		help="Natural barriers view name template in output_schema; {species} is replaced by each target_species code.",
	)
	p.add_argument(
		"--model-streams-table",
		default="streams",
		help="Unsplit-by-species streams table in output_schema used for node degree checks.",
	)
	p.add_argument(
		"--gradient-barriers-table",
		default="support.gradient_barriers",
		help="Schema-qualified source gradient barriers table.",
	)
	p.add_argument(
		"--engine-scripts-dir",
		default=os.getenv("FISHPASS_ENGINE_SCRIPTS_DIR", str(DEFAULT_ENGINE_SCRIPTS_DIR)),
		help="Folder containing graph_stats.py (default: ../fishpass_engine/scripts relative to this script).",
	)
	p.add_argument("--skip-rankings", action="store_true")
	p.add_argument("--statement-timeout", type=int, default=600, help="Per-check timeout in seconds.")
	p.add_argument("--connect-timeout", type=int, default=15)
	p.add_argument("--report-dir", default="qa_reports")
	p.add_argument("--list", action="store_true", help="List checks without connecting.")
	p.add_argument("--fail-on-failure", action="store_true", help="Exit 1 if any check fails/errors; useful in CI.")
	a = p.parse_args()
	a = apply_yaml_config(a)
	a.accessibility_accessible, a.accessibility_inaccessible = load_accessibility_values(a.engine_scripts_dir)
	return a


def main() -> int:
	global args_global
	args_global = parse_args()
	all_checks = checks(args_global)
	if args_global.list:
		for ch in all_checks:
			state = "enabled" if ch.enabled(args_global) else "skipped"
			print(f"{ch.check_id:<16}  {state:<7}  {ch.category}: {ch.name}")
		return 0
	missing = [n for n in ("host", "dbname", "user", "password") if not getattr(args_global, n)]
	if missing:
		print("ERROR: Missing connection settings: " + ", ".join(missing), file=sys.stderr)
		print("Set FISHPASS_HOST/DBNAME/USER/PASSWORD or pass command-line arguments.", file=sys.stderr)
		return 2
	report_dir = Path(args_global.report_dir)
	report_dir.mkdir(parents=True, exist_ok=True)
	stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
	# Failure CSVs go in a per-run folder so runs never overwrite each other; created only if something fails.
	failures_dir = report_dir / f"fishpass_qa_{stamp}_failures"
	results: list[dict[str, Any]] = []
	conn = connect(args_global)
	try:
		geom_oids = geometry_type_oids(conn)
		print(
			f"FishPass QA | database={args_global.dbname} | schema={args_global.output_schema} | species="
			+ "; ".join(
				f"{sp} ({', '.join(cfg['lifestages']) or 'no lifestages'})" for sp, cfg in args_global.species.items()
			)
		)
		print(f"Plan: {args_global.model_plan}")
		print(f"Species parameters: {args_global.species_parameters}")
		print("=" * 88)
		for ch in all_checks:
			failure_csv = None
			if not ch.enabled(args_global):
				result = dict(
					id=ch.check_id,
					category=ch.category,
					name=ch.name,
					status="SKIP",
					failures=0,
					duration_seconds=0,
					detail=ch.skip_reason,
				)
			else:
				failures_dir.mkdir(parents=True, exist_ok=True)
				failure_csv = failures_dir / f"{ch.check_id}_failures.csv"
				status, failures, duration, detail = execute(conn, ch, failure_csv, geom_oids)
				result = dict(
					id=ch.check_id,
					category=ch.category,
					name=ch.name,
					status=status,
					failures=failures,
					duration_seconds=round(duration, 3),
					detail=detail,
				)
			results.append(result)
			count = "" if result["status"] in ("PASS", "SKIP") else f" ({result['failures']} failing row(s))"
			print(f"[{result['status']:<5}] {ch.check_id} {ch.name}{count}")
			if result["detail"]:
				print(f"        {result['detail']}")
			if result["status"] == "FAIL" and failure_csv is not None:
				print(f"        rows: {failure_csv}")
	finally:
		conn.close()
		# Remove the failures folder if no check failed (it is only created to hold failure CSVs).
		if failures_dir.exists() and not any(failures_dir.iterdir()):
			failures_dir.rmdir()
	counts = {s: sum(r["status"] == s for r in results) for s in ["PASS", "FAIL", "ERROR", "SKIP"]}
	csv_path = report_dir / f"fishpass_qa_{stamp}.csv"
	with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
		w = csv.DictWriter(f, fieldnames=["id", "category", "name", "status", "failures", "duration_seconds", "detail"])
		w.writeheader()
		for row in results:
			w.writerow({k: row[k] for k in w.fieldnames})
	print("=" * 88)
	print("Summary: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
	print(f"CSV report:   {csv_path}")
	if failures_dir.exists():
		print(f"Failure CSVs: {failures_dir}")
	if args_global.fail_on_failure and (counts["FAIL"] or counts["ERROR"]):
		return 1
	return 0


args_global: argparse.Namespace

if __name__ == "__main__":
	raise SystemExit(main())
