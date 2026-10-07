#!/usr/bin/env python3
"""Create the combined output view for a model plan (run during a model run).

Builds one <code>_wcrp.combined_output_table_vw with a single row per actionable
barrier, stitching together four sources:

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

Every habitat length surfaced here is converted from metres to km and suffixed
_km, matching the ranked tables. The length columns are not hand-listed: they
come from postprocess_views.barrier_length_fields(), the same source that
defines the anthropogenic_barriers_<sp> view columns.

Join keys (they differ, by design of the upstream pipeline):
  * ranking tables  -> all_barriers.id       (ranked_barriers.barrier_id = id)
  * tracking table  -> all_barriers.feature_id
  * CABD dam/xing   -> all_barriers.feature_id = <fdw>.cabd_id  (empty for new
                       structures like beaver dams that aren't in the CABD)

Waterfalls/gradients (and anything else listed under natural_feature_types) are
excluded so the view holds only actionable barriers.

Not a standalone script: create_combined_view() is called by run_model.py
after run_ranking, on every model run. Anything it surfaces (CABD attributes,
the label_in_wcrp threshold) is picked up on the next full model run.
"""
import logging
import sys

from db import (
    DEFAULT_CONFIG_FILE,
    as_role,
    get_db_roles,
    load_config,
    quote_ident,
    table_columns,
    wcrp_setting,
)
from model_plan import IDENTIFIER_RE
from postprocess_views import barrier_length_fields

logger = logging.getLogger(__name__)

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

# Per-species habitat lengths pulled from anthropogenic_barriers_<sp>
# (postprocess_views.py) come from postprocess_views.barrier_length_fields() --
# see build_view_sql. Each is output as <species>_<column>_km, in km.

# Tracking columns to surface (non-species). Mirrors create_wcrp_tracking_table.py.
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
CABD_FDW_SQL_SCRIPT = "init/database/fishpass_cabd_raw_init.sql"

# --- OTHER SETTINGS --------------------------------------------------------------
# Shared settings live in config/fishpass.yaml, not here:
#   * database_roles -- owner + grant roles for the view (db.get_db_roles)
#   * wcrp.label_in_wcrp_rank_threshold -- per-plan overridable (db.wcrp_setting)


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
        cfg = load_config(config_path)
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
    pairs = sorted(tuple(p) for p in plan.get("reporting_species_lifecycles") or [])
    if not pairs:
        sys.exit(
            f"Plan {plan.get('code')!r} has no (species, lifecycle) pairs for the "
            f"combined view -- reporting_values must list at least one "
            f"'<species>_<lifecycle>' entry."
        )
    by_species = {}
    for sp, lc in pairs:
        if not IDENTIFIER_RE.match(sp):
            sys.exit(f"Invalid species code (unsafe for a column name): {sp!r}")
        by_species.setdefault(sp, []).append(lc)
    return by_species, pairs


def check_cabd_fdw_sources(cursor):
    """Stop with a clear message if a CABD foreign table the view joins is missing,
    or lacks one of the attributes surfaced from it.

    Called by run_model.py at the start of the run, BEFORE the output schema is
    rebuilt -- otherwise a missing cabd_fdw table only surfaces when the view is
    created, at the very end of a full model run.
    """
    problems = []
    for source, attributes in (
        (DAMS_FDW, DAM_ATTRIBUTES),
        (STREAM_CROSSINGS_FDW, STREAM_CROSSING_ATTRIBUTES),
    ):
        schema, table = source.split(".", 1)
        existing = table_columns(cursor, schema, table)
        if not existing:
            problems.append(f"{source} does not exist")
            continue
        missing = [c for c in [CABD_JOIN_KEY] + attributes if c not in existing]
        if missing:
            problems.append(f"{source} is missing column(s): {', '.join(missing)}")
    if problems:
        sys.exit(
            f"CABD foreign table(s) needed by {VIEW_NAME} are not usable: "
            f"{'; '.join(problems)}. Run {CABD_FDW_SQL_SCRIPT} against this database "
            f"(or re-import the foreign tables if CABD's columns have changed)."
        )
    logger.info("CABD foreign tables for %s are present.", VIEW_NAME)


# =================================================================================
#  SQL COLUMN / JOIN BUILDERS
# =================================================================================
def _col(src_alias, column, out_name):
    return f"{src_alias}.{quote_ident(column)} AS {quote_ident(out_name)}"


def _ranking_columns(alias, sp, lc, label_threshold):
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
        f"CASE WHEN {alias}.rank_combined <= {label_threshold}::numeric "
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
    label_threshold = wcrp_setting(plan, "label_in_wcrp_rank_threshold")
    roles = get_db_roles()

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

    # 4. Per-species passability + habitat lengths (metres in the source view,
    #    converted to km here), from the anthropogenic_barriers_<sp> views.
    for sp in sorted(by_species):
        bp = f"bp_{sp}"
        select_cols.append(_col(bp, "passability_status_spawn", f"{sp}_spawn_passability"))
        select_cols.append(_col(bp, "passability_status_rear", f"{sp}_rear_passability"))
        for src in barrier_length_fields(by_species[sp]):
            select_cols.append(
                f"{bp}.{quote_ident(src)} / 1000.0 AS {quote_ident(f'{sp}_{src}_km')}"
            )

    # 5. Per-(species, lifecycle) ranking fields.
    for sp, lc in pairs:
        select_cols.extend(_ranking_columns(f"rk_{sp}_{lc}", sp, lc, label_threshold))

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

    grants = "".join(
        f"\nGRANT ALL ON TABLE {view_id} TO {quote_ident(r)};" for r in roles["grant_all"]
    ) + "".join(
        f"\nGRANT SELECT ON TABLE {view_id} TO {quote_ident(r)};"
        for r in roles["grant_select"]
    )

    return f"""
DROP VIEW IF EXISTS {view_id} CASCADE;
CREATE VIEW {view_id} AS
SELECT
    {select_sql}
FROM {schema_id}.{quote_ident('all_barriers')} {ab}
{joins_sql}
{where};
ALTER VIEW {view_id} OWNER TO {quote_ident(roles["owner"])};{grants}
"""


# =================================================================================
#  EXECUTION
# =================================================================================
def create_combined_view(conn, cursor, plan):
    """Create <code>_wcrp.combined_output_table_vw. Called from run_model.py
    after run_ranking (it reads the ranked tables). Uses DROP VIEW + CREATE (not
    CREATE OR REPLACE) so the column set can change between runs (e.g. adding a
    species)."""
    natural_feature_types = _load_natural_feature_types(plan)
    sql = build_view_sql(plan, natural_feature_types)
    owner = get_db_roles()["owner"]
    view = f"{plan['code']}_wcrp.{VIEW_NAME}"

    # as_role() rolls back + RESETs afterwards (even on error), so a failure
    # surfaces the real error rather than "current transaction is aborted".
    with as_role(conn, cursor, owner):
        logger.info("Creating combined output view %s", view)
        cursor.execute(sql)
        conn.commit()
        logger.info("Combined output view %s created.", view)
