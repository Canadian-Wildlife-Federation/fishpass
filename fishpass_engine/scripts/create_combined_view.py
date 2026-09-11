#!/usr/bin/env python3
"""Create the combined output view for a model plan (run during a model run).

Builds one <code>_wcrp.combined_output_table_vw with a single row per actionable
barrier, stitching together three sources:

  * <output_schema>.all_barriers        -- base row set + feature_type/geometry,
                                           and (via anthropogenic_barriers_<sp>)
                                           per-species passability + habitat totals
  * <code>_wcrp.ranked_barriers_*       -- the per-(species, lifecycle) ranking
                                           fields produced by rank_barriers.py
  * <code>_wcrp.tracking_table_<code>   -- hand-entered tracking attributes
  * cabd_fdw dam / stream-crossing views -- descriptive CABD attributes

Everything is driven off the plan: target species and lifecycles come from
plan['reporting_species_lifecycles'] (so more species/lifecycles just widen the
column set -- still one row per barrier), and the natural feature types to
exclude come from fishpass.yaml (or the plan's natural_feature_types_override).

Join keys (they differ, by design of the upstream pipeline):
  * ranking tables  -> all_barriers.id       (ranked_barriers.barrier_id = id)
  * tracking table  -> all_barriers.feature_id
  * CABD dam/xing   -> all_barriers.feature_id = <fdw>.cabd_id  (empty for new
                       structures like beaver dams that aren't in the CABD)

Waterfalls/gradients (and anything else listed under natural_feature_types) are
excluded so the view holds only actionable barriers.

Connection details come from environment variables only (see db.py).

Usage:
    python create_combined_view.py <plan_code>
    python create_combined_view.py <plan_code> --dry-run
"""
import argparse
import logging
import sys
from pathlib import Path

import yaml

from db import db_connect, quote_ident, require_env
from model_plan import IDENTIFIER_RE, load_model_plan

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Repo layout mirrors model_plan.py / species_params.py.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_FILE = REPO_ROOT / "config" / "fishpass.yaml"

VIEW_NAME = "combined_output_table_vw"

# --- CONFIGURABLE ATTRIBUTE LISTS -----------------------------------------------
# CABD descriptive attributes pulled from the FDW views. Add/remove freely; each
# must be a real column on the corresponding foreign table. A barrier is either a
# dam OR a stream crossing (or a non-CABD new structure), so for any given row one
# of these blocks is populated and the other is NULL.
DAM_ATTRIBUTES = [
    "dam_name_en",
    "dam_use",
    "owner",
    "ownership_type",
    "structure_type",
    "construction_material",
    "up_passage_type",
    "down_passage_route",
]
STREAM_CROSSING_ATTRIBUTES = [
    "crossing_type_name",
    "crossing_condition_name",
    "land_ownership_context",
    "inlet_shape",
    "outlet_shape",
    "road_name",
    "addressed_status_name",
    "assessment_type_name",
    "date_assessed",
]

# Per-(species, lifecycle) habitat totals pulled from anthropogenic_barriers_<sp>
# (postprocess_views.py). Each is a <lc>_<field> column on that view; output is
# aliased <species>_<lifecycle>_<field>.
BARRIERS_HABITAT_LIFECYCLE_FIELDS = [
    "upstream_length",
    "functional_upstream_length",
    "weighted_disconnected_upstream_length",
    "functional_weighted_disconnected_upstream_length",
]

# Tracking columns to surface (non-species). Mirrors create_tracking_table.py.
# barrier_id is intentionally omitted (the view's barrier_id comes from
# all_barriers). road_name and structure_type collide with CABD crossing/dam
# attribute names, so they are re-aliased on output (see TRACKING_ALIAS_OVERRIDES).
TRACKING_NON_SPECIES_COLUMNS = [
    "internal_name",
    "watercourse_name",
    "road_name",
    "structure_type",
    "structure_owner",
    "private_owner_details",
    "passability_assessment_type",
    "assessment_step_completed",
    "reason_for_exclusion",
    "method_of_exclusion",
    "upstream_habitat_quality",
    "constructability",
    "estimated_cost_$",
    "priority",
    "type_of_rehabilitation",
    "rehabilitated_by",
    "rehabilitated_date",
    "estimated_rehabilitation_cost_$",
    "actual_project_cost_$",
    "next_steps",
    "timeline_for_next_steps",
    "lead_for_next_steps",
    "others_involved_in_next_steps",
    "reason",
    "notes",
    "supporting_links",
]
# Per-species tracking columns (suffixed with the species code).
TRACKING_SPECIES_COLUMN_STEMS = [
    "structure_list_status",
    "partial_passability",
    "partial_passability_notes",
]
# Output-name overrides for tracking columns whose raw name collides with a CABD
# attribute, so both can coexist in the one-row-per-barrier view.
TRACKING_ALIAS_OVERRIDES = {
    "road_name": "tracking_road_name",
    "structure_type": "tracking_structure_type",
}

# --- CABD FDW SOURCES ------------------------------------------------------------
DAMS_FDW = "cabd_fdw.dams_view_en"
STREAM_CROSSINGS_FDW = "cabd_fdw.stream_crossings_sites_structures_view_en"
CABD_JOIN_KEY = "cabd_id"

# --- OTHER SETTINGS --------------------------------------------------------------
# A barrier is flagged label_in_wcrp = 'yes' when its combined rank is at or above
# (numerically <=) this threshold, per (species, lifecycle).
LABEL_IN_WCRP_RANK_THRESHOLD = 20

OWNER_ROLE = "fishpass"
GRANT_ALL_ROLES = ("cwf_analyst", "cwf_tech")
GRANT_SELECT_ROLES = ("cwf_user",)


# =================================================================================
#  PLAN-DERIVED VALUES
# =================================================================================
def _load_natural_feature_types(plan, config_path=DEFAULT_CONFIG_FILE):
    """Feature types to EXCLUDE from the view. The plan's
    natural_feature_types_override wins if set (model_plan already validated it);
    otherwise read structure_classification.natural_feature_types from
    fishpass.yaml. Values are interpolated into the view SQL, so hold them to the
    safe identifier charset."""
    override = plan.get("natural_feature_types_override")
    if override is not None:
        types = override
    else:
        if not Path(config_path).is_file():
            sys.exit(f"Config file not found: {config_path}")
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        types = (cfg.get("structure_classification") or {}).get(
            "natural_feature_types"
        ) or []
    for t in types:
        if not isinstance(t, str) or not IDENTIFIER_RE.match(t):
            sys.exit(f"Invalid natural feature type (unsafe for SQL): {t!r}")
    return list(types)


def _species_lifecycles(plan):
    """{species: [lifecycle, ...]} and the sorted (species, lifecycle) pairs, from
    the cached model_plan.expand_reporting_values() result. Species codes are
    interpolated into identifiers, so they are charset-checked here."""
    pairs = sorted(tuple(p) for p in plan["reporting_species_lifecycles"])
    by_species = {}
    for sp, lc in pairs:
        if not IDENTIFIER_RE.match(sp):
            sys.exit(f"Invalid species code (unsafe for a column name): {sp!r}")
        by_species.setdefault(sp, []).append(lc)
    return by_species, pairs


# =================================================================================
#  SQL COLUMN / JOIN BUILDERS
# =================================================================================
def _col(src_alias, column, out_name):
    return f"{src_alias}.{quote_ident(column)} AS {quote_ident(out_name)}"


def _ranking_columns(alias, sp, lc):
    """(source_column, output_name) pairs for one ranked_barriers_<sp>_<lc> table,
    mirroring rank_barriers.py's slim output. The two lifecycle-named source
    columns are handled here so the output name carries species+lifecycle."""
    suffix = f"{sp}_{lc}"
    static = [
        ("group_id", f"group_id_{suffix}"),
        ("num_barriers_group", f"num_barriers_group_{suffix}"),
        (f"total_{lc}_hab_gain_group_km", f"total_hab_gain_group_km_{suffix}"),
        ("w_total_hab_gain_group_km", f"w_total_hab_gain_group_km_{suffix}"),
        ("avg_gain_per_barrier_km", f"avg_gain_per_barrier_km_{suffix}"),
        ("w_avg_gain_per_barrier_km", f"w_avg_gain_per_barrier_km_{suffix}"),
        ("downstr_group_ids", f"downstr_group_ids_{suffix}"),
        ("rank_w_avg_gain_tiered", f"rank_w_avg_gain_tiered_{suffix}"),
        (f"rank_w_total_upstr_{lc}_hab", f"rank_w_total_upstr_hab_{suffix}"),
        ("rank_combined", f"rank_combined_{suffix}"),
    ]
    cols = [_col(alias, src, out) for src, out in static]
    # label_in_wcrp: derived from combined rank.
    cols.append(
        f"CASE WHEN {alias}.rank_combined <= {LABEL_IN_WCRP_RANK_THRESHOLD}::numeric "
        f"THEN 'yes' ELSE 'no' END AS {quote_ident('label_in_wcrp_' + suffix)}"
    )
    return cols


def build_view_sql(plan, natural_feature_types):
    schema = plan["output_schema"]
    watershed = plan["code"]
    wcrp_schema = f"{watershed}_wcrp"

    schema_id = quote_ident(schema)
    wcrp_id = quote_ident(wcrp_schema)
    view_id = f"{wcrp_id}.{quote_ident(VIEW_NAME)}"

    by_species, pairs = _species_lifecycles(plan)

    ab = "ab"
    dm = "dm"
    sc = "sc"
    tt = "tt"

    select_cols = []

    # 1. Identity + base all_barriers fields.
    select_cols.append(f"{ab}.{quote_ident('feature_id')} AS {quote_ident('barrier_id')}")
    select_cols.append(f"{ab}.{quote_ident('feature_type')}")
    select_cols.append(f"{ab}.{quote_ident('snapped_geometry')}")

    # 2. CABD dam attributes.
    for attr in DAM_ATTRIBUTES:
        select_cols.append(_col(dm, attr, attr))
    # 3. CABD stream-crossing attributes.
    for attr in STREAM_CROSSING_ATTRIBUTES:
        select_cols.append(_col(sc, attr, attr))

    # 4. Per-species passability + per-(species, lifecycle) habitat totals, from
    #    the anthropogenic_barriers_<sp> views.
    for sp in sorted(by_species):
        bp = f"bp_{sp}"
        select_cols.append(_col(bp, "passability_status_spawn", f"{sp}_spawn_passability"))
        select_cols.append(_col(bp, "passability_status_rear", f"{sp}_rear_passability"))
        for lc in by_species[sp]:
            for field in BARRIERS_HABITAT_LIFECYCLE_FIELDS:
                src = f"{lc}_{field}"
                select_cols.append(_col(bp, src, f"{sp}_{lc}_{field}"))

    # 5. Per-(species, lifecycle) ranking fields.
    for sp, lc in pairs:
        select_cols.extend(_ranking_columns(f"rk_{sp}_{lc}", sp, lc))

    # 6. Tracking columns (non-species, with collision-safe aliases).
    for col in TRACKING_NON_SPECIES_COLUMNS:
        out = TRACKING_ALIAS_OVERRIDES.get(col, col)
        select_cols.append(_col(tt, col, out))
    # 7. Tracking columns (per-species).
    for sp in sorted(by_species):
        for stem in TRACKING_SPECIES_COLUMN_STEMS:
            col = f"{stem}_{sp}"
            select_cols.append(_col(tt, col, col))

    # --- JOINS ---
    joins = [
        f"LEFT JOIN {DAMS_FDW} {dm} "
        f"ON {dm}.{quote_ident(CABD_JOIN_KEY)} = {ab}.{quote_ident('feature_id')}",
        f"LEFT JOIN {STREAM_CROSSINGS_FDW} {sc} "
        f"ON {sc}.{quote_ident(CABD_JOIN_KEY)} = {ab}.{quote_ident('feature_id')}",
        f"LEFT JOIN {wcrp_id}.{quote_ident('tracking_table_' + watershed)} {tt} "
        f"ON {tt}.{quote_ident('barrier_id')} = {ab}.{quote_ident('feature_id')}",
    ]
    for sp in sorted(by_species):
        bp = f"bp_{sp}"
        joins.append(
            f"LEFT JOIN {schema_id}.{quote_ident('anthropogenic_barriers_' + sp)} {bp} "
            f"ON {bp}.{quote_ident('id')} = {ab}.{quote_ident('id')}"
        )
    for sp, lc in pairs:
        rk = f"rk_{sp}_{lc}"
        ranked_table = f"ranked_barriers_{sp}_{lc}_{watershed}"
        joins.append(
            f"LEFT JOIN {wcrp_id}.{quote_ident(ranked_table)} {rk} "
            f"ON {rk}.{quote_ident('barrier_id')} = {ab}.{quote_ident('id')}"
        )

    # --- WHERE: exclude natural feature types (waterfalls, gradients, ...). Any
    #     feature_type NOT in that list -- dams, stream_crossings, and new
    #     structures like beaver dams -- is kept. ---
    if natural_feature_types:
        natural_array = ", ".join(f"'{t}'" for t in natural_feature_types)
        where = (
            f"WHERE {ab}.{quote_ident('feature_type')} "
            f"<> ALL (ARRAY[{natural_array}]::varchar[])"
        )
    else:
        where = ""

    select_sql = ",\n    ".join(select_cols)
    joins_sql = "\n".join(joins)

    all_roles = ", ".join(quote_ident(r) for r in GRANT_ALL_ROLES)
    select_roles = ", ".join(quote_ident(r) for r in GRANT_SELECT_ROLES)

    return f"""
DROP VIEW IF EXISTS {view_id} CASCADE;
CREATE VIEW {view_id} AS
SELECT
    {select_sql}
FROM {schema_id}.{quote_ident('all_barriers')} {ab}
{joins_sql}
{where};
ALTER VIEW {view_id} OWNER TO {quote_ident(OWNER_ROLE)};
GRANT ALL ON TABLE {view_id} TO {all_roles};
GRANT SELECT ON TABLE {view_id} TO {select_roles};
"""


# =================================================================================
#  EXECUTION
# =================================================================================
def create_combined_view(conn, cursor, plan):
    """Create <code>_wcrp.combined_output_table_vw. Intended to run from
    run_model.py after run_ranking (it reads the ranked tables), and safe to
    rerun standalone. Uses DROP VIEW + CREATE (not CREATE OR REPLACE) so the
    column set can change between runs (e.g. adding a species)."""
    natural_feature_types = _load_natural_feature_types(plan)
    sql = build_view_sql(plan, natural_feature_types)

    cursor.execute(f"set role {quote_ident(OWNER_ROLE)};")
    conn.commit()
    try:
        view = f"{plan['code']}_wcrp.{VIEW_NAME}"
        logger.info("Creating combined output view %s", view)
        cursor.execute(sql)
        conn.commit()
        logger.info("Combined output view %s created.", view)
    finally:
        cursor.execute("reset role;")
        conn.commit()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "plan_code", help="Plan code -- loads config/models/<plan_code>.yaml"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated SQL and exit without connecting to a database.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    plan = load_model_plan(args.plan_code)

    if args.dry_run:
        natural_feature_types = _load_natural_feature_types(plan)
        print(build_view_sql(plan, natural_feature_types))
        return

    require_env()
    conn = db_connect()
    try:
        with conn.cursor() as cursor:
            create_combined_view(conn, cursor, plan)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    logger.info("Done!")


if __name__ == "__main__":
    main()
