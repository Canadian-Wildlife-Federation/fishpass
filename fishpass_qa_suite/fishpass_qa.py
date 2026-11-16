#!/usr/bin/env python3
"""Automated QA suite for ASF Phase 1 FishPass model outputs.

Runs read-only SQL checks, prints a concise PASS/FAIL/SKIP report, and writes
CSV and JSON reports. A check passes when its query returns zero failing rows,
unless it is explicitly a summary check.
"""

from __future__ import annotations

import argparse
import csv
import json
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


@dataclass(frozen=True)
class Check:
	check_id: str
	name: str
	sql: str
	category: str
	mode: str = "zero_count"  # zero_count, zero_rows, summary
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
	"Satellite propagation",
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

	args.loaded_config = {
		"model_plan": args.model_plan,
		"species_parameters": args.species_parameters,
		"plan_code": plan_code,
		"output_schema": args.output_schema,
		"target_species": target_species,
		"checked_species": selected,
		"reporting_values": reporting_values,
		"species": args.species,
	}
	return args


def qident(name: str) -> str:
	"""Safely quote a schema/table/view identifier supplied through CLI."""
	return '"' + name.replace('"', '""') + '"'


def relation(schema: str, table: str) -> str:
	return f"{qident(schema)}.{qident(table)}"


def id_count_sql(streams: str, combined: str, columns: list[tuple[str, str, str]]) -> str:
	"""Count/ID mismatches not explained by coincident final barrier locations.
	columns = [(test_id, count_column, ids_column), ...]"""
	values = ",\n                    ".join(f"('{t}', s.{cnt}, s.{ids})" for t, cnt, ids in columns)
	return f"""WITH barrier_locations AS (
                SELECT DISTINCT feature_id, snapped_geometry FROM {combined} WHERE feature_id IS NOT NULL AND snapped_geometry IS NOT NULL
            ), mismatches AS (
                SELECT s.id, v.test_id, v.reported_count, v.barrier_ids
                FROM {streams} s
                CROSS JOIN LATERAL (VALUES
                    {values}
                ) AS v(test_id, reported_count, barrier_ids)
                WHERE v.reported_count <> COALESCE(cardinality(v.barrier_ids), 0)
            )
            SELECT COUNT(*) FROM mismatches m
            WHERE m.reported_count <> (
                SELECT COUNT(DISTINCT bl.snapped_geometry) FROM unnest(m.barrier_ids) AS barrier_id
                JOIN barrier_locations bl ON bl.feature_id = barrier_id
            )"""


def ranking_checks(args: argparse.Namespace, sp: str, st: str, barriers: str) -> list[Check]:
	"""R01-R10 for one ranked table: ranked_barriers_<species>_<lifestage>_<plan_code>."""
	ranked = relation(args.wcrp_schema, args.ranked_table.format(species=sp, lifestage=st, plan_code=args.plan_code))
	# spawnrear is passable/impassable through both component lifestages.
	parts = ["spawn", "rear"] if st == "spawnrear" else [st]

	def cid(base):
		return f"{base}_{sp}_{st}"

	def label(text):
		return f"{text} [{sp} {st}]"

	r01 = " OR ".join(f"r.{sp}_{p}_passability IS DISTINCT FROM b.passability_status_{p}" for p in parts)
	r10 = " AND ".join(f"{sp}_{p}_passability=1" for p in parts)
	return [
		Check(
			cid("R01"),
			label("Ranked passability differs from source model"),
			f"SELECT r.feature_id FROM {ranked} r JOIN {barriers} b ON b.feature_id=r.feature_id WHERE {r01}",
			"Rankings",
			mode="zero_rows",
		),
		Check(
			cid("R02"),
			label("Ranked barrier missing from model output"),
			f"SELECT r.feature_id FROM {ranked} r LEFT JOIN {barriers} b ON b.feature_id=r.feature_id WHERE b.feature_id IS NULL",
			"Rankings",
			mode="zero_rows",
		),
		Check(
			cid("R03"),
			label(f"Ranked {st} barrier has no upstream {st} habitat"),
			f"SELECT r.feature_id FROM {ranked} r JOIN {barriers} b ON b.feature_id=r.feature_id WHERE COALESCE(b.{st}_upstream_length,0)<=0",
			"Rankings",
			mode="zero_rows",
		),
		Check(
			cid("R04"),
			label("Negative ranking gain"),
			f"SELECT feature_id FROM {ranked} WHERE total_{st}_hab_gain_group_km<0 OR w_total_hab_gain_group_km<0 OR avg_gain_per_barrier_km<0 OR w_avg_gain_per_barrier_km<0",
			"Rankings",
			mode="zero_rows",
		),
		Check(
			cid("R05"),
			label("Reported group size differs from actual membership"),
			f"WITH actual AS (SELECT group_id,COUNT(*) n FROM {ranked} GROUP BY group_id) SELECT r.group_id FROM {ranked} r JOIN actual a USING(group_id) GROUP BY r.group_id,a.n HAVING MIN(r.num_barriers_group)<>a.n OR MIN(r.num_barriers_group)<>MAX(r.num_barriers_group)",
			"Rankings",
			mode="zero_rows",
		),
		Check(
			cid("R06"),
			label("Average gain math mismatch"),
			f"SELECT feature_id FROM {ranked} WHERE ABS(avg_gain_per_barrier_km-total_{st}_hab_gain_group_km/NULLIF(num_barriers_group,0))>{TOL}",
			"Rankings",
			mode="zero_rows",
		),
		Check(
			cid("R07"),
			label("Weighted average gain math mismatch"),
			f"SELECT feature_id FROM {ranked} WHERE ABS(w_avg_gain_per_barrier_km-w_total_hab_gain_group_km/NULLIF(num_barriers_group,0))>{TOL}",
			"Rankings",
			mode="zero_rows",
		),
		Check(
			cid("R08"),
			label("Positive weighted average gain is missing tiered rank"),
			f"SELECT feature_id FROM {ranked} WHERE w_avg_gain_per_barrier_km > 0 AND rank_w_avg_gain_tiered IS NULL",
			"Rankings",
			mode="zero_rows",
		),
		Check(
			cid("R09"),
			label("Any required ranking fields are null"),
			f"SELECT feature_id FROM {ranked} WHERE group_id IS NULL OR rank_w_total_upstr_{st}_hab IS NULL OR rank_combined IS NULL",
			"Rankings",
			mode="zero_rows",
		),
		Check(
			cid("R10"),
			label(f"Fully passable {st} barrier appears in ranking (review exceptions)"),
			f"SELECT feature_id FROM {ranked} WHERE {r10}",
			"Rankings",
			mode="zero_rows",
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
				f"SELECT COUNT(*) FROM {streams} WHERE {st}_accessibility='not accessible' AND downstream_natural_{st}_count=0",
				"Accessibility",
			),
			Check(
				cid(ids["A_acc"][i]),
				label(f"{S} accessible with downstream natural barriers"),
				f"SELECT COUNT(*) FROM {streams} WHERE {st}_accessibility='accessible' AND downstream_natural_{st}_count>0",
				"Accessibility",
			),
			Check(
				cid(ids["A_hab"][i]),
				label(f"Inaccessible {st}ing segments marked as habitat"),
				f"SELECT COUNT(*) FROM {streams} WHERE {st}_accessibility='not accessible' AND {st}_habitat IS TRUE",
				"Accessibility",
			),
			Check(
				cid(ids["L_nonhab"][i]),
				label(f"{S} non-habitat has non-zero weighted length"),
				f"SELECT COUNT(*) FROM {streams} WHERE {st}_habitat IS FALSE AND ABS(COALESCE({st}_weighted_length,0))>{TOL}",
				"Stream lengths",
			),
			Check(
				cid(ids["L_sum"][i]),
				label(f"{S} connected + disconnected differs from weighted length"),
				f"SELECT COUNT(*) FROM {streams} WHERE ABS(COALESCE({st}_weighted_connected_length,0)+COALESCE({st}_weighted_disconnected_length,0)-COALESCE({st}_weighted_length,0))>{TOL}",
				"Stream lengths",
			),
			Check(
				cid(ids["L_inv"][i]),
				label(f"Invalid {st} connected/disconnected weighted lengths"),
				f"SELECT COUNT(*) FROM {streams} WHERE {st}_weighted_length<-{TOL} OR {st}_weighted_connected_length<-{TOL} OR {st}_weighted_disconnected_length<-{TOL} OR {st}_weighted_connected_length>{st}_weighted_length+{TOL} OR {st}_weighted_disconnected_length>{st}_weighted_length+{TOL}",
				"Stream lengths",
			),
			Check(
				cid(ids["B_func"][i]),
				label(f"{S} functional upstream exceeds total upstream"),
				f"SELECT COUNT(*) FROM {barriers} WHERE {st}_functional_upstream_length>{st}_upstream_length+{TOL}",
				"Barrier calculations",
			),
			Check(
				cid(ids["B_pass"][i]),
				label(f"Fully passable {st} barrier has disconnected upstream habitat"),
				f"SELECT COUNT(*) FROM {barriers} WHERE passability_status_{st}=1 AND ABS(COALESCE({st}_weighted_disconnected_upstream_length,0))>{TOL}",
				"Barrier calculations",
			),
			Check(
				cid(ids["B_imp"][i]),
				label(f"Fully impassable {st} barrier has connected upstream habitat"),
				f"SELECT COUNT(*) FROM {barriers} WHERE passability_status_{st}=0 AND ABS(COALESCE({st}_weighted_connected_upstream_length,0))>{TOL}",
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
				f"SELECT COUNT(*) FROM {streams} WHERE {st}_habitat IS TRUE AND (segment_gradient<{t['min_gradient']} OR segment_gradient>={t['max_gradient']} OR strahler_order<{t['min_strahler']} OR strahler_order>={t['max_strahler']})"
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
				f"SELECT COUNT(*) FROM {barriers} WHERE spawnrear_functional_upstream_length>spawnrear_upstream_length+{TOL}",
				"Barrier calculations",
			)
		)

	if staged:
		out_of_range = " OR ".join(f"passability_status_{st}<0 OR passability_status_{st}>1" for st in staged)
		c.append(
			Check(
				cid("B08"),
				label("Passability value outside 0 to 1"),
				f"SELECT COUNT(*) FROM {barriers} WHERE {out_of_range}",
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
			f"SELECT COUNT(*) FROM {streams} WHERE effective_length<-{TOL} OR effective_length>length+{TOL}",
			"Stream lengths",
		),
		Check(
			cid("L08"),
			label("Effective length is neither zero nor raw length"),
			f"SELECT COUNT(*) FROM {streams} WHERE ABS(effective_length)>{TOL} AND ABS(effective_length-length)>{TOL}",
			"Stream lengths",
		),
		Check(
			cid("L09"),
			label("Negative raw stream length"),
			f"SELECT COUNT(*) FROM {streams} WHERE length<0",
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
                     ST_AsText(bd.geometry) AS original_geometry_wkt,
                     ST_AsText(bd.snapped_geometry) AS snapped_geometry_wkt
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
			mode="zero_rows",
		),
	]

	# One set of ranking checks per reported lifestage: ranked_barriers_<species>_<lifestage>_<plan_code>.
	if not args.skip_rankings:
		for st in stages:
			c += ranking_checks(args, sp, st, barriers)

	# Requires the user to load the latest processed-review export into fishpass_dev.
	if not args.skip_satellite:
		fdw = relation(args.cabd_fdw_schema, args.cabd_fdw_table)
		staging = relation(args.satellite_schema, args.satellite_table)
		c.append(
			Check(
				cid("S03"),
				label("Processed CABD crossing missing from FishPass"),
				f"WITH f AS (SELECT DISTINCT cabd_id FROM {fdw}) SELECT r.cabd_id FROM {staging} r JOIN f ON f.cabd_id=r.cabd_id LEFT JOIN {barriers} b ON b.feature_id=r.cabd_id WHERE b.feature_id IS NULL",
				"Satellite propagation",
				mode="zero_rows",
			)
		)
	return c


def checks(args: argparse.Namespace) -> list[Check]:
	c: list[Check] = []
	for sp, cfg in args.species.items():
		c += species_checks(args, sp, cfg)

	# Satellite checks S01/S02 are not species-specific, so they run once.
	if not args.skip_satellite:
		fdw = relation(args.cabd_fdw_schema, args.cabd_fdw_table)
		staging = relation(args.satellite_schema, args.satellite_table)
		c += [
			Check(
				"S01",
				"Satellite-review crossing type differs from CABD FDW",
				f"WITH f AS (SELECT DISTINCT cabd_id,crossing_type_code FROM {fdw}) SELECT r.cabd_id FROM {staging} r JOIN f ON f.cabd_id=r.cabd_id WHERE r.crossing_type_code IS DISTINCT FROM f.crossing_type_code",
				"Satellite propagation",
				mode="zero_rows",
			),
			Check(
				"S02",
				"Satellite-review record missing from CABD FDW",
				f"WITH f AS (SELECT DISTINCT cabd_id FROM {fdw}) SELECT r.cabd_id FROM {staging} r LEFT JOIN f ON f.cabd_id=r.cabd_id WHERE f.cabd_id IS NULL",
				"Satellite propagation",
				mode="zero_rows",
			),
		]

	# Report in category order, then check ID (e.g. A01_as, A01_ae, A02_as ...).
	return sorted(c, key=lambda ch: (CATEGORY_ORDER.index(ch.category), ch.check_id.split("_", 1)[0], ch.check_id))


def connect(args: argparse.Namespace):
	load_db_driver()
	params = dict(host=args.host, port=args.port, dbname=args.dbname, user=args.user, password=args.password)
	if DRIVER == "psycopg":
		return psycopg.connect(**params, connect_timeout=args.connect_timeout)
	return psycopg.connect(**params, connect_timeout=args.connect_timeout)


def execute(conn, check: Check, sample_limit: int) -> tuple[str, int, list[Any], float, str]:
	started = time.perf_counter()
	try:
		with conn.cursor() as cur:
			cur.execute("SET TRANSACTION READ ONLY")
			cur.execute(f"SET LOCAL statement_timeout = '{int(args_global.statement_timeout * 1000)}ms'")
			cur.execute(check.sql)
			rows = cur.fetchmany(sample_limit + 1)
			if check.mode == "zero_count":
				failures = int(rows[0][0]) if rows else 0
				samples: list[Any] = []
			elif check.mode == "zero_rows":
				failures = len(rows)
				samples = rows[:sample_limit]
				if len(rows) > sample_limit:
					failures = sample_limit + 1  # reported as at least sample_limit+1
			else:
				failures, samples = 0, rows[:sample_limit]
		conn.commit()
		status = "PASS" if failures == 0 else "FAIL"
		return status, failures, samples, time.perf_counter() - started, ""
	except Exception as exc:
		conn.rollback()
		return "ERROR", 0, [], time.perf_counter() - started, str(exc).strip()


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
	p.add_argument("--cabd-fdw-schema", default="cabd_fdw")
	p.add_argument("--cabd-fdw-table", default="stream_crossings_sites_structures_view_en")
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
	p.add_argument("--satellite-schema", default="public")
	p.add_argument("--satellite-table", default="satellite_review_processed")
	p.add_argument("--skip-rankings", action="store_true")
	p.add_argument(
		"--skip-satellite",
		action="store_true",
		default=True,
		help="Skip satellite propagation checks (default). Use --include-satellite after staging the table.",
	)
	p.add_argument("--include-satellite", action="store_true")
	p.add_argument("--statement-timeout", type=int, default=600, help="Per-check timeout in seconds.")
	p.add_argument("--connect-timeout", type=int, default=15)
	p.add_argument("--sample-limit", type=int, default=20)
	p.add_argument("--report-dir", default="qa_reports")
	p.add_argument("--list", action="store_true", help="List checks without connecting.")
	p.add_argument("--fail-on-failure", action="store_true", help="Exit 1 if any check fails/errors; useful in CI.")
	a = p.parse_args()
	a = apply_yaml_config(a)
	if a.include_satellite:
		a.skip_satellite = False
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
	results: list[dict[str, Any]] = []
	conn = connect(args_global)
	try:
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
			if not ch.enabled(args_global):
				result = dict(
					id=ch.check_id,
					category=ch.category,
					name=ch.name,
					status="SKIP",
					failures=0,
					duration_seconds=0,
					detail=ch.skip_reason,
					samples=[],
				)
			else:
				status, failures, samples, duration, detail = execute(conn, ch, args_global.sample_limit)
				result = dict(
					id=ch.check_id,
					category=ch.category,
					name=ch.name,
					status=status,
					failures=failures,
					duration_seconds=round(duration, 3),
					detail=detail,
					samples=[list(r) if isinstance(r, tuple) else r for r in samples],
				)
			results.append(result)
			count = "" if result["status"] in ("PASS", "SKIP") else f" ({result['failures']} failing row(s))"
			print(f"[{result['status']:<5}] {ch.check_id} {ch.name}{count}")
			if result["detail"]:
				print(f"        {result['detail']}")
	finally:
		conn.close()

	counts = {s: sum(r["status"] == s for r in results) for s in ["PASS", "FAIL", "ERROR", "SKIP"]}
	summary = {
		"generated_utc": datetime.now(timezone.utc).isoformat(),
		"database": args_global.dbname,
		"output_schema": args_global.output_schema,
		"configuration": args_global.loaded_config,
		"counts": counts,
		"manual_follow_up": [
			"Sample upstream/downstream barrier IDs and network traces.",
			"Sample weighted-length calculations.",
			"Verify first qualifying downstream anthropogenic barrier selection.",
			"Verify functional reset behavior.",
			"Visually review sampled gradient barriers against DEM/hillshade.",
			"Investigate legitimate ranking and satellite-propagation exceptions returned by automated checks.",
		],
		"results": results,
	}
	json_path = report_dir / f"fishpass_qa_{stamp}.json"
	csv_path = report_dir / f"fishpass_qa_{stamp}.csv"
	json_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
	with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
		w = csv.DictWriter(f, fieldnames=["id", "category", "name", "status", "failures", "duration_seconds", "detail"])
		w.writeheader()
		for row in results:
			w.writerow({k: row[k] for k in w.fieldnames})

	print("=" * 88)
	print("Summary: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
	print(f"JSON report: {json_path}")
	print(f"CSV report:  {csv_path}")
	if args_global.fail_on_failure and (counts["FAIL"] or counts["ERROR"]):
		return 1
	return 0


args_global: argparse.Namespace
if __name__ == "__main__":
	raise SystemExit(main())
