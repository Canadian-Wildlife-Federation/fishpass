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

import yaml
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def apply_yaml_config(args: argparse.Namespace) -> argparse.Namespace:
    """Populate plan/species settings from runtime-selected YAML files."""
    plan = load_yaml(args.model_plan)
    species_doc = load_yaml(args.species_parameters)

    target_species = plan.get("target_species") or []
    if args.species_code:
        code = args.species_code
    elif len(target_species) == 1:
        code = str(target_species[0])
    else:
        raise ValueError("Set --species-code when the plan has zero or multiple target_species values.")

    species_rows = species_doc.get("species") or []
    species = next((row for row in species_rows if str(row.get("code")) == code), None)
    if species is None:
        raise ValueError(f"Species code {code!r} was not found in {args.species_parameters}.")

    plan_code = str(plan.get("code") or "").strip()
    output_schema = str(plan.get("output_schema") or "").strip()
    if not plan_code or not output_schema:
        raise ValueError("Model plan must define non-empty code and output_schema values.")

    args.species_code = code
    args.plan_code = plan_code
    args.output_schema = args.output_schema or output_schema
    args.streams_view = args.streams_view or f"streams_{code}"
    args.barriers_view = args.barriers_view or f"anthropogenic_barriers_{code}"
    args.wcrp_schema = args.wcrp_schema or f"{plan_code}_wcrp"
    args.ranked_rear_table = args.ranked_rear_table or f"ranked_barriers_{code}_rear_{plan_code}"

    mapping = {
        "min_spawn_gradient": "spawn_gradient_min",
        "max_spawn_gradient": "spawn_gradient_max",
        "min_spawn_strahler": "strahler_order_spawning_min",
        "max_spawn_strahler": "strahler_order_spawning_max",
        "min_rear_gradient": "rear_gradient_min",
        "max_rear_gradient": "rear_gradient_max",
        "min_rear_strahler": "strahler_order_rearing_min",
        "max_rear_strahler": "strahler_order_rearing_max",
    }
    for attr, key in mapping.items():
        value = species.get(key)
        if value is None:
            raise ValueError(f"Species {code!r} is missing required parameter {key!r}.")
        setattr(args, attr, value)

    args.loaded_config = {
        "model_plan": args.model_plan,
        "species_parameters": args.species_parameters,
        "plan_code": plan_code,
        "output_schema": args.output_schema,
        "species_code": code,
        "species_name": species.get("name"),
        "target_species": target_species,
        "reporting_values": plan.get("reporting_values") or [],
        "habitat_thresholds": {attr: getattr(args, attr) for attr in mapping},
    }
    return args


def qident(name: str) -> str:
    """Safely quote a schema/table/view identifier supplied through CLI."""
    return '"' + name.replace('"', '""') + '"'


def relation(schema: str, table: str) -> str:
    return f"{qident(schema)}.{qident(table)}"


def checks(args: argparse.Namespace) -> list[Check]:
    streams = relation(args.output_schema, args.streams_view)
    barriers = relation(args.output_schema, args.barriers_view)
    ranked = relation(args.wcrp_schema, args.ranked_rear_table)
    fdw = relation(args.cabd_fdw_schema, args.cabd_fdw_table)
    staging = relation(args.satellite_schema, args.satellite_table)
    combined = relation(args.wcrp_schema, "combined_output_table_vw")

    c: list[Check] = [
        Check("A01", "Spawn inaccessible with zero downstream natural barriers",
              f"SELECT COUNT(*) FROM {streams} WHERE spawn_accessibility='not accessible' AND downstream_natural_spawn_count=0", "Accessibility"),
        Check("A02", "Spawn accessible with downstream natural barriers",
              f"SELECT COUNT(*) FROM {streams} WHERE spawn_accessibility='accessible' AND downstream_natural_spawn_count>0", "Accessibility"),
        Check("A03", "Rear inaccessible with zero downstream natural barriers",
              f"SELECT COUNT(*) FROM {streams} WHERE rear_accessibility='not accessible' AND downstream_natural_rear_count=0", "Accessibility"),
        Check("A04", "Rear accessible with downstream natural barriers",
              f"SELECT COUNT(*) FROM {streams} WHERE rear_accessibility='accessible' AND downstream_natural_rear_count>0", "Accessibility"),
        Check("A05", "Inaccessible spawning segments marked as habitat",
              f"SELECT COUNT(*) FROM {streams} WHERE spawn_accessibility='not accessible' AND spawn_habitat IS TRUE", "Accessibility"),
        Check("A06", "Inaccessible rearing segments marked as habitat",
              f"SELECT COUNT(*) FROM {streams} WHERE rear_accessibility='not accessible' AND rear_habitat IS TRUE", "Accessibility"),

        Check("L01", "Spawn non-habitat has non-zero weighted length",
              f"SELECT COUNT(*) FROM {streams} WHERE spawn_habitat IS FALSE AND ABS(COALESCE(spawn_weighted_length,0))>{TOL}", "Stream lengths"),
        Check("L02", "Rear non-habitat has non-zero weighted length",
              f"SELECT COUNT(*) FROM {streams} WHERE rear_habitat IS FALSE AND ABS(COALESCE(rear_weighted_length,0))>{TOL}", "Stream lengths"),
        Check("L03", "Spawn connected + disconnected differs from weighted length",
              f"SELECT COUNT(*) FROM {streams} WHERE ABS(COALESCE(spawn_weighted_connected_length,0)+COALESCE(spawn_weighted_disconnected_length,0)-COALESCE(spawn_weighted_length,0))>{TOL}", "Stream lengths"),
        Check("L04", "Rear connected + disconnected differs from weighted length",
              f"SELECT COUNT(*) FROM {streams} WHERE ABS(COALESCE(rear_weighted_connected_length,0)+COALESCE(rear_weighted_disconnected_length,0)-COALESCE(rear_weighted_length,0))>{TOL}", "Stream lengths"),
        Check("L05", "Invalid spawn connected/disconnected weighted lengths",
              f"SELECT COUNT(*) FROM {streams} WHERE spawn_weighted_length<-{TOL} OR spawn_weighted_connected_length<-{TOL} OR spawn_weighted_disconnected_length<-{TOL} OR spawn_weighted_connected_length>spawn_weighted_length+{TOL} OR spawn_weighted_disconnected_length>spawn_weighted_length+{TOL}", "Stream lengths"),
        Check("L06", "Invalid rear connected/disconnected weighted lengths",
              f"SELECT COUNT(*) FROM {streams} WHERE rear_weighted_length<-{TOL} OR rear_weighted_connected_length<-{TOL} OR rear_weighted_disconnected_length<-{TOL} OR rear_weighted_connected_length>rear_weighted_length+{TOL} OR rear_weighted_disconnected_length>rear_weighted_length+{TOL}", "Stream lengths"),
        Check("L07", "Effective length negative or greater than raw length",
              f"SELECT COUNT(*) FROM {streams} WHERE effective_length<-{TOL} OR effective_length>length+{TOL}", "Stream lengths"),
        Check("L08", "Effective length is neither zero nor raw length",
              f"SELECT COUNT(*) FROM {streams} WHERE ABS(effective_length)>{TOL} AND ABS(effective_length-length)>{TOL}", "Stream lengths"),
        Check("L09", "Negative raw stream length",
              f"SELECT COUNT(*) FROM {streams} WHERE length<0", "Stream lengths"),

        Check("B01", "Spawn functional upstream exceeds total upstream",
              f"SELECT COUNT(*) FROM {barriers} WHERE spawn_functional_upstream_length>spawn_upstream_length+{TOL}", "Barrier calculations"),
        Check("B02", "Rear functional upstream exceeds total upstream",
              f"SELECT COUNT(*) FROM {barriers} WHERE rear_functional_upstream_length>rear_upstream_length+{TOL}", "Barrier calculations"),
        Check("B03", "Spawnrear functional upstream exceeds total upstream",
              f"SELECT COUNT(*) FROM {barriers} WHERE spawnrear_functional_upstream_length>spawnrear_upstream_length+{TOL}", "Barrier calculations"),
        Check("B04", "Fully passable spawn barrier has disconnected upstream habitat",
              f"SELECT COUNT(*) FROM {barriers} WHERE passability_status_spawn=1 AND ABS(COALESCE(spawn_weighted_disconnected_upstream_length,0))>{TOL}", "Barrier calculations"),
        Check("B05", "Fully passable rear barrier has disconnected upstream habitat",
              f"SELECT COUNT(*) FROM {barriers} WHERE passability_status_rear=1 AND ABS(COALESCE(rear_weighted_disconnected_upstream_length,0))>{TOL}", "Barrier calculations"),
        Check("B06", "Fully impassable spawn barrier has connected upstream habitat",
              f"SELECT COUNT(*) FROM {barriers} WHERE passability_status_spawn=0 AND ABS(COALESCE(spawn_weighted_connected_upstream_length,0))>{TOL}", "Barrier calculations"),
        Check("B07", "Fully impassable rear barrier has connected upstream habitat",
              f"SELECT COUNT(*) FROM {barriers} WHERE passability_status_rear=0 AND ABS(COALESCE(rear_weighted_connected_upstream_length,0))>{TOL}", "Barrier calculations"),
        Check("B08", "Passability value outside 0 to 1",
              f"SELECT COUNT(*) FROM {barriers} WHERE passability_status_spawn<0 OR passability_status_spawn>1 OR passability_status_rear<0 OR passability_status_rear>1", "Barrier calculations"),
        Check("I01", "Upstream spawn count differs from distinct final barrier locations",
            f"""WITH barrier_locations AS (
                SELECT DISTINCT feature_id, snapped_geometry FROM {combined} WHERE feature_id IS NOT NULL AND snapped_geometry IS NOT NULL
            ), mismatches AS (
                SELECT s.id, s.upstream_anthro_spawn_count AS reported_count, s.upstream_anthro_spawn_ids AS barrier_ids
                FROM {streams} s
                WHERE s.upstream_anthro_spawn_count <> COALESCE(cardinality(s.upstream_anthro_spawn_ids), 0)
            )
            SELECT COUNT(*) FROM mismatches m
            WHERE m.reported_count <> (
                SELECT COUNT(DISTINCT bl.snapped_geometry) FROM unnest(m.barrier_ids) AS barrier_id
                JOIN barrier_locations bl ON bl.feature_id = barrier_id
            )""",
            "Barrier IDs"
        ),
        Check("I02", "Upstream rear count differs from distinct final barrier locations",
            f"""WITH barrier_locations AS (
                SELECT DISTINCT feature_id, snapped_geometry FROM {combined} WHERE feature_id IS NOT NULL AND snapped_geometry IS NOT NULL
            ), mismatches AS (
                SELECT s.id, s.upstream_anthro_rear_count AS reported_count, s.upstream_anthro_rear_ids AS barrier_ids
                FROM {streams} s
                WHERE s.upstream_anthro_rear_count <> COALESCE(cardinality(s.upstream_anthro_rear_ids), 0)
            )
            SELECT COUNT(*) FROM mismatches m
            WHERE m.reported_count <> (
                SELECT COUNT(DISTINCT bl.snapped_geometry) FROM unnest(m.barrier_ids) AS barrier_id
                JOIN barrier_locations bl ON bl.feature_id = barrier_id
            )""",
            "Barrier IDs"
        ),
        Check("I03", "Downstream spawn count differs from distinct final barrier locations",
            f"""WITH barrier_locations AS (
                SELECT DISTINCT feature_id, snapped_geometry FROM {combined} WHERE feature_id IS NOT NULL AND snapped_geometry IS NOT NULL
            ), mismatches AS (
                SELECT s.id, s.downstream_anthro_spawn_count AS reported_count, s.downstream_anthro_spawn_ids AS barrier_ids
                FROM {streams} s
                WHERE s.downstream_anthro_spawn_count <> COALESCE(cardinality(s.downstream_anthro_spawn_ids), 0)
            )
            SELECT COUNT(*) FROM mismatches m
            WHERE m.reported_count <> (
                SELECT COUNT(DISTINCT bl.snapped_geometry) FROM unnest(m.barrier_ids) AS barrier_id
                JOIN barrier_locations bl ON bl.feature_id = barrier_id
            )""",
            "Barrier IDs"
        ),
        Check("I04", "Downstream rear count differs from distinct final barrier locations",
            f"""WITH barrier_locations AS (
                SELECT DISTINCT feature_id, snapped_geometry FROM {combined} WHERE feature_id IS NOT NULL AND snapped_geometry IS NOT NULL
            ), mismatches AS (
                SELECT s.id, s.downstream_anthro_rear_count AS reported_count, s.downstream_anthro_rear_ids AS barrier_ids
                FROM {streams} s
                WHERE s.downstream_anthro_rear_count <> COALESCE(cardinality(s.downstream_anthro_rear_ids), 0)
            )
            SELECT COUNT(*) FROM mismatches m
            WHERE m.reported_count <> (
                SELECT COUNT(DISTINCT bl.snapped_geometry) FROM unnest(m.barrier_ids) AS barrier_id
                JOIN barrier_locations bl ON bl.feature_id = barrier_id
            )""",
            "Barrier IDs"
        ),
        Check("I05", "Count/ID mismatches not explained by coincident final barrier locations",
            f"""WITH barrier_locations AS (
                SELECT DISTINCT feature_id, snapped_geometry FROM {combined} WHERE feature_id IS NOT NULL AND snapped_geometry IS NOT NULL
            ), mismatches AS (
                SELECT s.id, v.test_id, v.reported_count, v.barrier_ids
                FROM {streams} s
                CROSS JOIN LATERAL (VALUES
                    ('I01', s.upstream_anthro_spawn_count, s.upstream_anthro_spawn_ids),
                    ('I02', s.upstream_anthro_rear_count, s.upstream_anthro_rear_ids),
                    ('I03', s.downstream_anthro_spawn_count, s.downstream_anthro_spawn_ids),
                    ('I04', s.downstream_anthro_rear_count, s.downstream_anthro_rear_ids)
                ) AS v(test_id, reported_count, barrier_ids)
                WHERE v.reported_count <> COALESCE(cardinality(v.barrier_ids), 0)
            )
            SELECT COUNT(*) FROM mismatches m
            WHERE m.reported_count <> (
                SELECT COUNT(DISTINCT bl.snapped_geometry) FROM unnest(m.barrier_ids) AS barrier_id
                JOIN barrier_locations bl ON bl.feature_id = barrier_id
            )""",
            "Barrier IDs"
        ),
    ]

    # Parameter checks are enabled only when all four values for the life stage are supplied.
    spawn_cfg = all(v is not None for v in [args.min_spawn_gradient, args.max_spawn_gradient, args.min_spawn_strahler, args.max_spawn_strahler])
    rear_cfg = all(v is not None for v in [args.min_rear_gradient, args.max_rear_gradient, args.min_rear_strahler, args.max_rear_strahler])
    c += [
        Check("H01", "Spawn habitat violates configured gradient/Strahler limits",
              f"SELECT COUNT(*) FROM {streams} WHERE spawn_habitat IS TRUE AND (segment_gradient<{args.min_spawn_gradient or 0} OR segment_gradient>={args.max_spawn_gradient or 0} OR strahler_order<{args.min_spawn_strahler or 0} OR strahler_order>={args.max_spawn_strahler or 0})",
              "Habitat parameters", enabled=lambda _: spawn_cfg, skip_reason="Provide all spawn threshold arguments."),
        Check("H02", "Rear habitat violates configured gradient/Strahler limits",
              f"SELECT COUNT(*) FROM {streams} WHERE rear_habitat IS TRUE AND (segment_gradient<{args.min_rear_gradient or 0} OR segment_gradient>={args.max_rear_gradient or 0} OR strahler_order<{args.min_rear_strahler or 0} OR strahler_order>={args.max_rear_strahler or 0})",
              "Habitat parameters", enabled=lambda _: rear_cfg, skip_reason="Provide all rear threshold arguments."),
    ]

    if not args.skip_rankings:
        c += [
            Check("R01", "Ranked passability differs from source model",
                  f"SELECT r.feature_id FROM {ranked} r JOIN {barriers} b ON b.feature_id=r.feature_id WHERE r.as_rear_passability IS DISTINCT FROM b.passability_status_rear OR r.as_spawn_passability IS DISTINCT FROM b.passability_status_spawn", "Rankings", mode="zero_rows"),
            Check("R02", "Ranked barrier missing from model output",
                  f"SELECT r.feature_id FROM {ranked} r LEFT JOIN {barriers} b ON b.feature_id=r.feature_id WHERE b.feature_id IS NULL", "Rankings", mode="zero_rows"),
            Check("R03", "Ranked rear barrier has no upstream rear habitat",
                  f"SELECT r.feature_id FROM {ranked} r JOIN {barriers} b ON b.feature_id=r.feature_id WHERE COALESCE(b.rear_upstream_length,0)<=0", "Rankings", mode="zero_rows"),
            Check("R04", "Negative ranking gain",
                  f"SELECT feature_id FROM {ranked} WHERE total_rear_hab_gain_group_km<0 OR w_total_hab_gain_group_km<0 OR avg_gain_per_barrier_km<0 OR w_avg_gain_per_barrier_km<0", "Rankings", mode="zero_rows"),
            Check("R05", "Reported group size differs from actual membership",
                  f"WITH actual AS (SELECT group_id,COUNT(*) n FROM {ranked} GROUP BY group_id) SELECT r.group_id FROM {ranked} r JOIN actual a USING(group_id) GROUP BY r.group_id,a.n HAVING MIN(r.num_barriers_group)<>a.n OR MIN(r.num_barriers_group)<>MAX(r.num_barriers_group)", "Rankings", mode="zero_rows"),
            Check("R06", "Average gain math mismatch",
                  f"SELECT feature_id FROM {ranked} WHERE ABS(avg_gain_per_barrier_km-total_rear_hab_gain_group_km/NULLIF(num_barriers_group,0))>{TOL}", "Rankings", mode="zero_rows"),
            Check("R07", "Weighted average gain math mismatch",
                  f"SELECT feature_id FROM {ranked} WHERE ABS(w_avg_gain_per_barrier_km-w_total_hab_gain_group_km/NULLIF(num_barriers_group,0))>{TOL}", "Rankings", mode="zero_rows"),
            Check("R08", "Positive weighted average gain is missing tiered rank",
                  f"""SELECT feature_id FROM {ranked} WHERE w_avg_gain_per_barrier_km > 0 AND rank_w_avg_gain_tiered IS NULL""","Rankings",mode="zero_rows",),
            Check("R09", "Any required ranking fields are null",
                  f"SELECT feature_id FROM {ranked} WHERE group_id IS NULL OR rank_w_total_upstr_rear_hab IS NULL OR rank_combined IS NULL;", "Rankings", mode="zero_rows"),            
            Check("R10", "Fully passable rear barrier appears in ranking (review exceptions)",
                  f"SELECT feature_id FROM {ranked} WHERE as_rear_passability=1", "Rankings", mode="zero_rows"),
        ]

    # Requires the user to load the latest processed-review export into fishpass_dev.
    if not args.skip_satellite:
        c += [
            Check("S01", "Satellite-review crossing type differs from CABD FDW",
                  f"WITH f AS (SELECT DISTINCT cabd_id,crossing_type_code FROM {fdw}) SELECT r.cabd_id FROM {staging} r JOIN f ON f.cabd_id=r.cabd_id WHERE r.crossing_type_code IS DISTINCT FROM f.crossing_type_code", "Satellite propagation", mode="zero_rows"),
            Check("S02", "Satellite-review record missing from CABD FDW",
                  f"WITH f AS (SELECT DISTINCT cabd_id FROM {fdw}) SELECT r.cabd_id FROM {staging} r LEFT JOIN f ON f.cabd_id=r.cabd_id WHERE f.cabd_id IS NULL", "Satellite propagation", mode="zero_rows"),
            Check("S03", "Processed CABD crossing missing from FishPass",
                  f"WITH f AS (SELECT DISTINCT cabd_id FROM {fdw}) SELECT r.cabd_id FROM {staging} r JOIN f ON f.cabd_id=r.cabd_id LEFT JOIN {barriers} b ON b.feature_id=r.cabd_id WHERE b.feature_id IS NULL", "Satellite propagation", mode="zero_rows"),
        ]
    return c


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
        return status, failures, samples, time.perf_counter()-started, ""
    except Exception as exc:
        conn.rollback()
        return "ERROR", 0, [], time.perf_counter()-started, str(exc).strip()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run automated FishPass ASF QA checks.")
    p.add_argument("--host", default=os.getenv("FISHPASS_HOST"))
    p.add_argument("--port", type=int, default=int(os.getenv("FISHPASS_PORT", "5432")))
    p.add_argument("--dbname", default=os.getenv("FISHPASS_DBNAME"))
    p.add_argument("--user", default=os.getenv("FISHPASS_USER"))
    p.add_argument("--password", default=os.getenv("FISHPASS_PASSWORD"))
    p.add_argument("--model-plan", required=True, help="Path to the model plan YAML, e.g. asf_phase1.yaml.")
    p.add_argument("--species-parameters", required=True, help="Path to fish_species_parameters.yaml.")
    p.add_argument("--species-code", help="Override species code; required for multi-species plans.")
    p.add_argument("--output-schema", help="Override output_schema from the model plan.")
    p.add_argument("--streams-view", help="Override derived streams_<species> view name.")
    p.add_argument("--barriers-view", help="Override derived anthropogenic_barriers_<species> view name.")
    p.add_argument("--wcrp-schema", help="Override derived <plan_code>_wcrp schema.")
    p.add_argument("--ranked-rear-table", help="Override derived ranked_barriers_<species>_rear_<plan_code> table.")
    p.add_argument("--cabd-fdw-schema", default="cabd_fdw")
    p.add_argument("--cabd-fdw-table", default="stream_crossings_sites_structures_view_en")
    p.add_argument("--satellite-schema", default="public")
    p.add_argument("--satellite-table", default="satellite_review_processed")
    p.add_argument("--skip-rankings", action="store_true")
    p.add_argument("--skip-satellite", action="store_true", default=True,
                   help="Skip satellite propagation checks (default). Use --include-satellite after staging the table.")
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
            print(f"{ch.check_id:>3}  {state:<7}  {ch.category}: {ch.name}")
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
        print(f"FishPass QA | database={args_global.dbname} | schema={args_global.output_schema} | species={args_global.species_code}")
        print(f"Plan: {args_global.model_plan}")
        print(f"Species parameters: {args_global.species_parameters}")
        print("=" * 88)
        for ch in all_checks:
            if not ch.enabled(args_global):
                result = dict(id=ch.check_id, category=ch.category, name=ch.name, status="SKIP", failures=0, duration_seconds=0, detail=ch.skip_reason, samples=[])
            else:
                status, failures, samples, duration, detail = execute(conn, ch, args_global.sample_limit)
                result = dict(id=ch.check_id, category=ch.category, name=ch.name, status=status, failures=failures, duration_seconds=round(duration, 3), detail=detail, samples=[list(r) if isinstance(r, tuple) else r for r in samples])
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
            "Investigate legitimate ranking and satellite-propagation exceptions returned by automated checks."
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
